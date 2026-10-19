"""批量卡密去重与重复消费保护的回归测试。"""

import os
import tempfile
import unittest

from app.db_manager import DBManager


class CardDedupeTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.manager = DBManager(os.path.join(self.temp_dir.name, "cards.db"))
        self.user_id = self.manager.conn.execute(
            "SELECT id FROM users WHERE username='admin'"
        ).fetchone()[0]
        self.card_id = self.manager.create_card(
            "测试卡",
            "data",
            data_content="a\nb\na\n\nc",
            user_id=self.user_id,
        )

    def tearDown(self):
        self.manager.close()
        self.temp_dir.cleanup()

    def _data_content(self):
        return self.manager.conn.execute(
            "SELECT data_content FROM cards WHERE id=?", (self.card_id,)
        ).fetchone()[0]

    def test_create_card_removes_duplicate_and_blank_lines(self):
        self.assertEqual(self._data_content(), "a\nb\nc")

    def test_update_card_removes_duplicate_lines(self):
        self.manager.update_card(self.card_id, data_content="x\ny\nx\n")
        self.assertEqual(self._data_content(), "x\ny")

    def test_consumption_records_order_metadata(self):
        consumed = self.manager.consume_batch_data_batch(
            self.card_id,
            1,
            order_id="order-1",
            item_id="item-1",
            buyer_id="buyer-1",
            cookie_id="cookie-1",
        )
        self.assertEqual(consumed, ["a"])
        row = self.manager.conn.execute(
            "SELECT order_id, item_id, buyer_id, cookie_id, content "
            "FROM card_shipments WHERE card_id=?",
            (self.card_id,),
        ).fetchone()
        self.assertEqual(row[0], "order-1")
        self.assertEqual(row[1], "item-1")
        self.assertEqual(row[2], "buyer-1")
        self.assertEqual(row[3], "cookie-1")
        self.assertEqual(row[4], "a")

    def test_second_unique_line_can_be_consumed(self):
        self.manager.consume_batch_data_batch(self.card_id, 1, order_id="order-1")
        consumed = self.manager.consume_batch_data_batch(
            self.card_id, 1, order_id="order-2"
        )
        self.assertEqual(consumed, ["b"])

    def test_rejects_content_that_was_already_shipped(self):
        self.manager.consume_batch_data_batch(self.card_id, 1, order_id="order-1")
        # 模拟历史库存里还留着一条已经发过的重复行
        self.manager.conn.execute(
            "UPDATE cards SET data_content='a\nb' WHERE id=?", (self.card_id,)
        )
        self.manager.conn.commit()

        consumed = self.manager.consume_batch_data_batch(
            self.card_id, 1, order_id="order-2"
        )
        self.assertIsNone(consumed)
        self.assertEqual(self._data_content(), "a\nb")

    def test_rejects_internal_duplicate_lines(self):
        self.manager.conn.execute(
            "UPDATE cards SET data_content='x\nx' WHERE id=?", (self.card_id,)
        )
        self.manager.conn.commit()

        consumed = self.manager.consume_batch_data_batch(
            self.card_id, 2, order_id="order-3"
        )
        self.assertIsNone(consumed)
        self.assertEqual(self._data_content(), "x\nx")


if __name__ == "__main__":
    unittest.main()
