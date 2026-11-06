# xiaozhi-server 业务时序图

> 本文用 Mermaid 时序图描述后端各条业务链路的实际调用顺序。
> 所有调用点都标注了源文件，便于对照代码。
>
> 配套阅读：[ARCHITECTURE.md](ARCHITECTURE.md)（分层与目录职责）、[PERF_AUDIO_LATENCY.md](PERF_AUDIO_LATENCY.md)（延迟分析）

---

## 0. 参与者总览

| 参与者 | 代码位置 | 说明 |
|---|---|---|
| **设备** | — | ESP32 硬件 / Android App / Web 测试页 |
| **WS** | [core/websocket_server.py](core/websocket_server.py) | WebSocket 服务，端口 8000 |
| **HTTP** | [core/http_server.py](core/http_server.py) | aiohttp 服务，端口 8003 |
| **Conn** | [core/connection.py](core/connection.py) | `ConnectionHandler`，每设备一个实例 |
| **VAD** | [core/providers/vad/silero.py](core/providers/vad/silero.py) | 本地 ONNX，CPU 推理 |
| **ASR** | [core/providers/asr/doubao_stream.py](core/providers/asr/doubao_stream.py) | 火山流式识别 |
| **LLM** | [core/providers/llm/openai/openai.py](core/providers/llm/openai/openai.py) | DeepSeek 等，同步流式客户端 |
| **TTS** | [core/providers/tts/](core/providers/tts/) | EdgeTTS / 火山 / Linkerai 等 |
| **Tools** | [core/providers/tools/unified_tool_handler.py](core/providers/tools/unified_tool_handler.py) | 工具聚合与分发 |
| **云端** | — | 火山 / DeepSeek / 微软 等第三方服务 |

### 线程边界（读图关键）

```
主事件循环（asyncio）
  ├─ WS 收发、HTTP 服务
  └─ 各连接的协程任务

每个 ConnectionHandler 额外持有
  ├─ asr_text_priority_thread   ← 线程，消费 asr_audio_queue
  ├─ tts_text_priority_thread   ← 线程，消费 tts_text_queue
  ├─ _audio_play_priority_thread← 线程，消费 tts_audio_queue
  └─ ThreadPoolExecutor(5)      ← 线程池，跑 chat / 工具调用 / 上报
```

图中用 `Note over` 标出跨越的线程边界。**跨线程一律走 `run_coroutine_threadsafe` 或 `executor.submit`。**

---

## 1. 设备接入与握手

```mermaid
sequenceDiagram
    autonumber
    participant D as 设备
    participant WS as WebSocketServer
    participant Auth as AuthManager
    participant C as ConnectionHandler
    participant MCP as 设备MCP

    D->>WS: WS 连接 /xiaozhi/v1/<br/>Header 带 device-id / authorization
    activate WS
    WS->>WS: 校验 device-id 是否存在<br/>缺失则尝试从 URL query 取
    alt device-id 缺失
        WS-->>D: "端口正常，如需测试连接..."<br/>然后关闭
    end

    WS->>Auth: verify_token token, client_id, device_id
    activate Auth
    Note over Auth: 白名单设备直接放行<br/>否则校验 JWT
    Auth-->>WS: 通过 / AuthenticationError
    deactivate Auth

    alt 鉴权失败
        WS-->>D: "认证失败" 并关闭
    end

    WS->>C: new ConnectionHandler(config, vad, asr, llm, memory, intent, server)
    activate C
    WS->>C: handle_connection(ws)
    deactivate WS

    C->>C: 记录 client_ip / device_id<br/>启动 timeout_task
    C->>C: asyncio.create_task(_background_initialize())
    Note over C: 后台初始化，不阻塞主循环

    par 后台初始化（并行）
        C->>C: _initialize_private_config_async()<br/>从智控台拉设备差异化配置
        C->>C: executor.submit(_initialize_components)<br/>建 TTS 线程 / 工具句柄 / Prompt
    and 主循环
        Note over C: 进入 async for message in websocket
    end

    D->>C: {"type":"hello","mode":"auto",<br/>"audio_params":{...},"features":{"mcp":true}}
    C->>C: handleHelloMessage()
    C->>C: 保存 audio_format / features
    alt features.mcp == true
        C->>MCP: send_mcp_initialize_message()
        MCP-->>C: {"protocolVersion":"2024-11-05", serverInfo:...}
        C->>MCP: 请求工具列表 tools/list
        MCP-->>C: [capture_photo, ...]
        C->>C: 注册设备端工具到 UnifiedToolHandler
    end
    C-->>D: welcome_msg<br/>{type:hello, session_id, audio_params, ...}

    Note over D,C: 握手完成，可以开始收发音频
    deactivate C
```

