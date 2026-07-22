#!/usr/bin/env python3
"""AI Proxy — Interactive Setup Wizard"""
import argparse, getpass, json, subprocess, sys, time
from pathlib import Path

try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich.prompt import Prompt, Confirm
    from rich.style import Style
    from rich.text import Text
except ImportError:
    print("Installing rich...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "rich", "-q"])
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich.prompt import Prompt, Confirm
    from rich.style import Style
    from rich.text import Text

try:
    import questionary
    from questionary import Style as QStyle
except ImportError:
    print("Installing questionary...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "questionary", "-q"])
    import questionary
    from questionary import Style as QStyle

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_FILE = SCRIPT_DIR / "config.json"
sys.path.insert(0, str(SCRIPT_DIR))
from providers.presets import ProviderPreset, PROVIDER_PRESETS, find_preset, API_FORMAT_CHOICES
from providers.model_fetcher import test_connection

console = Console()

# ── Design System ──

C = {
    "p": "bold cyan",
    "s": "bold green",
    "w": "bold yellow",
    "e": "bold red",
    "m": "dim",
    "a": "cyan",
    "hl": "cyan",
}

QS = QStyle([
    ("qmark", "bold cyan"),
    ("question", "bold"),
    ("pointer", "bold cyan"),
    ("highlighted", "cyan"),
    ("selected", "green"),
    ("answer", "cyan"),
    ("instruction", "ansibrightblack"),
])

TIERS = [
    ("haiku",  "Haiku",  "轻量快速 — 简单查询、工具调用", True),
    ("sonnet", "Sonnet", "默认主力 — 日常编码、对话", True),
    ("opus",   "Opus",   "深度思考 — 架构设计、复杂推理", True),
    ("fable",  "Fable",  "前沿模型 — 最新能力，可选", False),
]

STEP_NAMES = {1: "添加供应商", 2: "配置模型分层", 3: "添加更多供应商", 4: "选择默认供应商"}


def print_step_header(step: int, total: int = 4):
    """Print step progress bar with header."""
    bar = Text()
    bar.append("  ◈ ", style=C["p"])
    bar.append("AI Proxy  配置向导", style="bold")
    console.print()
    console.print(Panel.fit(bar, border_style="cyan"))
    bar2 = Text()
    bar2.append("  " + "━" * step, style=C["p"])
    bar2.append("━" * (total - step), style="grey37")
    bar2.append(f"  步骤 {step}/{total}  ", style=C["m"])
    bar2.append(STEP_NAMES[step], style=C["a"])
    console.print(bar2)
    console.print()


def print_section(title: str):
    """Print a section divider."""
    console.print(f"  [bold {C['p']}]┌─ {title}[/bold {C['p']}]"
                  f"[{C['m']}]" + "─" * max(0, 50 - len(title)) + "[/" + C['m'] + "]")


def print_ok(msg: str):
    console.print(f"  [{C['s']}]◆[/{C['s']}]  {msg}")


def print_info(msg: str):
    console.print(f"  [{C['a']}]◇[/{C['a']}]  {msg}")


def print_warn(msg: str):
    console.print(f"  [{C['w']}]◇[/{C['w']}]  {msg}")


def print_err(msg: str):
    console.print(f"  [{C['e']}]◆[/{C['e']}]  {msg}")


def load_config():
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text())
        except:
            pass
    return {}


def save_config(config):
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n")


def read_key(existing_key=""):
    if existing_key:
        console.print(f"  [{C['m']}]已有 Key: {existing_key[:8]}...{existing_key[-4:]}[/{C['m']}]")
        k = Prompt.ask(f"  [{C['p']}]◇[/{C['p']}]  API Key", password=True, default="")
        return k if k else existing_key
    while True:
        k = Prompt.ask(f"  [{C['p']}]◇[/{C['p']}]  API Key", password=True, default="")
        if k:
            return k
        print_warn("API Key 不能为空")


def make_provider_config(preset, base_url, api_key, tier_map):
    m = dict(tier_map)
    passthrough = {v: v for v in m.values() if v and v not in m}
    m.update(passthrough)
    m.setdefault("default", m.get("sonnet") or next(iter(m.values()), ""))
    return {
        "name": preset.name if preset.id != "custom"
                else base_url.split("//")[1].split("/")[0],
        "base_url": base_url.rstrip("/"),
        "api_key": api_key,
        "auth_type": preset.auth_type if preset.id != "custom" else "bearer",
        "api_format": preset.api_format if preset.id != "custom" else "openai_chat",
        "max_tokens_limit": preset.max_tokens_limit,
        "model_mapping": m,
        "tags": preset.tags,
    }


def configure_tiers(available, preset):
    print_step_header(2)
    mapping, skip = {}, "(不配置)"
    for tid, tn, td, required in TIERS:
        sug = preset.tier_suggestions.get(tid)
        choices = [questionary.Choice(skip, value=skip)]
        default = available[0] if available else skip
        for m in available:
            rec = m == sug
            label = (m + "  ◆ 推荐" if rec else m)
            if rec:
                default = m
            choices.append(questionary.Choice(label, value=m))
        sel = questionary.select(
            f"  [{tn}]  {td}",
            choices=choices, default=default,
            pointer="●", qmark="◈", style=QS,
        ).ask()
        if sel is None:
            continue
        if sel == skip:
            if required and not questionary.confirm(
                    f"  ◈  {tn} 建议配置，确定跳过?", default=False,
                    qmark="◈", style=QS,
            ).ask():
                return configure_tiers(available, preset)
            continue
        mapping[tid] = sel
        print_ok(f"{tn} = [bold]{sel}[/bold]")
    return mapping


def pick_provider(existing):
    print_step_header(1)
    print_section("选择供应商")
    choices = [questionary.Choice(f"{p.name}  —  {p.base_url}", value=p.id)
               for p in PROVIDER_PRESETS]
    choices.append(questionary.Choice("自定义供应商  —  手动输入 URL 和格式", value="__custom__"))
    pid = questionary.select(
        "  ◈  供应商",
        choices=choices, pointer="●", qmark="", style=QS,
    ).ask()
    if pid is None:
        return None
    if pid == "__custom__":
        return configure_custom()
    return configure_preset(pid, existing)


def configure_preset(pid, existing):
    preset = find_preset(pid)
    ep = existing.get("providers", {}).get(pid)
    console.print()
    print_section(f"{preset.name}  配置")
    url = Prompt.ask(
        f"  [{C['p']}]?[/{C['p']}] API URL",
        default=(ep.get("base_url", preset.base_url) if ep else preset.base_url),
    )
    ek = ep.get("api_key", "") if ep else ""
    key = read_key(ek)
    if not key:
        return print_err("API Key 不能为空") or None
    print_info("正在测试连接...")
    ok, err, models = test_connection(url, key, preset.api_format)
    if ok:
        print_ok(f"连接成功  ·  发现 {len(models or [])} 个可用模型")
    else:
        print_warn(f"连接失败: {err}")
        print_info("使用预设模型列表继续")
    avail = models or preset.known_models
    if models:
        tbl = Table(show_header=False, box=None, padding=(0, 2))
        tbl.add_column("", style=C["m"])
        for m in avail:
            tbl.add_row(f"  {m}")
        console.print(tbl)
    return (pid, make_provider_config(preset, url, key, configure_tiers(avail, preset)))


def configure_custom():
    console.print()
    print_section("自定义供应商")
    name = Prompt.ask(f"  [{C['p']}]?[/{C['p']}] 供应商名称", default="my-provider")
    url = Prompt.ask(f"  [{C['p']}]?[/{C['p']}] API URL")
    url = "https://" + url if not url.startswith("http") else url
    fmt = questionary.select(
        "  ◈  API 格式",
        choices=[questionary.Choice(desc, value=fid) for fid, desc in API_FORMAT_CHOICES],
        default="openai_chat", pointer="●", qmark="", style=QS,
    ).ask()
    if fmt is None:
        return None
    at = "x-api-key" if fmt == "anthropic" else "bearer"
    print_info(f"认证方式: {at}")
    key = read_key()
    print_info("正在测试连接...")
    ok, err, models = test_connection(url, key, fmt)
    if ok:
        print_ok("连接成功")
        avail = models or []
    else:
        print_warn(f"{err}")
        avail = []
        m = Prompt.ask(f"  [{C['p']}]?[/{C['p']}] 手动输入模型名（逗号分隔）", default="")
        if m:
            avail = [x.strip() for x in m.split(",") if x.strip()]
    fp = ProviderPreset(
        id=f"custom_{name.lower().replace(' ', '-')}", name=name, base_url=url,
        api_format=fmt, auth_type=at, known_models=avail, tier_suggestions={},
    )
    return (fp.id, make_provider_config(fp, url, key, configure_tiers(avail, fp)))


def pick_default(providers):
    print_step_header(4)
    print_section("选择默认供应商")
    pids = list(providers.keys())
    choices = []
    for pid in pids:
        p, mm = providers[pid], providers[pid].get("model_mapping", {})
        choices.append(questionary.Choice(
            f"{p['name']}  —  Sonnet: {mm.get('sonnet', '?')}  /  Opus: {mm.get('opus', '?')}",
            value=pid,
        ))
    d = questionary.select(
        "  ◈  默认供应商", choices=choices,
        pointer="●", qmark="", style=QS,
    ).ask() or pids[0]
    af = Confirm.ask(f"  [{C['p']}]?[/{C['p']}] 启用自动故障转移？", default=False)
    return d, af


def render_summary(config):
    console.print()
    console.print(Panel.fit(
        Text("  ◆  配置完成  ◆", style=C["s"]), border_style="green",
    ))
    t = Table(header_style=C["p"])
    for col in ["供应商", "格式", "Haiku", "Sonnet", "Opus", "Fable"]:
        t.add_column(col)
    for pid, p in config.get("providers", {}).items():
        m = p.get("model_mapping", {})
        mk = "  ◀  默认" if pid == config.get("default_provider") else ""
        t.add_row(
            f"{p['name']}{mk}", p.get("api_format", "?"),
            m.get("haiku", "—"), m.get("sonnet", "—"),
            m.get("opus", "—"), m.get("fable", "—"),
        )
    console.print(t)
    fo = "ON" if config.get("auto_failover") else "OFF"
    console.print(f"  [{C['a']}]故障转移:[/{C['a']}] [bold]{fo}[/bold]")
    console.print(f"  [{C['m']}]配置: {CONFIG_FILE}[/{C['m']}]")


def wizard():
    console.print()
    console.print(Panel.fit(
        Text("  ◈  AI Proxy  配置向导  ◈", style=C["p"]),
        border_style="cyan",
    ))
    existing, providers = load_config(), {}
    while True:
        r = pick_provider(existing)
        if r is None:
            break
        pid, cfg = r
        providers[pid] = cfg
        print_ok(f"'{cfg['name']}' 配置完成")
        console.print()
        if not Confirm.ask(
            f"  [{C['p']}]?[/{C['p']}] 添加其他供应商？",
            default=False,
        ):
            break
    if not providers:
        return print_warn("未配置任何供应商")
    dp, af = pick_default(providers)
    config = {
        "listen": existing.get("listen", "127.0.0.1:19443"),
        "default_provider": dp,
        "auto_failover": af,
        "cert_dir": existing.get("cert_dir", "certs"),
        "body_filter": existing.get("body_filter",
                                      {"enabled": True, "whitelist": ["_metadata"]}),
        "providers": {**existing.get("providers", {}), **providers},
    }
    save_config(config)
    print_ok(f"配置已保存")
    render_summary(config)
    if Confirm.ask(f"  [{C['p']}]?[/{C['p']}] 立即启动代理？", default=True):
        sp = SCRIPT_DIR / "server.py"
        if not sp.exists():
            return print_err("server.py 未找到")
        r = subprocess.run(["pgrep", "-f", "server.py"],
                           capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            return print_warn("代理已在运行中")
        try:
            proc = subprocess.Popen(
                [sys.executable, str(sp)],
                stdout=open(SCRIPT_DIR / "proxy.log", "a"),
                stderr=subprocess.STDOUT, cwd=SCRIPT_DIR,
            )
            time.sleep(1)
            if proc.poll() is None:
                print_ok(f"代理已启动 (PID: {proc.pid})")
                print_info("运行 [bold]claude[/bold] 开始使用  ◆")
            else:
                print_err("代理启动失败，请手动运行 python3 server.py")
        except Exception as e:
            print_err(f"启动失败: {e}")


def main():
    ap = argparse.ArgumentParser(description="AI Proxy — 配置向导")
    ap.add_argument("--switch", action="store_true", help="切换默认供应商")
    ap.add_argument("--show", action="store_true", help="查看配置")
    ap.add_argument("--provider", metavar="ID", help="重配供应商")
    args = ap.parse_args()
    if args.switch:
        c = load_config()
        if not c.get("providers"):
            return print_warn("尚无供应商")
        choices = [questionary.Choice(
            f"{p['name']}{'  (当前)' if pid == c.get('default_provider') else ''}",
            value=pid,
        ) for pid, p in c["providers"].items()]
        nd = questionary.select(
            "  ◈  切换至", choices=choices,
            pointer="●", qmark="", style=QS,
        ).ask()
        if nd:
            c["default_provider"] = nd
            save_config(c)
            print_ok("默认供应商已切换")
    elif args.show:
        c = load_config()
        (render_summary(c) if c.get("providers") else print_warn("尚无供应商"))
    elif args.provider:
        c = load_config()
        r = configure_preset(args.provider, c)
        if r:
            pid, cfg = r
            c.setdefault("providers", {})[pid] = cfg
            save_config(c)
    else:
        wizard()


if __name__ == "__main__":
    main()
