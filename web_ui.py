"""
web_ui.py  —  Interactive web dashboard for the trading bot.

Run:   python web_ui.py
URL:   http://YOUR_SERVER_IP:8080
Auth:  WEB_UI_USERNAME / WEB_UI_PASSWORD in .env

Add to .env:
    WEB_UI_USERNAME=admin
    WEB_UI_PASSWORD=choose_a_strong_password
"""

import asyncio
import importlib
import json
import os
import secrets
import signal
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from dotenv import load_dotenv

load_dotenv()

BOT_DIR     = Path(__file__).parent
LOG_FILE    = BOT_DIR / "live_trader.log"
STATE_FILE  = BOT_DIR / "trader_state.json"
TRADES_FILE = BOT_DIR / "trades_log.csv"
PARAMS_FILE = BOT_DIR / "best_params.json"

_INT_PARAMS = {"BB_PERIOD", "RSI_PERIOD", "RSI_LONG_ENTRY", "RSI_SHORT_ENTRY",
               "RSI_LONG_EXIT", "RSI_SHORT_EXIT", "EMA_TREND1", "EMA_TREND2"}
_TUNABLE_PARAMS = _INT_PARAMS | {
    "BB_STD", "STOP_LOSS_PCT", "RISK_PER_TRADE", "MAX_DAILY_LOSS_PCT"
}

app = FastAPI(docs_url=None, redoc_url=None)
security = HTTPBasic()


def authenticate(credentials: HTTPBasicCredentials = Depends(security)):
    username = os.getenv("WEB_UI_USERNAME", "admin")
    password = os.getenv("WEB_UI_PASSWORD", "changeme")
    ok = (
        secrets.compare_digest(credentials.username.encode(), username.encode()) and
        secrets.compare_digest(credentials.password.encode(), password.encode())
    )
    if not ok:
        raise HTTPException(status_code=401, headers={"WWW-Authenticate": "Basic"})
    return credentials.username


def _bot_pid() -> int | None:
    """Find the PID of the live trader process using exact match to avoid false positives."""
    try:
        out = subprocess.check_output(
            ["pgrep", "-f", r"python.*live_trader\.py"],
            text=True,
        ).strip()
        pids = [int(p) for p in out.splitlines() if p.strip()]
        return pids[0] if pids else None
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def _load_params() -> dict:
    import config as cfg
    importlib.reload(cfg)
    result = {k: getattr(cfg, k, None) for k in _TUNABLE_PARAMS}
    if PARAMS_FILE.exists():
        try:
            overrides = json.loads(PARAMS_FILE.read_text())
            for k, v in overrides.items():
                if k in _TUNABLE_PARAMS:
                    result[k] = v
        except Exception:
            pass
    return result


# ── API endpoints ─────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def dashboard(_: str = Depends(authenticate)):
    return HTMLResponse(HTML)


@app.get("/api/status")
def get_status(_: str = Depends(authenticate)):
    import config as cfg
    importlib.reload(cfg)
    pid = _bot_pid()
    state = {}
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {
        "running": pid is not None,
        "pid": pid,
        "positions": state,
        "margin_type": getattr(cfg, "MARGIN_TYPE", "cross"),
        "long_only": getattr(cfg, "LONG_ONLY", True),
        "time": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/api/config")
def get_config(_: str = Depends(authenticate)):
    return _load_params()


@app.post("/api/config")
async def set_config(request: Request, _: str = Depends(authenticate)):
    body = await request.json()
    current = _load_params()
    updated = dict(current)
    errors = []

    for k, v in body.items():
        if k not in _TUNABLE_PARAMS:
            errors.append(f"Unknown param: {k}")
            continue
        try:
            updated[k] = int(round(float(v))) if k in _INT_PARAMS else float(v)
        except (TypeError, ValueError):
            errors.append(f"Invalid value for {k}: {v!r}")

    if updated.get("RSI_LONG_ENTRY", 0) >= updated.get("RSI_LONG_EXIT", 100):
        errors.append("RSI Long Entry must be < RSI Long Exit")
    if updated.get("RSI_SHORT_EXIT", 0) >= updated.get("RSI_SHORT_ENTRY", 100):
        errors.append("RSI Short Exit must be < RSI Short Entry")
    if updated.get("EMA_TREND1", 0) >= updated.get("EMA_TREND2", 100):
        errors.append("EMA Trend 1 must be < EMA Trend 2")

    if errors:
        raise HTTPException(status_code=400, detail=errors)

    tmp = PARAMS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(updated, indent=2))
    tmp.replace(PARAMS_FILE)
    return {"ok": True, "params": updated}


