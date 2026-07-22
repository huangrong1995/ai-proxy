#!/usr/bin/env python3
"""AI Proxy — Interactive Setup Wizard"""
import argparse, json, subprocess, sys, time
from pathlib import Path

try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich.prompt import Prompt, Confirm
    from rich.text import Text
except ImportError:
    print("Installing rich...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "rich", "-q"])
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich.prompt import Prompt, Confirm
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
C = {"p":"bold cyan","s":"bold green","w":"bold yellow","e":"bold red","m":"dim","a":"cyan"}

QS = QStyle([
    ("qmark","bold cyan"), ("question","bold"), ("pointer","bold cyan"),
    ("highlighted","cyan"), ("selected","green"), ("answer","cyan"),
    ("instruction","ansibrightblack"),
])

TIERS = [
    ("haiku","Haiku","轻量快速 — 简单查询、工具调用",True),
    ("sonnet","Sonnet","默认主力 — 日常编码、对话",True),
    ("opus","Opus","深度思考 — 架构设计、复杂推理",True),
    ("fable","Fable","前沿模型 — 最新能力，可选",False),
]
STEP_NAMES = {1:"添加供应商",2:"配置模型分层",3:"添加更多供应商",4:"选择默认供应商"}

def print_step_header(step, total=4):
    bar = Text("  ◈ ",style=C["p"]); bar.append("AI Proxy  配置向导",style="bold")
    console.print(); console.print(Panel.fit(bar,border_style="cyan"))
    bar2 = Text("  "+"━"*step,style=C["p"]); bar2.append("━"*(total-step),style="grey37")
    bar2.append(f"  步骤 {step}/{total}  ",style=C["m"]); bar2.append(STEP_NAMES[step],style=C["a"])
    console.print(bar2); console.print()

def print_section(title):
    console.print(f"  [bold {C['p']}]┌─ {title}[/bold {C['p']}][{C['m']}]"+"─"*max(0,50-len(title))+f"[/{C['m']}]")
def print_ok(m): console.print(f"  [{C['s']}]◆[/{C['s']}]  {m}")
def print_info(m): console.print(f"  [{C['a']}]◇[/{C['a']}]  {m}")
def print_warn(m): console.print(f"  [{C['w']}]◇[/{C['w']}]  {m}")
def print_err(m): console.print(f"  [{C['e']}]◆[/{C['e']}]  {m}")

def load_config():
    if CONFIG_FILE.exists():
        try: return json.loads(CONFIG_FILE.read_text())
        except: pass
    return {}

def save_config(config):
    CONFIG_FILE.parent.mkdir(parents=True,exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(config,indent=2,ensure_ascii=False)+"\n")

    if Confirm.ask("  ◈  同步到 Claude Code 设置？\n  将更新 ~/.claude/settings.json 中的 API、模型、证书配置", default=True):
        sync_to_claude(config)
    """Sync proxy config to ~/.claude/settings.json."""
    claude_dir = Path.home() / ".claude"
    claude_cfg = claude_dir / "settings.json"
    claude_dir.mkdir(parents=True,exist_ok=True)
    if claude_cfg.exists():
        backup = claude_dir / "settings.json.ai-proxy.bak"
        if not backup.exists():
            import shutil
            shutil.copy2(str(claude_cfg),str(backup))
            print_info(f"原配置已备份 -> {backup}")

    default_pid = config.get("default_provider","")
    providers = config.get("providers",{})
    default_p = providers.get(default_pid,next(iter(providers.values()),{}))
    api_key = default_p.get("api_key","")
    mm = default_p.get("model_mapping",{})

    existing = json.loads(claude_cfg.read_text()) if claude_cfg.exists() else {}
    env = existing.get("env",{})
    env["ANTHROPIC_BASE_URL"] = f"https://{config.get('listen','127.0.0.1:19443')}/"
    env["ANTHROPIC_API_KEY"] = api_key or env.get("ANTHROPIC_API_KEY","")
    env["NODE_EXTRA_CA_CERTS"] = str(SCRIPT_DIR/config.get("cert_dir","certs")/"ca.pem")
    env["API_TIMEOUT_MS"] = "3000000"
    env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] = "200000"
    env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    env["ANTHROPIC_MODEL"] = mm.get("sonnet") or mm.get("default","")
    env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = mm.get("haiku") or ""
    env["ANTHROPIC_DEFAULT_SONNET_MODEL"] = mm.get("sonnet") or ""
    env["ANTHROPIC_DEFAULT_OPUS_MODEL"] = mm.get("opus") or ""
    env["ANTHROPIC_DEFAULT_FABLE_MODEL"] = mm.get("fable") or ""
    existing["env"] = env
    claude_cfg.write_text(json.dumps(existing,indent=2,ensure_ascii=False)+"\n")
    print_ok("~/.claude/settings.json 已同步")