> **注意**：`_background_initialize()` 是 fire-and-forget 的。设备可能在组件初始化完成前就发了音频，
> `_route_message` 里会用 `bind_completed_event` 等待最多 1 秒（[connection.py:314-326](core/connection.py#L314-L326)）。

---

## 2. 完整语音对话（自动模式）

这是最核心的链路。以 `SileroVAD + DoubaoStreamASR + DeepSeekLLM + EdgeTTS` 为例。

```mermaid
sequenceDiagram
    autonumber
    participant D as 设备
    participant C as ConnectionHandler
    participant ASRQ as asr_audio_queue
    participant T1 as ASR线程
    participant VAD as SileroVAD
    participant ASR as DoubaoStreamASR
    participant 火山 as 火山引擎
    participant T2 as TTS线程
    participant TTS as EdgeTTS
    participant 微软 as 微软TTS服务
    participant E as 事件循环

    Note over D,E: ① 音频上行

    loop 每 60ms 一帧 opus
        D->>C: 二进制音频帧
        C->>C: _route_message<br/>isinstance(bytes) 分支
        C->>ASRQ: put(message)
    end

    Note over C,ASRQ: 跨线程：WS 在事件循环，消费在 ASR 线程

    ASRQ->>T1: get(timeout=1)
    T1->>E: run_coroutine_threadsafe(handleAudioMessage)
    activate E

    E->>VAD: is_vad(conn, audio)
    Note over VAD: 解码 PCM → 512样本切片<br/>双阈值 0.5/0.3 + 滑窗投票<br/>返回 have_voice
    VAD-->>E: have_voice

    E->>E: no_voice_close_connect()<br/>超过 120s 无语音则断连

    E->>ASR: receive_audio(conn, audio, have_voice)

    Note over ASR,火山: ② ASR 建连（仅在检测到首次人声时）

    alt have_voice 且 asr_ws 为空 且 未在处理
        ASR->>火山: websockets.connect + 初始化请求<br/>{workflow, end_window_size:200, ...}
        火山-->>ASR: 初始化响应 log_id
        ASR->>ASR: create_task(_forward_asr_results)
    end

    ASR->>火山: 发送音频帧（gzip 压缩的 PCM）
    deactivate E

    Note over 火山,ASR: ③ 云端断句与识别

    loop 持续接收
        火山-->>ASR: 中间结果 definite=false
        Note over ASR: 静默丢弃，无日志
    end

    火山-->>ASR: utterance definite=true, text="你好。"
    activate ASR
    ASR->>ASR: logger.info("识别到文本: 你好。")
    ASR->>ASR: self.text = current_text
    ASR->>ASR: handle_voice_stop(conn, asr_audio)

    Note over ASR: ④ 进入对话

    ASR->>ASR: speech_to_text_wrapper()<br/>取 self.text
    ASR->>ASR: stop_ws_connection() 关闭 ASR 连接
    ASR->>E: startToChat(conn, enhanced_text)
    deactivate ASR
    activate E

    E->>E: latency.reset() 打点开始
    E->>E: handle_user_intent()<br/>检查退出命令 / 唤醒词
    Note over E: intent_type=function_call 时直接返回 False<br/>不做前置意图识别
    E->>D: {"type":"stt","text":"你好。","session_id":...}
    E->>D: {"type":"tts","state":"start"}
    E->>C: executor.submit(conn.chat, text)
    deactivate E

    Note over C: 跨线程：进入 ThreadPoolExecutor

    activate C
    C->>C: 打点 llm_req
    C->>ASR: llm.response_with_functions(...)<br/>同步流式，带 7 个 tools
    Note over C,ASR: 实际是 openai 同步客户端<br/>阻塞持有 1 个 worker

    loop 流式 token
        ASR-->>C: content chunk
        C->>C: 打点 llm_token 仅首次
        C->>C: response_message.append(content)
        Note over C: ⚠️ 当前实现：整段缓冲<br/>不边收边推 TTS
    end
    C->>C: 打点 llm_done
    C->>C: parse_robot_response()<br/>剥离 [语音回复:][动作指令:]
    C->>D: {"type":"action","action":"WAVE"}
    C->>C: 打点 tts_enqueue
    C->>T2: tts_text_queue.put(segments) 逐段入队
    deactivate C

    Note over T2,微软: ⑤ TTS 合成

    T2->>T2: _get_segment_text()<br/>按标点切句
    T2->>TTS: to_tts_stream(segment)
    activate TTS
    TTS->>微软: edge_tts.Communicate().stream()<br/>新建 WSS 连接
    微软-->>TTS: mp3 音频流
    TTS->>TTS: pydub AudioSegment.from_file<br/>起 ffmpeg 子进程转 opus
    TTS->>T2: handle_opus(opus_data)
    deactivate TTS
    T2->>C: tts_audio_queue.put(MIDDLE, opus, None)

    Note over T2,E: ⑥ 音频下发

    T2->>E: _audio_play_priority_thread 取出
    activate E
    E->>E: sendAudioMessage()
    E->>D: {"type":"tts","state":"sentence_start","text":"你好呀"}
    E->>E: sendAudio() 经 AudioRateController 流控
    E->>D: opus 音频包（前 5 包直发，之后按 60ms 节奏）
    E->>E: 打点 audio_sent 仅首包
    E->>E: latency.report() 输出 [LATENCY] 汇总
    deactivate E

    T2->>C: tts_audio_queue.put(LAST, [], None)
    C->>D: {"type":"tts","state":"stop","session_id":...}
    Note over C: 等待队列排空 + 预缓冲播放完
```

### 关键点

| 位置 | 说明 |
|---|---|
| **VAD 只决定何时开 ASR 长连接** | 本地 VAD 的 `client_voice_stop` 对流式 ASR 是死代码，判定权在火山云端 |
| **`definite: true` 才算说完** | 中间结果被静默丢弃，代码里连 debug 日志都没有 |
| **`chat()` 跑在 executor 线程** | 同步阻塞，持有 `ThreadPoolExecutor(5)` 的一个 worker |
| **TTS 前先缓冲全文** | `chat()` 把整个回复收完才分段入队，这是首音延迟的主要来源之一 |
| **音频下发有独立流控** | `AudioRateController` 按 60ms/帧精确控制，前 5 包直发降延迟 |

---

## 3. 工具调用（function_call）

当 LLM 返回 `tool_calls` 时走这条支线，会**递归再请求一次 LLM**。

```mermaid
sequenceDiagram
    autonumber
    participant C as ConnectionHandler
    participant LLM as DeepSeekLLM
    participant TH as UnifiedToolHandler
    participant EX as 具体Executor
    participant TTS as TTS线程
    participant D as 设备

    C->>LLM: response_with_functions(dialogue, tools=[7个])
    activate LLM
    LLM-->>C: delta.tool_calls = [{name:"get_weather", arguments:"..."}]
    deactivate LLM

    C->>C: tool_call_flag = True<br/>_merge_tool_calls(tool_calls_list)
    C->>C: content_arguments 以 <tool_call> 开头也视为工具调用

    C->>TH: handle_llm_function_call(conn, function_call_data)
    activate TH
    TH->>TH: 遍历已注册的 Executor<br/>找持有该工具的
    Note over TH: server_plugins / device_mcp<br/>device_iot / server_mcp / mcp_endpoint
    TH->>EX: executor.execute(conn, tool_name, arguments)
    activate EX
    Note over EX: 可能耗时：HTTP 请求、<br/>设备 MCP 往返、IoT 控制
    EX-->>TH: ActionResponse(action, result, response)
    deactivate EX
    TH-->>C: ActionResponse
    deactivate TH

    alt action == RESPONSE
        C->>TTS: tts_one_sentence(result.response)
        Note over TTS: 直接播报，不再问 LLM
    else action == REQLLM
        C->>C: dialogue.put(tool 结果)
        C->>C: chat(text, depth=depth+1) 递归
        Note over C: MAX_DEPTH = 5<br/>超过则禁用工具、强制直接回答
    else action == RECORD
        C->>C: 只写入 dialogue，不调 LLM
    else action == ERROR / NOTFOUND
        C->>TTS: 播报兜底话术
    end

    C->>C: _handle_function_result(tool_results, depth, streamed_text)

    Note over C,TTS: 工具调用结果最终仍会走语音播报
    TTS-->>D: 音频下发
```

### 关键点

- **工具执行有超时**：`tool_call_timeout` 默认 30 秒（[connection.py:1111](core/connection.py#L1111)）
- **支持并行工具调用**：`tool_calls_list` 收集多个，逐个 `future.result()` 等待
- **递归深度上限 5**：`chat(depth+1)`，到顶后追加系统提示强制直接回答
- **调试提示**：日志里看到多次「大模型收到用户消息」是正常的，说明发生了递归

---

## 4. 打断（Abort）

设备在 TTS 播放期间检测到用户说话时发送。

```mermaid
sequenceDiagram
    autonumber
    participant D as 设备
    participant C as ConnectionHandler
    participant LLM as LLM流
    participant T2 as TTS线程
    participant RC as AudioRateController

    Note over D: 用户打断了正在播放的回复
    D->>C: {"type":"abort", session_id:...}
    C->>C: handleAbortMessage()
    C->>C: close_after_chat = False
    C->>C: client_abort = True
    C->>C: clear_queues() 清空 asr/tts 队列

    C-->>D: {"type":"tts","state":"stop","session_id":...}
    C->>C: clearSpeakStatus()

    par 各环节感知中断
        C->>LLM: for response in llm_responses 循环内检查<br/>client_abort == True 则 break
    and
        T2->>T2: tts_text_queue.get 后检查 client_abort<br/>为真则 continue 丢弃
    and
        C->>RC: send_callback 内检查<br/>client_abort 则 raise CancelledError
    end

    Note over C,RC: 打断是"软"的：靠各环节主动轮询标志位<br/>没有强杀线程
```

> ⚠️ **打断不是强杀**。`client_abort` 是个标志位，LLM 流、TTS 线程、音频发送各自检查。
> 如果某个环节正阻塞在 IO 上（比如 LLM 的同步流式读取），要等它自己走到检查点才会停。

---

## 5. HTTP：设备 OTA

设备上电后先调 OTA 拿 WebSocket 地址。

```mermaid
sequenceDiagram
    autonumber
    participant D as 设备
    participant H as SimpleHttpServer :8003
    participant OTA as OTAHandler
    participant Auth as AuthManager

    D->>H: POST /xiaozhi/ota/<br/>Header: Device-Id, Client-Id<br/>Body: 设备信息 JSON
    activate H
    H->>OTA: handle_post(request)
    activate OTA

    OTA->>OTA: 解析 Body，取 device_id
    OTA->>OTA: 检查固件版本，是否需要升级
    alt 需要固件升级
        OTA->>D: firmware.url = /xiaozhi/ota/download/xxx.bin
    end

    OTA->>Auth: generate_token(client_id, device_id)
    Auth-->>OTA: JWT token
    OTA-->>D: {websocket:{url, token},<br/>server_time:{...},<br/>firmware:{...}}
    deactivate OTA
    deactivate H

    Note over D: 设备带着 token 去连 WebSocket

    D->>H: GET /xiaozhi/ota/download/xxx.bin
    H-->>D: 固件二进制流
```

> OTA 接口**只在 `read_config_from_api: false` 时注册**（[http_server.py:45](core/http_server.py#L45)）。
> 用了智控台的话，OTA 由智控台提供。

---

## 6. HTTP：视觉分析

设备拍完照后把图片 POST 过来。

```mermaid
sequenceDiagram
    autonumber
    participant D as 设备
    participant H as SimpleHttpServer :8003
    participant V as VisionHandler
    participant VLLM as ChatGLMVLLM
    participant 智谱 as 智谱AI

    D->>H: POST /mcp/vision/explain<br/>Header: Authorization Bearer <token><br/>Body: multipart (question + image)
    activate H
    H->>V: handle_post(request)
    activate V

    V->>V: _verify_auth_token(request)
    alt token 无效
        V-->>D: 401 无效的认证token或token已过期
    end

    V->>V: 校验 Device-Id 与 token 中的一致
    V->>V: 解析 multipart，取 question 和 image
    V->>V: 校验文件大小 MAX_FILE_SIZE
    V->>V: 校验图片格式 is_valid_image_file()

    alt read_config_from_api == true
        V->>V: 从智控台拉取该设备的 VLLM 配置
    end

    V->>V: base64 编码图片
    V->>VLLM: 调用视觉模型 (image_base64 + question)
    activate VLLM
    VLLM->>智谱: OpenAI 兼容协议请求
    智谱-->>VLLM: 文本回答
    VLLM-->>V: 回答
    deactivate VLLM

    V-->>D: {success:true, text:"..."}
    deactivate V
    deactivate H
```

---

## 7. 空闲超时与连接释放

```mermaid
sequenceDiagram
    autonumber
    participant D as 设备
    participant C as ConnectionHandler
    participant T as timeout_task
    participant Rep as reportHandle

    C->>T: asyncio.create_task(_check_timeout())
    activate T

    loop 周期性检查
        T->>C: 比对 last_activity_time
        Note over T: 每帧音频 / 每条消息都会刷新该时间戳
    end

    alt 超过 close_connection_no_voice_time（默认 120s）
        T->>C: close_after_chat = True
        T->>C: 触发结束语播放
        C->>D: 播报 end_prompt 结束语
    end

    D->>C: WS 断开
    deactivate T
    C->>C: _save_and_close()

    par 守护线程
        C->>C: 线程1：generate_and_save_chat_title()
        Note over C: 仅在 read_config_from_api 时有效<br/>本地模式下 _instance 为 None 会报错
    and
        C->>C: 线程2：memory.save_memory()
        Note over C: Memory=nomem 时不执行
    end

    C->>Rep: 上报对话记录 / TTS / ASR（若开启）
    C->>C: close(ws)
    C->>C: executor.shutdown(wait=False)
    C->>C: 释放 VAD / ASR / TTS 资源
    C->>C: 工具处理器 cleanup()
    Note over C: 日志：连接资源已释放
```

---

## 8. 完整状态机速查

把上面几条链路串起来，一次会话的生命周期：

```mermaid
stateDiagram-v2
    [*] --> 连接中: 设备发起 WS
    连接中 --> 已拒绝: 鉴权失败 / 缺 device-id
    已拒绝 --> [*]

    连接中 --> 握手中: 鉴权通过，new ConnectionHandler
    握手中 --> 就绪: 收到 hello，回 welcome_msg

    就绪 --> 聆听中: 持续收音频帧
    聆听中 --> 聆听中: VAD 判定无语音
    聆听中 --> 识别中: VAD 检测到人声，开 ASR 连接
    识别中 --> 聆听中: 云端未返回 definite

    识别中 --> 思考中: definite=true，出文本
    思考中 --> 播报中: LLM 流式返回，TTS 合成
    播报中 --> 思考中: 工具调用触发递归 chat
    播报中 --> 聆听中: tts stop，一轮结束
    播报中 --> 聆听中: 收到 abort，打断

    聆听中 --> 已释放: 空闲超时 / 设备断开
    已释放 --> [*]
```

---

## 附录：一次对话的关键日志对照

按上面 §2 的顺序，日志会依次出现（`grep` 关键字可用于排查）：

| 阶段 | 日志关键字 | 来源 |
|---|---|---|
| ASR 建连 | `正在连接ASR服务` | `core.providers.asr.doubao_stream` |
| ASR 就绪 | `收到初始化响应` | 同上 |
| 识别完成 | `识别到文本: xxx` | 同上 |
| 进入对话 | `大模型收到用户消息: xxx` | `core.connection` |
| 首段音频 | `发送第一段语音: xxx` | `core.handle.sendAudioHandle` |
| 音频下发 | `发送音频消息: SentenceType.FIRST` | 同上 |
| TTS 完成 | `语音生成成功: xxx，重试0次` | `core.providers.tts.base` |
| 延迟汇总 | `[LATENCY] ...` | `core.utils.latency` |
| 一轮结束 | `发送音频消息: SentenceType.LAST` | `core.handle.sendAudioHandle` |
| 连接释放 | `连接资源已释放` | `core.connection` |

> `[LATENCY]` 行需要先启用毫秒日志，见 [PERF_AUDIO_LATENCY.md §4 P3](PERF_AUDIO_LATENCY.md)。
