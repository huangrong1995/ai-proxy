"""AI Proxy — Usage statistics with SQLite + rich tables + bar charts."""
import json, sqlite3, subprocess, sys
from pathlib import Path
from datetime import datetime, timedelta

SCRIPT_DIR = Path(__file__).resolve().parent
STATS_DB = SCRIPT_DIR / "proxy_stats.db"
CONFIG_FILE = SCRIPT_DIR / "config.json"

try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich.text import Text
except ImportError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "rich", "-q"])
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich.text import Text

console = Console()
C = {"p":"bold cyan","s":"bold green","w":"bold yellow","m":"dim"}

BAR = "■"
BAR_WIDTH = 40


def get_db():
    if not STATS_DB.exists():
        return None
    return sqlite3.connect(str(STATS_DB))


def since_filter(since: str) -> tuple[str, str]:
    """Return (where_clause, order_clause) for the time filter."""
    now = datetime.now()
    if since == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif since == "24h":
        start = now - timedelta(hours=24)
    elif since.endswith("d"):
        start = now - timedelta(days=int(since[:-1]))
    elif since.endswith("h"):
        start = now - timedelta(hours=int(since[:-1]))
    else:
        try:
            start = datetime.fromisoformat(since)
        except:
            start = now - timedelta(days=7)
    return f"WHERE timestamp >= '{start.isoformat(timespec='seconds')}'", \
           f"ORDER BY timestamp"


def query_single(sql: str, params: tuple = ()) -> list:
    db = get_db()
    if not db:
        return []
    cur = db.execute(sql, params)
    rows = cur.fetchall()
    db.close()
    return rows


def make_bar(value: int, max_val: int) -> str:
    if max_val <= 0:
        return ""
    filled = int(BAR_WIDTH * value / max_val)
    return BAR * max(1, filled) if value > 0 else ""


def fmt_num(n: int) -> str:
    if n >= 1_000_000:
        return f"{n/1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)


# ── Renderers ──

def render_overview(total_req, total_ok, total_fail, total_tokens, since_str):
    console.print()
    console.print(Panel.fit(
        Text("  ◆  AI Proxy  使用统计  ◆", style=C["p"]),
        border_style="cyan",
    ))
    console.print(f"  统计周期: {since_str}")
    console.print(f"  总请求: [bold]{total_req}[/bold]  "
                  f"[green]成功 {total_ok}[/green]  "
                  f"[red]失败 {total_fail}[/red]")

    cost = total_tokens / 1_000_000 * 0.15  # rough estimate
    console.print(f"  总消耗: [bold]{fmt_num(total_tokens)}[/bold] tokens  "
                  f"([dim]≈ ¥{cost:.2f}[/dim])")
    console.print()


def render_model_stats(rows: list):
    if not rows:
        return
    max_val = max(r[3] + r[4] for r in rows)  # input + output
    console.print(Text("按模型消耗", style="bold"))
    for model, provider, cnt, inp, out in rows:
        total = inp + out
        in_bar = make_bar(inp, max_val) if max_val else BAR * 2
        out_bar = make_bar(out, max_val) if max_val else ""
        pct = int(40 * total / max_val) if max_val else 20
        console.print(f"  [cyan]{model:<20}[/cyan] [bold]{fmt_num(total)}[/bold]")
        console.print(f"    inp {in_bar} [dim]{fmt_num(inp)}[/dim]")
        console.print(f"    out {out_bar} [dim]{fmt_num(out)}[/dim]  "
                      f"[{C['m']}]{cnt} req[/{C['m']}]")
    console.print()


def render_agent_stats(rows: list):
    if not rows:
        return
    max_val = max(r[2] for r in rows) if rows else 1
    console.print(Text("按 Agent 消耗", style="bold"))
    for agent, cnt, total in rows:
        bar = make_bar(total, max_val)
        console.print(f"  [cyan]{agent:<15}[/cyan] {bar} [bold]{fmt_num(total)}[/bold]  "
                      f"[{C['m']}]{cnt} req[/{C['m']}]")
    console.print()


def render_provider_stats(rows: list):
    if not rows:
        return
    console.print(Text("按供应商消耗", style="bold"))
    tbl = Table(header_style=C["p"])
    for col in ["供应商", "请求", "成功", "失败", "in tokens", "out tokens", "总 tokens"]:
        tbl.add_column(col, justify="right" if col != "供应商" else "left")
    for prov, cnt, ok, fail, inp, out in rows:
        total = inp + out
        tbl.add_row(prov, str(cnt), f"[green]{ok}[/green]", f"[red]{fail}[/red]",
                    fmt_num(inp), fmt_num(out), f"[bold]{fmt_num(total)}[/bold]")
    console.print(tbl)
    console.print()


def render_hourly_trend(rows: list):
    if not rows:
        return
    max_val = max(r[1] for r in rows) if rows else 1
    bars = min(len(rows), 24)
    console.print(Text("请求趋势 (最近 24h)", style="bold"))
    for hour, cnt, fails in rows[-bars:]:
        bar = make_bar(cnt, max_val)
        fail_mark = f" [red]{'★' * min(fails, 5)}[/red]" if fails else ""
        console.print(f"  [{C['m']}]{hour:>5}[/{C['m']}] {bar} [bold]{cnt}[/bold]{fail_mark}")
    console.print()


def compute_since_label(since: str) -> str:
    if since == "today":
        return "今天"
    if since == "24h":
        return "过去 24 小时"
    if since.endswith("d"):
        return f"过去 {since[:-1]} 天"
    if since.endswith("h"):
        return f"过去 {since[:-1]} 小时"
    try:
        datetime.fromisoformat(since)
        return f"自 {since}"
    except:
        return "过去 7 天"


