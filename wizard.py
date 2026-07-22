#!/usr/bin/env python3
"""
AI Proxy — Interactive Setup Wizard

Guides users through configuring model providers and assigning model tiers,
then generates a ready-to-use config.json for server.py.

Usage:
    python3 wizard.py              # Full 4-step wizard
    python3 wizard.py --switch     # Quick switch default provider
    python3 wizard.py --show       # Show current config
    python3 wizard.py --provider opencode  # Reconfigure one provider

Dependencies: rich, inquirer (auto-installed if missing)
"""

import argparse
import getpass
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

# Ensure rich is available
try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich.prompt import Prompt, Confirm
    from rich.text import Text
    from rich import print as rprint
except ImportError:
    print("正在安装 rich (CLI UI 库)...")
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "rich", "-q"]
    )
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich.prompt import Prompt, Confirm
    from rich.text import Text
    from rich import print as rprint

# Ensure inquirer is available (for interactive select menus)
try:
    import inquirer
except ImportError:
    print("正在安装 inquirer (交互选择库)...")
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "inquirer", "-q"]
    )
    import inquirer

# Project paths
SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_FILE = SCRIPT_DIR / "config.json"

# Provider modules
sys.path.insert(0, str(SCRIPT_DIR))
from providers.presets import (
    ProviderPreset,
    PROVIDER_PRESETS,
    find_preset,
    get_preset_choices,
    API_FORMAT_CHOICES,
    AUTH_TYPE_CHOICES,
)
from providers.model_fetcher import test_connection

console = Console()

# ── Tier definitions ─────────────────────────────────────────────────────────

TIERS = [
    ("haiku",  "Haiku",  "轻量快速模型 — 简单查询、工具调用，响应最快", True),
    ("sonnet", "Sonnet", "默认主力模型 — 日常编码、对话、大部分工作", True),
    ("opus",   "Opus",   "深度思考模型 — 架构设计、复杂推理、高可用性", True),
    ("fable",  "Fable",  "前沿模型 — 最新能力，可选配", False),
]

TIER_DESCRIPTIONS = {
    "haiku": "轻量级，适合简单问答和快速工具调用",
    "sonnet": "日常主力，覆盖大多数编码和对话场景 → 你说的高负荷工作",
    "opus": "深度推理，适合架构设计、复杂分析 → 你說的深度思考",
    "fable": "前沿能力，新模型快速迭代时可选用",
}

# ── Helpers ──────────────────────────────────────────────────────────────────

def load_existing_config() -> dict:
    """Load existing config.json, return empty dict if not found."""
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_config(config: dict):
    """Write config.json with pretty formatting."""
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
        f.write("\n")


def read_api_key(label: str = "API Key") -> str:
    """Prompt for API key with hidden input."""
    while True:
        key = getpass.getpass(f"  {label}: ")
        key = key.strip()
        if key:
            return key
        console.print("  [yellow]API Key 不能为空[/yellow]")


# ── Configuration Builder ────────────────────────────────────────────────────

def build_full_config(
    providers: dict,
    default_provider: str,
    auto_failover: bool,
    existing: dict,
) -> dict:
    """Merge wizard output with existing config."""
    config = {
        "listen": existing.get("listen", "127.0.0.1:19443"),
        "default_provider": default_provider,
        "auto_failover": auto_failover,
        "cert_dir": existing.get("cert_dir", "certs"),
        "body_filter": existing.get("body_filter", {
            "enabled": True,
            "whitelist": ["_metadata"],
        }),
        "providers": {},
    }

    # Merge: wizard-created providers take priority, preserve unmatched ones
    merged = dict(existing.get("providers", {}))
    merged.update(providers)
    config["providers"] = merged

    return config


