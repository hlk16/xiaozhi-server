# MCP 客户端对接指南

本文档面向 Android / ESP32 / 其他客户端开发者，说明如何通过 MCP（Model Context Protocol）协议与小智服务端对接，实现设备端工具注册、工具调用、视觉识别等能力。

## 1. 前置条件

- 服务端版本：支持 MCP 的 xiaozhi-esp32-server
- 通讯方式：WebSocket（与现有语音通讯共用同一个连接）
- 服务端已配置视觉模型（VLLM 模块），如果需要视觉识别功能

## 2. 总体流程

```
客户端                                    服务端
  │                                         │
  │  ① WebSocket 连接                        │
  ├────────────────────────────────────────► │
  │                                         │
  │  ② 发送 hello（声明 features.mcp=true）   │
  ├────────────────────────────────────────► │
  │                                         │
  │  ③ 收到 hello 响应（含 session_id）       │
  │ ◄─────────────────────────────────────┤
  │                                         │
  │  ④ 收到 MCP initialize                   │
  │     （含 vision.url、vision.token）       │
  │ ◄─────────────────────────────────────┤
  │                                         │
  │  ⑤ 回复 initialize result                │
  ├────────────────────────────────────────► │
  │                                         │
  │  ⑥ 收到 tools/list 请求                  │
  │ ◄─────────────────────────────────────┤
  │                                         │
  │  ⑦ 回复 tools 列表                       │
  ├────────────────────────────────────────► │
  │                                         │
  │  ... MCP 就绪，进入正常对话 ...           │
  │                                         │
  │  ⑧ 当 LLM 需要调用设备工具时：            │
  │  ◄─────────── tools/call ──────────────┤
  │                                         │
  │  ⑨ 执行工具，回复 result                  │
  ├────────────────────────────────────────► │
  │                                         │
```

**关键点**：MCP 消息全部封装在 WebSocket 的文本消息中，`type` 字段固定为 `"mcp"`，实际协议内容在 `payload` 字段里。

## 3. 协议格式

### 3.1 WebSocket 包装层

所有 MCP 消息都是 JSON 文本，通过 WebSocket 发送，外层格式统一为：

```json
{
  "type": "mcp",
  "payload": { ... }
}
```

`payload` 内部遵循 JSON-RPC 2.0 规范。

### 3.2 JSON-RPC 2.0 消息结构

**请求（服务端 → 客户端）：**

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "initialize",
  "params": { ... }
}
```

**响应（客户端 → 服务端）：**

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": { ... }
}
```

