# 小智语音助手 - 客户端通讯接口文档

## 1. 概述

小智语音助手服务端通过 **WebSocket** 协议与硬件客户端进行双向实时通讯。服务器同时提供少量 **HTTP API** 用于 OTA 固件升级和视觉分析等场景。

### 1.1 通讯架构

```
┌──────────────┐                         ┌──────────────────┐
│   ESP32 设备  │ ◄══ WebSocket (双向) ═══► │  xiaozhi-server  │
│   (客户端)    │                         │   (Python 后端)   │
└──────────────┘                         └──────────────────┘
                                                  │
                                    ┌─────────────┴─────────────┐
                                    │                           │
                              HTTP API (OTA)             HTTP API (Vision)
                              端口: 8003                  端口: 8003
```

### 1.2 端口说明

| 端口 | 协议 | 用途 |
|------|------|------|
| 8000 | WebSocket | 主通讯通道，双向音频流 + 文本消息 |
| 8003 | HTTP | OTA 固件升级、视觉分析接口 |

---

## 2. WebSocket 连接

### 2.1 连接地址

```
ws://<服务器IP>:8000/xiaozhi/v1/
```

连接参数可通过 URL Query 传递（不推荐），也可通过 Header 传递（推荐）。

### 2.2 连接参数

| 参数 | 位置 | 必填 | 说明 |
|------|------|------|------|
| `device-id` | Header 或 Query | **是** | 设备的唯一标识符（MAC 地址或自定义 ID） |
| `client-id` | Header 或 Query | 否 | 客户端连接 ID，配合认证使用 |
| `Authorization` | Header 或 Query | 否 | 认证 Token，格式: `Bearer <token>` |
| `x-real-ip` / `x-forwarded-for` | Header | 否 | 反向代理时传递真实客户端 IP |

### 2.3 认证机制

服务器支持可选的 HMAC-SHA256 Token 认证。

#### Token 生成规则

```
Token = Base64URL(HMAC-SHA256(secret_key, "{client_id}|{device_id}|{timestamp}")).{timestamp}
```

- `secret_key`：服务端配置的密钥
- `timestamp`：Unix 时间戳（秒）
- 默认有效期：30 天（可在 `auth.expire_seconds` 配置）

#### 白名单模式

配置 `auth.allowed_devices` 白名单后，白名单内的设备可跳过 Token 校验，直接连接。

---

## 3. 消息通讯总览

### 3.1 消息方向

```
客户端 ─────────────────────────────────────► 服务端
  文本消息: JSON 字符串 (UTF-8)
  音频数据: 二进制 OPUS 编码 (可以是原始 opus 帧或带 16 字节头部)

服务端 ─────────────────────────────────────► 客户端
  控制消息: JSON 字符串 (UTF-8)
  音频数据: 二进制 OPUS 编码帧 (不含头部)
```

### 3.2 消息类型速查

| type | 方向 | 说明 |
|------|------|------|
| `hello` | C→S, S→C | 握手 / 连接确认 |
| `listen` | C→S | 拾音状态控制 |
| `abort` | C→S | 打断当前播放 / 识别 |
| `iot` | C→S | IoT 设备描述符与状态同步 |
| `mcp` | C↔S | MCP 协议消息（双向） |
| `server` | C→S, S→C | 服务端配置管理（智控台场景） |
| `ping` | C→S | 心跳保活 |
| `pong` | S→C | 心跳响应 |
| `tts` | S→C | TTS 状态通知 |
| `stt` | S→C | 语音识别结果 |
| `emotion` | S→C | 表情/情绪指令 |

---

## 4. 完整通讯流程（典型生命周期）

```
1. 客户端打开 WebSocket 连接 (携带 device-id 等 Header)
              │
2. 服务端认证 (可选) ── 失败 → 发送 "认证失败" 文本并关闭连接
              │ 成功
3. 服务端后台初始化 LLM/TTS/ASR/Memory 等组件
              │
4. ┌─────────────────────────────────────┐
   │ 客户端 → 服务端: {"type":"hello",...}  │
   │ 服务端 → 客户端: {"type":"hello",...}  │  ← 握手，确认 session_id、音频参数
   └─────────────────────────────────────┘
              │
5. ┌─────────────────────────────────────┐
   │    循环：拾音 / 识别 / LLM / TTS       │
   │                                      │
   │  客户端 → 服务端: 二进制 OPUS 音频流    │
   │  客户端 → 服务端: {"type":"listen",     │
   │                     "state":"stop"}    │
   │  服务端 → 客户端: {"type":"stt","text": │
   │                    "你好",...}         │
   │  服务端 → 客户端: {"type":"tts","state":│
   │                    "sentence_start",...}│
   │  服务端 → 客户端: 二进制 OPUS 音频流    │
   │  服务端 → 客户端: {"type":"tts","state":│
   │                    "stop",...}         │
   └─────────────────────────────────────┘
              │
6. 连接超时 / 主动关闭 → 保存记忆并断开
```

