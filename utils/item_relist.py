"""通过浏览器重新上架已下架商品。

项目里没有上架/重新上架的 mtop 接口，只能操作卖家商品管理页。页面 URL 和
DOM 选择器会随平台改版变化，所以不在代码里写死：

1. 先运行 ``scripts/probe_relist_page.py``，用真实登录态打开卖家页；
2. 探测脚本会把确认过的配置写入项目根目录的 ``relist_config.json``；
3. 本模块读取该配置执行点击。

配置项：
- ``seller_url``：卖家商品管理页（建议直接指向「已售出/已下架」筛选视图）
- ``item_selector``：商品卡片选择器，卡片内需要能匹配到 item_id
- ``button_text``：重新上架按钮文本，默认「重新上架」
- ``success_text``：点击后的成功提示文本，默认「上架成功」

安全策略：配置缺失或页面找不到目标卡片时直接返回失败，绝不猜测按钮，
避免在卖家页面上误点其他操作。
"""

import asyncio
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

from loguru import logger

try:
    from playwright.async_api import async_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:  # pragma: no cover - 运行环境缺依赖时给出明确原因
    async_playwright = None
    PLAYWRIGHT_AVAILABLE = False

from utils import browser_limit

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "relist_config.json"

DEFAULT_BUTTON_TEXT = "重新上架"
DEFAULT_SUCCESS_TEXT = "上架成功"
CHALLENGE_TEXTS = ("安全验证", "滑动验证", "请完成验证", "人机验证")


def load_relist_config() -> Dict[str, str]:
    """读取探测脚本保存的配置，环境变量优先。"""
    config: Dict[str, str] = {}
    try:
        if CONFIG_PATH.exists():
            with open(CONFIG_PATH, "r", encoding="utf-8") as file:
                data = json.load(file)
            if isinstance(data, dict):
                config = {str(key): str(value) for key, value in data.items() if value}
    except Exception as error:
        logger.warning(f"读取 relist_config.json 失败: {error}")

    for key, env_name in (
        ("seller_url", "XIANYU_RELIST_URL"),
        ("item_selector", "XIANYU_RELIST_ITEM_SELECTOR"),
        ("button_text", "XIANYU_RELIST_BUTTON_TEXT"),
        ("success_text", "XIANYU_RELIST_SUCCESS_TEXT"),
    ):
        env_value = os.getenv(env_name)
        if env_value:
            config[key] = env_value

    config.setdefault("button_text", DEFAULT_BUTTON_TEXT)
    config.setdefault("success_text", DEFAULT_SUCCESS_TEXT)
    return config


def save_relist_config(config: Dict[str, str]) -> bool:
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as file:
            json.dump(config, file, ensure_ascii=False, indent=2)
        return True
    except Exception as error:
        logger.error(f"保存 relist_config.json 失败: {error}")
        return False


def _parse_cookies(cookies_str: str) -> list:
    cookies = []
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


async def relist_item(cookie_id: str, cookies_str: str, item_id: str,
                      headless: bool = True, timeout_ms: int = 30000) -> Dict[str, Any]:
    """重新上架单个商品。

    返回 ``{"success": bool, "reason"/"message": str}``。
    配置缺失、页面找不到商品、需要人工验证都会以失败返回。
    """
    config = load_relist_config()
    seller_url = config.get("seller_url", "").strip()
    item_selector = config.get("item_selector", "").strip()
    button_text = config.get("button_text", DEFAULT_BUTTON_TEXT)
    success_text = config.get("success_text", DEFAULT_SUCCESS_TEXT)

    if not seller_url or not item_selector:
        return {
            "success": False,
            "reason": (
                "未配置卖家商品页地址或商品卡片选择器。"
                "请先运行 scripts/probe_relist_page.py 生成 relist_config.json"
            ),
        }
    if not PLAYWRIGHT_AVAILABLE:
        return {"success": False, "reason": "Playwright 未安装，无法执行页面操作"}
    if not cookies_str:
        return {"success": False, "reason": "账号没有可用 Cookie"}

    slot_held = False
    playwright = None
    context = None
    try:
        await asyncio.to_thread(browser_limit.acquire_slot, "自动重新上架")
        slot_held = True

        playwright = await async_playwright().start()
        user_data_dir = os.path.join(tempfile.gettempdir(), f"xianyu_relist_{cookie_id}")
        os.makedirs(user_data_dir, exist_ok=True)
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir,
            headless=headless,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--no-first-run",
                "--lang=zh-CN",
                "--accept-lang=zh-CN,zh,en-US,en",
            ],
            viewport={"width": 1440, "height": 900},
            locale="zh-CN",
        )
        await context.add_cookies(_parse_cookies(cookies_str))
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto(seller_url, wait_until="domcontentloaded", timeout=timeout_ms)
        await page.wait_for_timeout(2000)

        body_text = await page.inner_text("body")
        if any(text in body_text for text in CHALLENGE_TEXTS) and button_text not in body_text:
            return {"success": False, "reason": "卖家页面要求人机验证，请先在账号页完成人工验证"}

        card = page.locator(f'{item_selector}[data-item-id="{item_id}"]').first
        if await card.count() == 0:
            card = page.locator(item_selector).filter(has_text=str(item_id)).first
        if await card.count() == 0:
            return {
                "success": False,
                "reason": f"页面中未找到商品 {item_id}，请检查选择器或商品是否在已下架列表",
            }

        button = card.get_by_role("button", name=button_text).first
        if await button.count() == 0:
            button = card.get_by_text(button_text, exact=False).first
        if await button.count() == 0:
            return {"success": False, "reason": f"商品卡片中没有找到「{button_text}」按钮"}

        await button.click(timeout=timeout_ms)
        await page.wait_for_timeout(2500)

        body_after = await page.inner_text("body")
        if success_text and success_text in body_after:
            return {"success": True, "message": success_text}

        return {
            "success": False,
            "reason": "点击后未检测到成功提示，请在闲鱼确认商品状态后再决定是否重试",
        }
    except Exception as error:
        logger.error(f"重新上架商品 {item_id} 失败: {error}")
        return {"success": False, "reason": str(error)}
    finally:
        try:
            if context:
                await context.close()
        except Exception:
            pass
        try:
            if playwright:
                await playwright.stop()
        except Exception:
            pass
        if slot_held:
            browser_limit.release_slot("自动重新上架")
