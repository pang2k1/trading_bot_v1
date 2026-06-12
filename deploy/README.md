# Deploy — fresh server setup

Templates matching the production Hetzner server. Paths assume the bot lives at `/root/bot_new`; adjust if different.

```bash
# 1. Code + environment
git clone https://github.com/pang2k1/trading_bot_v1.git /root/bot_new
cd /root/bot_new
apt install -y python3-venv
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env && nano .env     # fill in all keys

# 2. Smoke test before going live
python -m pytest tests/ -q
python live_trader.py --once --live   # expect a [llm-shadow] decision line

# 3. systemd services
cp deploy/trading-bot.service deploy/trading-bot-ui.service deploy/trading-optimizer.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now trading-bot trading-bot-ui trading-optimizer

# 4. cron jobs (reflection + daily email)
(crontab -l 2>/dev/null; cat deploy/crontab.txt) | crontab -

# 5. Dashboard access (tailnet-only, never expose publicly)
curl -fsSL https://tailscale.com/install.sh | sh
tailscale up
tailscale serve --bg 8080
```

Notes: outbound SMTP works on port 587 only (Hetzner blocks 25/465 by default). `chmod 600 .env`. The web UI binds 127.0.0.1 — Tailscale serve is the only intended way in.
