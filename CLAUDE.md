# flow2api — Mini PC Setup

## Overview

flow2api bridges Google Flow (Gemini) to an OpenAI-compatible API. This instance runs on a mini PC (Ubuntu) and serves as the generation backend for the **GenBatch** web app (separate repo: `Hunselvm/Video-Next`).

## Architecture

```
GenBatch web app → flow2api API (this machine, port 8000) → Google Flow (via proxy)
```

All outbound traffic to Google MUST go through the proxy bridge. Direct connections risk IP bans.

## Services (systemd)

| Service | Command | What it does |
|---|---|---|
| `flow2api` | `sudo systemctl restart flow2api` | Main API server (Python, port 8000) |
| `flow2api-captcha-browser` | `sudo systemctl restart flow2api-captcha-browser` | Chrome with captcha-solving extension |
| `proxy-bridge` | `sudo systemctl restart proxy-bridge` | Forwards localhost:3128 → Oxylabs residential proxy |

Check all: `sudo systemctl status flow2api flow2api-captcha-browser proxy-bridge`

## Cron Jobs

- **Token refresh**: every 1 hour, runs `refresh-at.py` via xvfb (headless Chrome)
- Log: `/var/log/flow2api-token-update.log`

## Key Files

| File | Purpose |
|---|---|
| `main.py` | Entry point for flow2api server |
| `config/setting.toml` | Main config (admin password, API keys, ports) — gitignored |
| `proxy_bridge.py` | Custom proxy bridge script |
| `refresh-at.py` | Headless Chrome token refresh for Google accounts |
| `create-captcha-profile.py` | Creates Playwright Chrome profile with Google session |
| `start-captcha-browser.sh` | Launches captcha browser with the right flags |
| `flow2api.db` | SQLite DB with accounts, tokens, tasks — gitignored |
| `/etc/flow2api.env` | Environment vars (proxy URL, admin pass) |

## Custom Scripts (our additions, not upstream flow2api)

- `refresh-at.py` — refreshes access tokens for all active accounts via headless Chrome
- `proxy_bridge.py` — local proxy that forwards to Oxylabs residential proxy
- `create-captcha-profile.py` — sets up Chrome profile with Google login for captcha solving
- `start-captcha-browser.sh` — starts Chrome with captcha extension and correct profile
- `update-flow2api-token.sh` — legacy token update script
- `refresh-token.py` — alternative token refresh approach

## Critical Rules

1. **NEVER disable or bypass the proxy**. All Google-bound traffic must go through `localhost:3128` (proxy-bridge). Swap to a different proxy if current one has issues.
2. **Browser profiles contain active Google sessions** — do not delete `captcha_profile/` or `refresh_profiles/` without understanding the impact.
3. **flow2api.db** contains all account data and tokens. Back up before destructive operations.
4. **config/setting.toml** has the admin password and API key. It is gitignored for a reason.

## Admin Panel

- URL: `http://localhost:8000` (on this machine)
- Login: `admin` / password in `config/setting.toml` (`admin_password` field)
- Use it to: manage accounts, check token status, view tasks, enable/disable accounts

## Upstream

- Forked from: `TheSmallHanCat/flow2api`
- Remote `upstream` tracks the original. Pull updates with: `git fetch upstream && git merge upstream/main`
- Our custom scripts and modifications live on top of upstream code

## Debugging

- flow2api logs: `journalctl -u flow2api -f`
- captcha browser: `journalctl -u flow2api-captcha-browser -f`  
- proxy bridge: `journalctl -u proxy-bridge -f`
- token refresh: `tail -f /var/log/flow2api-token-update.log`
- SQLite: `sqlite3 flow2api.db` (check accounts, tokens, tasks)
