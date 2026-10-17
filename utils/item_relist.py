"""通过鱼小铺卖家工作台重新上架已下架商品。

项目里没有上架/重新上架的 mtop 接口（``mtop.taobao.idle.item.polish`` 只擦亮），
下架列表里也没有「重新上架」按钮。实测可行的通道是：

    商品管理 → 下架 → 编辑
    → #/seller-item/publish?itemId=<商品ID>
    → 把库存改到 >= 1
    → 点「发布」

「发布」会复用原商品 ID 重新上架，不是新建商品，所以自动发货配置、
商品专属回复等按 item_id 绑定的数据都不会失效。2026-10-18 在鱼小铺
PC 工作台实测确认。

线程/并发：与项目其他浏览器任务共用 ``utils.browser_limit`` 的槽位，
弱机上会和扫码登录、Cookie 刷新排队，不会同时拉起多个 Chromium。
"""

import asyncio
import os
import tempfile
from typing import Any, Dict, List, Optional, Sequence

from loguru import logger

try:  # Patchright 是 Playwright 的反检测分支，优先使用
    from patchright.async_api import async_playwright
    ENGINE = "patchright"
except ImportError:  # pragma: no cover - 运行环境缺 Patchright 时退回 Playwright
    try:
        from playwright.async_api import async_playwright
        ENGINE = "playwright"
    except ImportError:  # pragma: no cover
        async_playwright = None
        ENGINE = ""

from utils import browser_limit

SELLER_EDIT_URL = (
    "https://seller.goofish.com/?site=COMMONPRO"
    "#/seller-item/publish?itemId={item_id}"
)

DEFAULT_INFINITE_STOCK = 9999
MAX_STOCK = 10000
SUCCESS_TEXT = "发布成功"

# 同一进程内串行执行上架：售出即时判定和定时循环可能同时命中不同商品，
# 而它们共用同一个 Chrome 用户目录，并发启动会互相抢 profile 锁。
# 按事件循环分别持锁，避免同一进程里多次 asyncio.run 时锁绑定到已关闭的循环。
_relist_locks: Dict[int, asyncio.Lock] = {}


def _get_relist_lock() -> asyncio.Lock:
    loop_key = id(asyncio.get_running_loop())
    lock = _relist_locks.get(loop_key)
    if lock is None:
        lock = asyncio.Lock()
        _relist_locks[loop_key] = lock
    return lock

# 登录失效、风控拦截的典型文案。命中后直接失败，不猜测页面上的按钮。
CHALLENGE_TEXTS = (
    "非法访问",
    "访问受限",
    "请完成验证",
    "安全验证",
    "滑动验证",
    "人机验证",
    "请先登录",
    "登录后查看",
)

# 发布失败时平台给出的表单错误
ERROR_TEXTS = (
    "发布失败",
    "库存必须",
    "请填写",
    "操作失败",
    "系统繁忙",
    "商品信息有误",
)

# 工作台右下角的问卷弹窗会盖住整页，导致点击被拦截；它是纯前端节点，直接删掉。
REMOVE_OVERLAYS_JS = """
() => {
  document.querySelectorAll('.ant-modal-root, .aes-survey-pop--bottom').forEach(e => e.remove());
}
"""

# 给库存输入框打标记。优先用 placeholder="1"（实测稳定），找不到时按
# 「向上 4 层内出现库存字样」兜底，避免平台改 placeholder 后彻底失效。
TAG_STOCK_INPUTS_JS = """
() => {
  document.querySelectorAll('[data-xy-relist-stock]').forEach(
    e => e.removeAttribute('data-xy-relist-stock')
  );
  const inputs = Array.from(document.querySelectorAll('input')).filter(input => {
    if (input.offsetParent === null) return false;
    return !['radio', 'checkbox', 'hidden', 'file'].includes(input.type);
  });
  let count = 0;
  for (const input of inputs) {
    let el = input;
    let matched = false;
    for (let depth = 0; depth < 4 && el; depth += 1, el = el.parentElement) {
      const text = (el.innerText || '').trim();
      if (text.includes('库存') && text.length <= 40) {
        matched = true;
        break;
      }
    }
    if (matched) {
      input.setAttribute('data-xy-relist-stock', '1');
      count += 1;
    }
  }
  return count;
}
"""