def make_provider_config(preset: ProviderPreset, base_url: str, api_key: str,
                         tier_mapping: dict) -> dict:
    """Build a provider config dict from wizard choices."""
    mapping = dict(tier_mapping)
    # Add exact-model passthrough entries so proxy doesn't remap known models
    for tier_model in mapping.values():
        if tier_model and tier_model not in mapping:
            mapping[tier_model] = tier_model
    # Ensure default is set
    if "default" not in mapping:
        mapping["default"] = mapping.get("sonnet") or list(mapping.values())[0]

    return {
        "name": preset.name if preset.id != "custom" else base_url.split("//")[1].split("/")[0],
        "base_url": base_url.rstrip("/"),
        "api_key": api_key,
        "auth_type": preset.auth_type if preset.id != "custom" else "bearer",
        "api_format": preset.api_format if preset.id != "custom" else "openai_chat",
        "max_tokens_limit": preset.max_tokens_limit,
        "model_mapping": mapping,
        "tags": preset.tags,
    }


# ── Step 1: Select & Configure Provider ──────────────────────────────────────

def step_select_provider(existing: dict) -> Optional[tuple[str, dict]]:
    """Interactive provider selection and configuration.

    Returns (provider_id, provider_config_dict) or None if cancelled.
    """
    console.print()
    console.print(Panel.fit(
        "[bold]步骤 1/4: 添加 AI 供应商[/bold]\n"
        "选择一个模型供应商，配置 API 地址和密钥",
        border_style="cyan",
    ))

    # ── Pick preset or custom ──
    choices = [(p.id, f"{p.name:<20} {p.base_url}") for p in PROVIDER_PRESETS]
    choices.append(("__custom__", "自定义供应商    手动输入 URL 和格式"))

    # Build choices for inquirer: (label, value)
    select_choices = [(label, pid) for pid, label in choices]
    question = [
        inquirer.List(
            "provider",
            message="选择供应商",
            choices=select_choices,
            carousel=True,
        )
    ]
    answer = inquirer.prompt(question)
    if answer is None:
        return None
    preset_id = answer["provider"]

    if preset_id == "__custom__":
        return _configure_custom_provider()
    else:
        return _configure_preset_provider(preset_id, existing)


def _configure_preset_provider(preset_id: str, existing: dict) -> Optional[tuple[str, dict]]:
    preset = find_preset(preset_id)
    assert preset is not None

    existing_providers = existing.get("providers", {})
    existing_provider = existing_providers.get(preset_id)

    console.print(f"\n[bold]{'━' * 50}[/bold]")
    console.print(f"  供应商: [cyan]{preset.name}[/cyan]")
    console.print(f"  默认地址: [dim]{preset.base_url}[/dim]")

    # URL (with default from preset or existing config)
    default_url = existing_provider.get("base_url", preset.base_url) if existing_provider else preset.base_url
    base_url = Prompt.ask("  [cyan]?[/cyan] API URL", default=default_url)

    # API Key
    existing_key = existing_provider.get("api_key", "") if existing_provider else ""
    if existing_key:
        console.print("  [dim]已保存 API Key，留空使用现有值[/dim]")
    api_key = getpass.getpass("  API Key: ") or existing_key
    if not api_key:
        console.print("[red]API Key 不能为空[/red]")
        return None

    # ── Test connection ──
    console.print(f"\n  → 测试连接 [dim]{base_url}/v1/models[/dim] ...")
    success, err, models = test_connection(base_url, api_key, preset.api_format)

    if success:
        console.print("  [green]✓ 连接成功![/green]")
    else:
        console.print(f"  [yellow]⚠ 连接失败: {err}[/yellow]")
        console.print("  [dim]将使用预设模型列表继续[/dim]")
        models = None

    # Determine available models
    available = models if models else preset.known_models

    if models:
        console.print("\n  [bold]可用模型:[/bold]")
        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column("")
        for i, m in enumerate(available, 1):
            table.add_row(f"  {i}. {m}")
        console.print(table)
    else:
        console.print(f"\n  [dim]预设模型: {', '.join(available)}[/dim]")

    # ── Configure tiers ──
    tier_mapping = _step_configure_tiers(available, preset)

    provider_config = make_provider_config(preset, base_url, api_key, tier_mapping)
    return (preset_id, provider_config)