---

## 5. 客户端 → 服务端 消息详情

### 5.1 hello - 握手消息

客户端连接成功后首先发送此消息。

```json
{
  "type": "hello",
  "version": 1,
  "transport": "websocket",
  "audio_params": {
    "format": "opus",
    "sample_rate": 24000,
    "channels": 1,
    "frame_duration": 60
  },
  "features": {
    "mcp": true
  }
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `type` | string | 固定值 `"hello"` |
| `version` | int | 协议版本 |
| `transport` | string | 传输方式 |
| `audio_params.format` | string | 音频编码格式（opus） |
| `audio_params.sample_rate` | int | 采样率，支持 8000/12000/16000/24000/48000 |
| `audio_params.channels` | int | 声道数（1） |
| `audio_params.frame_duration` | int | 每帧时长（毫秒） |
| `features` | object | 客户端能力声明 |
| `features.mcp` | bool | 是否支持 MCP 协议 |

### 5.2 listen - 拾音控制消息

控制麦克风拾音状态，用于 VAD（语音活动检测）流程。

```json
// 开始拾音
{
  "type": "listen",
  "state": "start"
}

// 停止拾音（说话结束）
{
  "type": "listen",
  "state": "stop"
}

// 设备端已检测到语音（含识别文本）
{
  "type": "listen",
  "state": "detect",
  "text": "今天天气怎么样"
}

// 切换拾音模式
{
  "type": "listen",
  "state": "start",
  "mode": "auto"
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `type` | string | 固定值 `"listen"` |
| `state` | string | `"start"` - 开始拾音 / `"stop"` - 停止拾音 / `"detect"` - 设备端本地已识别 |
| `mode` | string | 可选。拾音模式：`"auto"`（自动，默认）/ `"manual"`（手动按键） |
| `text` | string | 仅在 `state=detect` 时有效，设备端本地识别的文本（如唤醒词） |

**状态机说明：**

```
state=start  → 设备进入录音模式，清除之前的音频缓冲
state=stop   → 设备说话结束，触发 ASR 识别（流式/非流式）
state=detect → 设备本地 VAD/ASR 已识别到语音内容，跳过服务端 VAD
```

**`mode` 说明：**
- `auto`：自动模式，新语音到达时自动打断当前播放
- `manual`：手动模式，新语音不会打断当前播放

### 5.3 abort - 打断消息

立即打断当前的 TTS 播放和 LLM 推理。

```json
{
  "type": "abort"
}
```

**效果：**
1. 服务端停止当前 LLM 推理流
2. 清空 TTS 待播放队列
3. 发送 `{"type":"tts","state":"stop"}` 给客户端
4. 重置客户端说话状态

### 5.4 iot - IoT 设备消息

用于同步 IoT 设备的描述符（能力声明）和状态。

#### 上报设备描述符

```json
{
  "type": "iot",
  "descriptors": [
    {
      "name": "light",
      "description": "智能灯泡",
      "properties": {
        "power": {
          "description": "开关状态",
          "type": "boolean"
        },
        "brightness": {
          "description": "亮度",
          "type": "number"
        }
      },
      "methods": {
        "set_power": {
          "description": "设置开关状态",
          "parameters": {
            "power": {
              "description": "开关",
              "type": "boolean"
            }
          }
        },
        "set_brightness": {
          "description": "设置亮度",
          "parameters": {
            "brightness": {
              "description": "亮度值 (0-100)",
              "type": "number"
            }
          }
        }
      }
    }
  ]
}
```

**字段说明：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `descriptors[].name` | string | 设备名称 |
| `descriptors[].description` | string | 设备描述 |
| `descriptors[].properties` | object | 设备属性定义 |
| `descriptors[].properties.<key>.description` | string | 属性说明 |
| `descriptors[].properties.<key>.type` | string | 属性类型: `"boolean"` / `"number"` / `"string"` |
| `descriptors[].methods` | object | 设备方法定义 |
| `descriptors[].methods.<key>.description` | string | 方法说明 |
| `descriptors[].methods.<key>.parameters` | object | 方法参数定义 |

#### 上报设备状态

```json
{
  "type": "iot",
  "states": [
    {
      "name": "light",
      "state": {
        "power": true,
        "brightness": 80
      }
    }
  ]
}
```

**流程说明：**
1. 客户端连接后发送 `descriptors`，注册设备能力（被 LLM function calling 使用）
2. 设备状态变化时发送 `states`，更新服务端状态缓存

### 5.5 mcp - MCP 协议消息

用于向设备端发送 MCP（Model Context Protocol）指令，反向管理设备上的工具。

```json
{
  "type": "mcp",
  "payload": {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/list",
    "params": {}
  }
}
```

**字段说明：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `type` | string | 固定值 `"mcp"` |
| `payload` | object | JSON-RPC 2.0 格式的 MCP 协议消息体 |

**注意：** 客户端需要在 hello 握手时声明 `features.mcp = true` 才会启用 MCP 功能。

#### 常见的 MCP 消息

**1) 初始化（服务端 → 客户端）**

