"""
Provider presets — default configurations for supported AI model providers.

Each preset includes:
  - API endpoint URL and auth method
  - Known model list (offline fallback when online discovery fails)
  - Tier suggestions (recommended model for each Claude Code tier)
"""

from dataclasses import dataclass, field
from typing import Optional

@dataclass
class ProviderPreset:
    id: str
    name: str
    base_url: str
    api_format: str          # "anthropic" | "openai_chat"
    auth_type: str           # "x-api-key" | "bearer"
    models_endpoint: str = "/v1/models"
    max_tokens_limit: Optional[int] = None
    tags: list = field(default_factory=list)
    known_models: list = field(default_factory=list)
    tier_suggestions: dict = field(default_factory=dict)
    notes: str = ""

# ── Provider Presets ──────────────────────────────────────────────────────────

PROVIDER_PRESETS: list[ProviderPreset] = [
    ProviderPreset(
        id="opencode",
        name="OpenCode",
        base_url="https://opencode.ai/zen/go",
        api_format="anthropic",
        auth_type="x-api-key",
        max_tokens_limit=8000,
        tags=["tool_capable"],
        known_models=[
            "deepseek-v4-pro", "glm-5.2", "minimax-m3",
            "qwen3.7-max", "qwen3.7-plus", "qwen3.6-plus", "qwen3.5-plus",
        ],
        tier_suggestions={
            "haiku": "minimax-m3",
            "sonnet": "deepseek-v4-pro",
            "opus": "glm-5.2",
            "fable": "qwen3.7-max",
        },
        notes="Anthropic-format proxy to multiple models via opencode.ai",
    ),
    ProviderPreset(
        id="ark",
        name="Ark (火山方舟)",
        base_url="https://ark.cn-beijing.volces.com/api/v3",
        api_format="openai_chat",
        auth_type="bearer",
        max_tokens_limit=16000,
        known_models=[
            "doubao-pro-32k", "doubao-lite-32k",
            "doubao-pro-128k", "doubao-thinking-pro",
        ],
        tier_suggestions={
            "haiku": "doubao-lite-32k",
            "sonnet": "doubao-pro-32k",
            "opus": "doubao-thinking-pro",
        },
        notes="Volcengine Ark — Doubao model series via OpenAI-compatible API",
    ),
    ProviderPreset(
        id="glm",
        name="GLM (智谱)",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        api_format="openai_chat",
        auth_type="bearer",
        max_tokens_limit=8000,
        known_models=[
            "glm-5", "glm-5.1", "glm-5.2",
            "glm-5.2-flash", "glm-4-plus",
        ],
        tier_suggestions={
            "sonnet": "glm-5.2",
            "opus": "glm-5.2",
            "haiku": "glm-5.2-flash",
        },
        notes="Zhipu GLM series — OpenAI-compatible API",
    ),
    ProviderPreset(
        id="minimax",
        name="MiniMax",
        base_url="https://api.minimax.chat/v1",
        api_format="openai_chat",
        auth_type="bearer",
        max_tokens_limit=16000,
        known_models=[
            "minimax-m3", "minimax-m2.7",
        ],
        tier_suggestions={
            "haiku": "minimax-m2.7",
            "sonnet": "minimax-m3",
            "opus": "minimax-m3",
        },
        notes="MiniMax model family — OpenAI-compatible API",
    ),
    ProviderPreset(
        id="deepseek",
        name="DeepSeek",
        base_url="https://api.deepseek.com",
        api_format="openai_chat",
        auth_type="bearer",
        max_tokens_limit=8000,
        known_models=[
            "deepseek-chat", "deepseek-reasoner",
        ],
        tier_suggestions={
            "haiku": "deepseek-chat",
            "sonnet": "deepseek-chat",
            "opus": "deepseek-reasoner",
        },
        notes="DeepSeek official API — OpenAI-compatible",
    ),
    ProviderPreset(
        id="tongyi",
        name="通义千问",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_format="openai_chat",
        auth_type="bearer",
        max_tokens_limit=16000,
        known_models=[
            "qwen-turbo", "qwen-plus", "qwen-max",
            "qwen3-turbo", "qwen3-plus", "qwen3-max",
        ],
        tier_suggestions={
            "haiku": "qwen3-turbo",
            "sonnet": "qwen3-plus",
            "opus": "qwen3-max",
        },
        notes="Alibaba Cloud Tongyi Qianwen — OpenAI-compatible API",
    ),
]


def find_preset(preset_id: str) -> ProviderPreset | None:
    """Look up a preset by ID."""
    for p in PROVIDER_PRESETS:
        if p.id == preset_id:
            return p
    return None


def get_preset_by_name(name: str) -> ProviderPreset | None:
    """Look up a preset by display name (case-insensitive contains match)."""
    nl = name.lower()
    for p in PROVIDER_PRESETS:
        if nl in p.name.lower() or p.id == nl:
            return p
    return None


def get_preset_choices() -> list[tuple[str, str]]:
    """Return (id, display_name) pairs for the provider selection menu."""
    return [(p.id, p.name) for p in PROVIDER_PRESETS]


API_FORMAT_CHOICES = [
    ("anthropic", "Anthropic Messages API (x-api-key auth)"),
    ("openai_chat", "OpenAI Chat Completions API (Bearer auth)"),
]

AUTH_TYPE_CHOICES = [
    ("x-api-key", "x-api-key (Anthropic style)"),
    ("bearer", "Bearer Token (OpenAI style)"),
]