def _configure_custom_provider() -> Optional[tuple[str, dict]]:
    """Walk through custom provider configuration."""
    console.print("\n[bold]自定义供应商[/bold]")

    name = Prompt.ask("  [cyan]?[/cyan] 供应商名称", default="my-provider")
    base_url = Prompt.ask("  [cyan]?[/cyan] API URL (完整地址)")
    if not base_url.startswith("http"):
        base_url = "https://" + base_url

    # API format
    fmt_q = [
        inquirer.List(
            "api_format",
            message="API 格式",
            choices=[(fdesc, fid) for fid, fdesc in API_FORMAT_CHOICES],
            default="openai_chat",
        )
    ]
    fmt_a = inquirer.prompt(fmt_q)
    if fmt_a is None:
        return None
    api_format = fmt_a["api_format"]

    # Auth type
    auth_type = "x-api-key" if api_format == "anthropic" else "bearer"
    console.print(f"  [dim]认证方式: {auth_type}[/dim]")

    api_key = read_api_key()

    # Test
    console.print(f"\n  → 测试连接 [dim]{base_url}/v1/models[/dim] ...")
    success, err, models = test_connection(base_url, api_key, api_format)

    if success:
        console.print("  [green]✓ 连接成功![/green]")
        available = models
        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column("")
        for i, m in enumerate(available or [], 1):
            table.add_row(f"  {i}. {m}")
        console.print(table)
    else:
        console.print(f"  [yellow]⚠ {err}[/yellow]")
        available = []
        manual = Prompt.ask("  [cyan]?[/cyan] 手动输入模型名（逗号分隔）", default="")
        if manual:
            available = [m.strip() for m in manual.split(",") if m.strip()]

    # Build a minimal preset for make_provider_config
    fake_preset = ProviderPreset(
        id=f"custom_{name.lower().replace(' ', '-')}",
        name=name,
        base_url=base_url,
        api_format=api_format,
        auth_type=auth_type,
        known_models=available or [],
        tier_suggestions={},
    )

    tier_mapping = _step_configure_tiers(available or [], fake_preset)
    provider_config = make_provider_config(fake_preset, base_url, api_key, tier_mapping)
    return (fake_preset.id, provider_config)


# ── Step 2: Configure Model Tiers ───────────────────────────────────────────

def _step_configure_tiers(available: list[str], preset: ProviderPreset) -> dict:
    """Interactive model tier assignment using arrow-key selection.

    Returns a dict like {"haiku": "minimax-m3", "sonnet": "deepseek-v4-pro", ...}
    """
    console.print("\n[bold]步骤 2/4: 配置模型分层[/bold]")
    console.print("  将模型分配到不同的能力层级，Claude Code 会根据场景自动选择\n")

    mapping = {}
    skip_option = "(不配置)"

    for tier_id, tier_name, tier_desc, required in TIERS:
        default_suggestion = preset.tier_suggestions.get(tier_id)

        # Build select choices with default highlighted
        choices_list = []
        default_idx = 0
        choices_list.append((f"   {skip_option}", skip_option))
        for i, m in enumerate(available):
            display = f"   {m}"
            if m == default_suggestion:
                display += " (推荐)"
            choices_list.append((display, m))
            if m == default_suggestion:
                default_idx = i + 1

        console.print(f"\n  [bold]{tier_name}[/bold] — {tier_desc}")

        question = [
            inquirer.List(
                "model",
                message="选择模型（方向键导航，回车确认）",
                choices=choices_list,
                default=choices_list[default_idx][1] if default_idx > 0 else skip_option,
                carousel=True,
            )
        ]
        answer = inquirer.prompt(question)
        if answer is None:
            continue

        selected = answer["model"]

        if selected == skip_option:
            if required:
                console.print(f"  [yellow]⚠ {tier_name} 建议配置，否则将使用默认模型[/yellow]")
                retry_q = [
                    inquirer.List(
                        "confirm",
                        message="仍要跳过?",
                        choices=[("  否，继续配置", "n"), ("  是，跳过", "y")],
                        default="n",
                    )
                ]
                retry_a = inquirer.prompt(retry_q)
                if retry_a and retry_a["confirm"] == "y":
                    continue
                # Re-select
                return _step_configure_tiers(available, preset)
            continue

        mapping[tier_id] = selected
        console.print(f"  → {tier_name} = [green]{selected}[/green]")

    return mapping


