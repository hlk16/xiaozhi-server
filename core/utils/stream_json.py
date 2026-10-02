"""流式 JSON 增量解析器。

从 LLM 的流式输出中**边收边提取** voice 字段，让语音文本可以提前送进 TTS，
不必等整个响应生成完。对应设计文档 DESIGN_STRUCTURED_OUTPUT.md §4.2。

契约固定为 `{"voice": "...", "actions": [...]}`，且 **voice 必须是首字段**——
这是设计的一部分：JSON 字符串有明确的终结符（未转义的 `"`），
所以可以用极简状态机提取，不需要通用 JSON 解析器，也不需要旧方案那套
「lookahead 猜测标记是否被 token 切半」的补丁。

用法::

    parser = RobotReplyParser(ROBOT_ACTION_CODES)

    for chunk in llm_stream:
        voice_chunk = parser.feed(chunk)
        if voice_chunk:
            tts_queue.put(voice_chunk)      # 立刻送 TTS，此时 LLM 还在生成

    voice_text, actions = parser.finish()   # 流结束后拿完整结果

    行为：
      voice_text  —— 完整语音文本（finish 后以整体 JSON 解析为准，更可靠）
      actions     —— [{"code": "WAVE", "delay_ms": 0}, ...]，空列表表示无动作
      parser.ok   —— 整体 JSON 是否解析成功
      parser.stats—— 用于打点的统计（见下方 Stats）
"""

import json
from typing import Optional

# ---------------------------------------------------------------- 常量

# voice 键名。容忍 {"voice":"x"} / {"voice": "x"} / { "voice" : "x" } 等写法
_KEY = '"voice"'

# SEEK 阶段最多容忍多少字符还没找到 voice 键；超过则判定格式不符，放弃流式提取
_SEEK_LIMIT = 512

# 兜底上限（与 textUtils 保持一致）。真实上限由调用方传入。
_DEFAULT_MAX_ACTIONS = 3
_DEFAULT_MAX_DELAY_MS = 5000

_UNESCAPE = {
    '"': '"',
    "\\": "\\",
    "/": "/",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
}


# ---------------------------------------------------------------- 状态


class Stats:
    """解析统计，供调用方打点（本模块不直接依赖任何 metrics 实现）"""

    __slots__ = ("rejected", "truncated", "seek_failed", "value_truncated")

    def __init__(self):
        self.rejected = 0        # 被拒的动作数（非法码/类型错）
        self.truncated = 0       # 因超过 max_actions 被截断的动作数
        self.seek_failed = False  # 没找到 voice 前缀（格式不符）
        self.value_truncated = False  # voice 值超过安全上限被放弃流式提取


# ---------------------------------------------------------------- 提取器


def _find_value_start(buf: str) -> int:
    """定位 voice 字符串值的起始索引（即开引号之后）。

    容忍键名与冒号之间的空白。未找到返回 -1。
    """
    k = buf.find(_KEY)
    if k == -1:
        return -1
    i = k + len(_KEY)
    n = len(buf)

    while i < n and buf[i] in " \t\r\n":
        i += 1
    if i >= n or buf[i] != ":":
        return -1
    i += 1
    while i < n and buf[i] in " \t\r\n":
        i += 1
    if i >= n or buf[i] != '"':
        return -1
    return i + 1


class VoiceStreamExtractor:
    """从流式 JSON 中增量提取 voice 字符串值。

    状态机：SEEK → VALUE → DONE（或 FAILED）
    """

    SEEK, VALUE, DONE, FAILED = "SEEK", "VALUE", "DONE", "FAILED"

    def __init__(self):
        self.buf = ""
        self.state = self.SEEK
        self.escape = False
        self.seen = 0  # SEEK 阶段累计见到的字符数，用于判定放弃

    def feed(self, token: str) -> str:
        """喂入 token，返回本次可安全下发的文本（可能为空串）"""
        if self.state in (self.DONE, self.FAILED):
            return ""

        self.buf += token

        # ---- SEEK：定位 voice 值的起点 ----
        if self.state == self.SEEK:
            self.seen += len(token)
            idx = _find_value_start(self.buf)
            if idx == -1:
                if self.seen > _SEEK_LIMIT:
                    self.state = self.FAILED
                    self.buf = ""
                else:
                    # 保留尾部，可能是被切半的键名/冒号
                    self.buf = self.buf[-(len(_KEY) + 16):]
                return ""
            self.buf = self.buf[idx:]
            self.state = self.VALUE

        # ---- VALUE：状态机提取 JSON 字符串，处理转义 ----
        out = []
        consumed = 0
        for idx, ch in enumerate(self.buf):
            if self.escape:
                if ch == "u":
                    # \uXXXX 需要 4 位十六进制；数据不够就停下等下一片，
                    # 注意此时不消耗 'u'，靠 self.escape 保持状态
                    if len(self.buf) - idx < 5:
                        break
                    try:
                        out.append(chr(int(self.buf[idx + 1 : idx + 5], 16)))
                    except ValueError:
                        out.append("u")  # 非法转义，退化为字面量
                    consumed = idx + 5
                    self.escape = False
                else:
                    out.append(_UNESCAPE.get(ch, ch))
                    consumed = idx + 1
                    self.escape = False
            elif ch == "\\":
                self.escape = True
                consumed = idx + 1
            elif ch == '"':
                # 未转义的引号 = 字符串结束
                self.state = self.DONE
                consumed = idx + 1
                break
            else:
                out.append(ch)
                consumed = idx + 1

        self.buf = self.buf[consumed:]
        return "".join(out)


