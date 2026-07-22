# AI Proxy

基于 CC Switch 架构的本地 HTTPS 反向代理，专为 WSL2 设计。

支持 **Claude Code**、**Codex CLI**、**Gemini CLI** 等多客户端，
提供多供应商故障转移、层级模型映射、熔断器和请求体清洗功能。

## 快速开始

```bash
# 1. 交互式配置供应商
python3 ai-proxy wizard

# 2. 启动代理
python3 ai-proxy start

# 3. 运行 Claude Code
claude
```

## 使用方式

```bash
python3 ai-proxy              # 启动代理
python3 ai-proxy wizard       # 配置/重配供应商
python3 ai-proxy status       # 查看运行状态
python3 ai-proxy config       # 查看当前配置
python3 ai-proxy switch       # 切换默认供应商
python3 ai-proxy stop         # 停止代理
python3 ai-proxy restart      # 重启代理
python3 ai-proxy --help       # 帮助
```

## 支持的供应商

| 供应商 | API 格式 | 默认地址 |
|--------|----------|----------|
| OpenCode | Anthropic | https://opencode.ai/zen/go |
| Ark (火山方舟) | OpenAI | https://ark.cn-beijing.volces.com/api/v3 |
| GLM (智谱) | OpenAI | https://open.bigmodel.cn/api/paas/v4 |
| MiniMax | OpenAI | https://api.minimax.chat/v1 |
| DeepSeek | OpenAI | https://api.deepseek.com |
| 通义千问 | OpenAI | https://dashscope.aliyuncs.com/compatible-mode/v1 |
| 自定义 | 可选 | 手动输入 |

## 功能

- **多供应商管理** — 通过向导配置多个 AI 供应商，统一管理 API Key 和端点
- **层级模型映射** — Haiku / Sonnet / Opus / Fable 四级模型分层，Claude Code 自动选择
- **故障转移** — 熔断器机制，供应商不可用时自动切换
- **请求体清洗** — 自动过滤私有参数，防止信息泄露
- **TLS 证书** — 首次启动自动生成，无需手动配置
- **零外部依赖** — Python 标准库即可运行（向导使用 rich 库）

## 配置 Claude Code

编辑 `~/.claude/settings.json`：

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "https://127.0.0.1:19443/",
    "ANTHROPIC_API_KEY": "sk-placeholder",
    "NODE_EXTRA_CA_CERTS": "<仓库路径>/certs/ca.pem"
  }
}
```

## License

MIT
