"""商品售完下架后按卡密余量自动重新上架的资格判断。

职责边界：
- 本模块只回答“这个商品现在该不该自动上架”，以及“为什么不该”。
- 真正的上架动作由 ``utils/item_relist.py`` 执行，调度在 ``XianyuAutoAsync``。

库存口径：
- ``data``（批量卡密）按剩余行数判断，发完才算没有库存；
- ``text`` / ``image`` / ``api`` 没有本地库存概念，只要有内容就视为可继续上架。
  这是按“固定文本也能无限发”的实际用法定的，避免卖完一批后商品永久下架。
"""

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from loguru import logger

CARD_TYPE_LABELS = {
    'data': '批量卡密',
    'text': '固定文本',
    'api': 'API 发卡',
    'image': '图片',
}


class ItemRelistService:
    def __init__(self, db):
        self.db = db

    # ------------------------------------------------------------------
    # 库存判断
    # ------------------------------------------------------------------
    def evaluate_stock(self, cookie_id: str, item_id: str, user_id: int,
                       min_stock: int = 1) -> Dict[str, Any]:
        """按实际发货路径解析绑定，返回是否满足自动上架的最低库存。"""
        min_stock = max(1, int(min_stock or 1))

        # 优先看新版商品级配置；存在时旧版 delivery_rules 不兜底（与发货逻辑一致）
        config = self.db.get_item_delivery_config(cookie_id, item_id, user_id)
        if config:
            if not config.get('enabled'):
                return {'eligible': False, 'reason': '发货策略已停用', 'source': 'variant'}

            variants = [v for v in (config.get('variants') or []) if v.get('enabled', True)]
            if not variants:
                return {'eligible': False, 'reason': '没有启用的发货规格', 'source': 'variant'}

            details: List[Dict[str, Any]] = []
            for variant in variants:
                name = variant.get('display_name') or f"规格 {variant.get('id')}"
                if not variant.get('card_id') or not variant.get('binding_enabled'):
                    return {'eligible': False, 'reason': f'规格“{name}”未绑定卡密', 'source': 'variant'}
                if not variant.get('card_enabled'):
                    return {'eligible': False, 'reason': f'规格“{name}”绑定的卡密已停用', 'source': 'variant'}

                card_type = variant.get('card_type')
                required = max(1, int(variant.get('delivery_count') or 1)) * min_stock

                if card_type == 'data':
                    stock = variant.get('stock_count')
                    if stock is None or stock < required:
                        return {
                            'eligible': False,
                            'reason': f'规格“{name}”库存不足（{stock or 0}/{required}）',
                            'source': 'variant',
                        }
                    details.append({'variant': name, 'stock': stock, 'required': required})
                elif card_type == 'text':
                    if not (variant.get('text_content') or '').strip():
                        return {
                            'eligible': False,
                            'reason': f'规格“{name}”绑定的固定文本为空',
                            'source': 'variant',
                        }
                    details.append({'variant': name, 'stock': None, 'required': 0})
                elif card_type == 'image':
                    if not (variant.get('image_url') or '').strip():
                        return {
                            'eligible': False,
                            'reason': f'规格“{name}”绑定的图片地址为空',
                            'source': 'variant',
                        }
                    details.append({'variant': name, 'stock': None, 'required': 0})
                elif card_type == 'api':
                    # API 发卡的余量由外部服务决定，本地无法判断，按可用处理
                    details.append({'variant': name, 'stock': None, 'required': 0})
                else:
                    return {
                        'eligible': False,
                        'reason': f'规格“{name}”的卡密类型未知（{card_type}）',
                        'source': 'variant',
                    }

            return {'eligible': True, 'reason': '', 'source': 'variant', 'details': details}

        # 旧版发货规则
        rules = self.db.get_delivery_rules_for_item('', cookie_id, item_id)
        if not rules:
            return {'eligible': False, 'reason': '没有绑定发货卡密', 'source': 'rule'}
        if len(rules) != 1:
            return {
                'eligible': False,
                'reason': f'存在 {len(rules)} 条发货规则，无法确定库存',
                'source': 'rule',
            }

        rule = rules[0]
        card_type = rule.get('card_type')
        required = max(1, int(rule.get('delivery_count') or 1)) * min_stock

        if card_type == 'data':
            stock = self.db.get_card_usable_stock_count(
                rule.get('card_id'), rule.get('data_content')
            )
            if stock is None or stock < required:
                return {
                    'eligible': False,
                    'reason': f'库存不足（{stock or 0}/{required}）',
                    'source': 'rule',
                }
            details = [{'variant': '默认', 'stock': stock, 'required': required}]
        elif card_type == 'text':
            if not (rule.get('text_content') or '').strip():
                return {
                    'eligible': False,
                    'reason': '绑定的固定文本为空',
                    'source': 'rule',
                }
            details = [{'variant': '默认', 'stock': None, 'required': 0}]
        elif card_type == 'image':
            if not (rule.get('image_url') or '').strip():
                return {
                    'eligible': False,
                    'reason': '绑定的图片地址为空',
                    'source': 'rule',
                }
            details = [{'variant': '默认', 'stock': None, 'required': 0}]
        elif card_type == 'api':
            details = [{'variant': '默认', 'stock': None, 'required': 0}]
        else:
            return {
                'eligible': False,
                'reason': f'卡密类型未知（{card_type}）',
                'source': 'rule',
            }

        return {
            'eligible': True,
            'reason': '',
            'source': 'rule',
            'details': details,
        }

    # ------------------------------------------------------------------
    # 资格判断
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_utc(value) -> Optional[datetime]:
        if not value:
            return None
        try:
            return datetime.strptime(str(value), '%Y-%m-%d %H:%M:%S')
        except (TypeError, ValueError):
            return None

    def check_eligibility(self, candidate: Dict[str, Any], user_id: int,
                          cooldown_minutes: int = 60,
                          now: Optional[datetime] = None) -> Dict[str, Any]:
        """综合下架状态、同步来源、冷却时间和库存，返回是否可上架。"""
        now = now or datetime.utcnow()

        if candidate.get('listing_status') == 'on_sale_pending':
            return {'eligible': False, 'reason': '等待同步确认上架结果', 'stock': None}

        # 手动添加或从未被同步返回过的商品不参与自动上架
        if not candidate.get('last_seen_at'):
            return {'eligible': False, 'reason': '商品从未被同步返回，可能是手动添加', 'stock': None}

        last_attempt = self._parse_utc(candidate.get('last_attempt_at'))
        if last_attempt:
            cooldown = timedelta(minutes=max(1, int(cooldown_minutes or 1)))
            elapsed = now - last_attempt
            if elapsed < cooldown:
                minutes = int((cooldown - elapsed).total_seconds() // 60) + 1
                return {'eligible': False, 'reason': f'冷却中，约 {minutes} 分钟后重试', 'stock': None}

        stock = self.evaluate_stock(
            candidate.get('cookie_id'),
            candidate.get('item_id'),
            user_id,
            candidate.get('min_stock') or 1,
        )
        return {
            'eligible': stock['eligible'],
            'reason': stock['reason'],
            'stock': stock,
        }

    def list_candidates(self, user_id: int, cookie_id: str = None,
                        cooldown_minutes: int = 60,
                        now: Optional[datetime] = None) -> List[Dict[str, Any]]:
        """列出候选商品，并附带库存与可上架判断。"""
        rows = self.db.list_item_relist_candidates(user_id, cookie_id)
        result: List[Dict[str, Any]] = []
        for row in rows:
            evaluation = self.check_eligibility(row, user_id, cooldown_minutes, now)
            item = dict(row)
            item['eligible'] = evaluation['eligible']
            item['reason'] = evaluation['reason']
            item['stock'] = evaluation['stock']
            result.append(item)
        return result

    def record_attempt(self, user_id: int, cookie_id: str, item_id: str,
                       result: str, reason: str = '') -> None:
        self.db.record_item_relist_attempt(user_id, cookie_id, item_id, result, reason)
