"""单轮对话延迟打点。

一轮 = 从 ASR 出文本，到第一段音频下发到设备。

跨线程使用：打点状态挂在 conn 上，各阶段由不同线程写入
（ASR 在事件循环、LLM 在 executor 线程、TTS 在 TTS 线程、音频下发在事件循环），
最后由第一个音频包触发统一汇总输出。

用法：
    latency.reset(conn)              # 一轮开始时重置（startToChat）
    latency.mark(conn, "llm_req")    # 各阶段打点，只记录首次
    latency.report(conn, conn.logger)  # 首次音频下发后输出汇总

输出形如（各阶段为「距上一阶段」的增量）：
    [LATENCY] 说停→ASR 0.412s | LLM请求 +0.003s | 首token +0.681s | 生成完 +1.174s |
              入TTS +0.001s | TTS完 +0.512s | 发声 +0.041s | ASR→发声 2.412s
"""

import time

TAG = "core.utils.latency"

# 阶段顺序（即输出顺序）。每个阶段输出的是「距上一个阶段」的耗时。
# asr_text 是基准点（t0），不单独输出耗时。
_STAGES = (
    ("llm_req", "LLM请求"),
    ("llm_token", "首token"),
    ("llm_done", "生成完"),
    ("tts_enqueue", "入TTS"),
    ("tts_ok", "TTS完"),
    ("audio_sent", "发声"),
)

# 输出汇总前必须已存在的打点，避免被无关的提前调用消耗掉
_REQUIRED = ("audio_sent",)


class TurnLatency:
    __slots__ = ("t0", "marks", "reported", "voice_end_gap")

    def __init__(self):
        self.t0 = time.monotonic()
        self.marks = {}
        self.reported = False
        # 用户停止说话 -> ASR 出文本，单位秒。仅流式 ASR 下有意义
        self.voice_end_gap = None

    def mark(self, name):
        self.marks.setdefault(name, time.monotonic())


def reset(conn):
    """开始新的一轮打点"""
    lt = TurnLatency()
    conn._turn_latency = lt
    return lt


def mark(conn, name):
    """记录一个阶段时刻（只记首次）"""
    lt = getattr(conn, "_turn_latency", None)
    if lt is None:
        lt = reset(conn)
    lt.mark(name)
    return lt


def report(conn, logger):
    """输出本轮延迟汇总。同一轮只输出一次；关键打点未到时不输出。"""
    lt = getattr(conn, "_turn_latency", None)
    if lt is None or lt.reported:
        return
    if any(k not in lt.marks for k in _REQUIRED):
        return

    lt.reported = True

    # 各阶段输出「距上一阶段」的增量，+ 表示该阶段自身耗时
    segs = []
    prev = lt.t0
    for name, label in _STAGES:
        t = lt.marks.get(name)
        if t is None:
            continue
        segs.append(f"{label} +{t - prev:.3f}s")
        prev = t

    if not segs:
        return

    head = ""
    if lt.voice_end_gap is not None:
        head = f"说停→ASR {lt.voice_end_gap:.3f}s | "

    logger.bind(tag=TAG).info(
        f"[LATENCY] {head}{' | '.join(segs)} | ASR→发声 {prev - lt.t0:.3f}s"
    )