**错误响应（客户端 → 服务端）：**

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "error": {
    "code": -1,
    "message": "错误描述"
  }
}
```

### 3.3 id 值约定

服务端使用以下 `id` 值来区分消息：

| id | 用途 |
|----|------|
| `1` | MCP 初始化（initialize） |
| `2` | 工具列表请求/响应（tools/list） |
| `≥3` | 工具调用（tools/call），id 由服务端自增分配 |

## 4. 详细对接步骤

### Step 1：连接时声明 MCP 支持

在 WebSocket 握手阶段，通过 hello 消息的 `features.mcp` 字段声明支持 MCP：

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

`features.mcp = true` 是启用 MCP 功能的**唯一开关**。如果不设置或设为 false，服务端不会发送任何 MCP 消息。

### Step 2：接收并回复 MCP initialize

服务端收到带 MCP 声明的 hello 后，会自动发送一条 `initialize` 请求，**其中包含视觉 API 的地址和认证 Token**。

**服务端发来的消息：**

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
        "roots": { "listChanged": true },
        "sampling": {},
        "vision": {
          "url": "http://192.168.1.5:8003/mcp/vision/explain",
          "token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."
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

**客户端必须做两件事：**

1. **保存** `params.capabilities.vision.url` 和 `params.capabilities.vision.token`，后续拍照上传时使用
2. **回复** initialize result

**客户端回复：**

```json
{
  "type": "mcp",
  "payload": {
    "jsonrpc": "2.0",
    "id": 1,
    "result": {
      "protocolVersion": "2024-11-05",
      "capabilities": {},
      "serverInfo": {
        "name": "MyAndroidClient",
        "version": "1.0.0"
      }
    }
  }
}
```

> **注意**：`id` 必须与请求中的 `id`（即 `1`）保持一致。

### Step 3：接收 tools/list 并回复工具列表

服务端收到 initialize result 后，会发送 `tools/list` 请求，询问客户端支持哪些工具。

**服务端发来的消息：**

```json
{
  "type": "mcp",
  "payload": {
    "jsonrpc": "2.0",
    "id": 2,
    "method": "tools/list",
    "params": {}
  }
}
```

**客户端回复（示例：注册一个拍照工具）：**

```json
{
  "type": "mcp",
  "payload": {
    "jsonrpc": "2.0",
    "id": 2,
    "result": {
      "tools": [
        {
          "name": "capture_photo",
          "description": "拍摄一张照片并识别图片中的内容，返回文字描述",
          "inputSchema": {
            "type": "object",
            "properties": {},
            "required": []
          }
        }
      ]
    }
  }
}
```

**如果工具很多支持分页**，可以在 result 中加入 `nextCursor`：

```json
{
  "type": "mcp",
  "payload": {
    "jsonrpc": "2.0",
    "id": 2,
    "result": {
      "tools": [ ... ],
      "nextCursor": "page2_cursor_value"
    }
  }
}
```

服务端收到 `nextCursor` 后会再次发送 `tools/list` 并带上 `params.cursor`，客户端继续返回下一页工具。

当所有工具都返回完毕（不带 `nextCursor`），MCP 进入就绪状态。

### Step 4：处理 tools/call 工具调用

当用户在对话中提出需要设备端执行的操作时（如"帮我看一下这是什么"），服务端的 LLM 会决定调用客户端注册的工具，通过 `tools/call` 消息下发。

**服务端发来的消息：**

```json
{
  "type": "mcp",
  "payload": {
    "jsonrpc": "2.0",
    "id": 3,
    "method": "tools/call",
    "params": {
      "name": "capture_photo",
      "arguments": {}
    }
  }
}
```

**客户端执行工具后回复：**

成功时：

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
          "text": "图片中是一只橘色的猫趴在沙发上"
        }
      ]
    }
  }
}
```

失败时：

```json
{
  "type": "mcp",
  "payload": {
    "jsonrpc": "2.0",
    "id": 3,
    "result": {
      "isError": true,
      "error": "摄像头初始化失败"
    }
  }
}
```

> **重要**：`id` 必须与 tools/call 请求中的 id 一致，服务端靠它匹配请求和响应。

## 5. 视觉识别完整示例

下面是一个完整的"拍照识别"流程的客户端伪代码：

```
收到 tools/call(name="capture_photo")
  │
  ├── 1. 调用 Android CameraX 拍照
  │     val photoBytes = cameraCapture()
  │
  ├── 2. 用保存的 visionToken 和 visionUrl 发 HTTP 请求
  │     POST http://192.168.1.5:8003/mcp/vision/explain
  │     Headers:
  │       Authorization: Bearer <visionToken>
  │       Device-Id: <你的设备ID>
  │     Body (multipart/form-data):
  │       question: "请描述这张图片中的内容"
  │       image: <photoBytes> (jpg/png)
  │
  ├── 3. 解析 HTTP 响应
  │     {"success":true, "data":{"answer":"图片中是一只猫..."}}
  │
  └── 4. 通过 MCP 回复结果
        {"type":"mcp", "payload":{...,"result":{"content":[{"type":"text","text":"图片中是一只猫..."}]}}}
```

### HTTP 请求详情

```
POST <vision.url> HTTP/1.1
Content-Type: multipart/form-data; boundary=----Boundary
Authorization: Bearer <vision.token>
Device-Id: <device_id>
Client-Id: <client_id>

------Boundary
Content-Disposition: form-data; name="question"

请描述这张图片中的内容
------Boundary
Content-Disposition: form-data; name="image"; filename="photo.jpg"
Content-Type: image/jpeg

<二进制图片数据>
------Boundary--
```

