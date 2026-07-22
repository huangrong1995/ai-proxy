#!/usr/bin/env python3
"""AI Proxy — Interactive Setup Wizard"""

import argparse, getpass, json, os, subprocess, sys, time
from pathlib import Path
from typing import Optional

try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich.prompt import Prompt, Confirm
except ImportError:
    print("Installing rich...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "rich", "-q"])
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich.prompt import Prompt, Confirm

try:
    import questionary
except ImportError:
    print("Installing questionary...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "questionary", "-q"])
    import questionary

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_FILE = SCRIPT_DIR / "config.json"
sys.path.insert(0, str(SCRIPT_DIR))
from providers.presets import ProviderPreset, PROVIDER_PRESETS, find_preset, API_FORMAT_CHOICES
from providers.model_fetcher import test_connection

console = Console()

TIERS = [
    ("haiku",  "Haiku",  "轻量快速 — 简单查询、工具调用", True),
    ("sonnet", "Sonnet", "默认主力 — 日常编码、对话", True),
    ("opus",   "Opus",   "深度思考 — 架构设计、复杂推理", True),
    ("fable",  "Fable",  "前沿模型 — 最新能力，可选", False),
]

def load_config():
    if CONFIG_FILE.exists():
        try: return json.loads(CONFIG_FILE.read_text())
        except: pass
    return {}

def save_config(config):
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n")

def read_key():
    while True:
        k = getpass.getpass("  API Key: ").strip()
        if k: return k
        console.print("  [yellow]API Key 不能为空[/yellow]")

def make_provider_config(preset, base_url, api_key, tier_map):
    m = dict(tier_map)
    # Add passthrough entries without mutating during iteration
    passthrough = {v: v for v in m.values() if v and v not in m}
    m.update(passthrough)
    m.setdefault("default", m.get("sonnet") or next(iter(m.values()), ""))
    return {
        "name": preset.name if preset.id != "custom" else base_url.split("//")[1].split("/")[0],
        "base_url": base_url.rstrip("/"), "api_key": api_key,
        "auth_type": preset.auth_type if preset.id != "custom" else "bearer",
        "api_format": preset.api_format if preset.id != "custom" else "openai_chat",
        "max_tokens_limit": preset.max_tokens_limit,
        "model_mapping": m, "tags": preset.tags,
    }

def configure_tiers(available, preset):
    console.print("\n[bold]步骤 2/4: 配置模型分层[/bold]\n")
    mapping, skip = {}, "(不配置)"
    for tid, tn, td, required in TIERS:
        sug = preset.tier_suggestions.get(tid)
        choices = [questionary.Choice(skip, value=skip)]
        # Default to first real model, unless suggestion is available
        default = available[0] if available else skip
        for m in available:
            is_rec = m == sug
            label = m
            if is_rec:
                label += "  (推荐)"
                default = m
            choices.append(questionary.Choice(label, value=m))
        sel = questionary.select(f"[{tn}] {td}", choices=choices,
                                  default=default, pointer="\u25cf").ask()
        if sel is None: continue
        if sel == skip:
            if required and not questionary.confirm(f"确定跳过 {tn}?", default=False).ask():
                return configure_tiers(available, preset)
            continue
        mapping[tid] = sel
        console.print(f"  → {tn} = [green]{sel}[/green]")
    return mapping

def pick_provider(existing):
    console.print()
    console.print(Panel.fit("[bold]步骤 1/4: 添加 AI 供应商[/bold]\n选择一个模型供应商，配置 API 地址和密钥", border_style="cyan"))
    choices = [questionary.Choice(f"{p.name} — {p.base_url}", value=p.id) for p in PROVIDER_PRESETS]
    choices.append(questionary.Choice("自定义供应商 (手动输入 URL 和格式)", value="__custom__"))
    pid = questionary.select("选择供应商", choices=choices, pointer="\u25cf").ask()
    if pid is None: return None
    if pid == "__custom__": return configure_custom()
    return configure_preset(pid, existing)

def configure_preset(pid, existing):
    preset = find_preset(pid)
    ep = existing.get("providers", {}).get(pid)
    console.print(f"\n{'━'*50}\n  供应商: [cyan]{preset.name}[/cyan]\n  默认地址: [dim]{preset.base_url}[/dim]")
    url = Prompt.ask("  [cyan]?[/cyan] API URL", default=(ep.get("base_url", preset.base_url) if ep else preset.base_url))
    ek = ep.get("api_key", "") if ep else ""
    if ek: console.print("  [dim]已保存 API Key，留空使用现有值[/dim]")
    key = getpass.getpass("  API Key: ") or ek
    if not key: return console.print("[red]API Key 不能为空[/red]") or None
    console.print(f"\n  → 测试连接 [dim]{url}/v1/models[/dim] ...")
    ok, err, models = test_connection(url, key, preset.api_format)
    if ok:
        console.print("  [green]✓ 连接成功![/green]")
    else:
        console.print(f"  [yellow]⚠ 连接失败: {err}[/yellow]")
        console.print("  [dim]将使用预设模型列表继续[/dim]")
    avail = models or preset.known_models
    if models:
        console.print("\n  [bold]可用模型:[/bold]")
        t = Table(show_header=False, box=None, padding=(0,2))
        t.add_column(""); [t.add_row(f"  {m}") for m in avail]; console.print(t)
    return (pid, make_provider_config(preset, url, key, configure_tiers(avail, preset)))

def configure_custom():
    console.print("\n[bold]自定义供应商[/bold]")
    name = Prompt.ask("  [cyan]?[/cyan] 供应商名称", default="my-provider")
    url = Prompt.ask("  [cyan]?[/cyan] API URL (完整地址)")
    url = "https://" + url if not url.startswith("http") else url
    fmt = questionary.select("API 格式", choices=[questionary.Choice(d, v) for v,d in API_FORMAT_CHOICES],
                              default="openai_chat", pointer="\u25cf").ask()
    if fmt is None: return None
    at = "x-api-key" if fmt == "anthropic" else "bearer"
    console.print(f"  [dim]认证方式: {at}[/dim]")
    key = read_key()
    console.print(f"\n  → 测试连接 [dim]{url}/v1/models[/dim] ...")
    ok, err, models = test_connection(url, key, fmt)
    if ok:
        console.print("  [green]✓ 连接成功![/green]")
        avail = models or []
    else:
        console.print(f"  [yellow]⚠ {err}[/yellow]")
        avail = []
        m = Prompt.ask("  [cyan]?[/cyan] 手动输入模型名（逗号分隔）", default="")
        if m: avail = [x.strip() for x in m.split(",") if x.strip()]
    fp = ProviderPreset(id=f"custom_{name.lower().replace(' ','-')}", name=name, base_url=url,
                        api_format=fmt, auth_type=at, known_models=avail, tier_suggestions={})
    return (fp.id, make_provider_config(fp, url, key, configure_tiers(avail, fp)))

def pick_default(providers):
    console.print()
    console.print(Panel.fit("[bold]步骤 4/4: 选择默认供应商[/bold]", border_style="cyan"))
    pids = list(providers.keys())
    choices = []
    for pid in pids:
        p = providers[pid]; mm = p.get("model_mapping", {})
        choices.append(questionary.Choice(f"{p['name']} — Sonnet: {mm.get('sonnet','?')}  Opus: {mm.get('opus','?')}", value=pid))
    d = questionary.select("默认使用哪个供应商?", choices=choices, pointer="\u25cf").ask() or pids[0]
    af = Confirm.ask("\n[cyan]?[/cyan] 启用自动故障转移? (主供应商不可用时自动切换)", default=False)
    return d, af

def render_summary(config):
    console.print()
    console.print(Panel.fit("[bold]✓ 配置摘要[/bold]", border_style="green"))
    t = Table(header_style="bold cyan")
    for col in ["供应商", "格式", "Haiku", "Sonnet", "Opus", "Fable"]: t.add_column(col)
    for pid, p in config.get("providers", {}).items():
        m = p.get("model_mapping", {}); mk = " ← 默认" if pid == config.get("default_provider") else ""
        t.add_row(f"{p['name']}{mk}", p.get("api_format","?"), m.get("haiku","-"),
                  m.get("sonnet","-"), m.get("opus","-"), m.get("fable","-"))
    console.print(t)
    console.print(f"  故障转移: {'[green]开启[/green]' if config.get('auto_failover') else '[dim]关闭[/dim]'}")
    console.print(f"  配置文件: [dim]{CONFIG_FILE}[/dim]")

def wizard():
    console.print()
    console.print(Panel.fit("[bold]AI Proxy  配置向导[/bold]\n为 Claude Code 配置 AI 模型供应商", border_style="cyan"))
    existing, providers = load_config(), {}
    while True:
        r = pick_provider(existing)
        if r is None: break
        pid, cfg = r; providers[pid] = cfg
        console.print(f"\n[green]✓ '{cfg['name']}' 配置完成[/green]")
        if not Confirm.ask("\n[cyan]?[/cyan] 是否继续添加其他供应商?", default=False): break
    if not providers: return console.print("[yellow]未配置任何供应商[/yellow]")
    dp, af = pick_default(providers)
    config = {
        "listen": existing.get("listen", "127.0.0.1:19443"),
        "default_provider": dp, "auto_failover": af,
        "cert_dir": existing.get("cert_dir", "certs"),
        "body_filter": existing.get("body_filter", {"enabled": True, "whitelist": ["_metadata"]}),
        "providers": {**existing.get("providers", {}), **providers},
    }
    save_config(config)
    console.print(f"\n[green]✓ 配置已保存到 {CONFIG_FILE}[/green]")
    render_summary(config)
    if Confirm.ask("\n[cyan]?[/cyan] 立即启动代理?", default=True):
        if not (sp := SCRIPT_DIR / "server.py").exists():
            console.print("[red]server.py 未找到[/red]")
        else:
            r = subprocess.run(["pgrep", "-f", "server.py"], capture_output=True, text=True, timeout=5)
            if r.returncode == 0 and r.stdout.strip():
                console.print("[yellow]代理已在运行中[/yellow]")
            else:
                try:
                    proc = subprocess.Popen([sys.executable, str(sp)], stdout=open(SCRIPT_DIR/"proxy.log","a"),
                                            stderr=subprocess.STDOUT, cwd=SCRIPT_DIR)
                    time.sleep(1)
                    if proc.poll() is None:
                        console.print(f"[green]✓ 代理已启动 (PID: {proc.pid})[/green]")
                        console.print("  现在可以运行 [bold]claude[/bold] 了 💡")
                    else:
                        console.print("[red]代理启动失败[/red]")
                except Exception as e:
                    console.print(f"[red]启动失败: {e}[/red]")

def main():
    p = argparse.ArgumentParser(description="AI Proxy — 交互式配置向导")
    p.add_argument("--switch", action="store_true", help="快速切换默认供应商")
    p.add_argument("--show", action="store_true", help="查看当前配置")
    p.add_argument("--provider", metavar="ID", help="重新配置某个供应商")
    args = p.parse_args()
    if args.switch:
        c = load_config()
        if not c.get("providers"): return console.print("[red]没有已配置的供应商[/red]")
        choices = [questionary.Choice(f"{p['name']}{' (当前)' if pid==c.get('default_provider') else ''}", value=pid)
                   for pid, p in c["providers"].items()]
        nd = questionary.select("切换默认供应商", choices=choices, pointer="\u25cf").ask()
        if nd: c["default_provider"] = nd; save_config(c); console.print("[green]✓ 默认供应商已切换[/green]")
    elif args.show:
        c = load_config()
        if c.get("providers"): render_summary(c)
        else: console.print("[yellow]尚未配置供应商[/yellow]")
    elif args.provider:
        c = load_config()
        r = configure_preset(args.provider, c)
        if r: pid, cfg = r; c.setdefault("providers", {})[pid] = cfg; save_config(c)
    else:
        wizard()

if __name__ == "__main__":
    main()
