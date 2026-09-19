# xiaozhi-server 后端架构说明

> 本文面向新加入本项目的开发者，目标是读完能快速定位「一个功能应该改哪个文件」。

---

## 一、先说结论：这不是 api / service / dao 三层架构

如果你按传统 Java/Spring 或 Django 的分层经验来找 `service/`、`dao/`、`model/`、`repository/` 目录，会找不到——**这个项目根本没有这些目录，也没有数据库**。

它实际是：

| 特征 | 说明 |
|---|---|
| **事件驱动** | 核心是一个长连接 WebSocket 会话，靠消息/音频帧驱动，不是请求-响应式的 CRUD |
| **Provider 插件化** | VAD/ASR/LLM/TTS/Memory/Intent/VLLM 六类能力全部抽象成接口 + 动态加载实现，配置选型 |
| **工厂 + 注册表** | 组件靠 `create_instance()` 动态 import；消息靠 Registry 分发；工具靠 Executor 聚合 |
| **无持久层** | 无 DB、无 ORM。配置读 YAML 文件；需要远端数据时走 HTTP 客户端 |

用一句话概括：**这是一个「语音会话编排引擎」，不是「业务 CRUD 后端」。**

---

## 二、整体架构图

```
                        ┌─────────────────────────────────────┐
                        │            app.py  (入口)            │
                        │   asyncio.run(main()) → 两个并发任务   │
                        └──────────────┬──────────────────────┘
                                       │
                 ┌─────────────────────┴─────────────────────┐
                 │                                           │
        ┌────────▼─────────┐                       ┌─────────▼────────┐
        │ WebSocketServer  │                       │ SimpleHttpServer │
        │  :8000  /xiaozhi/v1/                     │  :8003  (aiohttp)│
        │  websockets 库    │                       │                  │
        └────────┬─────────┘                       └─────────┬────────┘
                 │                                           │
        每个连接 new 一个                             ┌───────┴────────┐
                 │                                    │ OTAHandler     │ /xiaozhi/ota/
                 ▼                                    │ VisionHandler  │ /mcp/vision/explain
      ┌──────────────────────┐                        └────────────────┘
      │  ConnectionHandler   │  ← 整个系统的核心（core/connection.py，1500+ 行）
      │  一个设备 = 一个实例   │
      └──────────┬───────────┘
                 │
    ┌────────────┼────────────┬──────────────┬──────────────┐
    │            │            │              │              │
┌───▼───┐  ┌────▼────┐  ┌────▼─────┐  ┌─────▼─────┐  ┌─────▼──────┐
│ handle│  │providers│  │plugins_  │  │  config   │  │   utils    │
│  消息  │  │ 能力层  │  │  func    │  │  配置层   │  │  工具库    │
│  处理  │  │(插件)   │  │  工具    │  │           │  │            │
└───────┘  └─────────┘  └──────────┘  └───────────┘  └────────────┘
```

---

## 三、目录结构与职责