# ── Main ──

def cmd_stats(since: str = "7d", fmt_json: bool = False, model_filter: str = "",
              provider_filter: str = ""):
    db = get_db()
    if not db:
        console.print("[yellow]尚无统计数据[/yellow]")
        console.print("启动代理并发送请求后会自动记录")
        return

    w_clause, _ = since_filter(since)
    w = w_clause

    # Apply extra filters
    extra = ""
    params = ()
    if model_filter:
        extra += f" AND model LIKE '%{model_filter}%'"
    if provider_filter:
        extra += f" AND provider LIKE '%{provider_filter}%'"

    # Overview
    row = db.execute(f"SELECT COUNT(*), SUM(CASE WHEN status=200 THEN 1 ELSE 0 END), "
                     f"SUM(CASE WHEN status!=200 THEN 1 ELSE 0 END), "
                     f"SUM(input_tokens+output_tokens) "
                     f"FROM requests {w}{extra}").fetchone()
    total_req = row[0] or 0
    total_ok = row[1] or 0
    total_fail = row[2] or 0
    total_tokens = row[3] or 0

    if total_req == 0:
        console.print("[yellow]所选时间段内无数据[/yellow]")
        db.close()
        return

    if fmt_json:
        # JSON output
        model_rows = db.execute(
            f"SELECT model, provider, COUNT(*), SUM(input_tokens), SUM(output_tokens) "
            f"FROM requests {w}{extra} GROUP BY model ORDER BY SUM(input_tokens+output_tokens) DESC"
        ).fetchall()
        agent_rows = db.execute(
            f"SELECT agent, COUNT(*), SUM(input_tokens+output_tokens) "
            f"FROM requests {w}{extra} GROUP BY agent ORDER BY SUM(input_tokens+output_tokens) DESC"
        ).fetchall()
        prov_rows = db.execute(
            f"SELECT provider_name, COUNT(*), "
            f"SUM(CASE WHEN status=200 THEN 1 ELSE 0 END), "
            f"SUM(CASE WHEN status!=200 THEN 1 ELSE 0 END), "
            f"SUM(input_tokens), SUM(output_tokens) "
            f"FROM requests {w}{extra} GROUP BY provider_name ORDER BY SUM(input_tokens+output_tokens) DESC"
        ).fetchall()
        db.close()
        print(json.dumps({
            "period": compute_since_label(since),
            "total_requests": total_req,
            "success": total_ok,
            "failed": total_fail,
            "total_tokens": total_tokens,
            "by_model": [{"model": r[0], "provider": r[1], "requests": r[2],
                          "input_tokens": r[3], "output_tokens": r[4]} for r in model_rows],
            "by_agent": [{"agent": r[0], "requests": r[1], "tokens": r[2]} for r in agent_rows],
            "by_provider": [{"provider": r[0], "requests": r[1], "success": r[2],
                             "failed": r[3], "input_tokens": r[4], "output_tokens": r[5]} for r in prov_rows],
        }, indent=2, ensure_ascii=False))
        return

    # Render rich output
    since_label = compute_since_label(since)
    render_overview(total_req, total_ok, total_fail, total_tokens, since_label)

    # By model
    rows = db.execute(
        f"SELECT model, provider, COUNT(*), SUM(input_tokens), SUM(output_tokens) "
        f"FROM requests {w}{extra} GROUP BY model ORDER BY SUM(input_tokens+output_tokens) DESC LIMIT 15"
    ).fetchall()
    render_model_stats(rows)

    # By agent
    rows = db.execute(
        f"SELECT agent, COUNT(*), SUM(input_tokens+output_tokens) "
        f"FROM requests {w}{extra} GROUP BY agent ORDER BY SUM(input_tokens+output_tokens) DESC"
    ).fetchall()
    render_agent_stats(rows)

    # By provider
    rows = db.execute(
        f"SELECT provider_name, COUNT(*), "
        f"SUM(CASE WHEN status=200 THEN 1 ELSE 0 END), "
        f"SUM(CASE WHEN status!=200 THEN 1 ELSE 0 END), "
        f"SUM(input_tokens), SUM(output_tokens) "
        f"FROM requests {w}{extra} GROUP BY provider_name ORDER BY SUM(input_tokens+output_tokens) DESC"
    ).fetchall()
    render_provider_stats(rows)

    # Hourly trend (last 24h)
    rows = db.execute(
        f"SELECT substr(timestamp,12,5), COUNT(*), "
        f"SUM(CASE WHEN status!=200 THEN 1 ELSE 0 END) "
        f"FROM requests WHERE timestamp >= datetime('now', '-24 hours', 'localtime') "
        f"{extra.replace('WHERE', 'AND') if extra else ''} "
        f"GROUP BY substr(timestamp,12,5) ORDER BY timestamp"
    ).fetchall()
    if rows:
        render_hourly_trend(rows)

    db.close()


def main():
    import argparse
    ap = argparse.ArgumentParser(description="AI Proxy — 使用统计")
    ap.add_argument("--since", default="7d", help="时间范围: today, 24h, 7d, 30d, 或日期")
    ap.add_argument("--json", action="store_true", dest="fmt_json", help="JSON 格式输出")
    ap.add_argument("--model", default="", help="筛选模型")
    ap.add_argument("--provider", default="", help="筛选供应商")
    args = ap.parse_args()

    if not STATS_DB.exists() or STATS_DB.stat().st_size == 0:
        console.print("[yellow]尚无统计数据[/yellow]")
        console.print("启动 [bold]ai-proxy[/bold] 并发送请求后会自动记录")
        return

    cmd_stats(args.since, args.fmt_json, args.model, args.provider)


if __name__ == "__main__":
    main()
