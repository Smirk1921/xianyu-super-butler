"""卡密备注位置、商品置顶、商品设置模板的回归测试。"""

import json
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from app.db_manager import DBManager
from XianyuAutoAsync import XianyuLive


class ItemFeatureTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.manager = DBManager(os.path.join(self.temp_dir.name, "features.db"))
        self.user_id = self.manager.conn.execute(
            "SELECT id FROM users WHERE username='admin'"
        ).fetchone()[0]
        self.manager.conn.execute(
            "INSERT INTO cookies (id, value, user_id) VALUES ('acc', 'cookie', ?)",
            (self.user_id,),
        )
        self.manager.conn.execute(
            "INSERT INTO item_info (cookie_id, item_id, item_title, item_detail) "
            "VALUES ('acc', 'item-1', '测试商品', '测试商品详情')"
        )
        self.manager.conn.commit()
        self.card_id = self.manager.create_card(
            "测试卡", "text", text_content="CARD-CONTENT", user_id=self.user_id
        )

    def tearDown(self):
        self.manager.close()
        self.temp_dir.cleanup()

    def test_card_description_position_round_trip(self):
        after_id = self.manager.create_card(
            "后置备注卡", "text", text_content="X", description="备注",
            description_position="after", user_id=self.user_id,
        )
        card = self.manager.get_card_by_id(after_id, self.user_id)
        self.assertEqual(card["description_position"], "after")
        listing = {item["id"]: item for item in self.manager.get_all_cards(self.user_id)}
        self.assertEqual(listing[after_id]["description_position"], "after")

    def test_description_render_before_after_and_placeholder(self):
        method = XianyuLive._process_delivery_content_with_description
        self.assertEqual(method(object(), "CARD", "NOTE", "before"), "NOTE\n\nCARD")
        self.assertEqual(method(object(), "CARD", "NOTE", "after"), "CARD\n\nNOTE")
        self.assertEqual(
            method(object(), "CARD", "说明：{DELIVERY_CONTENT}", "after"),
            "说明：CARD",
        )

    def test_delivery_rule_resolution_exposes_description_position(self):
        self.manager.update_card(
            self.card_id, description="备注", description_position="after"
        )
        self.manager.create_delivery_rule(
            "测试商品", self.card_id, user_id=self.user_id,
            cookie_id="acc", item_id="item-1",
        )
        rules = self.manager.get_delivery_rules_for_item(
            "测试商品 item-1", "acc", "item-1"
        )
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0]["card_description_position"], "after")

        self.manager.save_item_delivery_config(
            self.user_id, "acc", "item-1", True, False,
            [{
                "display_name": "默认规格",
                "spec_text": "",
                "spec_payload": {},
                "platform_sku_id": "",
                "card_id": self.card_id,
                "delivery_count": 1,
                "enabled": True,
                "binding_enabled": True,
            }],
        )
        binding = self.manager.resolve_item_delivery_binding("acc", "item-1")
        self.assertTrue(binding["matched"])
        self.assertEqual(binding["card_description_position"], "after")

    def test_item_pin_round_trip(self):
        self.assertTrue(self.manager.set_item_pinned("acc", "item-1", True))
        self.assertTrue(self.manager.get_item_info("acc", "item-1")["is_pinned"])
        self.assertTrue(self.manager.set_item_pinned("acc", "item-1", False))
        self.assertFalse(self.manager.get_item_info("acc", "item-1")["is_pinned"])

    def test_template_save_list_get_delete(self):
        payload = {
            "delivery_config": {"enabled": True, "is_multi_spec": False, "variants": []},
            "item_reply": {"reply_content": "hi", "priority": 1, "reply_once": 1},
        }
        template_id = self.manager.save_item_setting_template(
            self.user_id, "模板A", payload, "说明"
        )
        self.assertIsNotNone(template_id)
        templates = self.manager.list_item_setting_templates(self.user_id)
        self.assertEqual(templates[0]["name"], "模板A")
        self.assertEqual(templates[0]["payload"]["item_reply"]["reply_content"], "hi")
        self.assertEqual(
            self.manager.get_item_setting_template(self.user_id, template_id)["description"],
            "说明",
        )
        self.assertTrue(self.manager.delete_item_setting_template(self.user_id, template_id))
        self.assertEqual(self.manager.list_item_setting_templates(self.user_id), [])

    def test_apply_template_to_another_item(self):
        # 目标商品不同的 item_id，验证模板按目标 ID 写入
        self.manager.conn.execute(
            "INSERT INTO item_info (cookie_id, item_id, item_title, item_detail) "
            "VALUES ('acc', 'item-2', '新商品', '新商品详情')"
        )
        self.manager.conn.commit()
        self.manager.save_text_keywords_only(
            "acc",
            [
                ("全局词", "全局回复", None),
                ("旧商品词", "旧回复", "item-1"),
            ],
        )

        payload = {
            "version": 1,
            "keywords": [
                {"keyword": "模板关键词", "reply": "模板关键词回复", "type": "text", "image_url": ""},
            ],
            "delivery_config": {
                "enabled": True,
                "is_multi_spec": False,
                "variants": [{
                    "display_name": "默认规格",
                    "spec_text": "",
                    "spec_payload": {},
                    "platform_sku_id": "",
                    "card_id": self.card_id,
                    "card_name": "测试卡",
                    "delivery_count": 2,
                    "enabled": True,
                    "binding_enabled": True,
                    "source": "manual",
                }],
            },
            "item_reply": {"reply_content": "模板回复", "priority": 1, "reply_once": 1},
            "relist": {"enabled": True, "min_stock": 2},
            "confirmation": {
                "use_account_default": False,
                "enabled": True,
                "confirm_text": "请回复 {keyword}",
                "confirm_keywords": "确认发货",
                "exclude_keywords": "不确认",
                "timeout_minutes": 30,
                "reminder_minutes": 10,
                "max_reminders": 2,
                "reminder_text": "",
            },
            "item": {"is_multi_spec": False, "multi_quantity_delivery": True},
        }

        import app.reply_server as reply_server
        with patch.object(reply_server, "db_manager", self.manager):
            result = reply_server._apply_item_setting_template(
                "acc", "item-2", self.user_id, payload
            )

        self.assertIn("自动发货", result["applied"])
        delivery = self.manager.get_item_delivery_config("acc", "item-2", self.user_id)
        self.assertEqual(delivery["variants"][0]["card_id"], self.card_id)
        self.assertEqual(delivery["variants"][0]["delivery_count"], 2)
        self.assertEqual(
            self.manager.get_item_reply("acc", "item-2")["reply_content"], "模板回复"
        )
        item2_keywords = self.manager.get_keywords_for_item("acc", "item-2")
        self.assertEqual([row["keyword"] for row in item2_keywords], ["模板关键词"])
        item1_keywords = self.manager.get_keywords_for_item("acc", "item-1")
        self.assertEqual([row["keyword"] for row in item1_keywords], ["旧商品词"])
        all_keywords = self.manager.get_keywords_with_type("acc")
        self.assertIn(
            ("全局词", None),
            [(row["keyword"], row["item_id"]) for row in all_keywords],
        )
        relist = self.manager.get_item_relist_setting(self.user_id, "acc", "item-2")
        self.assertEqual(relist["enabled"], 1)
        self.assertEqual(relist["min_stock"], 2)
        effective = self.manager.get_effective_delivery_confirmation("acc", "item-2")
        self.assertTrue(effective["enabled"])
        self.assertEqual(effective["source"], "item")
        self.assertTrue(
            self.manager.get_item_multi_quantity_delivery_status("acc", "item-2")
        )


class PurchaseFirstMessageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.manager = DBManager(os.path.join(self.temp_dir.name, "first-message.db"))
        self.user_id = self.manager.conn.execute(
            "SELECT id FROM users WHERE username='admin'"
        ).fetchone()[0]
        self.manager.conn.execute(
            "INSERT INTO cookies (id, value, user_id) VALUES ('acc', 'cookie', ?)",
            (self.user_id,),
        )
        self.manager.conn.execute(
            "INSERT INTO item_info (cookie_id, item_id, item_title) "
            "VALUES ('acc', 'item-1', '测试商品')"
        )
        self.manager.conn.execute(
            "INSERT INTO item_info (cookie_id, item_id, item_title) "
            "VALUES ('acc', 'item-2', '默认回复商品')"
        )
        self.manager.conn.commit()
        self.instance = object.__new__(XianyuLive)
        self.instance.cookie_id = "acc"
        self.instance.ws = None
        self.instance.send_msg = AsyncMock()
        self.instance.send_im_text = AsyncMock()
        self.instance.send_image_msg = AsyncMock()
        self.instance._remember_bot_reply = Mock()
        self.instance._safe_str = lambda error: str(error)

    def tearDown(self):
        self.manager.close()
        self.temp_dir.cleanup()

    async def test_item_first_message_sent_once_before_delivery(self):
        self.manager.update_item_reply(
            "acc", "item-1", "你好 {send_user_name}", priority=1, reply_once=1
        )
        import XianyuAutoAsync as xa
        with patch.object(xa, "db_manager", self.manager), patch.object(
            xa, "AUTO_REPLY", {"enabled": True}
        ):
            sent = await self.instance._send_purchase_first_message(
                object(), {}, "买家", "buyer-1", "item-1", "chat-1", "now"
            )
            self.assertTrue(sent)
            self.instance.send_msg.assert_awaited_once()
            self.assertEqual(
                self.instance.send_msg.await_args.args[3], "你好 买家"
            )

            # reply_once=1：同一会话第二次付款不再重复问候
            sent_again = await self.instance._send_purchase_first_message(
                object(), {}, "买家", "buyer-1", "item-1", "chat-1", "now"
            )
            self.assertFalse(sent_again)
            self.instance.send_msg.assert_awaited_once()

    async def test_falls_back_to_default_reply(self):
        self.instance.get_default_reply = AsyncMock(
            return_value={"text": "默认首句", "image_url": None}
        )
        import XianyuAutoAsync as xa
        with patch.object(xa, "db_manager", self.manager), patch.object(
            xa, "AUTO_REPLY", {"enabled": True}
        ):
            sent = await self.instance._send_purchase_first_message(
                object(), {}, "买家", "buyer-2", "item-2", "chat-2", "now"
            )
            self.assertTrue(sent)
            self.instance.send_msg.assert_awaited_once()
            self.assertEqual(
                self.instance.send_msg.await_args.args[3], "默认首句"
            )


if __name__ == "__main__":
    unittest.main()