**HTTP 响应：**

```json
{
  "success": true,
  "action": "RESPONSE",
  "response": "图片中显示的是一个晴朗的天空，远处有几朵白云。"
}
```

### 视觉 API 限制

| 项目 | 限制 |
|------|------|
| 图片大小 | 最大 5MB |
| 支持格式 | JPEG、PNG、GIF、BMP、TIFF、WEBP |
| Token 有效期 | 1 小时 |
| 认证方式 | Bearer Token（由服务端 MCP initialize 时下发） |

## 6. 工具定义规范

客户端注册的工具需要遵循以下 schema：

```json
{
  "name": "工具名称",
  "description": "工具功能描述（LLM 会根据这段文字决定何时调用此工具）",
  "inputSchema": {
    "type": "object",
    "properties": {
      "参数名": {
        "type": "string",
        "description": "参数说明"
      }
    },
    "required": ["必填参数名"]
  }
}
```

### 工具命名规则

- 只能包含：英文字母、数字、下划线 `_`、连字符 `-`、中文
- 其他字符会被服务端自动替换为下划线
- 建议使用 `snake_case` 格式，如 `capture_photo`、`get_temperature`

### 多个工具示例

```json
{
  "tools": [
    {
      "name": "capture_photo",
      "description": "拍摄一张照片并识别内容",
      "inputSchema": {
        "type": "object",
        "properties": {},
        "required": []
      }
    },
    {
      "name": "get_sensor_data",
      "description": "获取设备传感器的当前数据",
      "inputSchema": {
        "type": "object",
        "properties": {
          "sensor_name": {
            "type": "string",
            "description": "传感器名称，如 temperature、humidity"
          }
        },
        "required": ["sensor_name"]
      }
    },
    {
      "name": "set_led",
      "description": "控制设备 LED 灯的开关和颜色",
      "inputSchema": {
        "type": "object",
        "properties": {
          "power": {
            "type": "boolean",
            "description": "开关状态"
          },
          "color": {
            "type": "string",
            "description": "颜色，如 red、green、#FF0000"
          }
        },
        "required": ["power"]
      }
    }
  ]
}
```

## 7. 客户端消息处理状态机

```
┌──────────────────┐
│   等待 WebSocket  │
│      连接         │
└────────┬─────────┘
         │ 连接成功
         ▼
┌──────────────────┐
│   发送 hello      │
│  (features.mcp)  │
└────────┬─────────┘
         │
         ▼
┌──────────────────┐     收到 id=1
│  等待 initialize  │ ───────────► 保存 vision URL/Token
└────────┬─────────┘              回复 initialize result
         │
         ▼
┌──────────────────┐     收到 id=2
│  等待 tools/list  │ ───────────► 回复工具列表
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│   MCP 就绪        │◄─────────── 服务端 set_ready
│  等待 tools/call  │              工具可被 LLM 调用
└────────┬─────────┘
         │ 收到 tools/call
         ▼
┌──────────────────┐
│   执行工具        │
│  (拍照/控制设备)   │
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│   回复 result     │
│  (content/error)  │
└────────┬─────────┘
         │
         └──────► 返回等待 tools/call
```

## 8. Android Kotlin 参考实现

### 8.1 MCP 管理器

