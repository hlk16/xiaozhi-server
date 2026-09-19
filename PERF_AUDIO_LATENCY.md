# 语音返回延迟分析与优化

> 数据来源：`tmp/server.log`，2026-05-14 ~ 2026-09-19，共 154 轮可配对对话。
> 分析脚本可复现，方法见文末附录。

---

## 一、结论摘要

> ⚠️ **2026-09-19 更新**：加了毫秒级打点后测了一轮，数据**推翻了本文档最初的判断**。
> 瓶颈不是 LLM 缓冲，而是 **TTS 段**。详见 §2.5 和 §3.0。下面的结论已按新数据修订。

| 项目 | 结论 |
|---|---|
| **核心指标** | 「大模型收到用户消息」→「发送第一段语音」**中位 4s，p90 13s** |
| **主因（修订）** | **TTS 段占 57%**（合成 + 转码 + 下发），单轮实测 1.344s |
| **次因（修订）** | LLM 段占 43%（首 token 0.750s + 生成 0.234s）；`chat()` 的整段缓冲在**长回复**时会放大 |
| **不是的问题** | ASR 断句（实测 0.281s）、ASR 握手（中位 0s）、工具调用（日志里根本没有） |
| **预期收益** | 换流式 TTS（P1）省 ~1s；恢复流式（P0）在长回复场景省 ~1.5s |

---

## 二、延迟数据

### 2.1 总分布

```
LLM收到文本 → 首段音频    n=154
  中位 4s   均值 5.4s   p90 13s   max 34s

  -1s #                    1
   2s #######################  23
   3s ###################################################  51   ← 主峰
   4s ###############################  31
   5s ####################  20
   6s ######                  6
   7s ##                      2
   9s #                       1
  11s #                       1
  13s ###                     3
  14s ##                      2
  15s ####                    4
  16s ####                    4
  18s #                       1
  32s ##                      2
  33s #                       1
  34s #                       1
```

**81%（125/154）落在 2–5s**，但有约 12% 落在 13–34s 的长尾。

> ⚠️ 日志时间戳只有**秒级精度**（`config/logger.py:66` 的 `{time:YYMMDD HH:mm:ss}`），
> 所有数字有 ±1s 误差，无法做百毫秒级拆分。

### 2.2 按日期看（排除偶发网络问题）

| 日期 | 轮数 | 中位 | 均值 | |
|---|---|---|---|---|
| 2026-05-14 | 4 | 4s | 4.2s | |
| 2026-05-16 | 25 | 4s | 3.9s | |
| 2026-05-17 | 8 | 3s | 3.1s | |
| **2026-05-18** | 6 | **10s** | **12.2s** | ← 异常 |
| 2026-05-19 | 18 | 4s | 3.7s | |
| 2026-05-21 | 7 | 3s | 2.9s | |
| 2026-05-27 | 11 | 5s | **12.1s** | ← 长尾 |
| 2026-05-28 | 26 | 4s | 6.6s | |
| 2026-06-09 | 13 | 4s | 6.4s | |
| 2026-06-15 | 13 | 3s | 5.1s | |
| **2026-09-19** | 8 | **2s** | **1.8s** | ← 当前配置，最好的 |

**基线稳定在 3–4s**，说明这是结构性问题，不是偶发抖动。

**09-19（当前 DeepSeek 配置）做到中位 2s**，是因为 DeepSeek 生成短句快；但结构缺陷仍在——一旦回复变长，延迟会线性增长。

### 2.3 关键否证：延迟与回复长度无关

按首音延迟分桶，统计该轮 TTS 句数（≈回复长度）：

| 延迟 | 轮数 | 平均句数 | 最大句数 |
|---|---|---|---|
| ≤2s | 24 | 2.2 | 4 |
| ≤3s | 51 | 2.1 | 5 |
| ≤4s | 31 | 2.3 | 4 |
| ≤5s | 20 | 2.4 | 4 |
| ≤8s | 8 | 2.5 | 3 |
| ≤16s | 15 | 2.0 | 4 |
| ≤40s | 5 | **2.2** | 4 |

**各桶几乎一样（2.0–2.5 句）**，包括最慢的 34s 那轮（只有 1 句）。

→ 说明慢**不是**「内容多所以要生成久」，而是**某个固定环节本身耗时**，且这个耗时是**串行前置**的——不管回复多短，都得先等它走完。

### 2.4 排除项