@app.get("/api/trades")
def get_trades(_: str = Depends(authenticate)):
    if not TRADES_FILE.exists():
        return []
    try:
        import pandas as pd
        df = pd.read_csv(TRADES_FILE)
        return df.tail(50).to_dict(orient="records")
    except Exception:
        return []


@app.post("/api/bot/start")
def bot_start(request: Request, _: str = Depends(authenticate)):
    """Start the bot. Defaults to testnet. Requires explicit mode=live for real money."""
    if _bot_pid():
        return {"ok": False, "reason": "already running"}
    python = BOT_DIR / "venv" / "bin" / "python"
    if not python.exists():
        python = Path(sys.executable)

    # Default to testnet for safety
    args = [str(python), "live_trader.py"]

    # Check if live mode is explicitly requested via query param
    import asyncio
    mode = request.query_params.get("mode", "testnet")
    if mode == "live":
        args.append("--live")

    subprocess.Popen(
        args,
        cwd=str(BOT_DIR),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return {"ok": True, "mode": mode}


@app.post("/api/bot/stop")
def bot_stop(_: str = Depends(authenticate)):
    pid = _bot_pid()
    if not pid:
        return {"ok": False, "reason": "not running"}
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    return {"ok": True}


@app.get("/api/logs")
async def stream_logs(_: str = Depends(authenticate)):
    async def generate():
        if not LOG_FILE.exists():
            yield "data: (log file not found)\n\n"
            return
        with open(LOG_FILE) as f:
            for line in f.readlines()[-100:]:
                yield f"data: {line.rstrip()}\n\n"
            while True:
                line = f.readline()
                if line:
                    yield f"data: {line.rstrip()}\n\n"
                else:
                    await asyncio.sleep(0.5)
    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── HTML dashboard ────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Trading Bot</title>
<style>
:root{--bg:#0d1117;--bg2:#161b22;--bg3:#21262d;--border:#30363d;--text:#e6edf3;--muted:#8b949e;--green:#3fb950;--red:#f85149;--yellow:#d29922;--blue:#58a6ff}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:'Consolas','Monaco',monospace;font-size:13px;display:flex;flex-direction:column;height:100vh;overflow:hidden}
header{background:var(--bg2);border-bottom:1px solid var(--border);padding:10px 16px;display:flex;align-items:center;gap:12px;flex-shrink:0}
h1{font-size:15px;color:var(--blue);font-weight:bold}
.dot{width:9px;height:9px;border-radius:50%;background:var(--red);flex-shrink:0;transition:background .3s}
.dot.on{background:var(--green);box-shadow:0 0 8px var(--green)}
.spacer{flex:1}
button{background:var(--bg3);border:1px solid var(--border);color:var(--text);padding:5px 12px;border-radius:5px;cursor:pointer;font:inherit;font-size:12px;transition:all .15s}
button:hover{opacity:.85}
.btn-stop{border-color:var(--red);color:var(--red)}
.btn-start{border-color:var(--green);color:var(--green)}
.btn-apply{border-color:var(--blue);color:var(--blue);background:#1f6feb22;width:100%;padding:8px;margin-top:8px;font-size:13px}
.muted{color:var(--muted);font-size:11px}
.main{display:grid;grid-template-columns:210px 1fr 270px;flex:1;overflow:hidden;min-height:0}
.pane{border-right:1px solid var(--border);padding:14px;overflow-y:auto;display:flex;flex-direction:column;gap:10px}
.pane:last-child{border-right:none}
h2{font-size:10px;text-transform:uppercase;letter-spacing:1px;color:var(--muted);padding-bottom:7px;border-bottom:1px solid var(--border);margin-bottom:2px;flex-shrink:0}
.row{display:flex;justify-content:space-between;align-items:center;gap:8px;padding:2px 0}
.row span:first-child{color:var(--muted)}
.card{background:var(--bg3);border-radius:5px;padding:10px;display:flex;flex-direction:column;gap:6px}
.card.long{border-left:3px solid var(--blue)}
.card.short{border-left:3px solid var(--yellow)}
.green{color:var(--green)}.red{color:var(--red)}.blue{color:var(--blue)}.yellow{color:var(--yellow)}
.cfg-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.field{display:flex;flex-direction:column;gap:3px}
.field label{color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.5px}
.field input{background:var(--bg);border:1px solid var(--border);color:var(--text);padding:5px 8px;border-radius:4px;font:inherit;font-size:12px;width:100%;transition:border-color .15s}
.field input:focus{outline:none;border-color:var(--blue)}
.alert{padding:7px 10px;border-radius:4px;font-size:12px;margin-top:4px}
.alert.ok{background:#3fb95015;border:1px solid var(--green);color:var(--green)}
.alert.err{background:#f8514915;border:1px solid var(--red);color:var(--red)}
.trade-card{background:var(--bg3);border-radius:4px;padding:9px;border-left:3px solid var(--border);display:flex;flex-direction:column;gap:4px}
.trade-card.win{border-left-color:var(--green)}
.trade-card.loss{border-left-color:var(--red)}
.log-wrap{background:#010409;border-top:1px solid var(--border);height:210px;overflow-y:auto;padding:8px 14px;flex-shrink:0}
.ll{white-space:pre-wrap;word-break:break-all;line-height:1.6;font-size:11.5px}
.ll.W{color:var(--yellow)}.ll.E{color:var(--red)}
.ll .ts{color:#484f58}.ll .lv{color:var(--blue)}
</style>
</head>
<body>

<header>
  <div class="dot" id="dot"></div>
  <h1>&#x1F916; Trading Bot</h1>
  <span class="muted" id="pid-lbl"></span>
  <div class="spacer"></div>
  <span class="muted" id="upd"></span>&nbsp;&nbsp;
  <button id="tog" onclick="toggleBot()">—</button>
</header>

<div class="main">

  <!-- STATUS -->
  <div class="pane">
    <h2>Status</h2>
    <div id="status-body"><span class="muted">Loading…</span></div>
  </div>

  <!-- CONFIG -->
  <div class="pane">
    <h2>Parameters &nbsp;<span class="muted" style="text-transform:none;letter-spacing:0">(hot-reloaded within 15 min)</span></h2>
    <div class="cfg-grid" id="cfg-grid"></div>
    <div id="cfg-msg" style="display:none"></div>
    <button class="btn-apply" onclick="applyConfig()">&#x25B6; Apply &amp; Hot-Reload</button>
  </div>

  <!-- TRADES -->
  <div class="pane">
    <h2>Recent Trades</h2>
    <div id="trades-body"><span class="muted">Loading…</span></div>
  </div>

</div>

<div class="log-wrap" id="log"></div>

<script>
const PARAMS = {
  BB_PERIOD:          {l:'BB Period',         int:true,  step:1,   pct:false},
  BB_STD:             {l:'BB Std Dev',         int:false, step:0.1, pct:false},
  RSI_PERIOD:         {l:'RSI Period',         int:true,  step:1,   pct:false},
  RSI_LONG_ENTRY:     {l:'RSI Long Entry',     int:true,  step:1,   pct:false},
  RSI_SHORT_ENTRY:    {l:'RSI Short Entry',    int:true,  step:1,   pct:false},
  RSI_LONG_EXIT:      {l:'RSI Long Exit',      int:true,  step:1,   pct:false},
  RSI_SHORT_EXIT:     {l:'RSI Short Exit',     int:true,  step:1,   pct:false},
  EMA_TREND1:         {l:'EMA Trend 1',        int:true,  step:1,   pct:false},
  EMA_TREND2:         {l:'EMA Trend 2',        int:true,  step:1,   pct:false},
  STOP_LOSS_PCT:      {l:'Stop Loss %',        int:false, step:0.1, pct:true},
  RISK_PER_TRADE:     {l:'Risk / Trade %',     int:false, step:1,   pct:true},
  MAX_DAILY_LOSS_PCT: {l:'Max Daily Loss %',   int:false, step:1,   pct:true},
};

let botRunning = false;

// ── Status ───────────────────────────────────────────────────
async function pollStatus() {
  try {
    const d = await fetch('/api/status').then(r => r.json());
    botRunning = d.running;
    document.getElementById('dot').className = 'dot' + (d.running ? ' on' : '');
    document.getElementById('pid-lbl').textContent = d.running ? `PID ${d.pid}` : 'Stopped';
    const btn = document.getElementById('tog');
    btn.textContent = d.running ? 'Stop Bot' : 'Start Bot';
    btn.className   = d.running ? 'btn-stop' : 'btn-start';
    document.getElementById('upd').textContent = new Date().toLocaleTimeString();

    const pos = d.positions || {};
    const syms = Object.keys(pos);
    let html = '';
    if (!syms.length) {
      html = `<div class="row"><span>Position</span><span class="muted">None</span></div>`;
    } else {
      for (const s of syms) {
        const p = pos[s];
        const sideColor = p.side === 'long' ? 'blue' : 'yellow';
        const margin = d.margin_type || 'cross';
        html += `<div class="card ${p.side}">
          <div class="row"><b class="${sideColor}">${s}</b><span class="${sideColor}">${p.side.toUpperCase()} (${margin})</span></div>
          <div class="row"><span>Entry</span><span>$${(+p.entry_price).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2})}</span></div>
          <div class="row"><span>Stop</span><span class="red">$${(+p.stop_loss).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2})}</span></div>
          <div class="row"><span>Qty</span><span>${(+p.qty).toFixed(6)}</span></div>
          <div class="row"><span>Notional</span><span>$${((+p.qty)*(+p.entry_price)).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2})}</span></div>
        </div>`;
      }
    }
    document.getElementById('status-body').innerHTML = html;
  } catch(e) {}
}

// ── Config ───────────────────────────────────────────────────
async function loadConfig() {
  try {
    const data = await fetch('/api/config').then(r => r.json());
    const grid = document.getElementById('cfg-grid');
    grid.innerHTML = '';
    for (const [k, m] of Object.entries(PARAMS)) {
      let v = data[k] ?? 0;
      if (m.pct) v = (parseFloat(v) * 100).toFixed(m.step < 1 ? 2 : 1);
      else        v = m.int ? Math.round(v) : parseFloat(v).toFixed(2);
      grid.innerHTML += `<div class="field">
        <label>${m.l}</label>
        <input id="p_${k}" type="number" step="${m.pct ? m.step : m.step}" value="${v}">
      </div>`;
    }
  } catch(e) {}
}

async function applyConfig() {
  const payload = {};
  for (const [k, m] of Object.entries(PARAMS)) {
    const el = document.getElementById('p_' + k);
    if (!el) continue;
    let v = parseFloat(el.value);
    if (m.pct) v = v / 100;
    if (m.int) v = Math.round(v);
    payload[k] = v;
  }
  const msg = document.getElementById('cfg-msg');
  try {
    const r = await fetch('/api/config', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    });
    const d = await r.json();
    msg.style.display = 'block';
    if (r.ok) {
      msg.className = 'alert ok';
      msg.textContent = '✓ Saved — bot will pick up changes within 15 min';
    } else {
      msg.className = 'alert err';
      msg.textContent = '✗ ' + (Array.isArray(d.detail) ? d.detail.join(', ') : d.detail);
    }
  } catch(e) {
    msg.style.display = 'block';
    msg.className = 'alert err';
    msg.textContent = '✗ ' + e.message;
  }
  setTimeout(() => { msg.style.display = 'none'; }, 5000);
}

// ── Trades ───────────────────────────────────────────────────
async function loadTrades() {
  try {
    const trades = await fetch('/api/trades').then(r => r.json());
    const el = document.getElementById('trades-body');
    if (!trades.length) {
      el.innerHTML = '<span class="muted">No trades yet</span>';
      return;
    }
    el.innerHTML = [...trades].reverse().slice(0, 25).map(t => {
      const win = parseFloat(t.pnl_usd || 0) > 0;
      const pnl = (win ? '+' : '') + parseFloat(t.pnl_usd || 0).toFixed(4);
      const dt  = t.exit_time ? new Date(t.exit_time).toLocaleString() : '';
      return `<div class="trade-card ${win ? 'win' : 'loss'}">
        <div class="row">
          <span class="${win ? 'green' : 'red'}">${pnl} USDT</span>
          <span class="muted">${(t.side || '').toUpperCase()}</span>
        </div>
        <div class="row muted">
          <span>$${parseFloat(t.entry_price||0).toFixed(1)} → $${parseFloat(t.exit_price||0).toFixed(1)}</span>
          <span>${t.reason || ''}</span>
        </div>
        <div class="muted">${dt}</div>
      </div>`;
    }).join('');
  } catch(e) {}
}

// ── Live logs ────────────────────────────────────────────────
function startLogs() {
  const box = document.getElementById('log');
  const es  = new EventSource('/api/logs');
  es.onmessage = e => {
    if (!e.data) return;
    const div = document.createElement('div');
    let cls = 'll';
    if (/ERROR/.test(e.data))   cls += ' E';
    else if (/WARNING/.test(e.data)) cls += ' W';
    div.className = cls;
    const m = e.data.match(/^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})\s+(\S+)\s+([\s\S]*)$/);
    if (m) {
      const ts  = document.createElement('span'); ts.className = 'ts'; ts.textContent = m[1];
      const lv  = document.createElement('span'); lv.className = 'lv'; lv.textContent = m[2];
      const msg = document.createTextNode('  ' + m[3]);
      div.appendChild(ts); div.appendChild(document.createTextNode('  ')); div.appendChild(lv); div.appendChild(msg);
    } else {
      div.textContent = e.data;
    }
    box.appendChild(div);
    while (box.children.length > 300) box.removeChild(box.firstChild);
    box.scrollTop = box.scrollHeight;
  };
  es.onerror = () => { es.close(); setTimeout(startLogs, 3000); };
}

// ── Bot toggle ───────────────────────────────────────────────
async function toggleBot() {
  const ep = botRunning ? '/api/bot/stop' : '/api/bot/start';
  await fetch(ep, {method: 'POST'});
  setTimeout(pollStatus, 1200);
}

// ── Init ─────────────────────────────────────────────────────
pollStatus();
loadConfig();
loadTrades();
startLogs();
setInterval(pollStatus, 5000);
setInterval(loadTrades, 30000);
</script>
</body>
</html>"""


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--host", type=str, default="127.0.0.1",
                        help="Bind address (default: 127.0.0.1 — localhost only)")
    args = parser.parse_args()
    load_dotenv()
    if not os.getenv("WEB_UI_PASSWORD") or os.getenv("WEB_UI_PASSWORD") == "changeme":
        print("ERROR: WEB_UI_PASSWORD is not set or is still 'changeme'.")
        print("Set a strong password in .env before starting the dashboard.")
        sys.exit(1)
    print(f"Dashboard → http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")