```
xiaozhi-server/
├── app.py                      # 进程入口，拉起 WS + HTTP 两个 server
├── config.yaml                 # 默认配置（会被 data/.config.yaml 覆盖）
│
├── config/                     # ── 配置层 ──
│   ├── settings.py             # 配置加载入口、合法性校验
│   ├── config_loader.py        # YAML 读取 + 从智控台 API 拉配置
│   ├── manage_api_client.py    # 智控台 HTTP 客户端（单例）
│   └── logger.py               # loguru 封装，按连接绑定设备日志上下文
│
├── core/                       # ── 核心层 ──
│   ├── websocket_server.py     # WS 服务：鉴权、连接分发
│   ├── http_server.py          # HTTP 服务：路由注册
│   ├── connection.py           # ★ ConnectionHandler：单连接的编排中枢
│   ├── auth.py                 # JWT 鉴权（AuthManager）
│   │
│   ├── api/                    # ── HTTP 接口层 ──
│   │   ├── base_handler.py     # BaseHandler：CORS、OPTIONS 预检
│   │   ├── ota_handler.py      # 设备 OTA
│   │   └── vision_handler.py   # 视觉分析接口
│   │
│   ├── handle/                 # ── 消息/流程处理层 ──
│   │   ├── textMessageProcessor.py        # 文本消息分发入口
│   │   ├── textMessageHandlerRegistry.py  # 处理器注册表
│   │   ├── textHandler/                   # 各类文本消息的具体处理器
│   │   │   ├── helloMessageHandler.py
│   │   │   ├── listenMessageHandler.py    # 拾音模式切换
│   │   │   ├── abortMessageHandler.py     # 打断
│   │   │   ├── iotMessageHandler.py
│   │   │   ├── mcpMessageHandler.py
│   │   │   ├── pingMessageHandler.py      # 心跳保活
│   │   │   └── serverMessageHandler.py
│   │   ├── receiveAudioHandle.py          # 音频入口：VAD → ASR → 进对话
│   │   ├── sendAudioHandle.py             # 音频出口：流控 → 下发 opus
│   │   ├── intentHandler.py               # 意图识别 / function_call 分支
│   │   ├── helloHandle.py                 # 唤醒词、开场白
│   │   ├── abortHandle.py                 # 打断清理
│   │   └── reportHandle.py                # 对话/工具调用上报（智控台）
│   │
│   ├── providers/              # ── 能力层（插件）──
│   │   ├── vad/                #   SileroVAD 等
│   │   ├── asr/                #   Doubao/FunASR/Aliyun…
│   │   ├── llm/                #   DeepSeek/Doubao/ChatGLM/Ollama…
│   │   ├── tts/                #   EdgeTTS/Doubao/IndexTTS…
│   │   ├── memory/             #   nomem / mem0ai / mem_local_short
│   │   ├── intent/             #   nointent / intent_llm / function_call
│   │   ├── vllm/               #   视觉模型
│   │   └── tools/              #   工具执行器（见第六节）
│   │
│   └── utils/                  # ── 基础工具 ──
│       ├── modules_initialize.py   # 组件初始化总入口
│       ├── tts.py / llm.py / asr.py / vad.py / intent.py / memory.py
│       │                           # 各 Provider 的工厂 create_instance()
│       ├── dialogue.py             # 对话历史管理
│       ├── textUtils.py            # 文本清洗、机器人回复解析
│       ├── audioRateController.py  # 音频发送速率控制
│       └── gc_manager.py           # 全局 GC
│
└── plugins_func/               # ── 工具函数（LLM 可调用）──
    ├── register.py             # ToolType / Action / ActionResponse / 注册表
    ├── loadplugins.py          # 按配置加载
    └── functions/              # get_weather / play_music / change_role …
```

---

## 四、和传统 api / service / dao 的对照

| 传统分层 | 本项目对应物 | 差异说明 |
|---|---|---|
| **API / Controller** | `core/api/*_handler.py`（HTTP）<br>`core/handle/textHandler/*.py`（WS 消息） | 确实是入口层。但 WS 是**长连接双向流**，主要数据通道是二进制音频帧，不走「一个请求一个响应」 |
| **Service** | `core/connection.py` + `core/handle/*Handle.py` | 存在，但形态完全不同：不是无状态的业务方法，而是**有状态的会话对象**。`ConnectionHandler` 持有 VAD/ASR/LLM/TTS/对话历史/工具句柄等全部状态，生命周期与设备连接绑定 |
| **DAO / Repository / Model** | **不存在** | 无数据库、无 ORM（`requirements.txt` 里没有 sqlalchemy/pymysql/redis/pymongo）。 |
| **外部依赖调用** | `core/providers/` + `config/manage_api_client.py` | Provider 层承担了传统 Service 里「调用第三方」的职责，但做成了可插拔接口 |

### 那「数据」存在哪？

| 数据类型 | 存储方式 |
|---|---|
| 服务配置 | `config.yaml`（默认）+ `data/.config.yaml`（覆盖，**实际生效的**） |
| 设备差异化配置 | 智控台 API，连接建立时拉取（`read_config_from_api: true` 时） |
| 对话历史 | 内存，`core/utils/dialogue.py` 挂在 `ConnectionHandler` 上，连接断开即释放 |
| 长期记忆 | 走 Memory Provider（mem0ai 等第三方），本项目不落地 |
| 对话记录 / 工具调用记录 | HTTP 上报给智控台（`core/handle/reportHandle.py` → `ManageApiClient`） |

> ⚠️ **`config/manage_api_client.py` 是本项目唯一的「数据访问层」**，但它是**写出去**的（上报），不是读进来做 CRUD。它是单例（`_instance` + `__new__`），内部用 httpx AsyncClient 带重试。
>
> 注意它只在 `read_config_from_api: true` 时才被 `init_service()` 初始化。若走本地 YAML 配置，`_instance` 恒为 `None`，此时调用 `generate_and_save_chat_title()` 会抛 `'NoneType' object has no attribute '_execute_async_request'`——**已知问题，缺少判空**（`report()` 有判空，它没有）。

