# Seal Gateway LLM 集成指南

## 简介

[Seal Gateway](https://github.com/lijinrui/seal-gateway) 是一个 WebSocket 协议的 LLM 网关服务，提供统一的对话管理、上下文维护和流式响应能力。

通过将 Seal Gateway 作为 LLM Provider，小智可以：
- 利用 Seal 的会话管理自动维护多轮对话上下文
- 通过 WebSocket 获取流式响应
- 支持自定义 Agent 和复杂的对话流程

## 功能特性

- **自动上下文管理**：Seal 的 session 自动维护对话历史，无需重复发送
- **流式响应**：WebSocket 实时流式输出
- **Token 节省**：不重复传输历史对话，减少 Token 消耗
- **高可靠性**：支持断线重连和超时处理

## 安装依赖

Seal Provider 需要 `websocket-client` 库：

```bash
pip install websocket-client
```

或重新安装项目依赖：

```bash
pip install -r requirements.txt
```

## 配置说明

### 基础配置

在 `config.yaml` 中配置 Seal LLM：

```yaml
LLM:
  SealLLM:
    type: seal
    url: ws://10.40.52.197:18789      # Seal Gateway WebSocket 地址
    token: your_token_here             # Gateway 认证 Token
    session_key: agent:main:main       # 会话 Key
    agent_id: main                     # Agent ID
    aliases: [seal, 希尔, 希儿, 海豹]  # 别名，用于语音切换模型

selected_module:
  LLM: SealLLM
```

### 配置参数详解

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `type` | string | 是 | - | 固定值 `seal` |
| `url` | string | 是 | - | Seal Gateway WebSocket 地址 |
| `token` | string | 是 | - | Gateway 认证 Token |
| `session_key` | string | 否 | `agent:main:main` | 会话 Key，格式 `agent:{agent_id}:main` |
| `agent_id` | string | 否 | `main` | Agent ID |
| `aliases` | list | 否 | - | 别名列表，用于语音切换模型 |

### 完整配置示例

```yaml
LLM:
  # 其他 LLM 配置...

  SealLLM:
    type: seal
    url: ws://10.40.52.197:18789
    token: sk-xxxxxxxxxxxxxxxx
    session_key: agent:main:main
    agent_id: main
    aliases: [seal, 希尔, 希儿, 海豹, seal网关]

selected_module:
  LLM: SealLLM
```

## 工作原理

```
┌─────────────────┐     WebSocket      ┌─────────────────┐
│  XiaoZhi Server │  ═══════════════►  │  Seal Gateway   │
│                 │                    │                 │
│  SealLLM        │  1. connect        │                 │
│  Provider       │  2. sessions.send  │                 │
│                 │  3. 接收 agent     │                 │
│                 │     事件流         │                 │
└─────────────────┘                    └─────────────────┘
```

1. **连接认证**：通过 WebSocket 连接到 Seal Gateway，使用 Token 认证
2. **发送消息**：使用 `sessions.send` 发送当前用户消息
3. **接收回复**：监听 `agent` 事件流，获取 AI 流式回复

## 多轮对话上下文

**Seal 的 session 会自动维护对话上下文！**

与传统的 OpenAI Provider 不同，Seal Provider 只需要发送当前用户消息：

```python
# 第1轮对话
dialogue = [{"role": "user", "content": "你好"}]
# Seal 回复: "你好！有什么可以帮你的？"

# 第2轮对话
dialogue = [
    {"role": "user", "content": "你好"},
    {"role": "assistant", "content": "你好！有什么可以帮你的？"},
    {"role": "user", "content": "今天天气怎么样？"}  # 只发送这条
]
# Seal 会根据 session_key 找到历史，理解上下文
```

**优势：**
- ✅ 减少网络传输（不重复发送历史）
- ✅ 减少 Token 消耗
- ✅ Seal 的 session 管理更可靠

## 语音切换模型

配置了 `aliases` 后，用户可以通过语音切换模型：

| 用户语音 | 效果 |
|----------|------|
| "切换到希尔" | 切换到 SealLLM |
| "用 seal 吧" | 切换到 SealLLM |
| "换成希儿" | 切换到 SealLLM |

更多详情参考 [switch_llm 插件](../plugins_func/functions/switch_llm.py)。

## 与其他 LLM Provider 对比

| 特性 | Seal Provider | OpenAI Provider | Gemini Provider |
|------|---------------|-----------------|-----------------|
| 协议 | WebSocket | HTTP REST | HTTP REST |
| 流式响应 | ✅ WebSocket 事件 | ✅ SSE | ✅ SSE |
| 认证方式 | Token + Connect | API Key | API Key |
| 上下文管理 | Seal session（自动） | 客户端维护 | 客户端维护 |
| Token 消耗 | 低（不重复历史） | 高（每次带历史） | 高（每次带历史） |
| 网络要求 | WebSocket 端口 | HTTP 80/443 | HTTP 80/443 |

## 故障排查

### 1. 连接失败

```
Seal connection error: ...
```

- 检查 `url` 是否正确
- 确认网络可以访问 Seal Gateway
- 确认 WebSocket 端口未被防火墙阻挡

### 2. 认证失败

```
Seal connect failed: {...}
```

- 检查 `token` 是否正确
- 确认 Token 有 `operator` 权限
- 检查 Token 是否过期

### 3. 无响应或超时

```
Seal response timeout
```

- Seal Gateway 可能没有正常运行
- 检查 `session_key` 对应的 agent 是否存在
- 检查 Seal Gateway 的日志
- 增加超时时间（修改代码中的 `ws.settimeout(60)`）

### 4. JSON 解析错误

```
Invalid JSON received: ...
```

- Seal Gateway 返回了非 JSON 数据
- 检查 Seal Gateway 版本是否兼容
- 查看完整返回数据（日志中已截取前200字符）

## 测试验证

可以通过以下方式测试 Seal Provider 是否正常工作：

```bash
# 激活虚拟环境
source .venv/bin/activate

# 测试 websocket-client 导入
python -c "import websocket; print('websocket-client 导入成功')"

# 测试 Seal Provider 导入
python -c "from core.providers.llm.seal.seal import LLMProvider; print('Seal Provider 导入成功')"
```

## 扩展开发

### 添加 Function Calling 支持

当前版本返回纯文本，如需支持工具调用，可扩展 `response_with_functions` 方法：

```python
def response_with_functions(self, session_id, dialogue, functions=None, **kwargs):
    # 参考 OpenAI Provider 实现
    # 在发送消息时附加工具定义
    # 解析 function call 结果
    pass
```

欢迎提交 PR 贡献代码！

## 相关资源

- [Seal Gateway GitHub](https://github.com/lijinrui/seal-gateway)
- [小智服务端源码](../main/xiaozhi-server/core/providers/llm/seal/)
- [config.yaml 配置说明](./config.yaml)

## License

MIT License - 与原项目保持一致
