"""统一的浏览器指纹（UA / sec-ch-ua）。

阿里风控网关会把 x5sec 通行 Cookie 与验证时浏览器的 User-Agent、
sec-ch-ua 指纹强绑定：不同模块各用各的 UA 版本会导致"验证通过但
请求被拒"（RRXVv_ERROR / FAIL_SYS_USER_VALIDATE）的指纹分裂问题
（见 issue #62）。所有出站请求与滑块浏览器必须从这里取值，保持一致。

升级版本时只需改这一处，并同步更新滑块浏览器的 UA。
"""

# 与容器内安装的 google-chrome-stable 保持同一个大版本。
# 2026-10-26 服务器安装版本为 156.0.8078.47。
CHROME_MAJOR = "156"

# 标准 Windows Chrome UA
CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    f"(KHTML, like Gecko) Chrome/{CHROME_MAJOR}.0.0.0 Safari/537.36"
)

# Chrome GREASE brand 格式；版本由 CHROME_MAJOR 统一推导
SEC_CH_UA = (
    f'"Not=A?Brand";v="99", "Google Chrome";v="{CHROME_MAJOR}", '
    f'"Chromium";v="{CHROME_MAJOR}"'
)