| 怀疑对象 | 实测 | 结论 |
|---|---|---|
| ASR 握手慢 | `ASR_CONN → READY` 中位 **0s**，均值 0.26s，74% 在同一秒内完成 | ❌ 不是瓶颈 |
| ASR 建连浪费 | 337 次建连 / 仅 103 次出文本，64 个会话平均建连 5.3 次 | ⚠️ 有浪费但不致命 |
| TTS 重试 | 363 次生成中 362 次「重试0次」 | ❌ 不是瓶颈 |
| 工具调用 | INFO 日志里 0 条（日志级别是 DEBUG） | ❓ 无法证伪，见 §5 |

### 2.5 毫秒级打点实测 ⭐ 这一节推翻了 §3.1 的判断

加完 P3 打点后实测一轮（2026-09-19 16:41，DeepSeek + EdgeTTS）：

```
[LATENCY] 说停→ASR 0.281s | LLM请求 +0.016s | 首token +0.750s | 生成完 +0.234s
          | 入TTS +0.000s | 发声 +1.344s | ASR→发声 2.344s
```

| 阶段 | 耗时 | 占比 | 含义 |
|---|---|---|---|
| **说停→ASR** | **0.281s** | — | 云端断句 + 识别。**彻底排除 ASR** |
| LLM 请求 | +0.016s | 1% | 发请求到拿到流 |
| **首 token** | **+0.750s** | 32% | DeepSeek 首字延迟（含 TLS + prompt 处理） |
| 生成完 | +0.234s | 10% | 剩余生成（这轮回复很短） |
| 入 TTS | +0.000s | 0% | 缓冲导致这里是 0 |
| **发声** | **+1.344s** | **57%** | TTS 排队 + 合成 + 解码 + opus 编码 + 下发 |
| **合计** | **2.344s** | | |

**关键修正**：`发声` 段（1.344s）才是最大单项，而不是 LLM 缓冲。

原文 §3.1 的判断来自秒级日志的推断——那时只能看到「LLM收到文本 → 首段音频 = 2s」这个端到端数字，
把 2s 全归给了 LLM。毫秒打点证明 LLM 只占 1.0s，剩下 1.3s 在 TTS。

> ⚠️ **样本量说明**：目前只有 1 轮毫秒级数据，且这一轮回复极短（"你好呀。我在呢~咋回事？"），
> 所以 `生成完` 只有 0.234s。**LLM 缓冲的代价随回复长度线性增长**——
> 回复长时 `生成完` 会显著变大，P0 的收益也随之变大。
> 需要补测几轮长回复才能定 P0/P1 的优先级。

**尚未拆开的**：`发声 +1.344s` 内部还没分清「EdgeTTS 合成」和「解码转码」各占多少。
已在 `text_to_speak()` 返回处补了 `TTS完` 打点，补测后会拆出：
- `入TTS → TTS完` = EdgeTTS 网络请求 + 合成
- `TTS完 → 发声` = mp3 解码 + 重采样 + opus 编码 + 首包发送

---

## 三、根因分析

### 3.0 主因（修订）：TTS 段

见 §2.5。`发声` 段 1.344s 占 57%。这一段串行包含四件事：

```
入TTS队列 → EdgeTTS 网络请求(TLS+WSS握手+合成整句) → mp3解码重采样(pydub→ffmpeg子进程) → opus编码 → 首包发送
```

其中 **EdgeTTS 每句都要新建一次到微软的 WSS 连接**（[edge.py](core/providers/tts/edge.py) 的
`edge_tts.Communicate(...).stream()`），加上 **每句一次 ffmpeg 子进程**，
两者都是固定开销，与文本长短无关。这正好解释了 §2.3 观察到的
「延迟与回复长度无关」——因为主导成本是**每句的固定开销**，不是生成长度。

### 3.1 次因：LLM 流式输出被整段缓冲

**证据**：`git show 704eeb7 -- core/connection.py`

```diff
                 if content is not None and len(content) > 0:
                     if not tool_call_flag:
                         response_message.append(content)
-                        self.tts.tts_text_queue.put(          # ← 被删掉的三行
-                            TTSMessageDTO(... content_detail=content ...)
-                        )
```

