"""付款后确认发货的纯逻辑。

这里只放不依赖数据库、WebSocket 和浏览器对象的函数，方便单元测试：
关键词解析、关键词匹配、文案渲染、过期与提醒时间计算。
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional

DEFAULT_CONFIRM_TEXT = (
    "亲，您拍下的商品需要确认后发货。\n"
    "确认无误请回复「{keyword}」，我们会立即为您发送卡密/发货。\n"
    "商品：{item_title}\n"
    "订单：{order_id}"
)
DEFAULT_CONFIRM_KEYWORDS = "确认发货|确认"
DEFAULT_EXCLUDE_KEYWORDS = "不确认|取消订单|申请退款|不买了|不要了"
DEFAULT_REMINDER_TEXT = "提醒：请回复「{keyword}」确认发货，确认后我们会立即为您发货。"
DEFAULT_TIMEOUT_MINUTES = 30
DEFAULT_REMINDER_MINUTES = 10
DEFAULT_MAX_REMINDERS = 2

_KEYWORD_SPLIT_RE = re.compile(r"[|,，;；\r\n]+")
_WHITESPACE_RE = re.compile(r"\s+")

# 这些是平台系统消息的常见形态，绝不应当作用户确认关键词匹配
_SYSTEM_MESSAGE_PREFIX = "["
_SYSTEM_MESSAGE_SUFFIX = "]"


def normalize_timeout_minutes(value: Any, default: int = DEFAULT_TIMEOUT_MINUTES) -> int:
    """把超时时间限制在 5 分钟到 24 小时之间。"""
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        minutes = default
    return max(5, min(1440, minutes))


def normalize_reminder_minutes(value: Any, timeout_minutes: int,
                               default: int = DEFAULT_REMINDER_MINUTES) -> int:
    """提醒间隔必须小于超时时间；0 表示不提醒。"""
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        minutes = default
    timeout_minutes = normalize_timeout_minutes(timeout_minutes)
    return max(0, min(timeout_minutes - 1, minutes))


def normalize_max_reminders(value: Any, default: int = DEFAULT_MAX_REMINDERS) -> int:
    try:
        count = int(value)
    except (TypeError, ValueError):
        count = default
    return max(0, min(10, count))


def parse_keyword_list(raw: Any, limit: int = 20, max_length: int = 50) -> List[str]:
    """解析 ``a|b,c`` 形式的关键词列表，保持配置顺序并去重。"""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple, set)):
        parts = [str(item) for item in raw]
    else:
        parts = _KEYWORD_SPLIT_RE.split(str(raw))

    result: List[str] = []
    seen = set()
    for part in parts:
        keyword = (part or "").strip()
        if not keyword:
            continue
        keyword = keyword[:max_length]
        normalized = normalize_match_text(keyword)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(keyword)
        if len(result) >= limit:
            break
    return result


def normalize_match_text(text: Any) -> str:
    """关键词匹配前统一去掉空白并转小写。"""
    return _WHITESPACE_RE.sub("", str(text or "")).lower()


def is_system_like_message(message: Any) -> bool:
    """平台订单/系统消息通常是 ``[xxx]``，不参与确认关键词匹配。"""
    text = str(message or "").strip()
    return len(text) >= 2 and text.startswith(_SYSTEM_MESSAGE_PREFIX) and text.endswith(
        _SYSTEM_MESSAGE_SUFFIX
    )


def find_excluded_keyword(message: Any, exclude_keywords: Any) -> Optional[str]:
    """如果消息命中排除词，返回该排除词。"""
    normalized_message = normalize_match_text(message)
    if not normalized_message:
        return None
    for keyword in parse_keyword_list(exclude_keywords):
        if normalize_match_text(keyword) in normalized_message:
            return keyword
    return None


def find_matching_keyword(message: Any, keywords: Any,
                          exclude_keywords: Any = None) -> Optional[str]:
    """返回命中的第一个关键词；排除词优先级更高。"""
    if is_system_like_message(message):
        return None
    if find_excluded_keyword(message, exclude_keywords):
        return None

    normalized_message = normalize_match_text(message)
    if not normalized_message:
        return None
    for keyword in parse_keyword_list(keywords):
        if normalize_match_text(keyword) in normalized_message:
            return keyword
    return None


def render_template(template: Any, context: Dict[str, Any]) -> str:
    """安全替换 ``{name}`` 占位符，不把用户文案当 format 字符串解析。"""
    text = str(template or "")
    for key, value in (context or {}).items():
        text = text.replace("{" + str(key) + "}", "" if value is None else str(value))
    return text


def build_confirmation_prompt(template: Any, keywords: Any,
                              context: Dict[str, Any]) -> str:
    """生成确认文案；文案没有提示关键词时自动追加一句。"""
    keyword_list = parse_keyword_list(keywords) or parse_keyword_list(
        DEFAULT_CONFIRM_KEYWORDS
    )
    primary = keyword_list[0] if keyword_list else ""
    merged_context = dict(context or {})
    merged_context.setdefault("keyword", primary)
    merged_context.setdefault("keywords", " / ".join(keyword_list))

    raw_template = str(template or "").strip() or DEFAULT_CONFIRM_TEXT
    text = render_template(raw_template, merged_context).strip()
    if not text:
        text = render_template(DEFAULT_CONFIRM_TEXT, merged_context).strip()

    if primary:
        has_keyword_placeholder = "{keyword}" in raw_template or "{keywords}" in raw_template
        if not has_keyword_placeholder and normalize_match_text(primary) not in normalize_match_text(text):
            text = f"{text}\n\n请回复「{primary}」确认发货。"
    return text


def build_reminder_text(template: Any, keywords: Any,
                        context: Dict[str, Any]) -> str:
    """生成提醒文案。"""
    keyword_list = parse_keyword_list(keywords) or parse_keyword_list(
        DEFAULT_CONFIRM_KEYWORDS
    )
    primary = keyword_list[0] if keyword_list else ""
    merged_context = dict(context or {})
    merged_context.setdefault("keyword", primary)
    merged_context.setdefault("keywords", " / ".join(keyword_list))

    raw_template = str(template or "").strip() or DEFAULT_REMINDER_TEXT
    text = render_template(raw_template, merged_context).strip()
    if not text:
        text = render_template(DEFAULT_REMINDER_TEXT, merged_context).strip()
    if primary and normalize_match_text(primary) not in normalize_match_text(text):
        text = f"{text}\n\n请回复「{primary}」确认发货。"
    return text


def compute_expires_at(prompt_sent_at: Any, timeout_minutes: Any) -> int:
    try:
        sent_at = int(prompt_sent_at)
    except (TypeError, ValueError):
        sent_at = 0
    return sent_at + normalize_timeout_minutes(timeout_minutes) * 60


def message_within_expiry(record: Dict[str, Any], message_ts: Any,
                          grace_seconds: int = 60) -> bool:
    """判断买家消息是否在过期前发出。

    断线重连时消息可能延迟处理，因此允许 ``grace_seconds`` 的宽限。
    没有 ``expires_at``（文案还没发出去）时不做时间限制。
    """
    expires_at = (record or {}).get("expires_at")
    if not expires_at:
        return True
    try:
        expires_at = int(expires_at)
        message_ts = int(float(message_ts))
    except (TypeError, ValueError):
        return True
    return message_ts <= expires_at + max(0, int(grace_seconds))


def reminder_due(record: Dict[str, Any], now: Any) -> bool:
    """判断是否应该给买家发确认提醒。"""
    record = record or {}
    if record.get("status") != "prompt_sent":
        return False
    try:
        max_reminders = int(record.get("max_reminders") or 0)
        reminder_minutes = int(record.get("reminder_minutes") or 0)
        reminder_count = int(record.get("reminder_count") or 0)
        current = int(float(now))
    except (TypeError, ValueError):
        return False
    if max_reminders <= 0 or reminder_minutes <= 0:
        return False
    if reminder_count >= max_reminders:
        return False

    expires_at = record.get("expires_at")
    if expires_at:
        try:
            if current >= int(expires_at):
                return False
        except (TypeError, ValueError):
            pass

    last_sent = record.get("last_reminder_at") or record.get("prompt_sent_at")
    if not last_sent:
        return False
    try:
        return current >= int(last_sent) + reminder_minutes * 60
    except (TypeError, ValueError):
        return False


def as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def split_keyword_text(raw: Any) -> str:
    """规范化保存的关键词文本。"""
    return "|".join(parse_keyword_list(raw))


def join_keywords(keywords: Iterable[Any]) -> str:
    return "|".join(str(item).strip() for item in keywords if str(item).strip())