---

## 五、核心链路：一次语音对话的完整时序

这是理解整个项目的关键路径。以 `DoubaoStreamASR + DeepSeekLLM + EdgeTTS` 为例：

```
设备                 WebSocketServer        ConnectionHandler             云服务
 │                        │                       │                          │
 │─① WS 连接 + device-id ─▶│                       │                          │
 │                        │─ AuthManager 校验 ─┐   │                          │
 │                        │◀──────────────────┘   │                          │
 │                        │─ new ConnectionHandler ─▶│                        │
 │                        │                       │─ 后台初始化组件            │
 │                        │                       │  (_initialize_components) │
 │                        │                       │  ├ 拉设备配置（可选）       │
 │                        │                       │  └ 建 TTS/工具线程         │
 │◀──────── hello 握手（带 audio_params）─────────│                          │
 │                        │                       │                          │
 │─② opus 音频帧（60ms/帧，持续）─────────────────▶│                          │
 │                        │                  _route_message                   │
 │                        │                       │─ handleAudioMessage       │
 │                        │                       │  ├ VAD 判定 have_voice     │
 │                        │                       │  └ ASR.receive_audio ─────▶│ 火山 ASR
 │                        │                       │                          │ (云端 VAD 断句)
 │                        │                       │◀── definite: true ────────│
 │                        │                       │                          │
 │                        │                  handle_voice_stop               │
 │                        │                       │─ startToChat              │
 │                        │                       │  ├ intent 判定             │
 │                        │                       │  └ executor.submit(chat)  │
 │                        │                       │                          │
 │                        │                       │─ chat() ─────────────────▶│ LLM 流式
 │                        │                       │◀── token 流 ──────────────│
 │                        │                       │                          │
 │                        │                       │─ 文本入 tts_text_queue     │
 │                        │                       │  TTS 线程合成 ───────────▶│ 云 TTS
 │◀──③ opus 音频 + tts 消息───────────────────────│  AudioRateController 流控  │
 │                        │                       │                          │
```

### 三个必须知道的角色

**1. `WebSocketServer`（全局唯一）**
启动时 `initialize_modules()` 初始化一次 Provider 实例，所有连接共享。每来一个连接 new 一个 `ConnectionHandler`。

**2. `ConnectionHandler`（每连接一个）**
系统的真正中枢，[core/connection.py](core/connection.py)。持有：
- Provider 引用（`self.vad / asr / llm / memory / intent`）
- 会话状态（`self.dialogue`、`self.sentence_id`、VAD 状态、音频缓冲）
- `self.executor`（`ThreadPoolExecutor(max_workers=5)`）
- `self.func_handler`（工具聚合器）

**3. `core/handle/` 下的流程函数**
注意它们是**接收 `conn` 参数的普通函数**，不是类方法。这是本项目的刻意设计——`ConnectionHandler` 只做状态持有和编排，具体流程拆成独立函数，避免单文件膨胀。

---

## 六、Provider 插件机制

这是项目最重要的扩展点。六类能力（VAD/ASR/LLM/TTS/Memory/Intent/VLLM）走同一套模式：

### 1. 配置选型

```yaml
selected_module:
  ASR: DoubaoStreamASR      # ← 选哪个
  LLM: DeepSeekLLM
  TTS: EdgeTTS

ASR:
  DoubaoStreamASR:          # ← 该实现的参数
    type: doubao_stream     # ← 决定加载哪个 py 文件
    appid: xxx
```

### 2. 抽象基类

`core/providers/<类型>/base.py` 里定义 ABC，例如 [core/providers/tts/base.py](core/providers/tts/base.py) 的 `TTSProviderBase`。

### 3. 动态加载工厂

`core/utils/<类型>.py` 里的 `create_instance()`：

```python
def create_instance(class_name, *args, **kwargs):
    if os.path.exists(os.path.join('core', 'providers', 'tts', f'{class_name}.py')):
        lib_name = f'core.providers.tts.{class_name}'
        if lib_name not in sys.modules:
            sys.modules[lib_name] = importlib.import_module(lib_name)
        return sys.modules[lib_name].TTSProvider(*args, **kwargs)
    raise ValueError(f"不支持的TTS类型: {class_name}...")
```

### 4. 统一初始化

[core/utils/modules_initialize.py](core/utils/modules_initialize.py) 的 `initialize_modules()` 是唯一入口。