def parse_cookies(cookies_str: str) -> List[Dict[str, str]]:
    cookies: List[Dict[str, str]] = []
    for pair in (cookies_str or "").split(";"):
        pair = pair.strip()
        if "=" not in pair:
            continue
        name, value = pair.split("=", 1)
        name = name.strip()
        if not name:
            continue
        cookies.append({
            "name": name,
            "value": value.strip(),
            "domain": ".goofish.com",
            "path": "/",
        })
    return cookies


def normalize_stock(value: Any, infinite_stock: int = DEFAULT_INFINITE_STOCK) -> int:
    """把库存目标收敛到平台允许的区间。

    ``None`` 表示固定文本/图片/API 这类没有本地库存的卡，按无限库存处理。
    数字为 0 时保持 0，由调用方拒绝上架——批量卡已经发完，不能拿 1 条库存
    去重新挂单，否则会产生发不出货的订单。
    """
    if value is None:
        value = infinite_stock
    try:
        stock = int(value)
    except (TypeError, ValueError):
        stock = int(infinite_stock)
    return max(0, min(MAX_STOCK, stock))


def build_stock_targets(stock_details: Sequence[Dict[str, Any]],
                        infinite_stock: int = DEFAULT_INFINITE_STOCK,
                        data_stock: int = 1) -> List[int]:
    """把资格判断返回的规格明细转成执行器需要的库存列表。

    ``data`` 规格固定只挂 1 件：同一张卡密可能被多个商品链接共用，如果把
    卡密总余量填到每个链接上，几个链接会重复卖出同一批卡。挂 1 件、卖出一单
    后再由“售出即时判定”补 1 件，虽然多一次上架，但不会超卖。

    ``text/image/api`` 没有本地余量，统一填一个较大的库存，避免每卖一单就
    重新上架。
    """
    targets: List[int] = []
    for detail in stock_details or []:
        stock = detail.get("stock")
        if stock is None:
            targets.append(normalize_stock(None, infinite_stock))
        else:
            # 余量为 0 时保留 0，交给 relist_item 拒绝；有货时只挂 data_stock 件
            targets.append(normalize_stock(min(int(stock), max(1, data_stock)), infinite_stock))
    if not targets:
        targets.append(normalize_stock(None, infinite_stock))
    return targets


async def _remove_overlays(page) -> None:
    try:
        await page.evaluate(REMOVE_OVERLAYS_JS)
    except Exception:
        pass


async def _locate_stock_inputs(page) -> List[Any]:
    """返回可见的库存输入框列表。"""
    locator = page.locator('input[placeholder="1"]')
    inputs = []
    for index in range(await locator.count()):
        element = locator.nth(index)
        try:
            if await element.is_visible():
                inputs.append(element)
        except Exception:
            continue
    if inputs:
        return inputs

    # placeholder 被改掉时的兜底
    try:
        tagged = await page.evaluate(TAG_STOCK_INPUTS_JS)
    except Exception:
        tagged = 0
    if not tagged:
        return []
    fallback = page.locator('input[data-xy-relist-stock="1"]')
    for index in range(await fallback.count()):
        element = fallback.nth(index)
        try:
            if await element.is_visible():
                inputs.append(element)
        except Exception:
            continue
    return inputs


async def _read_page_state(page) -> Dict[str, Any]:
    try:
        body = await page.inner_text("body")
    except Exception:
        body = ""
    return {
        "url": page.url,
        "title": await page.title(),
        "body": body,
        "challenge": next((text for text in CHALLENGE_TEXTS if text in body), ""),
    }