# ── Step 3: More Providers ──────────────────────────────────────────────────

def step_more_providers() -> bool:
    """Ask if user wants to add another provider."""
    return Confirm.ask("\n[cyan]?[/cyan] 是否继续添加其他供应商?", default=False)


# ── Step 4: Select Default Provider ─────────────────────────────────────────

def step_default_provider(providers: dict) -> tuple[str, bool]:
    """Choose the default provider and failover mode."""
    console.print()
    console.print(Panel.fit(
        "[bold]步骤 4/4: 选择默认供应商[/bold]",
        border_style="cyan",
    ))

    console.print("\n[bold]已配置的供应商:[/bold]")
    provider_ids = list(providers.keys())

    choices_list = []
    for pid in provider_ids:
        p = providers[pid]
        mm = p.get("model_mapping", {})
        summary = f"Sonnet: {mm.get('sonnet', '?')} | Opus: {mm.get('opus', '?')}"
        display = f"  {p.get('name', pid):<20} — {summary}"
        choices_list.append((display, pid))

    default_q = [
        inquirer.List(
            "provider",
            message="默认使用哪个供应商?",
            choices=choices_list,
            default=choices_list[0][1],
        )
    ]
    default_a = inquirer.prompt(default_q)
    if default_a is None:
        return provider_ids[0], False
    default_pid = default_a["provider"]

    auto_failover = Confirm.ask(
        "\n[cyan]?[/cyan] 启用自动故障转移? (主供应商不可用时自动切换)",
        default=False,
    )

    return default_pid, auto_failover


# ── Display & Save ──────────────────────────────────────────────────────────

def render_summary(config: dict):
    """Show a formatted summary of the generated config."""
    console.print()
    console.print(Panel.fit("[bold]✓ 配置摘要[/bold]", border_style="green"))

    providers = config.get("providers", {})
    default_pid = config.get("default_provider", "")
    default_provider = providers.get(default_pid, {})

    table = Table(show_header=True, header_style="bold cyan")
    table.add_column("供应商")
    table.add_column("格式")
    table.add_column("Haiku")
    table.add_column("Sonnet")
    table.add_column("Opus")
    table.add_column("Fable")

    for pid, p in providers.items():
        mm = p.get("model_mapping", {})
        marker = " ← 默认" if pid == default_pid else ""
        table.add_row(
            f"{p.get('name', pid)}{marker}",
            p.get("api_format", "?"),
            mm.get("haiku", "-"),
            mm.get("sonnet", "-"),
            mm.get("opus", "-"),
            mm.get("fable", "-"),
        )

    console.print(table)
    console.print(f"  故障转移: {'[green]开启[/green]' if config.get('auto_failover') else '[dim]关闭[/dim]'}")
    console.print(f"  配置文件: [dim]{CONFIG_FILE}[/dim]")


