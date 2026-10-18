"""商品自动重新上架的资格判断测试。

覆盖：
- 批量卡密库存充足/不足
- 固定文本、图片、API 卡密按无限库存处理
- 卡密停用、未绑定、多规格严格模式
- 旧版 delivery_rules 的库存判断
- 冷却、pending、手动商品等资格过滤
- 商品级开关的 upsert 和候选查询
- 执行器库存目标换算与 0 库存保护
"""

import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timedelta

from app.db_manager import DBManager
from app.item_relist import ItemRelistService
from utils.item_relist import (
    browser_cookies_to_string,
    build_stock_targets,
    check_seller_session,
    merge_cookie_strings,
    normalize_stock,
    parse_cookie_string,
    relist_item,
)


class ItemRelistTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.manager = DBManager(os.path.join(self.temp_dir.name, "relist.db"))
        self.user_id = self.manager.conn.execute(
            "SELECT id FROM users WHERE username='admin'"
        ).fetchone()[0]
        cursor = self.manager.conn.cursor()
        cursor.execute(
            "INSERT INTO cookies (id, value, user_id) VALUES ('acc', 'cookie', ?)",
            (self.user_id,),
        )
        # 批量卡密：3 条库存
        cursor.execute(
            "INSERT INTO cards (id, name, type, data_content, enabled, user_id) "
            "VALUES (1, '批量卡', 'data', ?, 1, ?)",
            ("k1\nk2\nk3", self.user_id),
        )
        # 固定文本卡密
        cursor.execute(
            "INSERT INTO cards (id, name, type, text_content, enabled, user_id) "
            "VALUES (2, '文本卡', 'text', 'hello', 1, ?)",
            (self.user_id,),
        )
        # 停用的批量卡密
        cursor.execute(
            "INSERT INTO cards (id, name, type, data_content, enabled, user_id) "
            "VALUES (3, '停用卡', 'data', ?, 0, ?)",
            ("x1\nx2", self.user_id),
        )
        # 图片卡密
        cursor.execute(
            "INSERT INTO cards (id, name, type, image_url, enabled, user_id) "
            "VALUES (4, '图片卡', 'image', 'https://example.test/a.png', 1, ?)",
            (self.user_id,),
        )
        # API 卡密
        cursor.execute(
            "INSERT INTO cards (id, name, type, api_config, enabled, user_id) "
            "VALUES (5, 'API卡', 'api', ?, 1, ?)",
            ('{"url": "https://example.test/api"}', self.user_id),
        )
        # 内容为空的固定文本卡密
        cursor.execute(
            "INSERT INTO cards (id, name, type, text_content, enabled, user_id) "
            "VALUES (6, '空文本卡', 'text', '', 1, ?)",
            (self.user_id,),
        )
        self.manager.conn.commit()
        self.service = ItemRelistService(self.manager)

    def tearDown(self):
        self.manager.close()
        self.temp_dir.cleanup()

    def _add_item(self, item_id, listing_status="off_shelf", last_seen="2026-10-17 00:00:00"):
        self.manager.conn.execute(
            "INSERT INTO item_info (cookie_id, item_id, item_title, listing_status, last_seen_at) "
            "VALUES ('acc', ?, ?, ?, ?)",
            (item_id, f"商品 {item_id}", listing_status, last_seen),
        )
        self.manager.conn.commit()

    def _bind_variant(self, item_id, card_id, delivery_count=1, enabled=1):
        cursor = self.manager.conn.cursor()
        cursor.execute(
            "INSERT INTO item_delivery_configs "
            "(user_id, cookie_id, item_id, enabled, is_multi_spec) VALUES (?, 'acc', ?, 1, 0)",
            (self.user_id, item_id),
        )
        cursor.execute(
            "INSERT INTO product_variants "
            "(user_id, cookie_id, item_id, display_name, platform_sku_id, "
            " spec_payload_json, canonical_spec_key, source, enabled) "
            "VALUES (?, 'acc', ?, '默认', '', '{}', 'default', 'manual', 1)",
            (self.user_id, item_id),
        )
        variant_id = cursor.lastrowid
        cursor.execute(
            "INSERT INTO variant_delivery_bindings "
            "(user_id, variant_id, card_id, delivery_count, enabled) VALUES (?, ?, ?, ?, ?)",
            (self.user_id, variant_id, card_id, delivery_count, enabled),
        )
        self.manager.conn.commit()

    def _add_legacy_rule(self, item_id, card_id, delivery_count=1):
        self.manager.conn.execute(
            "INSERT INTO delivery_rules "
            "(keyword, card_id, delivery_count, enabled, description, user_id, cookie_id, item_id) "
            "VALUES ('', ?, ?, 1, '', ?, 'acc', ?)",
            (card_id, delivery_count, self.user_id, item_id),
        )
        self.manager.conn.commit()

    # ------------------------------------------------------------------
    # 库存判断
    # ------------------------------------------------------------------
    def test_data_card_with_stock_is_eligible(self):
        self._add_item("1001")
        self._bind_variant("1001", 1)
        result = self.service.evaluate_stock("acc", "1001", self.user_id, 1)
        self.assertTrue(result["eligible"])
        self.assertEqual(result["details"][0]["stock"], 3)

    def test_data_card_without_enough_stock_is_rejected(self):
        self._add_item("1001")
        self._bind_variant("1001", 1, delivery_count=2)
        result = self.service.evaluate_stock("acc", "1001", self.user_id, 2)
        self.assertFalse(result["eligible"])
        self.assertIn("库存不足", result["reason"])

    def test_text_card_is_eligible(self):
        self._add_item("1001")
        self._bind_variant("1001", 2)
        result = self.service.evaluate_stock("acc", "1001", self.user_id, 1)
        self.assertTrue(result["eligible"])
        self.assertIsNone(result["details"][0]["stock"])

    def test_empty_text_card_is_rejected(self):
        self._add_item("1001")
        self._bind_variant("1001", 6)
        result = self.service.evaluate_stock("acc", "1001", self.user_id, 1)
        self.assertFalse(result["eligible"])
        self.assertIn("固定文本为空", result["reason"])

    def test_image_card_is_eligible(self):
        self._add_item("1001")
        self._bind_variant("1001", 4)
        result = self.service.evaluate_stock("acc", "1001", self.user_id, 1)
        self.assertTrue(result["eligible"])

    def test_api_card_is_eligible(self):
        self._add_item("1001")
        self._bind_variant("1001", 5)
        result = self.service.evaluate_stock("acc", "1001", self.user_id, 1)
        self.assertTrue(result["eligible"])

    def test_disabled_card_is_rejected(self):
        self._add_item("1001")
        self._bind_variant("1001", 3)
        result = self.service.evaluate_stock("acc", "1001", self.user_id, 1)
        self.assertFalse(result["eligible"])
        self.assertIn("已停用", result["reason"])

    def test_no_binding_is_rejected(self):
        self._add_item("1001")
        result = self.service.evaluate_stock("acc", "1001", self.user_id, 1)
        self.assertFalse(result["eligible"])
        self.assertIn("没有绑定", result["reason"])

    def test_legacy_rule_data_card(self):
        self._add_item("2002")
        self._add_legacy_rule("2002", 1, delivery_count=1)
        result = self.service.evaluate_stock("acc", "2002", self.user_id, 1)
        self.assertTrue(result["eligible"])
        self.assertEqual(result["source"], "rule")

    # ------------------------------------------------------------------
    # 资格过滤
    # ------------------------------------------------------------------
    def test_manual_item_without_last_seen_is_rejected(self):
        self._add_item("1001", last_seen=None)
        self._bind_variant("1001", 1)
        candidate = {
            "cookie_id": "acc",
            "item_id": "1001",
            "listing_status": "off_shelf",
            "last_seen_at": None,
            "last_attempt_at": None,
            "min_stock": 1,
        }
        result = self.service.check_eligibility(candidate, self.user_id)
        self.assertFalse(result["eligible"])
        self.assertIn("手动添加", result["reason"])

    def test_pending_status_is_rejected(self):
        candidate = {
            "cookie_id": "acc",
            "item_id": "1001",
            "listing_status": "on_sale_pending",
            "last_seen_at": "2026-10-17 00:00:00",
            "last_attempt_at": None,
            "min_stock": 1,
        }
        result = self.service.check_eligibility(candidate, self.user_id)
        self.assertFalse(result["eligible"])
        self.assertIn("等待同步确认", result["reason"])

    def test_cooldown_blocks_recent_attempt(self):
        self._add_item("1001")
        self._bind_variant("1001", 1)
        recent = (datetime.utcnow() - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
        candidate = {
            "cookie_id": "acc",
            "item_id": "1001",
            "listing_status": "off_shelf",
            "last_seen_at": "2026-10-17 00:00:00",
            "last_attempt_at": recent,
            "min_stock": 1,
        }
        result = self.service.check_eligibility(candidate, self.user_id, cooldown_minutes=60)
        self.assertFalse(result["eligible"])
        self.assertIn("冷却中", result["reason"])

    def test_eligible_candidate_passes(self):
        self._add_item("1001")
        self._bind_variant("1001", 1)
        candidate = {
            "cookie_id": "acc",
            "item_id": "1001",
            "listing_status": "off_shelf",
            "last_seen_at": "2026-10-17 00:00:00",
            "last_attempt_at": None,
            "min_stock": 1,
        }
        result = self.service.check_eligibility(candidate, self.user_id)
        self.assertTrue(result["eligible"])

    # ------------------------------------------------------------------
    # 配置与记录
    # ------------------------------------------------------------------
    def test_upsert_setting_and_candidate_query(self):
        self._add_item("1001")
        self.assertTrue(
            self.manager.upsert_item_relist_setting(self.user_id, "acc", "1001", True, 1)
        )
        candidates = self.manager.list_item_relist_candidates(self.user_id, "acc")
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["item_id"], "1001")
        self.assertEqual(candidates[0]["enabled"], 1)

        # 关闭后不再出现在候选里
        self.manager.upsert_item_relist_setting(self.user_id, "acc", "1001", False, 1)
        self.assertEqual(self.manager.list_item_relist_candidates(self.user_id, "acc"), [])

    def test_record_attempt_updates_counter(self):
        self._add_item("1001")
        self.manager.upsert_item_relist_setting(self.user_id, "acc", "1001", True, 1)
        self.manager.record_item_relist_attempt(
            self.user_id, "acc", "1001", "failed", "测试失败"
        )
        setting = self.manager.get_item_relist_setting(self.user_id, "acc", "1001")
        self.assertEqual(setting["last_result"], "failed")
        self.assertEqual(setting["last_reason"], "测试失败")
        self.assertEqual(setting["attempts_today"], 1)

    def test_mark_listing_pending(self):
        self._add_item("1001")
        self.assertTrue(self.manager.set_item_listing_pending("acc", "1001"))
        item = self.manager.get_item_info("acc", "1001")
        self.assertEqual(item["listing_status"], "on_sale_pending")

    def test_list_candidates_attaches_eligibility(self):
        self._add_item("1001")
        self._bind_variant("1001", 1)
        self.manager.upsert_item_relist_setting(self.user_id, "acc", "1001", True, 1)
        rows = self.service.list_candidates(self.user_id, "acc")
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["eligible"])