### 怎么加一个新 Provider

1. 在 `core/providers/tts/` 下新建 `mytts.py`，定义 `TTSProvider(TTSProviderBase)`，实现 `text_to_speak()`
2. 在 `config.yaml` 的 `TTS` 段加 `MyTTS: {type: mytts, ...}`
3. 把 `selected_module.TTS` 改成 `MyTTS`

**不需要改任何核心代码**——这就是插件化的意义。

> 注意：LLM 有点特殊，它的目录结构是 `core/providers/llm/<name>/<name>.py`（多一层），工厂里的路径拼接也对应不同。

---

## 七、工具（Function Calling）体系

LLM 能调用的一切能力都在这里。这是本项目**改动最频繁、也最容易出问题**的模块。

### 三层结构

```
plugins_func/register.py          ← 工具定义与注册（ToolType / Action / ActionResponse）
    ▲
    │ 注册
core/providers/tools/
    ├── unified_tool_manager.py   ← 管理器：持有多个 Executor
    ├── unified_tool_handler.py   ← 聚合器：合并工具描述、分发调用、合并结果
    └── <各种>_executor.py        ← 执行器（实现 ToolExecutor ABC）
        ├── server_plugins/       ← 服务端插件（get_weather、play_music…）
        ├── server_mcp/           ← 接入的外部 MCP 服务
        ├── device_mcp/           ← 设备端 MCP 工具（如 capture_photo）
        ├── device_iot/           ← IoT 设备控制
        └── mcp_endpoint/         ← MCP 接入点
```

### 关键抽象

- **`ToolExecutor`**（[core/providers/tools/base/tool_executor.py](core/providers/tools/base/tool_executor.py)）：ABC，三个方法 `execute()` / `get_tools()` / `has_tool()`
- **`Action`**（`plugins_func/register.py`）：工具执行后如何继续
  - `RESPONSE` 直接回复用户
  - `REQLLM` 拿到结果后再请求一次 LLM 生成回复
  - `RECORD` 只记录到对话历史，不调 LLM
  - `ERROR` / `NOTFOUND` / `NONE`
- **`UnifiedToolHandler.get_functions()`**：把所有 Executor 的工具描述合并成一个 `tools` 数组，塞进 LLM 请求

### 一次工具调用的流程

```
LLM 流式返回 tool_calls
  └→ connection.chat() 收集 tool_calls_list        [connection.py:~1093]
      └→ func_handler.handle_llm_function_call()
          └→ 找到持有该工具的 Executor → executor.execute()
              └→ 返回 ActionResponse(action=...)
                  └→ _handle_function_result() 按 action 分支
                      ├─ RESPONSE → tts_one_sentence() 直接播
                      ├─ REQLLM   → chat(depth+1) 递归再问 LLM
                      └─ RECORD   → 写入 dialogue
```

> ⚠️ `chat()` 支持递归（`MAX_DEPTH = 5`），用于工具调用后的二次生成。调试时看到「大模型收到用户消息」出现多次是正常的。

---

## 八、配置体系

### 三层优先级

```
data/.config.yaml   ← 实际生效（本地开发改这个）
        ↓ 缺失的键
config.yaml         ← 默认值仓库
        ↓
智控台 API          ← read_config_from_api: true 时整体接管
```

[config/settings.py](config/settings.py) 的 `check_config_file()` 会校验：如果 `read_config_from_api: true` 但 `.config.yaml` 里又有 `selected_module`，直接报错并提示你用 `config_from_api.yaml` 模板。

### 配置驱动的行为

很多逻辑不是写死的，而是读配置分支：

| 配置项 | 影响 |
|---|---|
| `selected_module.*` | 选 Provider 实现 |
| `Intent` 的 type | 决定是否走 `function_call` 还是前置意图识别 |
| `delete_audio` | TTS 是走内存字节流还是落地临时文件 |
| `close_connection_no_voice_time` | 无语音多久断连（默认 120s） |
| `max_history_rounds` | 对话历史截断轮数（0 = 不截断） |
| `enable_websocket_ping` | WS 心跳保活 |

---

## 九、并发模型（重要）

这是本项目最容易踩坑的地方，也是性能问题的常见根源。

