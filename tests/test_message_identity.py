"""订单卡片更新消息与自动发货触发的回归测试。

覆盖这次线上故障的三个根因：
- 付款卡片更新（message['1'] 为字符串、详情在 message['4']）不能被当成非聊天消息
- 商品 ID 必须来自 reminderUrl，不能把 ``435...PNM`` 或 ``chatId@goofish`` 当成商品
- 付款消息文本必须能命中自动发货触发词
"""

import types
import unittest

from XianyuAutoAsync import XianyuLive

PAID_CARD_UPDATE = {
    "1": "4350224982274.PNM",
    "2": "68018276297@goofish",
    "3": 1,
    "4": {
        "detailNotice": "[我已付款，等待你发货]",
        "reminderContent": "[我已付款，等待你发货]",
        "reminderUrl": (
            "fleamarket://message_chat?itemId=1093294412397"
            "&peerUserId=2217722690868&sid=68018276297"
        ),
        "senderUserId": "2217722690868",
        "reminderTitle": "买家",
    },
    "5": 1792230728165,
}

NORMAL_CHAT = {
    "1": {
        "1": {"1": "2217722690868@goofish"},
        "2": "68018276297@goofish",
        "5": 1792230728165,
        "10": {
            "senderUserId": "2217722690868",
            "senderNick": "买家",
            "reminderContent": "你好",
            "reminderUrl": (
                "fleamarket://message_chat?itemId=1093294412397"
                "&peerUserId=2217722690868&sid=68018276297"
            ),
        },
    }
}


def make_live():
    dummy = types.SimpleNamespace(cookie_id="acc")
    dummy._is_card_update_message = XianyuLive._is_card_update_message
    return dummy


class MessageIdentityTests(unittest.TestCase):
    def test_card_update_is_recognized_as_chat(self):
        self.assertTrue(
            XianyuLive.is_chat_message(make_live(), PAID_CARD_UPDATE)
        )

    def test_card_update_identity_comes_from_reminder_url(self):
        item_id, buyer_id = XianyuLive._extract_message_identity(
            make_live(), PAID_CARD_UPDATE
        )
        self.assertEqual(item_id, "1093294412397")
        self.assertEqual(buyer_id, "2217722690868")

    def test_pnm_content_id_is_not_used_as_item_id(self):
        parsed = XianyuLive.extract_item_id_from_message(
            make_live(), PAID_CARD_UPDATE
        )
        self.assertNotEqual(parsed, "4350224982274")

    def test_normal_chat_identity(self):
        item_id, buyer_id = XianyuLive._extract_message_identity(
            make_live(), NORMAL_CHAT
        )
        self.assertEqual(item_id, "1093294412397")
        self.assertEqual(buyer_id, "2217722690868")

    def test_paid_message_hits_delivery_trigger(self):
        self.assertTrue(
            XianyuLive._is_auto_delivery_trigger(
                make_live(), "[我已付款，等待你发货]"
            )
        )


if __name__ == "__main__":
    unittest.main()
