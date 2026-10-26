# -*- coding: utf-8 -*-
"""人工验证结束后必须恢复监听任务。"""

import unittest
from unittest.mock import AsyncMock, Mock, patch

from app import reply_server


class ManualCaptchaRestartTests(unittest.IsolatedAsyncioTestCase):
    async def test_successful_manual_captcha_restarts_account_task(self):
        cookie_id = 'account-1'
        old_cookie = 'unb=account-1; _m_h5_tk=tk; cookie2=c2; sgcookie=sg'
        verified_cookie = 'unb=account-1; cookie2=c2; sgcookie=sg; x5sec=passed'
        manager = Mock()
        manager.pause_cookie_task = AsyncMock(return_value=True)
        manager.ensure_cookie_task = Mock(return_value=None)
        manager.get_cookie_status.return_value = False
        manager.update_cookie_status = Mock(return_value=None)
        guard = Mock()
        guard.is_blocked = True
        guard.remaining_seconds = 120
        guard.last_hit_reason = 'RGV587'

        with (
            patch.object(
                reply_server.db_manager,
                'get_all_cookies',
                return_value={cookie_id: old_cookie},
            ),
            patch.object(
                reply_server.db_manager,
                'get_cookie_details',
                return_value={'value': old_cookie},
            ),
            patch.object(reply_server.db_manager, 'update_cookie_value') as update_value,
            patch.object(reply_server.cookie_manager, 'manager', manager),
            patch('utils.risk_control.registry.get', return_value=guard),
            patch(
                'utils.manual_captcha.open_manual_session',
                AsyncMock(return_value={
                    'success': True,
                    'cookies_str': verified_cookie,
                    'message': '人工验证完成',
                    'session_id': cookie_id,
                }),
            ),
        ):
            response = await reply_server.start_manual_captcha(
                cookie_id=cookie_id,
                timeout=300,
                current_user={'user_id': 7, 'username': 'tester'},
            )

        self.assertEqual(response.status_code, 200)
        manager.pause_cookie_task.assert_awaited_once_with(cookie_id)
        update_value.assert_called_once()
        merged = update_value.call_args.args[1]
        self.assertIn('_m_h5_tk=tk', merged)
        self.assertIn('x5sec=passed', merged)
        manager.ensure_cookie_task.assert_called_once()
        self.assertEqual(
            manager.ensure_cookie_task.call_args.kwargs['save_to_db'], False
        )
        manager.update_cookie_status.assert_called_once_with(cookie_id, True)


if __name__ == '__main__':
    unittest.main()