```kotlin
class McpManager(
    private val scope: CoroutineScope,
    private val onVisionCapture: suspend (question: String) -> String
) {
    private var visionUrl: String? = null
    private var visionToken: String? = null
    private var nextId = 3

    private val pendingCalls = ConcurrentHashMap<Int, CompletableDeferred<String>>()

    fun handleMcpMessage(payload: JSONObject): JSONObject? {
        return when {
            payload.has("method") -> handleRequest(payload)
            payload.has("result") -> handleResponse(payload)
            payload.has("error") -> handleError(payload)
            else -> null
        }
    }

    private fun handleRequest(payload: JSONObject): JSONObject? {
        val id = payload.getInt("id")
        val method = payload.getString("method")

        return when (method) {
            "initialize" -> {
                val capabilities = payload.getJSONObject("params")
                    .getJSONObject("capabilities")
                if (capabilities.has("vision")) {
                    val vision = capabilities.getJSONObject("vision")
                    visionUrl = vision.getString("url")
                    visionToken = vision.getString("token")
                }
                JSONObject().apply {
                    put("jsonrpc", "2.0")
                    put("id", id)
                    put("result", JSONObject().apply {
                        put("protocolVersion", "2024-11-05")
                        put("capabilities", JSONObject())
                        put("serverInfo", JSONObject().apply {
                            put("name", "AndroidClient")
                            put("version", "1.0.0")
                        })
                    })
                }
            }
            "tools/list" -> {
                JSONObject().apply {
                    put("jsonrpc", "2.0")
                    put("id", id)
                    put("result", JSONObject().apply {
                        put("tools", JSONArray().apply {
                            put(JSONObject().apply {
                                put("name", "capture_photo")
                                put("description", "拍摄一张照片并识别图片内容")
                                put("inputSchema", JSONObject().apply {
                                    put("type", "object")
                                    put("properties", JSONObject())
                                    put("required", JSONArray())
                                })
                            })
                        })
                    })
                }
            }
            "tools/call" -> {
                val params = payload.getJSONObject("params")
                val toolName = params.getString("name")
                scope.launch {
                    val result = executeTool(toolName, id)
                    sendMcpMessage(result)
                }
                null
            }
            else -> null
        }
    }

    private suspend fun executeTool(toolName: String, requestId: Int): JSONObject {
        return try {
            when (toolName) {
                "capture_photo" -> {
                    val answer = onVisionCapture("请描述这张图片中的内容")
                    JSONObject().apply {
                        put("jsonrpc", "2.0")
                        put("id", requestId)
                        put("result", JSONObject().apply {
                            put("content", JSONArray().apply {
                                put(JSONObject().apply {
                                    put("type", "text")
                                    put("text", answer)
                                })
                            })
                        })
                    }
                }
                else -> JSONObject().apply {
                    put("jsonrpc", "2.0")
                    put("id", requestId)
                    put("result", JSONObject().apply {
                        put("isError", true)
                        put("error", "未知工具: $toolName")
                    })
                }
            }
        } catch (e: Exception) {
            JSONObject().apply {
                put("jsonrpc", "2.0")
                put("id", requestId)
                put("result", JSONObject().apply {
                    put("isError", true)
                    put("error", e.message ?: "执行失败")
                })
            }
        }
    }

    private fun handleResponse(payload: JSONObject): JSONObject? {
        val id = payload.getInt("id")
        pendingCalls[id]?.complete(payload.toString())
        return null
    }

    private fun handleError(payload: JSONObject): JSONObject? {
        val id = payload.getInt("id")
        val errorMsg = payload.getJSONObject("error").getString("message")
        pendingCalls[id]?.completeExceptionally(Exception(errorMsg))
        return null
    }

    fun hasVisionCapability(): Boolean = visionUrl != null && visionToken != null
    fun getVisionUrl(): String = visionUrl ?: ""
    fun getVisionToken(): String = visionToken ?: ""

    private fun sendMcpMessage(payload: JSONObject) {
        val message = JSONObject().apply {
            put("type", "mcp")
            put("payload", payload)
        }
        // 通过 WebSocket 发送
        webSocket.send(message.toString())
    }
}
```

### 8.2 拍照上传工具

