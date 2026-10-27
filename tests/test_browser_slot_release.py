"""浏览器槽位与扫码会话的释放。

browser_limit 的槽位全局只有一个，漏掉一次之后所有浏览器任务（滑块、扫码、
商品抓取、Cookie 刷新）都要卡满 300 秒超时才动得了。扫码会话里存着完整登录
Cookie，同样不能只靠"用户恰好还在轮询"来回收。
"""

import asyncio
import unittest
from unittest.mock import patch

from utils import browser_limit
from utils.qr_login import QRLoginManager, QRLoginSession
from utils.xianyu_slider_stealth import XianyuSliderStealth


class SliderSlotReleaseTests(unittest.TestCase):
    """滑块实例初始化失败时必须归还槽位。"""

    def _make_instance(self):
        instance = XianyuSliderStealth.__new__(XianyuSliderStealth)
        instance.user_id = "acc"
        instance.pure_user_id = "acc"
        instance._browser_slot_held = True
        instance.page = None
        instance.context = None
        instance.browser = None
        instance.playwright = None
        return instance

    def test_init_failure_cleanup_returns_the_slot(self):
        instance = self._make_instance()

        with patch.object(browser_limit, "release_slot") as release:
            instance._cleanup_on_init_failure()

        # 浏览器启动失败时只清进程不还槽位，槽位就永久漏掉
        release.assert_called_once()
        self.assertFalse(instance._browser_slot_held)

    def test_cleanup_is_idempotent(self):
        instance = self._make_instance()

        with patch.object(browser_limit, "release_slot") as release:
            instance._cleanup_on_init_failure()
            instance._cleanup_on_init_failure()

        # 重复清理不能把别人的槽位也还掉
        self.assertEqual(release.call_count, 1)

    def test_cleanup_without_slot_releases_nothing(self):
        instance = self._make_instance()
        instance._browser_slot_held = False

        with patch.object(browser_limit, "release_slot") as release:
            instance._cleanup_on_init_failure()

        release.assert_not_called()


class PasswordLoginSlotReleaseTests(unittest.TestCase):
    """浏览器启动失败时，密码登录同样不能漏槽位。"""

    def test_slot_is_released_when_browser_fails_to_start(self):
        instance = XianyuSliderStealth.__new__(XianyuSliderStealth)
        instance.user_id = "acc"
        instance.pure_user_id = "acc"

        # 占槽在浏览器启动之前，而正常释放它的 finally 属于更靠后的内层 try：
        # 启动本身失败时会跳过那个 finally
        with (
            patch.object(browser_limit, "acquire_slot") as acquire,
            patch.object(browser_limit, "release_slot") as release,
            patch(
                "utils.xianyu_slider_stealth.sync_playwright",
                side_effect=RuntimeError("Executable doesn't exist"),
            ),
        ):
            result = instance.login_with_password_playwright("user", "pass")

        self.assertIsNone(result)
        self.assertEqual(acquire.call_count, release.call_count, "占用与归还次数不一致")
        release.assert_called_once()


class QrSessionReleaseTests(unittest.IsolatedAsyncioTestCase):
    """监控结束后会话要自清，不能等前端来触发。"""

    async def test_session_is_discarded_after_monitor_finishes(self):
        manager = QRLoginManager()
        manager.sessions["s1"] = QRLoginSession("s1")

        await manager._discard_session_later("s1", delay=0)
        await asyncio.sleep(0)

        # 会话里存着完整登录 Cookie，用户关掉弹窗后不能一直留在内存
        self.assertNotIn("s1", manager.sessions)

    async def test_discarding_a_missing_session_is_safe(self):
        manager = QRLoginManager()

        await manager._discard_session_later("nope", delay=0)

        self.assertEqual(manager.sessions, {})

    async def test_session_survives_long_enough_for_frontend_to_read_result(self):
        manager = QRLoginManager()
        session = QRLoginSession("s2")
        session.status = "success"
        manager.sessions["s2"] = session

        task = asyncio.create_task(manager._discard_session_later("s2", delay=5))
        await asyncio.sleep(0)

        # 删除前要留窗口期，否则前端最后一次轮询会拿到 not_found
        self.assertIn("s2", manager.sessions)
        task.cancel()


class SlotCancellationTests(unittest.IsolatedAsyncioTestCase):
    """取消等待方不能把槽位永久吞掉。

    旧实现用 ``asyncio.to_thread(semaphore.acquire)``，线程不会被取消：
    外层 ``wait_for`` 超时后线程仍会抢到槽位，而调用方已经离开，槽位再没人
    归还。之后所有浏览器任务都会等满 300 秒，扫码增强失败后回滚成过期
    Cookie，账号就表现为“扫码成功但不运行”。
    """

    def setUp(self):
        self._saved_semaphore = browser_limit._semaphore
        self._saved_limit = browser_limit._limit
        browser_limit._semaphore = None
        browser_limit._limit = 1

    def tearDown(self):
        browser_limit._semaphore = self._saved_semaphore
        browser_limit._limit = self._saved_limit

    async def test_cancelled_waiter_returns_the_slot_it_acquires(self):
        semaphore = browser_limit._get_semaphore()
        self.assertTrue(semaphore.acquire(False), "测试前置：应能占到唯一槽位")

        waiter = asyncio.create_task(
            browser_limit.acquire_slot_async("取消测试", timeout=5)
        )
        await asyncio.sleep(0.1)
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter

        # 把槽位放回：被取消的等待线程仍可能抢到它，但必须立刻归还
        semaphore.release()
        await asyncio.sleep(0.5)

        self.assertTrue(
            semaphore.acquire(False),
            "被取消的等待方吞掉了槽位，后续浏览器任务会全部排队超时",
        )
        semaphore.release()

    async def test_timeout_does_not_consume_the_slot(self):
        semaphore = browser_limit._get_semaphore()
        self.assertTrue(semaphore.acquire(False))

        with self.assertRaises(TimeoutError):
            await browser_limit.acquire_slot_async("超时测试", timeout=0.2)

        semaphore.release()
        await asyncio.sleep(0.3)
        self.assertTrue(semaphore.acquire(False), "超时的等待方不应占用槽位")
        semaphore.release()


class PauseAcrossLoopsTests(unittest.IsolatedAsyncioTestCase):
    """扫码重登时从 Web 事件循环暂停账号任务要真正取消。"""

    async def test_pause_cancels_task_owned_by_another_loop(self):
        import threading

        from app.cookie_manager import CookieManager

        manager = CookieManager.__new__(CookieManager)
        manager._task_locks = {}
        manager.tasks = {}
        manager.instances = {}

        owner_loop = asyncio.new_event_loop()
        thread = threading.Thread(target=owner_loop.run_forever, daemon=True)
        thread.start()
        manager.loop = owner_loop
        try:
            async def _start_task():
                async def _never():
                    await asyncio.sleep(3600)

                task = owner_loop.create_task(_never())
                manager.tasks["acc"] = task
                manager.instances["acc"] = object()
                return task

            future = asyncio.run_coroutine_threadsafe(_start_task(), owner_loop)
            task = await asyncio.wrap_future(future)

            # 调用方在另一个事件循环里（Web 线程），旧实现会抛
            # “future belongs to a different loop” 并放走旧任务
            self.assertTrue(await manager.pause_cookie_task("acc", timeout=2))
            self.assertNotIn("acc", manager.tasks)
            self.assertNotIn("acc", manager.instances)
            self.assertTrue(task.done(), "旧监听任务没有被真正取消")
        finally:
            owner_loop.call_soon_threadsafe(owner_loop.stop)
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