```
主线程：asyncio 事件循环
  ├─ WebSocket 收发（websockets 库）
  ├─ HTTP 服务（aiohttp）
  └─ 各连接注册的协程任务

每个 ConnectionHandler 额外有：
  ├─ ThreadPoolExecutor(max_workers=5)     ← 同步阻塞任务的避难所
  │   └─ 跑 conn.chat()、工具调用、上报
  ├─ ASR 消费线程（queue.Queue）            ← core/providers/asr/base.py
  ├─ TTS 文本消费线程（queue.Queue）        ← core/providers/tts/base.py
  ├─ TTS 音频播放线程（queue.Queue）
  └─ AudioRateController 的发送协程         ← 60ms 精度的音频流控
```

### 跨线程通信规则

| 方向 | 方式 |
|---|---|
| 线程 → 事件循环 | `asyncio.run_coroutine_threadsafe(coro, conn.loop)` |
| 事件循环 → 线程 | `conn.executor.submit(fn, *args)` |
| 音频下发 | `AudioRateController.add_audio()` → 后台协程按 60ms/帧节奏发 |

### 为什么这么复杂

音频流有**严格的时间要求**：Opus 60ms 一帧，发快了设备端缓冲溢出，发慢了卡顿。但 ASR/TTS 是阻塞式 IO。所以用「线程池跑阻塞 IO + 队列解耦 + 独立协程做精确流控」的组合。

> ⚠️ **已知陷阱**：`conn.chat()` 在 executor 线程里执行，而它是**同步阻塞**的（`openai` 同步客户端流式迭代）。它持有 `max_workers=5` 中的一个 worker。如果工具调用里再 `future.result()` 等待事件循环，就容易出现线程饥饿和互相等待。排查「响应慢」时这里是第一嫌疑点。

---

## 十、快速定位表

| 我要改… | 去哪 |
|---|---|
| 加一个新的 ASR/TTS/LLM 厂商 | `core/providers/<类型>/` + `config.yaml`，见第六节 |
| 加一个 LLM 能调用的工具 | `plugins_func/functions/` 新建 py + `Intent.function_call.functions` 里挂上 |
| 改对话流程（进 LLM 前做什么） | `core/handle/intentHandler.py`、`core/handle/receiveAudioHandle.py` |
| 改音频下发节奏 / 延迟 | `core/handle/sendAudioHandle.py`、`core/utils/audioRateController.py` |
| 改 TTS 断句规则 | `core/providers/tts/base.py` 的 `_get_segment_text()` / `first_sentence_punctuations` |
| 改 VAD 灵敏度 | `config.yaml` → `VAD.SileroVAD`，逻辑在 `core/providers/vad/silero.py` |
| 加 HTTP 接口 | `core/api/` 新建 handler + `core/http_server.py` 注册路由 |
| 加 WS 消息类型 | `core/handle/textHandler/` 新建 + `textMessageHandlerRegistry.py` 注册 |
| 改设备鉴权 | `core/auth.py` + `WebSocketServer._handle_auth()` |
| 对接/修改智控台上报 | `config/manage_api_client.py` + `core/handle/reportHandle.py` |

---

## 十一、已知问题清单

| 问题 | 位置 | 说明 |
|---|---|---|
| 首音延迟 2–3s | `core/connection.py` `chat()` | LLM 流式输出被整段缓冲，直到生成完才推 TTS。原实现是逐 token 推送 |
| 聊天标题上报崩溃 | `config/manage_api_client.py:210` | `generate_and_save_chat_title()` 缺 `_instance` 判空，本地配置模式下必现 |
| 本地 VAD 结束判定空转 | `core/providers/vad/silero.py:109` | `client_voice_stop` 被设置但流式 ASR 不读取（`asr/base.py:76` 排除了 STREAM），导致 `min_silence_duration_ms` 配置无效 |
| 日志无毫秒 | `config/logger.py` | `{time:YYMMDD HH:mm:ss}`，排查延迟问题困难，建议加 `.SSS` |

---

## 十二、技术栈速查

| 领域 | 选型 |
|---|---|
| 运行时 | Python 3.10 |
| WS 服务 | `websockets` 14.2 |
| HTTP 服务 | `aiohttp` 3.13 |
| 日志 | `loguru` |
| 配置 | `ruamel.yaml` |
| 音频编解码 | `opuslib_next`（Opus）+ `pydub`/ffmpeg（MP3/WAV 转换） |
| VAD | `onnxruntime` + Silero VAD ONNX |
| LLM 客户端 | `openai`（**同步**客户端，兼容 OpenAI 协议的各家） |
| 鉴权 | `PyJWT` |
| MCP | `mcp` / `mcp-proxy` |