```kotlin
class VisionUploader(
    private val httpClient: OkHttpClient,
    private val deviceId: String,
    private val clientId: String
) {
    suspend fun captureAndAnalyze(
        question: String,
        visionUrl: String,
        visionToken: String,
        photoBytes: ByteArray
    ): String = withContext(Dispatchers.IO) {
        val requestBody = MultipartBody.Builder()
            .setType(MultipartBody.FORM)
            .addFormDataPart("question", question)
            .addFormDataPart(
                "image", "photo.jpg",
                photoBytes.toRequestBody("image/jpeg".toMediaType())
            )
            .build()

        val request = Request.Builder()
            .url(visionUrl)
            .addHeader("Authorization", "Bearer $visionToken")
            .addHeader("Device-Id", deviceId)
            .addHeader("Client-Id", clientId)
            .post(requestBody)
            .build()

        val response = httpClient.newCall(request).execute()
        val body = response.body?.string() ?: throw Exception("空响应")
        val json = JSONObject(body)

        if (json.optBoolean("success", false)) {
            json.getJSONObject("data").getString("answer")
        } else {
            throw Exception(json.optString("message", "视觉分析失败"))
        }
    }
}
```

### 8.3 集成到 WebSocket 消息处理

```kotlin
override fun onMessage(message: String) {
    val msgJson = JSONObject(message)
    val type = msgJson.optString("type")

    when (type) {
        "mcp" -> {
            val payload = msgJson.getJSONObject("payload")
            val response = mcpManager.handleMcpMessage(payload)
            if (response != null) {
                val wrapper = JSONObject().apply {
                    put("type", "mcp")
                    put("payload", response)
                }
                webSocket.send(wrapper.toString())
            }
        }
        "hello" -> {
            sessionId = msgJson.getString("session_id")
        }
        "tts" -> {
            handleTtsState(msgJson.getString("state"), msgJson.optString("text"))
        }
        "stt" -> {
            handleSttResult(msgJson.getString("text"))
        }
        // ... 其他消息类型
    }
}
```

## 9. 常见问题

### Q: MCP 工具调用有超时吗？

有。服务端调用工具时默认等待 **30 秒**。如果客户端 30 秒内没有回复 result，服务端会认为调用失败。建议在客户端也设置相同的超时。

### Q: 客户端能主动发送 MCP 消息给服务端吗？

可以，但当前服务端只处理以下情况：
- `result` 响应（对应之前的 tools/call 请求）
- `error` 响应

客户端主动发起的 `method` 请求不会被服务端处理。

### Q: 多个工具可以并行调用吗？

可以。服务端可能在同一时间发出多个 `tools/call`，每个有不同的 `id`。客户端应根据 `id` 匹配响应，支持并行执行。

### Q: visionToken 会过期吗？

会。Token 有效期 **1 小时**。如果连接持续超过 1 小时，token 会失效。需要重新建立 WebSocket 连接获取新的 token。

### Q: 没有注册任何工具会怎样？

不会出错。MCP 仍然正常初始化，只是 LLM 不会向客户端发起 tools/call。视觉功能等需要设备端工具的能力将不可用。

### Q: tools/list 返回空列表可以吗？

可以。返回空数组表示客户端没有可用工具：

```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "result": {
    "tools": []
  }
}
```

### Q: 工具返回的结果怎么被 LLM 使用？

工具返回的 `content[].text` 会作为上下文传递给 LLM，LLM 根据返回内容生成自然语言回复，再通过 TTS 播放给用户。例如：

1. 用户说"帮我看一下这是什么"
2. 服务端 LLM 决定调用 `capture_photo`
3. 客户端拍照并上传视觉 API，得到"图片中是一只橘猫"
4. 客户端通过 MCP 返回 `"图片中是一只橘猫"`
5. 服务端 LLM 基于这个结果生成回复"这是一只橘色的猫咪，看起来很可爱"
6. TTS 合成语音播放给用户

## 10. 调试技巧

1. **测试模式跳过认证**：HTTP 请求头设置 `Client-Id: web_test_client` 可跳过 token 验证，方便调试视觉 API
2. **查看服务端日志**：MCP 相关日志包含 `MCP` 关键字，可以追踪消息收发
3. **使用浏览器测试**：项目自带的 `test/test_page.html` 可以在浏览器中测试 WebSocket 通讯
4. **检查 VLLM 配置**：视觉功能需要服务端 `selected_module.VLLM` 已配置且对应的 VLLM provider 正常初始化
