# OpenCode Proxy v2

基于 CC Switch 架构的本地 HTTPS 反向代理，专为 WSL2 设计。

支持 **Claude Code**、**Codex CLI**、**Gemini CLI** 等多客户端，
提供多供应商故障转移、层级模型映射、熔断器和请求体清洗功能。

## 功能对比

| 功能 | v1 (proxy.py) | v2 (proxy_v2.py) | CC Switch |
|------|:---:|:---:|:---------:|
| 多供应商配置 | ❌ 硬编码 | ✅ `config.json` | ✅ GUI |
| 层级模型映射 | ❌ 单模型 | ✅ haiku/sonnet/opus/fable | ✅ |
| 故障转移 | ❌ | ✅ 熔断器 + 自动切换 | ✅ |
| 私有参数过滤 | ❌ | ✅ `_` 前缀字段递归清洗 | ✅ |
| 健康检查端点 | ❌ | ✅ `/health` `/status` | ✅ |
| 多客户端 | ❌ Claude 仅 | ✅ Anthropic + OpenAI Chat | ✅ |
| 模型列表 | ❌ | ✅ `/v1/models` | ✅ |
| 证书管理 | ✅ | ✅ 自动生成 | ✅ |
| 流式用量注入 | ❌ | ✅ `stream_options.include_usage` | ✅ |
| 零外部依赖 | ✅ | ✅ Python stdlib | 🦀 Rust |

## 快速开始

### 1. 配置

编辑 `config.json`，设置你的 API Key（或通过环境变量）：

```json
{
  "providers": {
    "opencode-go": {
      "api_key_env": "OPENCODE_API_KEY"
    }
  }
}
```

或者直接设置环境变量：

```bash
export OPENCODE_API_KEY="sk-your-key-here"
```

### 2. 启动

```bash
python3 proxy_v2.py
```

首次启动自动生成 TLS 证书。

### 3. 配置 Claude Code

编辑 `~/.claude/settings.json`：

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "https://127.0.0.1:19443/",
    "ANTHROPIC_API_KEY": "sk-your-key",
    "NODE_EXTRA_CA_CERTS": "<仓库路径>/certs/ca.pem"
  }
}
```

### 4. 配置 Codex CLI

设置环境变量：

```bash
export CODEX_BASE_URL="http://127.0.0.1:19443"
export CODEX_API_KEY="sk-your-key"
```

## 配置参考

### provider 属性

| 字段 | 类型 | 说明 |
|------|------|------|
| `name` | string | 显示名称 |
| `base_url` | string | 上游 API 地址 |
| `api_key_env` | string | 环境变量名（优先） |
| `api_key` | string | 直接填写 API Key |
| `auth_type` | string | `x-api-key` / `bearer` / `header` |
| `api_format` | string | `anthropic` / `openai_chat` / `openai_responses` |
| `max_tokens_limit` | int | 最大 token 限制（默认不限） |
| `model_mapping` | object | 模型映射规则 |
| `tags` | string[] | 能力标签（`tool_capable` 等） |

### 模型映射规则

```
haiku  → minimax-m3       # 轻量模型
sonnet → qwen3.7-max      # 主力模型
opus   → qwen3.7-max      # 强推理模型
fable  → qwen3.7-max      # 前沿模型（未配置时回退到 opus）
```

匹配优先级：精确匹配 > 层级匹配 > fable→opus 回退 > default

### circuit_breaker

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `max_failures` | 5 | 连续失败次数触发熔断 |
| `recovery_seconds` | 30 | 熔断后恢复等待时间 |
| `half_open_max_requests` | 3 | 半开状态允许的探测请求数 |

## 命令行选项

```bash
python3 proxy_v2.py                    # 启动代理
python3 proxy_v2.py --port 9999        # 自定义端口
python3 proxy_v2.py -v                 # 详细日志（同时输出到终端）
python3 proxy_v2.py --generate-certs   # 重新生成证书
python3 proxy_v2.py --status           # 检查运行状态
```

## 端点

| 端点 | 方法 | 说明 |
|------|------|------|
| `/v1/messages` | POST | Anthropic Messages API（Claude Code） |
| `/v1/messages?beta=true` | POST | 同上（含 beta 功能） |
| `/chat/completions` | POST | OpenAI Chat API（Codex） |
| `/v1/chat/completions` | POST | OpenAI Chat API（新版） |
| `/health` | GET | 健康检查 |
| `/status` | GET | 详细状态（供应商健康/熔断） |
| `/v1/models` | GET | 可用模型列表 |

## 故障转移示例

配置多个供应商，开启 `auto_failover: true`：

```json
{
  "auto_failover": true,
  "providers": {
    "primary": {
      "name": "主力供应商",
      "base_url": "https://primary.api.com",
      "tags": ["tool_capable"]
    },
    "backup": {
      "name": "备用供应商",
      "base_url": "https://backup.api.com",
      "tags": ["tool_capable"]
    }
  }
}
```

当主供应商连续 5 次故障后：
1. 熔断器断开该供应商
2. 自动切换到备用供应商
3. 30 秒后尝试恢复主供应商

## License

MIT
