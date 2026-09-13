import ahocorasick
from typing import List, Dict, Tuple, Any

class ProhibitedWordSanitizer:
    """
    基于 Aho-Corasick 算法的主播流式违禁词与极限词实时平替/拦截引擎
    时间复杂度 O(N + M)，单句耗时 < 1ms，支持直播中动态热加载
    """
    def __init__(self):
        self.automaton = ahocorasick.Automaton()
        self.word_meta: Dict[str, Tuple[str, str, str]] = {}
        self.is_ready = False

    def load_words(self, words_data: List[Dict[str, Any]]):
        """
        加载违禁词配置并构建 AC 自动机树
        :param words_data: 包含 word, category, action_policy, replacement_word, role_scope 的字典列表
        """
        self.automaton = ahocorasick.Automaton()
        self.word_meta.clear()

        for item in words_data:
            if not item.get("is_enabled", 1):
                continue
            word = item["word"].strip()
            if not word:
                continue
            action = item.get("action_policy", "substitute")
            replacement = item.get("replacement_word", "") or ""
            category = item.get("category", "extreme")
            role_scope = item.get("role_scope", "all")

            # 存入元数据
            self.word_meta[word] = (action, replacement, category, role_scope)
            self.automaton.add_word(word, (word, action, replacement, category, role_scope))

        if len(self.word_meta) > 0:
            self.automaton.make_automaton()
            self.is_ready = True
        else:
            self.is_ready = False

    def sanitize(self, text: str, current_role: str = "all") -> Tuple[str, List[Dict[str, Any]], bool]:
        """
        对输入文本进行违禁词扫描和平替/阻断
        :param text: 待检测播报文本
        :param current_role: 当前主播角色 (ecommerce / entertainment / expert)
        :return: (处理后的文本, 触发的违规记录列表, 是否需要整句丢弃阻断)
        """
        if not self.is_ready or not text:
            return text, [], False

        hits = []
        is_dropped = False

        # 第一遍扫描：收集所有命中的违禁词信息
        # AC 自动机返回的是 (end_index, value)
        raw_matches = []
        for end_idx, (word, action, replacement, category, role_scope) in self.automaton.iter(text):
            # 作用域过滤：如果是特定角色专有或针对所有角色
            if role_scope != "all" and role_scope != current_role:
                continue
            start_idx = end_idx - len(word) + 1
            raw_matches.append((start_idx, end_idx, word, action, replacement, category))

        if not raw_matches:
            return text, [], False

        # 如果命中需要整句阻断的词 (action == 'drop')
        for match in raw_matches:
            start_idx, end_idx, word, action, replacement, category = match
            hits.append({
                "matched_word": word,
                "category": category,
                "action": action,
                "start_idx": start_idx,
                "end_idx": end_idx,
                "replacement": replacement
            })
            if action == "drop":
                is_dropped = True

        if is_dropped:
            # 整句阻断，返回空文本
            return "", hits, True

        # 按照 start_idx 升序，长度降序排序
        raw_matches.sort(key=lambda x: (x[0], -(x[1] - x[0])))

        # 过滤重叠区间，优先保留最长匹配
        non_overlapping_matches = []
        last_end = -1
        for m in raw_matches:
            start_idx, end_idx = m[0], m[1]
            if start_idx > last_end:
                non_overlapping_matches.append(m)
                last_end = end_idx

        # 从后往前执行平替，避免索引偏移
        non_overlapping_matches.sort(key=lambda x: x[0], reverse=True)
        sanitized = list(text)

        for start_idx, end_idx, word, action, replacement, category in non_overlapping_matches:
            if action == "substitute":
                sanitized[start_idx:end_idx + 1] = list(replacement)

        result_text = "".join(sanitized)
        return result_text, hits, False

# 单例实例
global_guardrail = ProhibitedWordSanitizer()
