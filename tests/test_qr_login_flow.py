import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch

from app import reply_server


class QrLoginFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        reply_server.qr_check_processed.clear()

    def tearDown(self):
        reply_server.qr_check_processed.clear()

    async def test_fast_persistence_returns_account_before_cookie_enhancement(self):
        manager = Mock()
        manager.add_cookie.return_value = None
        current_user = {"user_id": 7, "username": "tester"}

        with (
            patch.object(reply_server.db_manager, "get_all_cookies", return_value={}),
            patch.object(reply_server.db_manager, "save_cookie", return_value=True) as save_cookie,
            patch.object(reply_server.cookie_manager, "manager", manager),
        ):
            result = await reply_server.process_qr_login_cookies(
                "unb=account-1; cookie2=test-value",
                "account-1",
                current_user,
            )

        self.assertEqual(result["account_id"], "account-1")
        self.assertTrue(result["is_new_account"])
        self.assertEqual(result["cookie_field_count"], 2)
        save_cookie.assert_called_once()
        # 新账号也不能在增强前启动半成品任务，否则会占住浏览器槽位。
        manager.add_cookie.assert_not_called()

    async def test_existing_account_qr_bootstrap_allows_temp_partial_cookie(self):
        """扫码确认只给 9 个核心字段；先暂停旧任务，增强后再启动。"""
        manager = Mock()
        manager.pause_cookie_task = AsyncMock(return_value=True)
        current_user = {"user_id": 7, "username": "tester"}
        full_existing_cookie = (
            "unb=account-1; _m_h5_tk=old-token; cookie2=old-c2; sgcookie=old-sg"
        )
        qr_cookie = "unb=account-1; cookie2=new-c2; sgcookie=new-sg"

        with (
            patch.object(
                reply_server.db_manager,
                "get_all_cookies",
                return_value={"account-1": full_existing_cookie},
            ),
            patch.object(
                reply_server.db_manager,
                "update_cookie_account_info",
                return_value=True,
            ) as update_cookie,
            patch.object(reply_server.cookie_manager, "manager", manager),
        ):
            result = await reply_server.process_qr_login_cookies(
                qr_cookie,
                "account-1",
                current_user,
            )

        self.assertEqual(result["account_id"], "account-1")
        self.assertFalse(result["is_new_account"])
        self.assertEqual(result["cookie_field_count"], 3)
        update_cookie.assert_called_once()
        self.assertIn("allow_cookie_regression=True", repr(update_cookie.call_args))
        manager.pause_cookie_task.assert_awaited_once_with("account-1")
        manager.update_cookie.assert_not_called()
        self.assertEqual(result["_previous_cookie"], full_existing_cookie)

    async def test_enhancement_starts_task_only_after_full_cookie(self):
        manager = Mock()
        manager.ensure_cookie_task = Mock(return_value=None)
        full_cookie = "unb=account-1; _m_h5_tk=new; cookie2=c2; sgcookie=sg; cna=x"
        account_info = {
            "account_id": "account-1",
            "cookie_field_count": 9,
            "real_cookie_refreshed": False,
        }
        temp_instance = Mock()
        temp_instance.refresh_cookies_from_qr_login = AsyncMock(return_value=True)

        with (
            patch("XianyuAutoAsync.XianyuLive", return_value=temp_instance),
            patch.object(
                reply_server.db_manager,
                "get_cookie_by_id",
                return_value={"cookies_str": full_cookie},
            ),
            patch.object(reply_server.cookie_manager, "manager", manager),
            patch.object(
                reply_server,
                "_fetch_and_store_account_profile",
                AsyncMock(return_value={"success": True}),
            ),
        ):
            await reply_server._enhance_qr_login_cookies(
                "session-1",
                account_info,
                "unb=account-1; cookie2=c2; sgcookie=sg",
                {"user_id": 7, "username": "tester"},
            )

        manager.ensure_cookie_task.assert_called_once_with(
            "account-1",
            full_cookie,
            user_id=7,
            restart=False,
            save_to_db=False,
        )

    async def test_enhancement_failure_restores_previous_cookie(self):
        manager = Mock()
        previous_cookie = "unb=account-1; _m_h5_tk=old; cookie2=old; sgcookie=old"
        account_info = {"account_id": "account-1", "cookie_field_count": 9}
        temp_instance = Mock()
        temp_instance.refresh_cookies_from_qr_login = AsyncMock(return_value=False)

        with (
            patch("XianyuAutoAsync.XianyuLive", return_value=temp_instance),
            patch.object(
                reply_server.db_manager,
                "update_cookie_account_info",
                return_value=True,
            ) as update_cookie,
            patch.object(reply_server.cookie_manager, "manager", manager),
        ):
            await reply_server._enhance_qr_login_cookies(
                "session-1",
                account_info,
                "unb=account-1; cookie2=c2; sgcookie=sg",
                {"user_id": 7, "username": "tester"},
                previous_cookie=previous_cookie,
            )

        update_cookie.assert_called_once()
        self.assertEqual(
            update_cookie.call_args.kwargs["cookie_value"], previous_cookie
        )
        manager.ensure_cookie_task.assert_not_called()

    async def test_mismatched_account_identifier_is_rejected_before_save(self):
        current_user = {"user_id": 7, "username": "tester"}

        with patch.object(reply_server.db_manager, "save_cookie") as save_cookie:
            with self.assertRaisesRegex(ValueError, "账号标识不一致"):
                await reply_server.process_qr_login_cookies(
                    "unb=cookie-account; cookie2=test-value",
                    "response-account",
                    current_user,
                )

        save_cookie.assert_not_called()

    async def test_session_becomes_success_while_enhancement_is_still_running(self):
        session_id = "session-fast-success"
        enhancement_release = asyncio.Event()

        async def wait_for_release(**_):
            await enhancement_release.wait()

        account_info = {
            "account_id": "account-1",
            "is_new_account": True,
            "real_cookie_refreshed": False,
            "cookie_field_count": 2,
            "_manager_operation": None,
        }

        with (
            patch.object(
                reply_server,
                "process_qr_login_cookies",
                new=AsyncMock(return_value=account_info),
            ),
            patch.object(
                reply_server,
                "_enhance_qr_login_cookies",
                side_effect=wait_for_release,
            ),
        ):
            task = asyncio.create_task(
                reply_server._process_qr_login_session(
                    session_id,
                    {"cookies": "unb=account-1; cookie2=test-value", "unb": "account-1"},
                    {"user_id": 7, "username": "tester"},
                )
            )

            for _ in range(20):
                if session_id in reply_server.qr_check_processed:
                    break
                await asyncio.sleep(0)

            result = reply_server.qr_check_processed[session_id]["result"]
            self.assertEqual(result["status"], "success")
            self.assertTrue(result["account_ready"])
            self.assertEqual(result["cookie_refresh_status"], "processing")
            self.assertFalse(task.done())

            enhancement_release.set()
            await task


if __name__ == "__main__":
    unittest.main()