class RelistExecutorTest(unittest.TestCase):
    """执行器辅助函数的单元测试（不启动浏览器）。"""

    def test_data_stock_lists_one_unit_to_avoid_overselling(self):
        # 多个商品链接可能共用同一张批量卡，每个链接只挂 1 件
        self.assertEqual(build_stock_targets([{"stock": 5}]), [1])

    def test_text_image_api_use_infinite_stock(self):
        self.assertEqual(
            build_stock_targets([{"stock": None}], infinite_stock=9999),
            [9999],
        )

    def test_mixed_specs_map_in_order(self):
        self.assertEqual(
            build_stock_targets([{"stock": 2}, {"stock": None}, {"stock": 7}]),
            [1, 9999, 1],
        )

    def test_no_details_falls_back_to_infinite(self):
        self.assertEqual(build_stock_targets([]), [9999])

    def test_stock_is_capped_at_platform_limit(self):
        self.assertEqual(normalize_stock(99999), 10000)

    def test_zero_stock_is_preserved(self):
        self.assertEqual(normalize_stock(0), 0)

    def test_zero_stock_detail_stays_zero(self):
        # 批量卡发完后不能变成 1，否则会重新挂出无货订单
        self.assertEqual(build_stock_targets([{"stock": 0}]), [0])

    def test_relist_refuses_zero_stock_without_launching_browser(self):
        result = asyncio.run(
            relist_item("acc", "cookie=1", "1093294412397", stock_targets=[0])
        )
        self.assertFalse(result["success"])
        self.assertIn("库存目标为 0", result["reason"])


