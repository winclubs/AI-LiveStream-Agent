"""
电商价格防幻觉双重审计引擎 (规划 §14.3)
实时校验大模型输出口播报价 vs 商品库底价 (Floor Price)
防止大模型幻觉虚报过低价格造成直播违规或严重亏损，发现低于底价时自动修正或拦截
"""
import re
import logging
from typing import Dict, Any, List, Tuple, Optional

logger = logging.getLogger("LiveAgent.PriceAuditor")

class PriceAuditor:
    # 常见价格提取正则模式 (提取口播中的金额数值)
    PRICE_PATTERNS = [
        re.compile(r"(?:只要|仅需|到手价|专享价|现价|只要价|米|单价|拍下|现在买|今天)[^\d]{0,4}(\d+(?:\.\d+)?)\s*(?:元|块|米)?", re.IGNORECASE),
        re.compile(r"(\d+(?:\.\d+)?)\s*(?:元|块|米)(?:带走|包邮|到手|开抢)", re.IGNORECASE),
        re.compile(r"价格[是为]\s*(\d+(?:\.\d+)?)\s*(?:元|块)?", re.IGNORECASE)
    ]

    @classmethod
    def audit_sentence(cls, sentence: str, current_product: Optional[Dict[str, Any]] = None, all_products: Optional[List[Dict[str, Any]]] = None) -> Tuple[str, bool, Optional[dict]]:
        """
        审计单句口播中的价格是否存在幻觉
        :param sentence: 待播报文本
        :param current_product: 当前主推商品 {"title": ..., "live_price": float, ...}
        :param all_products: 当前直播间所有上架商品
        :return: (修正后的文本, 是否触发了价格幻觉修正, 审计详情)
        """
        if not sentence or not (current_product or all_products):
            return sentence, False, None

        # 确定底价基准 (优先当前主推品，否则取所有商品中的最低价)
        ref_price = 0.0
        prod_title = "当前商品"
        if current_product and current_product.get("live_price"):
            ref_price = float(current_product["live_price"])
            prod_title = current_product.get("title", "当前商品")
        elif all_products:
            valid_prices = [float(p["live_price"]) for p in all_products if p.get("live_price")]
            if valid_prices:
                ref_price = min(valid_prices)

        if ref_price <= 0:
            return sentence, False, None

        # 允许的最低安全底线 (例如允许直播券微减，但不能低于标价的 85%)
        floor_price = round(ref_price * 0.85, 2)

        modified_sentence = sentence
        triggered = False
        audit_detail = None

        for pattern in cls.PRICE_PATTERNS:
            for match in pattern.finditer(sentence):
                raw_num = match.group(1)
                try:
                    spoken_price = float(raw_num)
                except ValueError:
                    continue

                # 如果口播价格过小(明显属于折扣数字如 0.8折) 则跳过
                if spoken_price < 2.0:
                    continue

                # 发现大模型幻觉虚报过低价格 (低于底价 85%)
                if spoken_price < floor_price:
                    triggered = True
                    correct_price_str = f"{ref_price:.1f}".rstrip("0").rstrip(".")
                    # 将虚报的错误低价平替为官方直播专享价
                    old_matched_text = match.group(0)
                    new_matched_text = old_matched_text.replace(raw_num, correct_price_str)
                    modified_sentence = modified_sentence.replace(old_matched_text, new_matched_text, 1)

                    audit_detail = {
                        "product": prod_title,
                        "spoken_price": spoken_price,
                        "floor_price": floor_price,
                        "official_price": ref_price,
                        "original_snippet": old_matched_text,
                        "corrected_snippet": new_matched_text
                    }
                    logger.warning(
                        f"【价格防幻觉审计拦截】检测到口播低价幻觉 ({spoken_price}元 < 底价{floor_price}元)，"
                        f"已自动修正为官方直播价: {correct_price_str}元"
                    )
                    break
            if triggered:
                break

        return modified_sentence, triggered, audit_detail

global_price_auditor = PriceAuditor()
