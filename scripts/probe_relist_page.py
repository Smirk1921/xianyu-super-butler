"""检查鱼小铺「编辑 → 发布」重新上架通道是否可用。

自动上架依赖卖家工作台的编辑页：``#/seller-item/publish?itemId=<商品ID>``。
平台改版后，库存输入框或「发布」按钮的选择器可能变化，这个脚本用来快速确认
页面结构，不做任何写操作（不会点发布）。

用法（在项目根目录执行）：

    .\.venv\Scripts\python.exe scripts\probe_relist_page.py --cookie-id <账号ID> --item-id <商品ID>

也可以直接传 Cookie：

    .\.venv\Scripts\python.exe scripts\probe_relist_page.py --cookie "unb=...; _m_h5_tk=..." --item-id 1093294412397

输出：页面标题、库存输入框数量/当前值、「发布」按钮是否可见、是否命中登录/风控文案。
"""

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    from patchright.sync_api import sync_playwright
    ENGINE = "patchright"
except ImportError:
    from playwright.sync_api import sync_playwright
    ENGINE = "playwright"

from app.db_manager import DBManager
from utils.item_relist import CHALLENGE_TEXTS, SELLER_EDIT_URL, parse_cookies


def main() -> int:
    parser = argparse.ArgumentParser(description="检查商品编辑页的重新上架通道")
    parser.add_argument("--cookie-id", help="账号 ID（cookies 表主键）")
    parser.add_argument("--cookie", help="直接传入 Cookie 字符串")
    parser.add_argument("--item-id", required=True, help="要检查的商品 ID")
    parser.add_argument("--headed", action="store_true", help="显示浏览器窗口")
    parser.add_argument(
        "--output",
        default=str(PROJECT_ROOT / "relist_probe.html"),
        help="页面 HTML 导出路径（排查选择器用）",
    )
    args = parser.parse_args()

    cookies_str = args.cookie
    if not cookies_str:
        if not args.cookie_id:
            parser.error("必须提供 --cookie-id 或 --cookie")
        db = DBManager()
        try:
            details = db.get_cookie_details(args.cookie_id)
        finally:
            db.close()
        if not details:
            print(f"找不到账号 {args.cookie_id}")
            return 1
        cookies_str = details.get("value") or ""

    if not cookies_str:
        print("账号没有 Cookie，请先在后台完成扫码登录")
        return 1

    print(f"浏览器引擎: {ENGINE}")
    print(f"商品 ID: {args.item_id}")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=not args.headed,
            channel="chrome",
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            locale="zh-CN", viewport={"width": 1440, "height": 900}
        )
        context.add_cookies(parse_cookies(cookies_str))
        page = context.new_page()
        page.goto(
            SELLER_EDIT_URL.format(item_id=args.item_id),
            wait_until="domcontentloaded",
            timeout=45000,
        )
        page.wait_for_timeout(7000)
        page.evaluate(
            "() => document.querySelectorAll('.ant-modal-root, .aes-survey-pop--bottom')"
            ".forEach(e => e.remove())"
        )

        body = page.inner_text("body")
        print(f"当前 URL: {page.url}")
        print(f"页面标题: {page.title()}")

        stock_inputs = page.locator('input[placeholder="1"]')
        visible_stocks = []
        for index in range(stock_inputs.count()):
            element = stock_inputs.nth(index)
            try:
                if element.is_visible():
                    visible_stocks.append(element.input_value())
            except Exception:
                continue
        print(f"库存输入框: {len(visible_stocks)} 个，当前值={visible_stocks}")

        publish = page.locator("button.ant-btn-primary", has_text="发布")
        visible_publish = 0
        for index in range(publish.count()):
            try:
                if publish.nth(index).is_visible():
                    visible_publish += 1
            except Exception:
                continue
        print(f"「发布」按钮: {visible_publish} 个可见")
        print("编辑器: 有输入框" if page.locator("textarea").count() else "编辑器: 无 textarea")

        challenge = next((text for text in CHALLENGE_TEXTS if text in body), "")
        print(f"登录/风控提示: {challenge or '无'}")

        Path(args.output).write_text(page.content(), encoding="utf-8")
        print(f"页面 HTML 已保存: {args.output}")
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