def read_key(existing_key=""):
    if existing_key:
        console.print(f"  [{C['m']}]已有 Key: {existing_key[:8]}...{existing_key[-4:]}[/{C['m']}]")
        k = Prompt.ask(f"  [{C['p']}]◇[/{C['p']}]  API Key")
        return k if k else existing_key
    while True:
        k = Prompt.ask(f"  [{C['p']}]◇[/{C['p']}]  API Key")
        if k: return k
        print_warn("API Key 不能为空")

def make_provider_config(preset,base_url,api_key,tier_map):
    m = dict(tier_map)
    passthrough = {v:v for v in m.values() if v and v not in m}
    m.update(passthrough)
    m.setdefault("default",m.get("sonnet") or next(iter(m.values()),""))
    return {
        "name": preset.name if preset.id!="custom" else base_url.split("//")[1].split("/")[0],
        "base_url": base_url.rstrip("/"),"api_key": api_key,
        "auth_type": preset.auth_type if preset.id!="custom" else "bearer",
        "api_format": preset.api_format if preset.id!="custom" else "openai_chat",
        "max_tokens_limit": preset.max_tokens_limit,
        "model_mapping": m,"tags": preset.tags,
    }

def configure_tiers(available,preset):
    print_step_header(2)
    mapping,skip = {},"(不配置)"
    for tid,tn,td,required in TIERS:
        sug = preset.tier_suggestions.get(tid)
        choices = [questionary.Choice(skip,value=skip)]
        default = available[0] if available else skip
        for m in available:
            rec = m==sug
            choices.append(questionary.Choice(m+"  ◆ 推荐" if rec else m,value=m))
            if rec: default = m
        sel = questionary.select(f"  [{tn}]  {td}",choices=choices,default=default,
                                 pointer="\u25cf",qmark="\u25c8",style=QS).ask()
        if sel is None: continue
        if sel==skip:
            if required and not questionary.confirm(f"  \u25c8  {tn} 建议配置，确定跳过?",
                                                    default=False,qmark="\u25c8",style=QS).ask():
                return configure_tiers(available,preset)
            continue
        mapping[tid] = sel
        print_ok(f"{tn} = [bold]{sel}[/bold]")
    return mapping

def configure_vision_model(available, preset, tier_map):
    """Let user pick a vision model for image requests."""
    console.print(); print_step_header(2)
    sug = preset.tier_suggestions.get("vision", "minimax-m3")
    models = [m for m in available if m not in tier_map.values()] + list(tier_map.values())
    models = list(dict.fromkeys(models))  # dedupe preserve order
    choices = [questionary.Choice("(不配置 — 发图可能失败)", value="")]
    for m in models:
        rec = m == sug
        choices.append(questionary.Choice(m + "  ◆ 推荐" if rec else m, value=m))
    default = sug if sug in models else ""
    sel = questionary.select(
        "  ◈ 默认视觉模型 — 发图片时自动切换至此模型",
        choices=choices, default=default, pointer="●", qmark="◈", style=QS
    ).ask()
    if sel: print_ok(f"视觉模型 = [bold]{sel}[/bold]")
    else: print_info("未配置视觉模型，发图时将警告")
    return sel or ""

