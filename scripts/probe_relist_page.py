"""探测闲鱼卖家商品管理页，生成 relist_config.json。

背景：项目里没有上架/重新上架的 mtop 接口，自动上架只能操作卖家页面。
页面 URL 和选择器会随平台改版变化，所以先用这个脚本实测一次。

用法（在项目根目录执行，必须有桌面环境，浏览器会弹出来）：

    .\.venv\Scripts\python.exe scripts\probe_relist_page.py --cookie-id <账号ID>

也可以直接传 Cookie：

    .\.venv\Scripts\python.exe scripts\probe_relist_page.py --cookie "unb=...; _m_h5_tk=..." --url "https://seller.goofish.com/"

流程：
1. 用账号 Cookie 打开卖家页（有头模式，优先系统 Chrome）；
2. 在浏览器里完成登录，并切换到「已售出 / 已下架」列表；
3. 回到终端按 Enter，脚本会导出页面 HTML 和含「上架/售出/下架」的按钮；
4. 输入商品卡片选择器和按钮文本，确认后写入 relist_config.json。
"""

import argparse
import json
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
from utils.item_relist import save_relist_config


def parse_cookies(cookies_str):
    cookies = []
    for pair in (cookies_str or "").split(";"):
        pair = pair.strip()
        if "=" not in pair:
            continue
        name, value = pair.split("=", 1)
        name = name.strip()
        if name:
            cookies.append({
                "name": name,
                "value": value.strip(),
                "domain": ".goofish.com",
                "path": "/",
            })
    return cookies


def main() -> int:
    parser = argparse.ArgumentParser(description="探测卖家商品页并生成自动上架配置")
    parser.add_argument("--cookie-id", help="账号 ID（cookies 表主键）")
    parser.add_argument("--cookie", help="直接传入 Cookie 字符串")
    parser.add_argument("--url", default="https://seller.goofish.com/", help="卖家页地址")
    parser.add_argument(
        "--output",
        default=str(PROJECT_ROOT / "relist_probe.html"),
        help="页面 HTML 导出路径",
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
    print(f"打开地址: {args.url}")
    print("浏览器打开后，请在页面里完成登录，并切换到「已售出 / 已下架」列表。")
    print("确认商品列表已经显示出来后，回到这个终端按 Enter 继续。")

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(
                headless=False, channel="chrome", args=["--lang=zh-CN"]
            )
        except Exception:
            browser = p.chromium.launch(headless=False, args=["--lang=zh-CN"])

        context = browser.new_context(
            locale="zh-CN", viewport={"width": 1440, "height": 900}
        )
        context.add_cookies(parse_cookies(cookies_str))
        page = context.new_page()
        page.goto(args.url, wait_until="domcontentloaded")

        input()

        print(f"\n当前 URL: {page.url}")
        html = page.content()
        Path(args.output).write_text(html, encoding="utf-8")
        print(f"页面 HTML 已保存: {args.output}")

        print("\n含「上架 / 售出 / 下架 / 重新」的可点击元素：")
        elements = page.locator("button, [role=button], a").all()
        for element in elements:
            try:
                text = (element.inner_text() or "").strip()
            except Exception:
                continue
            if not text or not any(key in text for key in ("上架", "售出", "下架", "重新")):
                continue
            aria = element.get_attribute("aria-label") or ""
            class_name = (element.get_attribute("class") or "")[:80]
            print(f"  text={text!r} aria={aria!r} class={class_name!r}")

        print("\n请用浏览器开发者工具（F12）选中一个商品卡片，")
        print("找到能包住商品 ID、并且每个商品一个的 CSS 选择器。")
        selector = input("商品卡片选择器（例如 .item-card）：").strip()
        if not selector:
            print("选择器不能为空，已取消")
            browser.close()
            return 1

        button_text = input("重新上架按钮文本（回车默认「重新上架」）：").strip() or "重新上架"
        success_text = input("成功提示文本（回车默认「上架成功」）：").strip() or "上架成功"

        config = {
            "seller_url": page.url,
            "item_selector": selector,
            "button_text": button_text,
            "success_text": success_text,
        }
        print("\n将保存以下配置：")
        print(json.dumps(config, ensure_ascii=False, indent=2))
        if input("确认保存？(y/N) ").strip().lower() == "y":
            if save_relist_config(config):
                print("已保存到 relist_config.json")
            else:
                print("保存失败，请检查目录权限")
        else:
            print("未保存")

        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
