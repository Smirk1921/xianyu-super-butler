"""商品专属自动回复的存储层回归测试。

覆盖：
- 同一 item_id 在不同账号下必须互不串号（旧实现会跨账号取到别人的回复）
- update_item_reply 是原子 upsert，重复保存只有一行
- priority 正确持久化（1=优先，0=兜底）
- 列表查询按 (cookie_id, item_id) 关联商品标题，不会串到同名商品
"""

import os
import sqlite3
import tempfile
import unittest

from app.db_manager import DBManager


class ItemReplyTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.manager = DBManager(os.path.join(self.temp_dir.name, "item_reply.db"))
        cursor = self.manager.conn.cursor()
        self.user_id = cursor.execute(
            "SELECT id FROM users WHERE username='admin'"
        ).fetchone()[0]
        cursor.executemany(
            "INSERT INTO cookies (id, value, user_id) VALUES (?, ?, ?)",
            [
                ("acc-a", "cookie-a", self.user_id),
                ("acc-b", "cookie-b", self.user_id),
            ],
        )
        cursor.executemany(
            """
            INSERT INTO item_info (cookie_id, item_id, item_title, item_detail)
            VALUES (?, ?, ?, ?)
            """,
            [
                ("acc-a", "1001", "A 商品", "A 描述"),
                ("acc-b", "1001", "B 商品", "B 描述"),
            ],
        )
        self.manager.conn.commit()

    def tearDown(self):
        self.manager.close()
        self.temp_dir.cleanup()

    def test_same_item_id_is_isolated_by_cookie(self):
        self.assertTrue(
            self.manager.update_item_reply("acc-a", "1001", "A 的专属回复", priority=1)
        )
        self.assertTrue(
            self.manager.update_item_reply("acc-b", "1001", "B 的专属回复", priority=0)
        )

        reply_a = self.manager.get_item_reply("acc-a", "1001")
        reply_b = self.manager.get_item_reply("acc-b", "1001")

        self.assertEqual(reply_a["reply_content"], "A 的专属回复")
        self.assertEqual(reply_a["priority"], 1)
        self.assertEqual(reply_b["reply_content"], "B 的专属回复")
        self.assertEqual(reply_b["priority"], 0)

    def test_update_is_upsert_with_single_row(self):
        self.manager.update_item_reply("acc-a", "1001", "第一版")
        self.manager.update_item_reply("acc-a", "1001", "第二版", priority=0)

        rows = self.manager.conn.execute(
            "SELECT reply_content, priority FROM item_replay "
            "WHERE cookie_id=? AND item_id=?",
            ("acc-a", "1001"),
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "第二版")
        self.assertEqual(rows[0][1], 0)

    def test_unique_index_rejects_duplicate_insert(self):
        self.manager.update_item_reply("acc-a", "1001", "first")
        with self.assertRaises(sqlite3.IntegrityError):
            self.manager.conn.execute(
                "INSERT INTO item_replay (item_id, cookie_id, reply_content) "
                "VALUES (?, ?, ?)",
                ("1001", "acc-a", "duplicate"),
            )
        self.manager.conn.rollback()

    def test_list_joins_title_by_cookie(self):
        self.manager.update_item_reply("acc-a", "1001", "A 回复")
        self.manager.update_item_reply("acc-b", "1001", "B 回复")

        rows_a = self.manager.get_itemReplays_by_cookie("acc-a")
        self.assertEqual(len(rows_a), 1)
        self.assertEqual(rows_a[0]["item_title"], "A 商品")
        self.assertEqual(rows_a[0]["priority"], 1)

        rows_b = self.manager.get_itemReplays_by_cookie("acc-b")
        self.assertEqual(len(rows_b), 1)
        self.assertEqual(rows_b[0]["item_title"], "B 商品")

    def test_missing_reply_returns_none(self):
        self.assertIsNone(self.manager.get_item_reply("acc-a", "9999"))
        self.assertIsNone(self.manager.get_item_replay("9999", cookie_id="acc-a"))


if __name__ == "__main__":
    unittest.main()