async def _publish_error(page, body_before: str) -> str:
    """读取点「发布」之后新出现的表单错误/提示。"""
    try:
        errors = page.locator('.ant-form-item-explain-error')
        messages = []
        for index in range(await errors.count()):
            element = errors.nth(index)
            if await element.is_visible():
                text = (await element.inner_text()).strip()
                if text:
                    messages.append(text)
        if messages:
            return "；".join(dict.fromkeys(messages))
    except Exception:
        pass

    try:
        body_after = await page.inner_text("body")
    except Exception:
        return ""
    for text in ERROR_TEXTS:
        if text in body_after and text not in body_before:
            # 截取错误附近的一小段，便于定位
            position = body_after.find(text)
            return body_after[max(0, position - 20): position + 60].replace("\n", " ").strip()
    return ""


async def relist_item(
    cookie_id: str,
    cookies_str: str,
    item_id: str,
    stock_targets: Optional[Sequence[Any]] = None,
    infinite_stock: int = DEFAULT_INFINITE_STOCK,
    headless: bool = True,
    timeout_ms: int = 45000,
) -> Dict[str, Any]:
    """重新上架单个商品，返回 ``{"success": bool, ...}``。

    ``stock_targets`` 按商品规格顺序给出库存；单规格只传一个值。
    未传时按无限库存处理。库存为 0 的批量卡不应走到这里，资格判断会先拦下。
    """
    if not async_playwright:
        return {"success": False, "reason": "未安装 Patchright/Playwright，无法执行页面操作"}
    if not cookies_str:
        return {"success": False, "reason": "账号没有可用 Cookie"}
    if not item_id or not str(item_id).isdigit():
        return {"success": False, "reason": f"商品 ID 无效: {item_id!r}"}

    if stock_targets is None:
        targets = [normalize_stock(None, infinite_stock)]
    else:
        targets = [normalize_stock(value, infinite_stock) for value in stock_targets]
        if not targets:
            targets = [normalize_stock(None, infinite_stock)]
    if any(target < 1 for target in targets):
        return {
            "success": False,
            "reason": "库存目标为 0，已取消上架（批量卡密可能已经发完）",
        }

    lock = _get_relist_lock()
    await lock.acquire()
    slot_held = False
    playwright = None
    context = None
    try:
        await asyncio.to_thread(browser_limit.acquire_slot, "自动重新上架")
        slot_held = True

        playwright = await async_playwright().start()
        user_data_dir = os.path.join(
            tempfile.gettempdir(), f"xianyu_relist_{cookie_id}"
        )
        os.makedirs(user_data_dir, exist_ok=True)
        launch_options = {
            "headless": headless,
            "args": [
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--no-first-run",
                "--lang=zh-CN",
                "--accept-lang=zh-CN,zh,en-US,en",
                "--disable-blink-features=AutomationControlled",
            ],
            "viewport": {"width": 1440, "height": 900},
            "locale": "zh-CN",
        }
        try:
            context = await playwright.chromium.launch_persistent_context(
                user_data_dir, channel="chrome", **launch_options
            )
        except Exception as launch_error:
            # 服务器上不一定装了系统 Chrome；回退到 Playwright 自带 Chromium。
            # 检测风险略高，但比直接失败好，命中风控时会在页面文案上返回明确原因。
            logger.warning(f"系统 Chrome 启动失败，回退到内置 Chromium: {launch_error}")
            context = await playwright.chromium.launch_persistent_context(
                user_data_dir, **launch_options
            )
        await context.add_cookies(parse_cookies(cookies_str))
        page = context.pages[0] if context.pages else await context.new_page()

        await page.goto(
            SELLER_EDIT_URL.format(item_id=item_id),
            wait_until="domcontentloaded",
            timeout=timeout_ms,
        )
        # 表单是异步渲染的，等库存输入框出现（固定 sleep 在慢机器上不够）
        try:
            await page.wait_for_selector(
                'input[placeholder="1"]', timeout=timeout_ms
            )
        except Exception:
            pass
        await page.wait_for_timeout(1500)
        await _remove_overlays(page)

        state = await _read_page_state(page)
        if state["challenge"]:
            return {
                "success": False,
                "reason": f"卖家页面要求验证或登录（{state['challenge']}），请先在账号页人工处理",
            }
        if "商品发布" not in (state["title"] or ""):
            return {
                "success": False,
                "reason": f"未能打开商品编辑页（标题: {state['title'] or '空'}）",
            }

        stock_inputs = await _locate_stock_inputs(page)
        if not stock_inputs:
            return {
                "success": False,
                "reason": "编辑页没有找到库存输入框，平台页面可能已改版",
            }
        if len(stock_inputs) != len(targets):
            return {
                "success": False,
                "reason": (
                    f"商品规格数与库存目标数不一致（页面 {len(stock_inputs)} 个规格，"
                    f"绑定 {len(targets)} 个），为避免填错库存已放弃"
                ),
            }

        # 先把库存改到目标值；React 受控输入需要触发 input 事件，fill 会处理。
        applied: List[int] = []
        for element, target in zip(stock_inputs, targets):
            await element.scroll_into_view_if_needed()
            await element.click()
            await element.fill(str(target))
            await element.press("Tab")
            await page.wait_for_timeout(200)
            current = (await element.input_value()).strip()
            if current != str(target):
                return {
                    "success": False,
                    "reason": f"库存写入失败，期望 {target}，实际 {current or '空'}",
                }
            applied.append(target)

        await _remove_overlays(page)

        publish_button = page.locator("button.ant-btn-primary", has_text="发布")
        visible_buttons = []
        for index in range(await publish_button.count()):
            element = publish_button.nth(index)
            try:
                if await element.is_visible():
                    visible_buttons.append(element)
            except Exception:
                continue
        if not visible_buttons:
            return {"success": False, "reason": "编辑页没有找到「发布」按钮"}
        button = visible_buttons[0]

        try:
            body_before = await page.inner_text("body")
        except Exception:
            body_before = ""

        await button.scroll_into_view_if_needed()
        await button.click(timeout=timeout_ms)

        # 部分场景会先弹二次确认
        await page.wait_for_timeout(800)
        confirm = page.locator(
            ".ant-modal-confirm-btns button.ant-btn-primary, "
            ".ant-modal-footer button.ant-btn-primary"
        )
        for index in range(await confirm.count()):
            element = confirm.nth(index)
            try:
                if await element.is_visible():
                    await element.click()
                    break
            except Exception:
                continue

        deadline = asyncio.get_event_loop().time() + max(10, timeout_ms / 1000 / 3)
        last_error = ""
        while asyncio.get_event_loop().time() < deadline:
            await page.wait_for_timeout(700)
            try:
                body_after = await page.inner_text("body")
            except Exception:
                continue
            if SUCCESS_TEXT in body_after:
                return {
                    "success": True,
                    "message": SUCCESS_TEXT,
                    "stocks": applied,
                    "item_id": str(item_id),
                }
            error = await _publish_error(page, body_before)
            if error:
                last_error = error
                break

        if not last_error:
            last_error = "点击发布后未检测到「发布成功」，请到卖家工作台确认商品状态"
        return {"success": False, "reason": last_error, "stocks": applied}

    except asyncio.CancelledError:
        raise
    except Exception as error:
        logger.error(f"重新上架商品 {item_id} 失败: {error}")
        return {"success": False, "reason": str(error)}
    finally:
        # 取消/异常时也要把浏览器和槽位收干净，否则后续上架会一直排队
        try:
            if context:
                await context.close()
        except BaseException:
            pass
        try:
            if playwright:
                await playwright.stop()
        except BaseException:
            pass
        if slot_held:
            browser_limit.release_slot("自动重新上架")
        lock.release()
