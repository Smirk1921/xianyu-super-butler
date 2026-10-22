"""付款后确认发货：纯逻辑、存储层和补单过滤的回归测试。"""

import os
import tempfile
import time
import unittest
from datetime import datetime

from app.db_manager import DBManager
from app.delivery_confirmation import (
    build_confirmation_prompt,
    compute_expires_at,
    find_matching_keyword,
    message_within_expiry,
    reminder_due,
)


class DeliveryConfirmationLogicTests(unittest.TestCase):
    def test_keyword_contains_and_exclusion(self):
        self.assertEqual(
            find_matching_keyword("确认，发吧", "确认发货|确认", "不确认"),
            "确认",
        )
        self.assertIsNone(
            find_matching_keyword("不确认，退款", "确认发货|确认", "不确认|退款")
        )
        self.assertEqual(
            find_matching_keyword("OK please send", "ok|send", ""),
            "ok",
        )
        self.assertIsNone(
            find_matching_keyword("[我已付款，等待你发货]", "确认|付款", "")
        )

    def test_prompt_appends_keyword_hint(self):
        prompt = build_confirmation_prompt(
            "你好 {buyer_name}",
            "确认发货|确认",
            {"buyer_name": "小明", "order_id": "o1", "item_title": "商品"},
        )
        self.assertIn("小明", prompt)
        self.assertIn("请回复「确认发货」确认发货", prompt)

        prompt_with_placeholder = build_confirmation_prompt(
            "请回复 {keyword}",
            "确认发货|确认",
            {"keyword": "确认发货"},
        )
        self.assertEqual(prompt_with_placeholder.count("确认发货"), 1)

    def test_expiry_and_reminder(self):
        now = int(time.time())
        record = {
            "status": "prompt_sent",
            "prompt_sent_at": now,
            "expires_at": now + 1800,
            "reminder_minutes": 10,
            "max_reminders": 2,
            "reminder_count": 0,
            "last_reminder_at": None,
        }
        self.assertEqual(compute_expires_at(now, 30), now + 1800)
        self.assertTrue(message_within_expiry(record, now + 10))
        self.assertFalse(message_within_expiry(record, now + 1801, grace_seconds=0))
        self.assertFalse(reminder_due(record, now + 60))
        self.assertTrue(reminder_due(record, now + 601))


class DeliveryConfirmationDBTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.manager = DBManager(os.path.join(self.temp_dir.name, "confirm.db"))
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

    def _create_record(self, order_id="o1", **overrides):
        data = dict(
            user_id=self.user_id,
            cookie_id="acc",
            order_id=order_id,
            item_id="item-1",
            buyer_id="buyer-1",
            buyer_name="买家",
            chat_id="chat-1",
            prompt_text="请回复「确认发货」",
            keywords="确认发货|确认",
            exclude_keywords="不确认",
            timeout_minutes=30,
            reminder_text="提醒：请回复确认发货",
            reminder_minutes=10,
            max_reminders=2,
        )
        data.update(overrides)
        return self.manager.create_delivery_confirmation(**data)

    def test_account_and_item_override_resolution(self):
        effective = self.manager.get_effective_delivery_confirmation("acc", "item-1")
        self.assertFalse(effective["enabled"])

        self.assertTrue(
            self.manager.save_delivery_confirmation_settings(
                self.user_id, "acc", True, "账号文案 {keyword}", "确认|好", "不确认",
                30, 10, 2, "提醒 {keyword}",
            )
        )
        effective = self.manager.get_effective_delivery_confirmation("acc", "item-1")
        self.assertTrue(effective["enabled"])
        self.assertIn("账号文案", effective["confirm_text"])

        self.assertTrue(
            self.manager.save_item_delivery_confirmation_setting(
                self.user_id, "acc", "item-1", True, False,
                "商品文案", "商品确认", "不确认", 60, 20, 1, "商品提醒",
            )
        )
        effective = self.manager.get_effective_delivery_confirmation("acc", "item-1")
        self.assertTrue(effective["enabled"])
        self.assertEqual(effective["confirm_text"], "账号文案 {keyword}")

        self.assertTrue(
            self.manager.save_item_delivery_confirmation_setting(
                self.user_id, "acc", "item-1", False, True,
                "商品文案 {keyword}", None, None, 60, 20, 1, None,
            )
        )
        effective = self.manager.get_effective_delivery_confirmation("acc", "item-1")
        self.assertTrue(effective["enabled"])
        self.assertEqual(effective["confirm_text"], "商品文案 {keyword}")
        self.assertEqual(effective["confirm_keywords"], "确认|好")
        self.assertEqual(effective["timeout_minutes"], 60)

    def test_record_is_unique_and_transitions_are_atomic(self):
        first = self._create_record()
        second = self._create_record()
        self.assertEqual(first["id"], second["id"])
        count = self.manager.conn.execute(
            "SELECT COUNT(*) FROM delivery_confirmations WHERE order_id='o1'"
        ).fetchone()[0]
        self.assertEqual(count, 1)

        self.assertTrue(self.manager.mark_delivery_confirmation_prompt_attempt(first["id"]))
        now = int(time.time())
        self.assertTrue(
            self.manager.mark_delivery_confirmation_prompt_sent(
                first["id"], now, now + 1800
            )
        )
        self.assertTrue(
            self.manager.mark_delivery_confirmation_confirmed(
                first["id"], now + 5, "msg-1"
            )
        )
        self.assertFalse(
            self.manager.mark_delivery_confirmation_confirmed(
                first["id"], now + 6, "msg-2"
            )
        )
        self.assertTrue(
            self.manager.mark_delivery_confirmation_delivered(first["id"], now + 10)
        )
        record = self.manager.get_delivery_confirmation("acc", "o1")
        self.assertEqual(record["status"], "delivered")

    def test_cancel_for_order(self):
        record = self._create_record()
        self.assertTrue(
            self.manager.mark_delivery_confirmation_confirmed(
                record["id"], int(time.time()), "msg"
            )
        )
        changed = self.manager.cancel_delivery_confirmations_for_order(
            "o1", "manual"
        )
        self.assertEqual(changed, 1)
        self.assertEqual(
            self.manager.get_delivery_confirmation("acc", "o1")["status"],
            "cancelled",
        )

    def test_pending_delivery_orders_excludes_waiting_confirmations(self):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.manager.insert_or_update_order(
            order_id="o1",
            item_id="item-1",
            buyer_id="buyer-1",
            cookie_id="acc",
            order_status="pending_ship",
            created_at=now,
        )
        self.assertEqual(
            [item["order_id"] for item in self.manager.list_pending_delivery_orders("acc")],
            ["o1"],
        )

        record = self._create_record()
        self.assertEqual(self.manager.list_pending_delivery_orders("acc"), [])

        # 买家确认后允许补单继续真实发货
        self.manager.mark_delivery_confirmation_confirmed(
            record["id"], int(time.time()), "msg"
        )
        self.assertEqual(
            [item["order_id"] for item in self.manager.list_pending_delivery_orders("acc")],
            ["o1"],
        )

        self.manager.mark_delivery_confirmation_status(record["id"], "expired", "timeout")
        self.assertEqual(self.manager.list_pending_delivery_orders("acc"), [])

    def test_reset_for_resend(self):
        record = self._create_record()
        now = int(time.time())
        self.manager.mark_delivery_confirmation_prompt_attempt(record["id"])
        self.manager.mark_delivery_confirmation_prompt_sent(record["id"], now, now + 1800)
        self.manager.increment_delivery_confirmation_reminder(record["id"], now + 601)
        self.assertTrue(
            self.manager.reset_delivery_confirmation_for_resend(record["id"])
        )
        refreshed = self.manager.get_delivery_confirmation("acc", "o1")
        self.assertEqual(refreshed["status"], "pending")
        self.assertEqual(refreshed["prompt_attempts"], 0)
        self.assertEqual(refreshed["reminder_count"], 0)
        self.assertIsNone(refreshed["expires_at"])


if __name__ == "__main__":
    unittest.main()