# ---------------------------------------------------------------- 解析器


class RobotReplyParser:
    """完整解析器：voice 流式提取 + actions 最终解析。

    Args:
        valid_codes:  合法动作码集合
        max_actions:  单次回复的动作数量上限
        max_delay_ms: 单个动作延迟上限（毫秒）
    """

    def __init__(
        self,
        valid_codes,
        max_actions: int = _DEFAULT_MAX_ACTIONS,
        max_delay_ms: int = _DEFAULT_MAX_DELAY_MS,
    ):
        self.valid_codes = frozenset(valid_codes)
        self.max_actions = max_actions
        self.max_delay_ms = max_delay_ms

        self._extractor = VoiceStreamExtractor()
        self._streamed_voice = ""  # 增量吐出的全部内容，用于整体解析失败时兜底
        self.raw = ""
        self.voice_text = ""
        self.actions: list = []
        self.ok = False
        self.stats = Stats()

    # -------------------------------------------------- 流式阶段

    def feed(self, token: str) -> str:
        """喂入一个 token 分片，返回可立刻送 TTS 的文本（可能为空串）"""
        self.raw += token
        chunk = self._extractor.feed(token)
        self._streamed_voice += chunk
        return chunk

    # -------------------------------------------------- 收尾

    def finish(self) -> tuple:
        """流结束。返回 (voice_text, actions)。

        以**整体 json.loads 为准**——增量提取只用于提前送 TTS，
        最终文本仍以完整解析结果覆盖，避免转义/分片边界造成偏差。
        """
        self.stats.seek_failed = self._extractor.state == VoiceStreamExtractor.FAILED

        try:
            obj = json.loads(self.raw)
            if not isinstance(obj, dict):
                raise TypeError(f"顶层不是对象: {type(obj).__name__}")

            voice = obj.get("voice") or ""
            if not isinstance(voice, str):
                voice = str(voice)
            self.voice_text = voice.strip()

            raw_actions = obj.get("actions")
            if isinstance(raw_actions, (str, dict)):  # 容错：模型给了单个而非数组
                raw_actions = [raw_actions]
            if not isinstance(raw_actions, list):
                raw_actions = []

            self.actions = self._normalize(raw_actions)
            self.ok = True

        except (json.JSONDecodeError, TypeError, AttributeError, ValueError):
            # 整体解析失败：保留增量提取到的文本，动作为空。
            # 调用方应记录 parse_fallback（见设计文档 §4.7）。
            self.voice_text = self._extractor_fallback_text()
            self.actions = []
            self.ok = False

        return self.voice_text, self.actions

    def _extractor_fallback_text(self) -> str:
        """整体解析失败时，用增量提取器已经吐出的内容兜底。

        大多数情况下 JSON 解析失败是因为 actions 部分畸形，而 voice 字符串
        已经完整提取——此时返回的就是完整的语音文本，用户能正常听到回答。

        返回空串表示连 voice 前缀都没找到（格式完全不符），
        调用方应改用原始文本直送 TTS（见设计文档 §4.7）。
        """
        return self._streamed_voice.strip()

    # -------------------------------------------------- 动作校验

    def _normalize(self, raw_actions: list) -> list:
        """逐项校验：非法码跳过、延迟钳制、数量截断。

        策略是**逐项处理**而不是「有一项非法就整体丢弃」——
        模型返回 [WAVE, FLY] 时保留 WAVE，比丢掉整句动作合理。
        """
        out = []
        for item in raw_actions:
            if len(out) >= self.max_actions:
                self.stats.truncated += 1
                continue

            if isinstance(item, str):  # 容错：模型只给了码
                code, delay = item, 0
            elif isinstance(item, dict):
                code, delay = item.get("code"), item.get("delay_ms", 0)
            else:
                self.stats.rejected += 1
                continue

            if code not in self.valid_codes:
                self.stats.rejected += 1
                continue

            try:
                delay = max(0, min(int(delay), self.max_delay_ms))
            except (TypeError, ValueError):
                delay = 0

            out.append({"code": code, "delay_ms": delay})

        return out


# ---------------------------------------------------------------- 便捷接口


def parse_complete(text: str, valid_codes, **kwargs) -> tuple:
    """一次性解析完整响应（非流式场景，或作为流式的兜底）。

    返回 (voice_text, actions, parser)。parser 上有 ok / stats。
    """
    p = RobotReplyParser(valid_codes, **kwargs)
    p.feed(text)
    voice, actions = p.finish()
    return voice, actions, p