**改动前**：DeepSeek 每吐一个 token 就推进 TTS 队列，[tts/base.py:459-496](core/providers/tts/base.py#L459-L496) 的 `_get_segment_text()` 一旦攒到第一个标点就立刻开合成——**LLM 还在生成后半句，第一句的语音已经在播了**。

**改动后**（[connection.py:1026-1028](core/connection.py#L1026-L1028)）：只往 `response_message` 里堆，直到流结束，才在 [connection.py:1138-1164](core/connection.py#L1138-L1164) 统一 `re.split` 后入队。

```
改动前：  [token][token][token]...  → 边到边切句 → TTS 并行
改动后：  [token][token][token]...  → 全部收完 → 切句 → TTS 串行
                  └────────── 这段时间完全是白等的 ──────────┘
```

改动原因是为了支持 `[语音回复:XXX][动作指令:YYY]` 格式（[textUtils.py:28](core/utils/textUtils.py#L28) 的 `parse_robot_response`）——必须拿到完整文本才能剥壳。

**代价是把 TTS 从并行打回了串行**：原本「LLM 还在生成后半句时，第一句已经在合成/播放」，
现在必须等全文生成完，TTS 才开始动。

> ⚠️ **但这个改动不是当前延迟的主因**（见 §2.5）。它的代价 = `生成完` 的耗时，
> 本轮实测只有 0.234s，因为回复短。**回复越长，这个代价越大**——
> 一段 200 字的回复，DeepSeek 要生成约 300 token，按 ~50 token/s 算就是 6 秒，
> 而改回流式后首音时间基本恒定在 1s 左右。所以 P0 的价值在**长回复场景**，
> 短回复场景收益有限。

### 3.2 次因：TTS 非流式 + ffmpeg 子进程

[edge.py](core/providers/tts/edge.py) 的 `text_to_speak()` 把整句 mp3 **收完**才返回：

```python
async for chunk in communicate.stream():
    if chunk["type"] == "audio":
        f.write(chunk["data"])     # 全写完了才往下走
```

然后 [util.py:338](core/utils/util.py#L338) 走 pydub：

```python
audio = AudioSegment.from_file(BytesIO(audio_bytes), format=file_type, ...)
```

`AudioSegment.from_file` 会**起一个 ffmpeg 子进程**做解码+重采样。日志里有实证：

```
语音生成失败1次: 好啦好啦，不急不急～...，错误: Decoding failed. ffmpeg returned error code: 4294967279
```

每个句子一次 ffmpeg 进程（Windows 上约 150–400ms），**且必须等整句 TTS 完成才能开始**。

### 3.3 放大因素

| 因素 | 现状 | 影响 |
|---|---|---|
| `max_history_rounds` | `0`（**不截断**） | 历史越长 prompt 越大，DeepSeek 首 token 越慢。会话末尾可能比开头慢一倍 |
| 常驻工具数 | 7 个（`get_weather` / `play_music` / `handle_exit_intent` / `get_news_from_newsnow` / `get_lunar` / `change_role` / `capture_photo`） | 每轮都塞进 request，增加 prompt 处理时间 |
| LLM 客户端 | `openai.OpenAI`（**同步**），跑在 `ThreadPoolExecutor(5)` 里 | 阻塞 worker；若并发多，会排队 |

---

## 四、优化方案

### P0 —— 恢复流式（收益 ~1.5–2s，必做）

核心思路：**边收 token 边剥壳边推 TTS**，而不是等全文。

需要一个增量解析器替代现在的 `parse_robot_response(text_buff)`。

```python
# core/utils/textUtils.py 新增

class RobotReplyStreamParser:
    """增量剥离 [语音回复:...][动作指令:XXX]，把语音文本边到边吐出来"""

    VOICE_OPEN  = "[语音回复:"
    ACTION_OPEN = "[动作指令:"
    # 末尾可能被切半的标记最长长度，必须回退保留
    KEEP = max(len(VOICE_OPEN), len(ACTION_OPEN)) + 1

    def __init__(self):
        self.buf = ""
        self.in_voice = False
        self.finished = False
        self.action_code = "NOACTION"

    def feed(self, token: str) -> str:
        """喂入新 token，返回「本次可安全下发给 TTS」的文本（可能为 ""）"""
        if self.finished:
            self.buf += token
            return ""
        self.buf += token

        # 阶段1：等待 [语音回复: 前缀
        if not self.in_voice:
            i = self.buf.find(self.VOICE_OPEN)
            if i == -1:
                self.buf = self.buf[-self.KEEP:]      # 保留尾部等待前缀拼接
                return ""
            self.buf = self.buf[i + len(self.VOICE_OPEN):]
            self.in_voice = True

        # 阶段2：语音段内，找结束标记
        end, marker = -1, ""
        for m in ("]", self.ACTION_OPEN):
            i = self.buf.find(m)
            if i != -1 and (end == -1 or i < end):
                end, marker = i, m

        if end != -1:
            head = self.buf[:end]
            self.finished = True
            self.buf = self.buf[end:]                 # 剩余部分留给 finish()
            return head

        # 阶段3：还没遇到结束标记 —— 只吐「确定安全」的前缀
        if len(self.buf) <= self.KEEP:
            return ""
        safe, self.buf = self.buf[:-self.KEEP], self.buf[-self.KEEP:]
        return safe

    def finish(self) -> tuple:
        """流结束时调用，返回 (收尾文本, 动作码)"""
        m = re.search(r"\[动作指令:(.*?)\]", self.buf)
        if m and m.group(1).strip() in ACTION_CODES:
            self.action_code = m.group(1).strip()

        tail = ""
        if not self.finished:
            # 模型没按格式输出，兜底：去掉动作标记后的残留
            tail = self.buf.split(self.ACTION_OPEN)[0].rstrip("]")
        self.buf = ""
        return tail, self.action_code
```

调用点改动 —— [connection.py](core/connection.py) 的 `chat()` 流循环：

```python
# 初始化（在 for response in llm_responses: 之前）
parser = RobotReplyStreamParser()

for response in llm_responses:
    ...
    content = ...            # 保持原来的取值逻辑不变
    if content:
        response_message.append(content)
        # ▼ 新增：边收边往下吐
        safe_text = parser.feed(content)
        if safe_text:
            self.tts.tts_text_queue.put(
                TTSMessageDTO(
                    sentence_id=current_sentence_id,
                    sentence_type=SentenceType.MIDDLE,
                    content_type=ContentType.TEXT,
                    content_detail=safe_text,
                )
            )

# 流结束后（替换原来的 parse_robot_response(text_buff) 逻辑）
tail_text, action_code = parser.finish()
if tail_text:
    self.tts.tts_text_queue.put(TTSMessageDTO(
        sentence_id=current_sentence_id,
        sentence_type=SentenceType.MIDDLE,
        content_type=ContentType.TEXT,
        content_detail=tail_text,
    ))
asyncio.run_coroutine_threadsafe(send_action_message(self, action_code), self.loop)
```

**关键点**：不需要改 `_get_segment_text()`——它本来就会对入队文本做增量缓冲+标点切句。原来的设计就是为此写的，只需要把 token 重新喂回去。

**风险与验证**：
- 标记被 token 切半（如 `[语音回` + `复:你好`）——`KEEP` 回退长度覆盖了这个情况
- 模型不按格式输出——`finish()` 有兜底
- **必须验证**：`[动作指令:WAVE]` 仍能正确下发；`store_tts_text()` / `dialogue.put()` 拿到的仍是完整文本

---

### P1 —— TTS 换流式（收益 ~0.5–1s）

把 [edge.py](core/providers/tts/edge.py) 换成支持流式的 TTS，一步同时解决「整句等待」和「ffmpeg 子进程」：

| 方案 | 说明 |
|---|---|
| `huoshan_double_stream` | 火山双向流式，**项目里已有实现**，且和当前 ASR 同一家，账号可复用 |
| `index_stream` | 项目自带 |
| 自建 | 若必须留 EdgeTTS，可改成边收 chunk 边转码，但 ffmpeg 子进程开销仍在 |

改配置即可，不用改代码：

```yaml
selected_module:
  TTS: HuoshanDoubleStreamTTS   # 替换 EdgeTTS
```

> 注意火山官方提示：免费额度只有 2 并发，会频繁报错，需购买（起步 30 元 / 100 并发）。

---

### P2 —— 参数调优（收益 ~0.3–1s，随会话长度递增）

```yaml
# data/.config.yaml
max_history_rounds: 10      # 原来 0（不截断）；DeepSeek 64K 上下文档建议 8-15
```

精简工具列表 —— `Intent.function_call.functions` 里删掉当前场景用不到的（比如没有 Home Assistant 就删 `hass_*`，不需要新闻就删 `get_news_from_newsnow`）。

每少一个工具就少一段 JSON schema 进入 prompt。

---

### P3 —— 补齐可观测性（**建议最先做**）

现在连问题都测不准，优化也没法验证。

**1. 日志加毫秒** —— [config/logger.py:66](config/logger.py#L66)

```python
# 改前
"<green>{time:YYMMDD HH:mm:ss}</green>..."
# 改后
"<green>{time:YYMMDD HH:mm:ss.SSS}</green>..."
```

**2. 加一条结构化延迟日志** —— 在 [connection.py](core/connection.py) `chat()` 里记录各阶段耗时：

```python
# chat() 开头
t_start = time.monotonic()
first_audio_logged = False

# 第一次有文本入 TTS 队列时
if not first_audio_logged:
    self.logger.bind(tag=TAG).info(
        f"[LATENCY] LLM首字 {t_llm_first:.3f}s  TTS首句入队 {time.monotonic()-t_start:.3f}s"
    )
    first_audio_logged = True
```

建议打点的四个位置（对应 §2 的问题：现在只能测端到端）：

| 打点 | 位置 | 意义 |
|---|---|---|
| `asr_done` | `handle_voice_stop()` 入口 | ASR 耗时 |
| `llm_first_token` | `chat()` 流循环首次拿到 content | LLM 首 token |
| `tts_enqueue` | 首次推入 `tts_text_queue` | 端到端「文字→可合成」 |
| `first_audio_sent` | `sendAudioMessage` 首次调用 | 用户真正听到的时间 |

**3. 打开工具调用的日志** —— 现在 `检测到 N 个工具调用` 是 `debug` 级（[connection.py:1079](core/connection.py#L1079)），INFO 级别完全看不到。临时改为 `info` 或排查时调 `log_level: DEBUG`。

---

## 五、优化优先级（按 §2.5 新数据修订）

| 优先级 | 动作 | 预期收益 | 成本 | 风险 |
|---|---|---|---|---|
| ~~P3~~ | ~~加毫秒日志 + 延迟打点~~ | **已完成** | — | — |
| **P1** ⬆ | 换流式 TTS | **~1s（57% 的那一段）** | 低（改配置） | 低，需付费 |
| **P0** | 恢复流式（增量解析） | 短回复 ~0.2s<br>**长回复 ~1.5–2s** | 中 | 中，需测动作指令 |
| **P2** | 历史截断 + 精简工具 | ~0.3–1s | 低 | 低 |

**修订后的顺序：P1 先做（改配置即可，收益最大且立竿见影）→ 补测长回复 → 再定 P0。**

理由：
- `发声` 段占 57%，其中主导成本是**每句的固定开销**（EdgeTTS 建连 + ffmpeg 子进程），
  与回复长短无关 → 换流式 TTS 立刻见效
- `生成完` 段（LLM 全量生成）在当前短回复场景只占 10%，
  但随回复长度线性增长 → P0 的价值需要长回复样本才能确认

---

## 六、预期效果

以 09-19 那轮实测为例（`你好。` → `你好呀，我在呢`）：

| 阶段 | 现状 | P0 后 | 说明 |
|---|---|---|---|
| LLM 生成完整回复 | ~1.5s | — | 不再需要等完 |
| 首句边界出现 | （含在上面） | ~0.6s | 首 token ~0.4s + 首句 6~8 token |
| TTS 合成 + 转码 | ~0.5s | ~0.5s | P1 后可再省 0.3s |
| **合计** | **~2s** | **~1.1s** | |

长回复场景收益更大——现在回复越长首音越慢（线性），改后首音时间**基本恒定**，与回复长度无关。

---

## 附录：分析脚本

本次分析的复现脚本（严格单轮内配对，避免跨轮错配污染）：

```python
import re, statistics
from datetime import datetime
pat = re.compile(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})')
ev = []
for line in open('tmp/server.log', encoding='utf-8', errors='ignore'):
    m = pat.match(line)
    if not m: continue
    t = datetime.strptime(m.group(1), '%Y-%m-%d %H:%M:%S')
    if   '大模型收到用户消息' in line: k = 'LLM_IN'
    elif '发送第一段语音' in line:    k = 'FIRST'
    else: continue
    ev.append((t, k))

# 关键：LLM_IN 到下一个 LLM_IN 之间算一轮，期间没有 FIRST 就丢弃
turns, i = [], 0
while i < len(ev):
    if ev[i][1] == 'LLM_IN':
        t0, j, f = ev[i][0], i + 1, None
        while j < len(ev) and ev[j][1] != 'LLM_IN':
            if ev[j][1] == 'FIRST' and f is None: f = ev[j][0]
            j += 1
        if f: turns.append((f - t0).total_seconds())
        i = j
    else:
        i += 1

print(f"n={len(turns)} 中位={statistics.median(turns):.0f}s "
      f"均值={statistics.mean(turns):.1f}s max={max(turns):.0f}s")
```

> ⚠️ 第一次分析时用了「找下一个 FIRST，不管中间隔了多少轮」的简单匹配，
> 得到均值 6.86s / max 54s —— **那是错的**，慢轮次都是跨轮错配的假数据。
> 上面的「单轮内配对」版本才是本文采用的数字（均值 5.4s / max 34s）。
