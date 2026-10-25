# -*- coding: utf-8 -*-
"""fresh-captcha-url 端点的熔断解除接线测试。

只拿到 punish 链接不代表风控解除：x5sec 仍留在用户浏览器里，服务端尚未拿到
通行凭证。此前端点一拿到 URL 就 reset 熔断，心跳/自动滑块立刻恢复，把全局
出口 IP 的 RGV587 惩罚越打越长。现在只有在响应包含 accessToken（平台确认
放行）时才解除账号与全局熔断。
"""
import asyncio
import unittest
from unittest import mock

from app.reply_server import get_fresh_captcha_url


class _AsyncCM:
    """把一个已构造好的值包成 async with 上下文。"""

    def __init__(self, value):
        self._value = value

    async def __aenter__(self):
        return self._value

    async def __aexit__(self, *exc_info):
        return False


def _make_aiohttp(payload):
    """伪造 aiohttp.ClientSession，post 任意参数都返回同一份 payload。"""
    response = mock.MagicMock()
    response.json = mock.AsyncMock(return_value=payload)
    session = mock.MagicMock()
    session.post = mock.MagicMock(return_value=_AsyncCM(response))
    return mock.MagicMock(return_value=_AsyncCM(session))


class FreshCaptchaUnblockTests(unittest.TestCase):
    def _run(self, payload, cookie_id='cid-1'):
        with mock.patch('aiohttp.ClientSession', _make_aiohttp(payload)), \
             mock.patch('app.reply_server.db_manager') as db, \
             mock.patch('utils.xianyu_utils.trans_cookies',
                        return_value={'_m_h5_tk': 'tk_abc'}), \
             mock.patch('utils.risk_control.registry') as registry:
            db.get_all_cookies.return_value = {cookie_id: '_m_h5_tk=tk_abc; x=1'}
            guard = registry.get.return_value
            result = asyncio.run(get_fresh_captcha_url(
                cookie_id, {'user_id': 7, 'username': 'alice'},
            ))
        return result, registry, guard

    def test_punish_link_does_not_clear_risk_block(self):
        """仅取得惩罚 URL 不解除熔断，避免自动重试风暴。"""
        result, registry, guard = self._run({
            'data': {'url': 'https://login.taobao.com/jump?punish=x5secdata&action=captcha'},
        })
        self.assertTrue(result['need_verify'], result)
        self.assertIn('punish', result['verification_url'])
        guard.reset.assert_not_called()

    def test_already_recovered_clears_risk_block(self):
        """令牌接口给了 accessToken 说明风控已解除，本地冷却也要清掉。"""
        result, registry, guard = self._run({
            'data': {'accessToken': 'tok-ok'},
        })
        self.assertFalse(result['need_verify'], result)
        guard.reset.assert_called_once()


if __name__ == '__main__':
    unittest.main()