当客户端声明支持 MCP 时，服务端自动发送：

```json
{
  "type": "mcp",
  "payload": {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
      "protocolVersion": "2024-11-05",
      "capabilities": {
        "roots": {"listChanged": true},
        "sampling": {},
        "vision": {
          "url": "http://<服务器IP>:8003/mcp/vision/explain",
          "token": "<JWT-Token>"
        }
      },
      "clientInfo": {
        "name": "XiaozhiClient",
        "version": "1.0.0"
      }
    }
  }
}
```

**2) 请求工具列表（服务端 → 客户端）**

```json
{
  "type": "mcp",
  "payload": {
    "jsonrpc": "2.0",
    "id": 2,
    "method": "tools/list"
  }
}
```

**3) 客户端响应工具列表（客户端 → 服务端）**

```json
{
  "type": "mcp",
  "payload": {
    "jsonrpc": "2.0",
    "id": 2,
    "result": {
      "tools": [
        {
          "name": "get_sensor_data",
          "description": "获取传感器数据",
          "inputSchema": {
            "type": "object",
            "properties": {
              "sensor_name": {
                "type": "string",
                "description": "传感器名称"
              }
            },
            "required": ["sensor_name"]
          }
        }
      ]
    }
  }
}
```

**4) 调用工具（服务端 → 客户端）**

```json
{
  "type": "mcp",
  "payload": {
    "jsonrpc": "2.0",
    "id": 3,
    "method": "tools/call",
    "params": {
      "name": "get_sensor_data",
      "arguments": {
        "sensor_name": "temperature"
      }
    }
  }
}
```

**5) 工具调用结果（客户端 → 服务端）**

```json
{
  "type": "mcp",
  "payload": {
    "jsonrpc": "2.0",
    "id": 3,
    "result": {
      "content": [
        {
          "type": "text",
          "text": "当前温度: 25.6°C"
        }
      ]
    }
  }
}
```

### 5.6 ping - 心跳保活

```json
{
  "type": "ping"
}
```

**说明：**
- 需要在配置中启用 `enable_websocket_ping: true`
- 服务端收到后回复 `pong`
- 发送后会更新 `last_activity_time`，延长超时计时器

### 5.7 server - 服务端管理消息（智控台场景）

仅当 `read_config_from_api: true` 时生效，用于管理后台动态更新服务端配置。

```json
{
  "type": "server",
  "action": "update_config",
  "content": {
    "secret": "<管理后台密钥>"
  }
}
```

---

## 6. 客户端 → 服务端 二进制音频数据

### 6.1 标准音频格式

客户端发送 **OPUS 编码的原始音频数据**（无包装头部），服务端通过 VAD 模块处理。

```
格式: OPUS 编码
采样率: 16000 或 24000 (由设备端决定)
声道: 单声道
帧时长: 60ms
```

### 6.2 MQTT 网关音频格式（带 16 字节头部）

当连接通过 MQTT 网关代理时，音频数据带有 16 字节二进制头部：

```
┌───────┬──────────┬───────────────────┬───────────────────┬───────────────────┐
│ Type  │ Reserved │  Payload Length   │     Sequence      │    Timestamp      │  OPUS Length  │
│ 1byte │  1byte   │      2 bytes      │     4 bytes       │     4 bytes       │    4 bytes     │
│  0x01 │   0x00   │    Big Endian     │    Big Endian     │    Big Endian     │   Big Endian   │
└───────┴──────────┴───────────────────┴───────────────────┴───────────────────┴───────────────────┘
   Byte 0  Byte 1    Bytes 2-3            Bytes 4-7          Bytes 8-11          Bytes 12-15
```