def pick_provider(existing):
    print_step_header(1); print_section("选择供应商")
    choices = [questionary.Choice(f"{p.name}  —  {p.base_url}",value=p.id) for p in PROVIDER_PRESETS]
    choices.append(questionary.Choice("自定义供应商  —  手动输入 URL 和格式",value="__custom__"))
    pid = questionary.select("  \u25c8  供应商",choices=choices,pointer="\u25cf",qmark="",style=QS).ask()
    if pid is None: return None
    return configure_custom() if pid=="__custom__" else configure_preset(pid,existing)

def configure_preset(pid,existing):
    preset = find_preset(pid); ep = existing.get("providers",{}).get(pid)
    console.print(); print_section(f"{preset.name}  配置")
    url = Prompt.ask(f"  [{C['p']}]?[/{C['p']}] API URL",
                     default=(ep.get("base_url",preset.base_url) if ep else preset.base_url))
    ek = ep.get("api_key","") if ep else ""
    key = read_key(ek)
    if not key: return print_err("API Key 不能为空") or None
    print_info("正在测试连接...")
    ok,err,models = test_connection(url,key,preset.api_format)
    if ok: print_ok(f"连接成功  ·  发现 {len(models or [])} 个可用模型")
    else: print_warn(f"连接失败: {err}"); print_info("使用预设模型列表继续")
    avail = models or preset.known_models
    if models:
        t=Table(show_header=False,box=None,padding=(0,2)); t.add_column("",style=C["m"])
        for m in avail: t.add_row(f"  {m}")
        console.print(t)
    tiers = configure_tiers(avail, preset)
    vision = configure_vision_model(avail, preset, tiers)
    cfg = make_provider_config(preset,url,key,tiers)
    cfg["vision_model"] = vision
    return (pid, cfg)

def configure_custom():
    console.print(); print_section("自定义供应商")
    name = Prompt.ask(f"  [{C['p']}]?[/{C['p']}] 供应商名称",default="my-provider")
    url = Prompt.ask(f"  [{C['p']}]?[/{C['p']}] API URL")
    url = "https://"+url if not url.startswith("http") else url
    fmt = questionary.select("  \u25c8  API 格式",
        choices=[questionary.Choice(desc,value=fid) for fid,desc in API_FORMAT_CHOICES],
        default="openai_chat",pointer="\u25cf",qmark="",style=QS).ask()
    if fmt is None: return None
    at = "x-api-key" if fmt=="anthropic" else "bearer"
    print_info(f"认证方式: {at}"); key = read_key()
    print_info("正在测试连接...")
    ok,err,models = test_connection(url,key,fmt)
    if ok: print_ok("连接成功"); avail = models or []
    else:
        print_warn(f"{err}"); avail=[]
        m = Prompt.ask(f"  [{C['p']}]?[/{C['p']}] 手动输入模型名（逗号分隔）",default="")
        if m: avail=[x.strip() for x in m.split(",") if x.strip()]
    fp = ProviderPreset(id=f"custom_{name.lower().replace(' ','-')}",name=name,base_url=url,
                        api_format=fmt,auth_type=at,known_models=avail,tier_suggestions={})
    tiers = configure_tiers(avail,fp)
    vision = configure_vision_model(avail,fp,tiers)
    cfg = make_provider_config(fp,url,key,tiers)
    cfg["vision_model"] = vision
    return (fp.id, cfg)

def pick_default(providers):
    print_step_header(4); print_section("选择默认供应商")
    pids = list(providers.keys()); choices=[]
    for pid in pids:
        p,mm = providers[pid],providers[pid].get("model_mapping",{})
        choices.append(questionary.Choice(f"{p['name']}  —  Sonnet: {mm.get('sonnet','?')}  /  Opus: {mm.get('opus','?')}",value=pid))
    d = questionary.select("  \u25c8  默认供应商",choices=choices,pointer="\u25cf",qmark="",style=QS).ask() or pids[0]
    af = Confirm.ask(f"  [{C['p']}]?[/{C['p']}] 启用自动故障转移？",default=False)
    return d,af