def prompt_start_proxy():
    """Ask if user wants to start the proxy."""
    if Confirm.ask("\n[cyan]?[/cyan] 立即启动代理?", default=True):
        # Check if server.py exists
        proxy_path = SCRIPT_DIR / "server.py"
        if not proxy_path.exists():
            console.print("[red]server.py 未找到[/red]")
            return

        # Check if already running
        import subprocess
        result = subprocess.run(
            ["pgrep", "-f", "server.py"],
            capture_output=True, text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            console.print("[yellow]代理已在运行中[/yellow]")
            return

        # Start in background
        log_path = SCRIPT_DIR / "proxy.log"
        try:
            proc = subprocess.Popen(
                [sys.executable, str(proxy_path)],
                stdout=open(log_path, "a"),
                stderr=subprocess.STDOUT,
                cwd=SCRIPT_DIR,
            )
            time.sleep(1)
            if proc.poll() is None:
                console.print(f"[green]✓ 代理已启动 (PID: {proc.pid})[/green]")
                console.print(f"  日志: {log_path}")
                console.print("  现在可以运行 [bold]claude[/bold] 了 💡")
            else:
                console.print("[red]代理启动失败，请手动运行 python3 server.py[/red]")
        except Exception as e:
            console.print(f"[red]启动失败: {e}[/red]")


# ── Quick Commands ──────────────────────────────────────────────────────────

def cmd_switch():
    """Quick switch default provider without full wizard."""
    config = load_existing_config()
    providers = config.get("providers", {})

    if not providers:
        console.print("[red]没有已配置的供应商，请先运行 python3 wizard.py[/red]")
        return

    choices = list(providers.keys())
    console.print("[bold]切换默认供应商[/bold]\n")
    for i, pid in enumerate(choices, 1):
        p = providers[pid]
        cur = " ← 当前" if pid == config.get("default_provider") else ""
        console.print(f"  {i}. [cyan]{p.get('name', pid)}[/cyan]{cur}")

    choice = Prompt.ask(
        "\n[cyan]?[/cyan] 选择新的默认供应商",
        choices=[str(i) for i in range(1, len(choices) + 1)],
    )
    new_default = choices[int(choice) - 1]
    config["default_provider"] = new_default
    save_config(config)
    console.print(f"[green]✓ 默认供应商已切换至: {providers[new_default].get('name', new_default)}[/green]")


def cmd_show():
    """Display current configuration."""
    config = load_existing_config()
    if not config.get("providers"):
        console.print("[yellow]尚未配置任何供应商[/yellow]")
        console.print("运行 [bold]python3 wizard.py[/bold] 开始配置")
        return

    render_summary(config)


def cmd_quick_provider(provider_id: str):
    """Reconfigure a single provider."""
    config = load_existing_config()
    result = _configure_preset_provider(provider_id, config)
    if result is None:
        return

    pid, provider_cfg = result
    if "providers" not in config:
        config["providers"] = {}
    config["providers"][pid] = provider_cfg

    # If this is the only provider, make it default
    if len(config["providers"]) == 1:
        config["default_provider"] = pid

    save_config(config)
    console.print(f"[green]✓ 供应商 '{provider_cfg['name']}' 配置已更新[/green]")


# ── Main Wizard ─────────────────────────────────────────────────────────────

def cmd_wizard():
    """Full 4-step interactive wizard."""
    console.print()
    console.print(Panel.fit(
        "[bold]OpenCode Proxy v2  配置向导[/bold]\n"
        "为 Claude Code 配置 AI 模型供应商",
        border_style="cyan",
    ))

    existing = load_existing_config()
    providers = {}

    while True:
        result = step_select_provider(existing)
        if result is None:
            break

        pid, provider_cfg = result
        providers[pid] = provider_cfg

        console.print(f"\n[green]✓ '{provider_cfg['name']}' 配置完成[/green]")

        if not step_more_providers():
            break

    if not providers:
        console.print("[yellow]未配置任何供应商，退出[/yellow]")
        return

    # Step 4: default + failover
    default_pid, auto_failover = step_default_provider(providers)

    # Build full config (merge with existing)
    config = build_full_config(providers, default_pid, auto_failover, existing)

    # Save
    save_config(config)
    console.print(f"\n[green]✓ 配置已保存到 {CONFIG_FILE}[/green]")

    # Display summary
    render_summary(config)

    # Start proxy?
    prompt_start_proxy()

    console.print("\n[dim]💡 以后可以运行来重新配置:[/dim]")
    console.print("  [bold]python3 wizard.py[/bold]         — 完整向导")
    console.print("  [bold]python3 wizard.py --switch[/bold] — 快速切换默认供应商")
    console.print("  [bold]python3 wizard.py --show[/bold]   — 查看当前配置")


# ── Entry Point ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="OpenCode Proxy v2 — 交互式配置向导",
    )
    parser.add_argument("--switch", action="store_true",
                        help="快速切换默认供应商")
    parser.add_argument("--show", action="store_true",
                        help="查看当前配置")
    parser.add_argument("--provider", metavar="ID",
                        help="重新配置某个供应商 (opencode / ark / glm / ...)")

    args = parser.parse_args()

    if args.switch:
        cmd_switch()
    elif args.show:
        cmd_show()
    elif args.provider:
        cmd_quick_provider(args.provider)
    else:
        cmd_wizard()


if __name__ == "__main__":
    main()