| 字段 | 偏移 | 大小 | 说明 |
|------|------|------|------|
| Type | 0 | 1 | 固定 0x01 |
| Reserved | 1 | 1 | 保留字节 |
| Payload Length | 2 | 2 | 后续带头部总数据长度 |
| Sequence | 4 | 4 | 序列号（用于乱序重排） |
| Timestamp | 8 | 4 | 时间戳 |
| OPUS Length | 12 | 4 | OPUS 音频数据实际长度 |

---

## 7. 服务端 → 客户端 消息详情

### 7.1 hello - 握手响应

```json
{
  "type": "hello",
  "version": 1,
  "transport": "websocket",
  "session_id": "a1b2c3d4-...",
  "audio_params": {
    "format": "opus",
    "sample_rate": 24000,
    "channels": 1,
    "frame_duration": 60
  }
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `type` | string | 固定值 `"hello"` |
| `session_id` | string | 本次会话唯一标识（UUID4），后续消息会附带 |
| `audio_params` | object | 服务端 TTS 输出音频参数，客户端应按此格式播放 |

### 7.2 tts - TTS 状态消息

```json
// TTS 开始播放
{
  "type": "tts",
  "state": "start",
  "session_id": "a1b2c3d4-..."
}

// 新句子开始（附带文本）
{
  "type": "tts",
  "state": "sentence_start",
  "text": "你好呀，今天天气不错！",
  "session_id": "a1b2c3d4-..."
}

// TTS 播放结束
{
  "type": "tts",
  "state": "stop",
  "session_id": "a1b2c3d4-..."
}
```

**状态流转：**

```
start → sentence_start → (音频流) → stop
  │                                       │
  └──── 如果有新句子 ──────────────────────┘
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `type` | string | 固定值 `"tts"` |
| `state` | string | `"start"` - 开始播放 / `"sentence_start"` - 新句子 / `"stop"` - 结束 |
| `text` | string | 仅在 `sentence_start` 时附带，当前句子的文本内容 |
| `session_id` | string | 会话 ID |

### 7.3 stt - 语音识别结果