def render_summary(config):
    console.print(); console.print(Panel.fit(Text("  ◆  配置完成  ◆",style=C["s"]),border_style="green"))
    t=Table(header_style=C["p"])
    for col in ["供应商","格式","Haiku","Sonnet","Opus","Fable"]: t.add_column(col)
    for pid,p in config.get("providers",{}).items():
        m=p.get("model_mapping",{}); mk="  ◀  默认" if pid==config.get("default_provider") else ""
        t.add_row(f"{p['name']}{mk}",p.get("api_format","?"),m.get("haiku","—"),
                  m.get("sonnet","—"),m.get("opus","—"),m.get("fable","—"))
    console.print(t)
    fo="ON" if config.get("auto_failover") else "OFF"
    console.print(f"  [{C['a']}]故障转移:[/{C['a']}] [bold]{fo}[/bold]")
    console.print(f"  [{C['m']}]配置: {CONFIG_FILE}[/{C['m']}]")

def wizard():
    console.print(); console.print(Panel.fit(Text("  ◈  AI Proxy  配置向导  ◈",style=C["p"]),border_style="cyan"))
    existing,providers = load_config(),{}
    while True:
        r = pick_provider(existing)
        if r is None: break
        pid,cfg = r; providers[pid]=cfg
        print_ok(f"'{cfg['name']}' 配置完成"); console.print()
        if not Confirm.ask(f"  [{C['p']}]?[/{C['p']}] 添加其他供应商？",default=False): break
    if not providers: return print_warn("未配置任何供应商")
    dp,af = pick_default(providers)
    config = {
        "listen": existing.get("listen","127.0.0.1:19443"),"default_provider": dp,"auto_failover": af,
        "cert_dir": existing.get("cert_dir","certs"),
        "body_filter": existing.get("body_filter",{"enabled":True,"whitelist":["_metadata"]}),
        "providers": {**existing.get("providers",{}),**providers},
    }
    save_config(config); print_ok("配置已保存")
    if Confirm.ask("  ◈  同步到 Claude Code 设置？\n  将更新 ~/.claude/settings.json 中的 API、模型、证书配置", default=True):
        sync_to_claude(config)
    render_summary(config)
    if Confirm.ask(f"  [{C['p']}]?[/{C['p']}] 立即启动代理？",default=True):
        sp = SCRIPT_DIR/"proxy-server.py"
        if not sp.exists(): return print_err("proxy-server.py 未找到")
        r = subprocess.run(["pgrep","-f","proxy-server.py"],capture_output=True,text=True,timeout=5)
        if r.returncode==0 and r.stdout.strip(): return print_warn("代理已在运行中")
        try:
            proc = subprocess.Popen([sys.executable,str(sp)],
                stdout=open(SCRIPT_DIR/"proxy.log","a"),stderr=subprocess.STDOUT,cwd=SCRIPT_DIR)
            time.sleep(1)
            if proc.poll() is None: print_ok(f"代理已启动 (PID: {proc.pid})"); print_info("运行 [bold]claude[/bold] 开始使用  ◆")
            else: print_err("代理启动失败，请手动运行 python3 proxy-server.py")
        except Exception as e: print_err(f"启动失败: {e}")

def main():
    ap = argparse.ArgumentParser(description="AI Proxy — 配置向导")
    ap.add_argument("--switch",action="store_true",help="切换默认供应商")
    ap.add_argument("--show",action="store_true",help="查看配置")
    ap.add_argument("--provider",metavar="ID",help="重配供应商")
    args = ap.parse_args()
    if args.switch:
        c = load_config()
        if not c.get("providers"): return print_warn("尚无供应商")
        choices=[questionary.Choice(f"{p['name']}{'  (当前)' if pid==c.get('default_provider') else ''}",value=pid)
                 for pid,p in c["providers"].items()]
        nd = questionary.select("  \u25c8  切换至",choices=choices,pointer="\u25cf",qmark="",style=QS).ask()
        if nd: c["default_provider"]=nd; save_config(c); sync_to_claude(c); print_ok("默认供应商已切换")
    elif args.show:
        c = load_config()
        (render_summary(c) if c.get("providers") else print_warn("尚无供应商"))
    elif args.provider:
        c = load_config(); r = configure_preset(args.provider,c)
        if r: pid,cfg=r; c.setdefault("providers",{})[pid]=cfg; save_config(c); sync_to_claude(c)
    else: wizard()

if __name__ == "__main__":
    main()
