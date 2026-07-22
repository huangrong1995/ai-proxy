# Interactive Setup Wizard Design

## Overview

An interactive CLI configuration wizard for OpenCode Proxy v2 that guides users through
adding model providers (OpenCode, Ark, GLM, MiniMax, DeepSeek, Tongyi Qianwen, or custom),
assigning models to Claude Code's native tier system (Haiku/Sonnet/Opus/Fable),
and generating a ready-to-use `config.json`.

## Motivation

The current proxy requires users to manually edit `config.json` with provider details,
API keys, and model mappings. This is error-prone and requires understanding the config schema.
A wizard eliminates friction and makes the proxy accessible to users who aren't familiar with
the internal configuration format.

## Architecture

### Component Diagram

```
wizard.py (CLI entry point + 4-step state machine)
    │
    ├── providers/presets.py      ← Static preset data for 6 providers + custom
    ├── providers/model_fetcher.py ← Online model discovery (GET /v1/models)
    │
    └── output: config.json       ← Compatible with existing proxy_v2.py
```

### Files

| File | Purpose | Lines (est.) |
|------|---------|-------------|
| `wizard.py` | CLI entry, 4-step wizard, quick commands | ~350 |
| `providers/__init__.py` | Package init | ~5 |
| `providers/presets.py` | 6 provider presets + helper functions | ~150 |
| `providers/model_fetcher.py` | Connection testing + model listing | ~100 |

## Data Flow

```
User input (select + prompt) → wizard.py
                                    ↓
                            providers/presets.py  (URL defaults, known models)
                            providers/model_fetcher.py (online discovery)
                                    ↓
                            Build config dict → JSON serialization
                                    ↓
                            config.json → proxy_v2.py reads on startup
```

## User Interaction Flow (4 Steps)

### Step 1: Select & Configure Provider

- Display provider list using rich.table with presets
- Support "Custom" for manual URL + format selection
- Fill default URL from preset, editable
- API Key input hidden (getpass)
- Connection test: `GET {base_url}/v1/models` with appropriate auth header
- Display discovered models in a rich.table

### Step 2: Assign Model Tiers

For each of Haiku / Sonnet / Opus / Fable:

- Show tier description in context
- rich.Select from discovered models (or known_models as fallback)
- Preselect tier_suggestions from preset
- Option to skip a tier (model not mapped, falls to default)

### Step 3: Add More Providers (Loop)

- Ask "Add another provider?" after each completion
- Support copying tier config from an existing provider
- Show already-configured providers with checkmark

### Step 4: Select Default Provider

- List configured providers with model summary
- Choose one as default
- Toggle auto-failover
- Save config.json

### Post-Wizard

- Display configuration summary as rich.table
- Option to start proxy immediately
- Option to save quick-switch profiles

## Data Format

### config.json (fully backward-compatible)

```json
{
  "listen": "127.0.0.1:19443",
  "default_provider": "opencode",
  "auto_failover": true,
  "cert_dir": "certs",
  "body_filter": { "enabled": true, "whitelist": ["_metadata"] },
  "providers": {
    "opencode": {
      "name": "OpenCode",
      "base_url": "https://opencode.ai/zen/go",
      "api_key": "sk-xxx",
      "auth_type": "x-api-key",
      "api_format": "anthropic",
      "max_tokens_limit": 8000,
      "model_mapping": {
        "minimax-m3": "minimax-m3",
        "deepseek-v4-pro": "deepseek-v4-pro",
        "haiku": "minimax-m3",
        "sonnet": "deepseek-v4-pro",
        "opus": "glm-5.2",
        "default": "deepseek-v4-pro"
      },
      "tags": ["tool_capable"]
    }
  }
}
```

### Provider Preset Schema

```python
{
    "id": str,              # unique key, used as provider_id
    "name": str,            # display name
    "base_url": str,        # default API endpoint
    "api_format": str,      # "anthropic" | "openai_chat"
    "auth_type": str,       # "x-api-key" | "bearer"
    "models_endpoint": str, # path to model list API
    "max_tokens_limit": int | None,
    "tags": list[str],      # e.g. ["tool_capable"]
    "known_models": list[str],  # offline fallback
    "tier_suggestions": dict,   # haiku/sonnet/opus → recommended model
}
```

## Provider Presets

| Provider | id | base_url | api_format |
|----------|-----|----------|------------|
| OpenCode | opencode | https://opencode.ai/zen/go | anthropic |
| Ark (火山方舟) | ark | https://ark.cn-beijing.volces.com/api/v3 | openai_chat |
| GLM (智谱) | glm | https://open.bigmodel.cn/api/paas/v4 | openai_chat |
| MiniMax | minimax | https://api.minimax.chat/v1 | openai_chat |
| DeepSeek | deepseek | https://api.deepseek.com | openai_chat |
| 通义千问 | tongyi | https://dashscope.aliyuncs.com/compatible-mode/v1 | openai_chat |
| Custom | custom_xxx | user-provided | user-selected |

## Model Discovery

### Anthropic Format (OpenCode)

```
GET {base_url}/v1/models
Header: x-api-key: {key}

Response: {"data": [{"type": "model", "id": "model-name"}, ...]}
```

### OpenAI Format (Ark, GLM, MiniMax, DeepSeek, Tongyi)

```
GET {base_url}/v1/models
Header: Authorization: Bearer {key}

Response: {"data": [{"id": "model-name", ...}, ...]}
```

### Fallback

When the API call fails (network, auth, unsupported endpoint):
- Use `known_models` from preset as an offline list
- Allow user to manually type model names

## Error Handling

| Scenario | Behavior |
|----------|----------|
| Network error | Print error, ask retry or continue (use known_models) |
| 401 Unauthorized | Print error, re-prompt API key |
| 404 No model endpoint | Use known_models fallback |
| Ctrl+C | Print "Configuration not saved", exit |
| config.json read error | Start fresh with defaults |

## CLI Quick Commands

```
python3 wizard.py              # Full 4-step wizard
python3 wizard.py --switch     # Quick switch default provider
python3 wizard.py --show       # Show current config summary
python3 wizard.py --provider opencode  # Reconfigure one provider only
```

## Dependencies

- `rich` — CLI UI framework (tables, select, confirm, prompt)
- Standard library: `json`, `urllib.request`, `getpass`, `argparse`, `os`, `sys`

## Future Considerations

- Profile support: save named config profiles, switch between them
- Batch provider validation: test all providers at once
- Import from existing Claude Code settings.json
- Export config as shareable template
