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

登录态说明：
- 主站 WebSocket/mtop 的 ``_m_h5_tk`` 会自动续期，但卖家工作台
  ``seller.goofish.com`` 是独立的 PC 登录态，会单独过期。
- 上架前先调用卖家端 ``sold.get`` 接口刷新 Set-Cookie（``_m_h5_tk``、
  ``cookie2``、``sgcookie``、``_tb_token_``），再打开编辑页；如果仍落到
  登录页，重试一次并返回 ``login_required``，由调度层暂停该账号并通知。
- 浏览器里刷新出来的 Cookie 会随结果返回，调用方负责写回内存和数据库。

线程/并发：与项目其他浏览器任务共用 ``utils.browser_limit`` 的槽位，
弱机上会和扫码登录、Cookie 刷新排队，不会同时拉起多个 Chromium。
"""

import asyncio
import os
import shutil
import tempfile
from pathlib import Path
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

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PROFILE_ROOT = PROJECT_ROOT / "data" / "relist_profiles"

SELLER_EDIT_URL = (
    "https://seller.goofish.com/?site=COMMONPRO"
    "#/seller-item/publish?itemId={item_id}"
)
SELLER_MANAGE_URL = (
    "https://seller.goofish.com/?site=COMMONPRO#/seller-item/goods-manage"
)

DEFAULT_INFINITE_STOCK = 9999
MAX_STOCK = 10000
SUCCESS_TEXT = "发布成功"

# 同一进程内串行执行上架/会话检查：两者共用同一个 Chrome 用户目录，
# 并发启动会互相抢 profile 锁。按事件循环分别持锁，避免同一进程里
# 多次 asyncio.run 时锁绑定到已关闭的循环。
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


# ----------------------------------------------------------------------
# Cookie 工具
# ----------------------------------------------------------------------
def parse_cookie_string(cookies_str: str) -> Dict[str, str]:
    cookies: Dict[str, str] = {}
    for pair in (cookies_str or "").split(";"):
        pair = pair.strip()
        if "=" not in pair:
            continue
        name, value = pair.split("=", 1)
        name = name.strip()
        if name:
            cookies[name] = value.strip()
    return cookies


def parse_cookies(cookies_str: str) -> List[Dict[str, str]]:
    """转成 Playwright 的 Cookie 列表。

    卖家工作台与主站同在 .goofish.com 下，实测把主站 Cookie 注入到该域
    即可打开编辑页；浏览器导出时会同时保留 .taobao.com 的 Cookie 供合并。
    """
    return [
        {"name": name, "value": value, "domain": ".goofish.com", "path": "/"}
        for name, value in parse_cookie_string(cookies_str).items()
    ]


def merge_cookie_strings(*cookie_strings: str) -> str:
    """按传入顺序合并 Cookie，后面的值覆盖前面的同名项。"""
    merged: Dict[str, str] = {}
    for cookies_str in cookie_strings:
        for name, value in parse_cookie_string(cookies_str or "").items():
            merged[name] = value
    return "; ".join(f"{name}={value}" for name, value in merged.items())


def browser_cookies_to_string(cookies: Sequence[Dict[str, Any]]) -> str:
    """从浏览器上下文导出 goofish/taobao 域 Cookie，忽略第三方统计 Cookie。"""
    picked: Dict[str, str] = {}
    # 让 .goofish.com 的值最后写入，优先于 .taobao.com 的同名 Cookie
    ordered = sorted(
        cookies or [],
        key=lambda cookie: 0 if "taobao.com" in str(cookie.get("domain") or "") else 1,
    )
    for cookie in ordered:
        domain = str(cookie.get("domain") or "")
        if "goofish.com" not in domain and "taobao.com" not in domain:
            continue
        name = str(cookie.get("name") or "").strip()
        if name:
            picked[name] = str(cookie.get("value") or "").strip()
    return "; ".join(f"{name}={value}" for name, value in picked.items())


def prepare_profile_dir(cookie_id: str) -> str:
    """返回持久化的浏览器用户目录，并从旧的临时目录迁移一次。

    旧的 ``/tmp/xianyu_relist_<id>`` 在容器重建后会丢；放到 ``data`` 挂载卷
    下可以保留卖家 SSO 刷新出来的 Cookie。迁移只在新目录为空时做一次。
    """
    root = Path(os.getenv("XIANYU_RELIST_PROFILE_DIR") or DEFAULT_PROFILE_ROOT)
    target = root / str(cookie_id)
    target.mkdir(parents=True, exist_ok=True)

    legacy = Path(tempfile.gettempdir()) / f"xianyu_relist_{cookie_id}"
    if legacy.is_dir() and not any(target.iterdir()):
        try:
            shutil.copytree(legacy, target, dirs_exist_ok=True)
            logger.info(f"【{cookie_id}】已把旧的卖家页浏览器目录迁移到 {target}")
        except Exception as error:
            logger.debug(f"【{cookie_id}】迁移旧浏览器目录失败（忽略）: {error}")
    return str(target)


# ----------------------------------------------------------------------
# 卖家登录态预刷新 / 健康检查
# ----------------------------------------------------------------------
async def refresh_seller_cookies(cookie_id: str, cookies_str: str) -> Dict[str, Any]:
    """调用卖家端接口刷新 Token，返回合并后的 Cookie。

    ``sold.get`` 走主站签名，响应会下发新的 ``_m_h5_tk`` 等 Cookie；即使
    卖家工作台网页登录态已经悬空，这一步也常常能把令牌续上。调用失败不
    阻断上架流程，只记录原因，让后续页面自己判断。
    """
    if str(os.getenv("XIANYU_RELIST_PREFLIGHT", "1")).strip().lower() in ("0", "false", "no"):
        return {"ok": False, "skipped": True, "cookies_str": cookies_str}

    try:
        from utils.xianyu_seller_api import XianyuSellerAPI
    except Exception as error:  # pragma: no cover - 依赖缺失时保持旧行为
        return {"ok": False, "reason": f"卖家接口不可用: {error}", "cookies_str": cookies_str}

    try:
        async with XianyuSellerAPI(cookie_id, cookies_str) as api:
            # 只取一条，目的不是订单数据，而是让服务端下发最新 Cookie
            await api.get_sold_orders(page_number=1, rows_per_page=1, query_code="ALL")
            refreshed = api.cookies_str or cookies_str
            return {
                "ok": True,
                "cookies_str": merge_cookie_strings(cookies_str, refreshed),
            }
    except Exception as error:
        return {"ok": False, "reason": str(error), "cookies_str": cookies_str}


async def _launch_context(playwright, profile_dir: str, headless: bool):
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
        return await playwright.chromium.launch_persistent_context(
            profile_dir, channel="chrome", **launch_options
        )
    except Exception as launch_error:
        # 服务器上不一定装了系统 Chrome；回退到 Patchright/Playwright 自带 Chromium。
        # 检测风险略高，但比直接失败好，命中风控时会在页面文案上返回明确原因。
        logger.warning(f"系统 Chrome 启动失败，回退到内置 Chromium: {launch_error}")
        return await playwright.chromium.launch_persistent_context(
            profile_dir, **launch_options
        )


async def _page_state(page) -> Dict[str, Any]:
    try:
        body = await page.inner_text("body")
    except Exception:
        body = ""
    title = await page.title()
    url = page.url
    lowered_url = url.lower()
    login_required = (
        "登录" in title
        or "#/login" in lowered_url
        or "/login" in lowered_url
    )
    challenge = next((text for text in CHALLENGE_TEXTS if text in body), "")
    return {
        "url": url,
        "title": title,
        "body": body,
        "login_required": login_required,
        "challenge": challenge,
    }


async def check_seller_session(
    cookie_id: str,
    cookies_str: str,
    headless: bool = True,
    timeout_ms: int = 45000,
) -> Dict[str, Any]:
    """只读检查卖家工作台登录态，供定时健康检查和手动诊断使用。

    返回 ``status``：``ok`` / ``login_required`` / ``challenge`` / ``error``。
    同时返回浏览器里刷新后的 ``cookies_str``，调用方可写回数据库。
    """
    if not async_playwright:
        return {"status": "error", "reason": "未安装 Patchright/Playwright", "cookies_str": cookies_str}
    if not cookies_str:
        return {"status": "login_required", "reason": "账号没有可用 Cookie", "cookies_str": cookies_str}

    preflight = await refresh_seller_cookies(cookie_id, cookies_str)
    working_cookies = preflight.get("cookies_str") or cookies_str

    lock = _get_relist_lock()
    await lock.acquire()
    slot_held = False
    playwright = None
    context = None
    try:
        await asyncio.to_thread(browser_limit.acquire_slot, "卖家登录态检查")
        slot_held = True
        profile_dir = await asyncio.to_thread(prepare_profile_dir, cookie_id)

        playwright = await async_playwright().start()
        context = await _launch_context(playwright, profile_dir, headless)
        await context.add_cookies(parse_cookies(working_cookies))
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto(SELLER_MANAGE_URL, wait_until="domcontentloaded", timeout=timeout_ms)
        await page.wait_for_timeout(8000)
        try:
            await page.evaluate(REMOVE_OVERLAYS_JS)
        except Exception:
            pass

        state = await _page_state(page)
        try:
            exported = browser_cookies_to_string(await context.cookies())
            working_cookies = merge_cookie_strings(working_cookies, exported)
        except Exception:
            pass

        if state["login_required"]:
            return {
                "status": "login_required",
                "reason": f"卖家工作台要求登录（{state['title'] or state['url']}）",
                "cookies_str": working_cookies,
            }
        if state["challenge"]:
            return {
                "status": "challenge",
                "reason": f"卖家页面要求验证（{state['challenge']}）",
                "cookies_str": working_cookies,
            }
        if "商品管理" not in (state["title"] or "") and "卖家工作台" not in (state["title"] or ""):
            return {
                "status": "error",
                "reason": f"未能打开卖家工作台（标题: {state['title'] or '空'}）",
                "cookies_str": working_cookies,
            }
        return {
            "status": "ok",
            "reason": "",
            "cookies_str": working_cookies,
            # 不要把 Cookie 再塞进诊断信息，避免接口/日志泄漏
            "preflight": {
                key: value for key, value in preflight.items() if key != "cookies_str"
            },
        }
    except asyncio.CancelledError:
        raise
    except Exception as error:
        logger.error(f"【{cookie_id}】检查卖家工作台登录态失败: {error}")
        return {"status": "error", "reason": str(error), "cookies_str": working_cookies}
    finally:
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
            browser_limit.release_slot("卖家登录态检查")
        lock.release()


# ----------------------------------------------------------------------
# 库存换算
# ----------------------------------------------------------------------
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


# ----------------------------------------------------------------------
# 页面操作
# ----------------------------------------------------------------------
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

    结果里的 ``cookies_str`` 是预刷新 + 浏览器刷新后的 Cookie，调用方应写回。
    ``login_required=True`` 表示卖家工作台登录态失效，需要暂停自动上架并通知。
    """
    if not async_playwright:
        return {"success": False, "reason": "未安装 Patchright/Playwright，无法执行页面操作",
                "cookies_str": cookies_str}
    if not cookies_str:
        return {"success": False, "reason": "账号没有可用 Cookie", "cookies_str": cookies_str}
    if not item_id or not str(item_id).isdigit():
        return {"success": False, "reason": f"商品 ID 无效: {item_id!r}", "cookies_str": cookies_str}

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
            "cookies_str": cookies_str,
        }

    # 先刷新一次卖家端 Token；失败也继续，让页面自己判断是否需要重新登录
    preflight = await refresh_seller_cookies(cookie_id, cookies_str)
    working_cookies = preflight.get("cookies_str") or cookies_str
    if not preflight.get("ok") and not preflight.get("skipped"):
        logger.debug(f"【{cookie_id}】上架前 Token 预刷新失败（继续打开页面）: {preflight.get('reason')}")

    lock = _get_relist_lock()
    await lock.acquire()
    slot_held = False
    playwright = None
    context = None
    try:
        await asyncio.to_thread(browser_limit.acquire_slot, "自动重新上架")
        slot_held = True
        profile_dir = await asyncio.to_thread(prepare_profile_dir, cookie_id)

        playwright = await async_playwright().start()
        context = await _launch_context(playwright, profile_dir, headless)
        await context.add_cookies(parse_cookies(working_cookies))
        page = context.pages[0] if context.pages else await context.new_page()

        state: Dict[str, Any] = {}
        for attempt in range(2):
            await page.goto(
                SELLER_EDIT_URL.format(item_id=item_id),
                wait_until="domcontentloaded",
                timeout=timeout_ms,
            )
            # 表单是异步渲染的，等库存输入框出现（固定 sleep 在慢机器上不够）
            try:
                await page.wait_for_selector('input[placeholder="1"]', timeout=timeout_ms)
            except Exception:
                pass
            await page.wait_for_timeout(1500)
            await _remove_overlays(page)
            state = await _page_state(page)

            # 每次页面加载后都把浏览器里的 Cookie 合并回来
            try:
                exported = browser_cookies_to_string(await context.cookies())
                working_cookies = merge_cookie_strings(working_cookies, exported)
            except Exception:
                pass

            if state["login_required"]:
                if attempt == 0:
                    logger.info(f"【{cookie_id}】卖家工作台落到登录页，刷新 Token 后重试一次")
                    refreshed = await refresh_seller_cookies(cookie_id, working_cookies)
                    working_cookies = refreshed.get("cookies_str") or working_cookies
                    try:
                        await context.add_cookies(parse_cookies(working_cookies))
                    except Exception:
                        pass
                    await page.wait_for_timeout(3000)
                    continue
                return {
                    "success": False,
                    "login_required": True,
                    "reason": "卖家工作台登录已失效，请在后台重新扫码登录该账号",
                    "cookies_str": working_cookies,
                }
            if state["challenge"]:
                return {
                    "success": False,
                    "reason": f"卖家页面要求验证（{state['challenge']}），请先在账号页人工处理",
                    "cookies_str": working_cookies,
                }
            if "商品发布" not in (state["title"] or ""):
                return {
                    "success": False,
                    "reason": f"未能打开商品编辑页（标题: {state['title'] or '空'}）",
                    "cookies_str": working_cookies,
                }
            break

        stock_inputs = await _locate_stock_inputs(page)
        if not stock_inputs:
            return {
                "success": False,
                "reason": "编辑页没有找到库存输入框，平台页面可能已改版",
                "cookies_str": working_cookies,
            }
        if len(stock_inputs) != len(targets):
            return {
                "success": False,
                "reason": (
                    f"商品规格数与库存目标数不一致（页面 {len(stock_inputs)} 个规格，"
                    f"绑定 {len(targets)} 个），为避免填错库存已放弃"
                ),
                "cookies_str": working_cookies,
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
                    "cookies_str": working_cookies,
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
            return {"success": False, "reason": "编辑页没有找到「发布」按钮",
                    "cookies_str": working_cookies}
        button = visible_buttons[0]

        try:
            body_before = await page.inner_text("body")
        except Exception:
            body_before = ""

        await button.scroll_into_view_if_needed()
        await button.click(timeout=timeout_ms)

        # 只在真正的二次确认弹窗里点主按钮；发布成功弹窗也有底部按钮，
        # 但不会带 ant-modal-confirm-btns，避免误点到「查看商品」跳走。
        await page.wait_for_timeout(800)
        confirm = page.locator(".ant-modal-confirm-btns button.ant-btn-primary")
        for index in range(await confirm.count()):
            element = confirm.nth(index)
            try:
                if await element.is_visible():
                    await element.click()
                    break
            except Exception:
                continue

        deadline = asyncio.get_running_loop().time() + max(10, timeout_ms / 1000 / 3)
        last_error = ""
        while asyncio.get_running_loop().time() < deadline:
            await page.wait_for_timeout(700)
            try:
                body_after = await page.inner_text("body")
            except Exception:
                continue
            if SUCCESS_TEXT in body_after:
                try:
                    exported = browser_cookies_to_string(await context.cookies())
                    working_cookies = merge_cookie_strings(working_cookies, exported)
                except Exception:
                    pass
                return {
                    "success": True,
                    "message": SUCCESS_TEXT,
                    "stocks": applied,
                    "item_id": str(item_id),
                    "cookies_str": working_cookies,
                }
            error = await _publish_error(page, body_before)
            if error:
                last_error = error
                break

        if not last_error:
            last_error = "点击发布后未检测到「发布成功」，请到卖家工作台确认商品状态"
        return {"success": False, "reason": last_error, "stocks": applied,
                "cookies_str": working_cookies}

    except asyncio.CancelledError:
        raise
    except Exception as error:
        logger.error(f"重新上架商品 {item_id} 失败: {error}")
        return {"success": False, "reason": str(error), "cookies_str": working_cookies}
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