```json
{
  "type": "stt",
  "text": "你好小智",
  "session_id": "a1b2c3d4-..."
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `type` | string | 固定值 `"stt"` |
| `text` | string | ASR 识别出的文本（已去除标点符号和表情符号） |
| `session_id` | string | 会话 ID |

**注意：** 收到 `stt` 消息后，紧接着会收到 `{"type":"tts","state":"start"}`，客户端应做好播放准备。

### 7.4 emotion - 表情指令

```json
{
  "type": "emotion",
  "emotion": "happy",
  "session_id": "a1b2c3d4-..."
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `emotion` | string | 表情标识：`happy` / `sad` / `surprised` / `angry` / `neutral` 等 |

### 7.5 pong - 心跳响应

```json
{
  "type": "pong",
  "timestamp": "2026-05-23 14:30:00"
}
```

### 7.6 mcp - MCP 协议消息（服务端 → 客户端）

格式与客户端发送的 MCP 消息一致，参见 5.5 节。

---

## 8. 服务端 → 客户端 二进制音频数据

### 8.1 TTS 音频流

服务端生成的 TTS 语音转为 OPUS 编码后发送给客户端。

```
格式: OPUS 编码
采样率: 24000 (由 xiaozhi.audio_params.sample_rate 配置)
声道: 单声道
帧时长: 60ms
数据包: 原始 OPUS 帧，无头部包装
```

### 8.2 音频流控制（服务端侧）

服务端使用 `AudioRateController` 进行流量控制：

- **预缓冲**：前 5 个音频包直接发送，减少首字延迟
- **流控**：后续音频包按帧时长（60ms）间隔发送，避免客户端缓冲区溢出
- **队列清空事件**：所有包发送完毕后等待预缓冲播放完成

---

## 9. HTTP API 接口

### 9.1 OTA 固件升级

#### 查询固件更新

```
GET /xiaozhi/ota/
```

**请求头：**

| Header | 说明 |
|--------|------|
| `Device-ID` | 设备唯一标识 |
| `Client-ID` | 客户端 ID |
| `Authorization` | `Bearer <token>`（如果启用了认证） |
| `User-Agent` | 设备型号/版本信息，格式: `model_version`（如 `esp32s3_1.2.3`） |

**请求参数（Query）：**

| 参数 | 说明 |
|------|------|
| `version` | 设备当前固件版本，格式: `x.y.z` |

**响应示例：**

```json
{
  "success": true,
  "message": "发现新版本",
  "data": {
    "version": "1.3.0",
    "url": "http://<服务器IP>:8003/xiaozhi/ota/download/esp32s3_1.3.0.bin",
    "size": 2097152
  }
}
```

#### 手动上传固件

```
POST /xiaozhi/ota/
```

Content-Type: `multipart/form-data`

| 字段 | 说明 |
|------|------|
| `file` | .bin 固件文件 |
| `model` | 设备型号（如 `esp32s3`） |
| `version` | 固件版本号 |

#### 下载固件

```
GET /xiaozhi/ota/download/{filename}
```

### 9.2 视觉分析接口

用于设备端的图像识别功能，设备通过 MCP 协议获取 Token 后调用此接口。

```
POST /mcp/vision/explain
```

**请求头：**

| Header | 说明 |
|--------|------|
| `Authorization` | `Bearer <token>`（由 MCP 初始化时下发） |
| `Device-Id` | 设备 ID |
| `Client-Id` | 客户端 ID |

**请求体：** `multipart/form-data`

| 字段 | 类型 | 说明 |
|------|------|------|
| `question` | string | 关于图片的问题（如 "这是什么？"） |
| `image` | file | 图片文件（支持 jpg/png/bmp，最大 5MB） |

**响应示例：**

```json
{
  "success": true,
  "message": "识别成功",
  "data": {
    "answer": "图片中是一只橘色的猫正趴在沙发上睡觉。"
  }
}
```

---

## 10. 错误处理

### 10.1 认证失败

服务端发送纯文本消息 `"认证失败"` 后关闭 WebSocket 连接。

### 10.2 超时断开

- **无语音活动超时**：默认 `close_connection_no_voice_time` 秒 + 额外 60 秒
- **连接期间**：每 10 秒检查一次，超时后服务端主动关闭连接
- **心跳保活**：客户端可通过 `{"type":"ping"}` 保持连接

### 10.3 TTS 播放异常

服务端 `tts` 消息中 `state=stop` 表示一轮播放结束，客户端应检查收到的音频数据完整性。

### 10.4 设备绑定

如果启用了智控台（`read_config_from_api: true`）且设备未绑定，所有消息会被丢弃，服务端会周期性播放绑定提示语音。

---

## 11. 配置参考（config.yaml 相关部分）

```yaml
server:
  ip: 0.0.0.0
  port: 8000
  http_port: 8003
  auth:
    enabled: false
    allowed_devices: ["11:22:33:44:55:66"]

xiaozhi:
  type: hello
  version: 1
  transport: websocket
  audio_params:
    format: opus
    sample_rate: 24000
    channels: 1
    frame_duration: 60

# 唤醒词列表
wakeup_words:
  - "你好小智"
  - "嘿你好呀"
  - "小爱同学"

# 退出命令
exit_commands:
  - "再见"
  - "拜拜"
  - "晚安"

# 对话历史滑动窗口（保留最近 N 轮完整对话）
max_history_rounds: 0  # 0 表示不限制

# WebSocket 心跳
enable_websocket_ping: false

# 无语音活动超时（秒）
close_connection_no_voice_time: 120

# 每日输出上限（字符数），0 表示不限
device_max_output_size: 0
```

---

## 12. 客户端实现 Checklist

- [ ] 建立 WebSocket 连接，传递 `device-id` Header
- [ ] 发送 `hello` 消息，声明 `audio_params` 和 `features`
- [ ] 接收服务端 `hello` 响应，记录 `session_id`
- [ ] 实现音频采集（MIC），OPUS 编码后以二进制发送
- [ ] 在检测到说话结束时发送 `{"type":"listen","state":"stop"}`
- [ ] 处理 `tts` 状态消息，管理播放器状态
- [ ] 接收二进制 OPUS 音频并播放
- [ ] 处理 `stt` 消息，显示识别文本
- [ ] 支持 `abort` 打断，发送后等待新的 `tts stop`
- [ ] （可选）发送 `ping` 心跳保活
- [ ] （可选）实现 MCP 协议，注册设备工具
- [ ] （可选）实现 IoT 描述符，声明设备能力
- [ ] 断线重连机制
