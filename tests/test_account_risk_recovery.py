# -*- coding: utf-8 -*-
"""账号删除/残缺 Cookie/全局风控/人工验证截图的回归测试。"""

import asyncio
import os
import shutil
import tempfile
import unittest
from unittest import mock

from app.cookie_manager import (
    CookieManager,
    missing_required_cookie_fields,
)
from app.db_manager import DBManager
from utils import risk_control
from utils.captcha_remote_control import CaptchaRemoteController


FULL_COOKIE = (
    '_m_h5_tk=abc; _m_h5_tk_enc=def; cookie2=c2; sgcookie=sg; '
    'unb=123; x5sec=pass'
)
PARTIAL_COOKIE = 'cookie2=c2; x5secdata=challenge-only'


class CookiePersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db = DBManager(os.path.join(self.temp_dir, 'xianyu.db'))
        self.addCleanup(self.db.close)
        self.addCleanup(shutil.rmtree, self.temp_dir, True)

    def test_runtime_update_cannot_recreate_deleted_account(self):
        self.assertTrue(self.db.update_cookie_account_info(
            'acc', FULL_COOKIE, user_id=1, allow_create=True,
        ))
        self.assertTrue(self.db.delete_cookie('acc'))
        self.assertIsNone(self.db.get_cookie('acc'))

        success = self.db.update_cookie_account_info(
            'acc', PARTIAL_COOKIE, user_id=1,
            allow_create=False, allow_cookie_regression=False,
        )
        self.assertFalse(success)
        self.assertIsNone(self.db.get_cookie('acc'))

    def test_partial_cookie_cannot_replace_complete_login(self):
        self.db.update_cookie_account_info(
            'acc', FULL_COOKIE, user_id=1, allow_create=True,
        )
        success = self.db.update_cookie_account_info(
            'acc', PARTIAL_COOKIE,
            allow_create=False, allow_cookie_regression=False,
        )
        self.assertFalse(success)
        self.assertIn('_m_h5_tk', self.db.get_cookie('acc'))

    def test_delete_removes_stale_enabled_status(self):
        self.db.update_cookie_account_info(
            'acc', FULL_COOKIE, user_id=1, allow_create=True,
        )
        self.db.save_cookie_status('acc', True)
        self.assertTrue(self.db.delete_cookie('acc'))
        with self.db.lock:
            row = self.db.conn.execute(
                'SELECT 1 FROM cookie_status WHERE cookie_id=?', ('acc',)
            ).fetchone()
        self.assertIsNone(row)


class CookieManagerValidationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db = DBManager(os.path.join(self.temp_dir, 'manager.db'))
        self.addCleanup(self.db.close)
        self.addCleanup(shutil.rmtree, self.temp_dir, True)

    async def test_partial_cookie_is_need_relogin_and_never_starts(self):
        self.db.update_cookie_account_info(
            'broken', PARTIAL_COOKIE, user_id=1, allow_create=True,
        )
        with mock.patch('app.cookie_manager.db_manager', self.db):
            manager = CookieManager(asyncio.new_event_loop())
            manager.loop.close()
            self.assertEqual(missing_required_cookie_fields(PARTIAL_COOKIE), ('_m_h5_tk',))
            self.assertEqual(manager.get_task_state('broken'), 'need_relogin')
            task = await manager._ensure_cookie_task_async('broken')
            self.assertIsNone(task)
            self.assertEqual(manager.get_task_state('broken'), 'need_relogin')


class GlobalRiskControlTests(unittest.TestCase):
    def tearDown(self):
        risk_control.global_guard.reset('test')

    def test_any_account_trip_blocks_shared_ip(self):
        risk_control.global_guard.reset('test')
        self.assertFalse(risk_control.is_globally_blocked())
        risk_control.registry.get('account-a').trip('RGV587_ERROR::SM::busy')
        self.assertTrue(risk_control.is_globally_blocked())
        self.assertGreaterEqual(risk_control.global_remaining_seconds(), 599)
        # 账号 B 自己从未命中，但共享同一出口 IP，也必须被全局熔断拦住。
        self.assertFalse(risk_control.registry.get('account-b').is_blocked)
        self.assertTrue(risk_control.is_globally_blocked())

    def test_global_reset_restores_all_accounts(self):
        risk_control.global_guard.reset('test')
        risk_control.registry.get('account-a').trip('FAIL_SYS_USER_VALIDATE')
        risk_control.global_guard.reset('account-a')
        self.assertFalse(risk_control.is_globally_blocked())
        self.assertEqual(risk_control.global_remaining_seconds(), 0)


class FakeCaptchaElement:
    def __init__(self, box):
        self.box = box

    async def bounding_box(self):
        return self.box


class FakeCaptchaPage:
    viewport_size = {'width': 1280, 'height': 800}
    frames = ()

    def __init__(self):
        self.screenshot_clip = None

    async def query_selector(self, selector):
        if selector == '#nocaptcha':
            return FakeCaptchaElement({'x': 0, 'y': 475, 'width': 1280, 'height': 34})
        return None

    async def screenshot(self, **kwargs):
        self.screenshot_clip = kwargs.get('clip')
        return b'jpeg-bytes'


class CaptchaRemoteCropTests(unittest.IsolatedAsyncioTestCase):
    async def test_alipay_thin_slider_bar_is_recognized_and_clipped(self):
        controller = CaptchaRemoteController()
        page = FakeCaptchaPage()
        info = await controller._get_captcha_info(page)
        self.assertIsNotNone(info)
        self.assertEqual(info['selector'], '#nocaptcha')

        await controller._screenshot_captcha_area(page, info)
        clip = page.screenshot_clip
        self.assertEqual(clip['x'], 0)
        self.assertEqual(clip['width'], 1280)  # 不能越界变成 1300
        self.assertLessEqual(clip['height'], 800)


if __name__ == '__main__':
    unittest.main()