class RelistCookieHelpersTest(unittest.TestCase):
    """Cookie 合并/导出与会话检查的纯函数测试。"""

    def test_parse_cookie_string_ignores_broken_pairs(self):
        self.assertEqual(
            parse_cookie_string("a=1; b=2; broken; =empty"),
            {"a": "1", "b": "2"},
        )

    def test_merge_later_values_win(self):
        merged = merge_cookie_strings("a=1; b=2", "b=3; c=4")
        self.assertEqual(
            parse_cookie_string(merged),
            {"a": "1", "b": "3", "c": "4"},
        )

    def test_browser_cookies_filter_and_goofish_priority(self):
        cookies = [
            {"name": "cookie2", "value": "taobao", "domain": ".taobao.com"},
            {"name": "cookie2", "value": "goofish", "domain": ".goofish.com"},
            {"name": "cna", "value": "third-party", "domain": ".alicdn.com"},
        ]
        parsed = parse_cookie_string(browser_cookies_to_string(cookies))
        self.assertEqual(parsed["cookie2"], "goofish")
        self.assertNotIn("cna", parsed)

    def test_check_session_without_cookie_requires_login(self):
        result = asyncio.run(check_seller_session("acc", "", headless=True))
        self.assertEqual(result["status"], "login_required")


class CookieStorageTest(unittest.TestCase):
    """增量刷新 Cookie 时不能清空账号其他字段。"""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.manager = DBManager(os.path.join(self.temp_dir.name, "cookie.db"))
        user_id = self.manager.conn.execute(
            "SELECT id FROM users WHERE username='admin'"
        ).fetchone()[0]
        self.manager.conn.execute(
            "INSERT INTO cookies (id, value, user_id, remark, show_browser) "
            "VALUES ('acc', 'old=1', ?, 'keep-me', 1)",
            (user_id,),
        )
        self.manager.conn.commit()

    def tearDown(self):
        self.manager.close()
        self.temp_dir.cleanup()

    def test_update_cookie_value_preserves_other_columns(self):
        self.assertTrue(self.manager.update_cookie_value("acc", "new=2"))
        row = self.manager.conn.execute(
            "SELECT value, remark, show_browser FROM cookies WHERE id='acc'"
        ).fetchone()
        self.assertEqual(row[0], "new=2")
        self.assertEqual(row[1], "keep-me")
        self.assertEqual(row[2], 1)


if __name__ == "__main__":
    unittest.main()
