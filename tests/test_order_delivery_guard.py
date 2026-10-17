"""订单身份防覆盖与待补发对账的存储层测试。"""

import os
import tempfile
import unittest
from datetime import datetime, timedelta

from app.db_manager import DBManager


class OrderDeliveryGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.manager = DBManager(os.path.join(self.temp_dir.name, "orders.db"))
        self.user_id = self.manager.conn.execute(
            "SELECT id FROM users WHERE username='admin'"
        ).fetchone()[0]
        self.manager.conn.execute(
            "INSERT INTO cookies (id, value, user_id) VALUES ('acc', 'cookie', ?)",
            (self.user_id,),
        )
        self.manager.conn.commit()

    def tearDown(self):
        self.manager.close()
        self.temp_dir.cleanup()

    def test_bad_identity_does_not_overwrite_real_values(self):
        self.manager.insert_or_update_order(
            order_id="o1",
            item_id="1093294412397",
            buyer_id="2217722690868",
            cookie_id="acc",
            order_status="pending_ship",
        )
        # 模拟消息解析失败：内容 ID 不是纯数字，买家是 unknown_user
        self.manager.insert_or_update_order(
            order_id="o1",
            item_id="4350224982274.PNM",
            buyer_id="unknown_user",
            cookie_id="acc",
        )
        row = self.manager.get_order_by_id("o1")
        self.assertEqual(row["item_id"], "1093294412397")
        self.assertEqual(row["buyer_id"], "2217722690868")

    def test_pending_orders_without_shipment_are_listed(self):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for order_id, status in (("o1", "pending_ship"), ("o2", "completed")):
            self.manager.insert_or_update_order(
                order_id=order_id,
                item_id="1093294412397",
                buyer_id=f"buyer-{order_id}",
                cookie_id="acc",
                order_status=status,
                created_at=now,
            )
        self.manager.insert_or_update_order(
            order_id="o3",
            item_id="1093294412397",
            buyer_id="buyer-o3",
            cookie_id="acc",
            order_status="pending_ship",
            created_at=now,
        )
        self.manager.conn.execute(
            "INSERT INTO card_shipments "
            "(card_id, card_name, content, order_id, user_id) "
            "VALUES (1, '测试卡', 'key-1', 'o3', ?)",
            (self.user_id,),
        )
        self.manager.conn.commit()

        orders = self.manager.list_pending_delivery_orders("acc")
        self.assertEqual([order["order_id"] for order in orders], ["o1"])

    def test_old_pending_order_is_excluded(self):
        old = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
        self.manager.insert_or_update_order(
            order_id="old",
            item_id="1093294412397",
            buyer_id="buyer-old",
            cookie_id="acc",
            order_status="pending_ship",
            created_at=old,
        )
        self.assertEqual(self.manager.list_pending_delivery_orders("acc"), [])


if __name__ == "__main__":
    unittest.main()
