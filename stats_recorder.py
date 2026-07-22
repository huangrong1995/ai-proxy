"""Stats recording module — called by server.py to log request data to SQLite."""
import json, sqlite3, datetime
from pathlib import Path

STATS_DB = Path(__file__).resolve().parent / "proxy_stats.db"

def init_stats_db():
    conn = sqlite3.connect(str(STATS_DB))
    conn.execute("""CREATE TABLE IF NOT EXISTS requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT NOT NULL, provider TEXT NOT NULL,
        provider_name TEXT NOT NULL, model TEXT NOT NULL,
        agent TEXT NOT NULL DEFAULT 'unknown',
        input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0,
        status INTEGER NOT NULL, cache_hit INTEGER DEFAULT 0
    )""")
    conn.commit(); conn.close()

def record_request(pid, pname, model, status, inp=0, out=0, cache=False, agent="unknown"):
    try:
        conn = sqlite3.connect(str(STATS_DB))
        conn.execute(
            "INSERT INTO requests (timestamp,provider,provider_name,model,agent,"
            "input_tokens,output_tokens,status,cache_hit) VALUES (?,?,?,?,?,?,?,?,?)",
            (datetime.datetime.now().isoformat(timespec="seconds"),
             pid, pname, model, agent, inp, out, status, 1 if cache else 0))
        conn.commit(); conn.close()
    except Exception:
        pass

def extract_tokens(body, client_type):
    try:
        d = json.loads(body)
        if client_type == "anthropic":
            u = d.get("usage", {}) or {}
            return (u.get("input_tokens",0) or 0, u.get("output_tokens",0) or 0,
                    (u.get("cache_read_input_tokens",0) or 0) > 0)
        u = d.get("usage", {}) or {}
        return (u.get("prompt_tokens",0) or 0, u.get("completion_tokens",0) or 0, False)
    except:
        return 0, 0, False

def get_response_model(body):
    try: return json.loads(body).get("model", "?")
    except: return "?"

def detect_agent(client_type, path, headers):
    ua = headers.get("User-Agent","").lower()
    if "claude" in ua: return "Claude Code"
    if "codex" in ua or "openai" in ua: return "Codex CLI"
    if "gemini" in ua: return "Gemini CLI"
    return "Claude Code" if client_type == "anthropic" else "Codex CLI" if client_type == "openai_chat" else "Other"
