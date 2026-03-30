#!/usr/bin/env python3
"""
Auto-refresh Access Token for flow2api accounts without 2FA.

Uses each token's captcha_proxy_url for IP affinity (each account always
uses the same proxy/IP). Falls back to local proxy_bridge (localhost:3128)
if no per-token proxy is set.

Chromium cannot inject proxy credentials into HTTPS CONNECT tunnels via
Playwright's context.proxy — the tunnel hangs. Fix: spin up a temporary
local proxy_bridge subprocess (unauthenticated) that wraps the upstream
authenticated proxy, then point Chromium at localhost.

Usage:
  python3 refresh-at.py                    # Refresh all eligible tokens
  python3 refresh-at.py --token-id 2       # Refresh specific token
  python3 refresh-at.py --check-only       # Only check which tokens need refresh

Requires: playwright (pip install playwright && playwright install chromium)
"""

import argparse
import contextlib
import os
import socket
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout

DB_PATH = "/home/max/flow2api/data/flow.db"
PROXY_BRIDGE_PATH = "/home/max/flow2api/proxy_bridge.py"
FALLBACK_PROXY = "http://127.0.0.1:3128"
FLOW_URL = "https://labs.google/fx/tools/flow"
SESSION_URL = "https://labs.google/fx/api/auth/session"
# Refresh tokens expiring within this window
REFRESH_WITHIN_HOURS = 2


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def find_free_port():
    """Find an available localhost port."""
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


@contextlib.contextmanager
def local_proxy_for(upstream_url):
    """Wrap an authenticated upstream proxy in a local unauthenticated proxy_bridge.

    Chromium cannot inject Proxy-Authorization headers into CONNECT tunnels
    when the proxy requires auth. proxy_bridge adds credentials internally,
    so Chromium sees a no-auth localhost proxy.

    Falls back to the upstream URL directly if it's already localhost.
    """
    parsed = urlparse(upstream_url)
    if parsed.hostname in ("127.0.0.1", "localhost"):
        # Already local — no wrapper needed
        yield upstream_url
        return

    port = find_free_port()
    local_url = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(
        [sys.executable, PROXY_BRIDGE_PATH, upstream_url, "--port", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    # Wait until port is accepting connections
    for _ in range(20):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                break
        except OSError:
            time.sleep(0.1)

    try:
        yield local_url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()


def get_tokens_to_refresh(db, token_id=None):
    """Get tokens eligible for auto-refresh."""
    query = """
        SELECT id, email, google_password, at_expires, is_active, captcha_proxy_url
        FROM tokens
        WHERE has_2fa = 0
          AND google_password IS NOT NULL
          AND google_password != ''
    """
    if token_id:
        query += f" AND id = {int(token_id)}"

    rows = db.execute(query).fetchall()
    result = []
    now = datetime.now(timezone.utc)

    for row in rows:
        tid, email, password, at_expires_str, is_active, captcha_proxy_url = row
        needs_refresh = False

        if not is_active:
            needs_refresh = True
            reason = "disabled"
        elif at_expires_str:
            try:
                at_expires = datetime.fromisoformat(at_expires_str.replace("+00:00", "+00:00"))
                if at_expires.tzinfo is None:
                    at_expires = at_expires.replace(tzinfo=timezone.utc)
                if at_expires - now < timedelta(hours=REFRESH_WITHIN_HOURS):
                    needs_refresh = True
                    remaining = at_expires - now
                    reason = f"expires in {int(remaining.total_seconds() / 60)}min"
                else:
                    reason = f"valid for {int((at_expires - now).total_seconds() / 3600)}h"
            except (ValueError, TypeError):
                needs_refresh = True
                reason = "invalid expiry"
        else:
            needs_refresh = True
            reason = "no expiry set"

        result.append({
            "id": tid,
            "email": email,
            "password": password,
            "proxy_url": (captcha_proxy_url or "").strip() or None,
            "needs_refresh": needs_refresh,
            "reason": reason,
        })

    return result


def refresh_token(email, password, proxy_url=None):
    """Login to Google and extract fresh AT from Flow session.

    Uses per-token proxy_url for IP affinity via a local proxy_bridge wrapper.
    Falls back to proxy_bridge (localhost:3128) if no per-token proxy is set.
    """
    upstream = proxy_url or FALLBACK_PROXY

    with local_proxy_for(upstream) as local_proxy:
        proxy_label = upstream.split("@")[-1] if "@" in upstream else upstream
        log(f"  Launching Chrome (via {proxy_label} -> {local_proxy})...")

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=False,
                args=[
                    "--no-sandbox",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            context = browser.new_context(
                ignore_https_errors=True,
                proxy={"server": local_proxy},
            )
            page = context.new_page()

            try:
                # Step 1: Google login
                log(f"  Navigating to Google login...")
                page.goto("https://accounts.google.com/signin", timeout=30000)
                page.wait_for_load_state("networkidle", timeout=15000)
                time.sleep(1)

                # Enter email
                log(f"  Entering email: {email}")
                email_input = page.wait_for_selector('input[type="email"]', timeout=10000)
                email_input.fill(email)
                page.click("#identifierNext")
                time.sleep(3)
                page.wait_for_load_state("networkidle", timeout=15000)

                # Enter password
                log(f"  Entering password...")
                pw_input = page.wait_for_selector('input[type="password"]:visible', timeout=10000)
                pw_input.fill(password)
                page.click("#passwordNext")
                time.sleep(4)
                page.wait_for_load_state("networkidle", timeout=20000)

                current_url = page.url
                if "challenge" in current_url and "pwd" not in current_url:
                    raise RuntimeError(f"2FA or challenge required: {current_url}")

                log(f"  Login OK, navigating to Flow...")

                # Step 2: Navigate to Flow
                page.goto(FLOW_URL, timeout=30000, wait_until="networkidle")
                time.sleep(3)

                # Step 3: Might need to click "Create with Flow" to trigger session
                try:
                    create_btn = page.query_selector('button:has-text("Create with Flow")')
                    if create_btn:
                        log(f"  Clicking 'Create with Flow'...")
                        create_btn.click()
                        time.sleep(5)
                        page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass

                # Step 4: Extract AT from session endpoint
                log(f"  Fetching session...")
                session_data = page.evaluate("""
                    async () => {
                        const resp = await fetch('%s', { credentials: 'include' });
                        return await resp.json();
                    }
                """ % SESSION_URL)

                at = session_data.get("access_token")
                user_email = session_data.get("user", {}).get("email")

                if not at or len(at) < 50:
                    raise RuntimeError(
                        f"No access_token in session response. "
                        f"hasUser={bool(session_data.get('user'))}, "
                        f"email={user_email}"
                    )

                log(f"  Got AT ({len(at)} chars) for {user_email}")
                return at

            except PwTimeout as e:
                raise RuntimeError(f"Timeout: {e}")
            finally:
                context.close()
                browser.close()


def update_token_at(db, token_id, at):
    """Update token's AT in the database."""
    expires = (datetime.now(timezone.utc) + timedelta(hours=14)).strftime(
        "%Y-%m-%d %H:%M:%S+00:00"
    )
    db.execute(
        "UPDATE tokens SET at = ?, at_expires = ?, is_active = 1 WHERE id = ?",
        (at, expires, token_id),
    )
    db.commit()
    log(f"  DB updated: at_expires={expires}, is_active=1")


def main():
    parser = argparse.ArgumentParser(description="Auto-refresh flow2api Access Tokens")
    parser.add_argument("--token-id", type=int, help="Refresh specific token ID")
    parser.add_argument("--check-only", action="store_true", help="Only check, don't refresh")
    parser.add_argument("--db", default=DB_PATH, help="Path to flow2api database")
    args = parser.parse_args()

    db = sqlite3.connect(args.db)
    tokens = get_tokens_to_refresh(db, args.token_id)

    if not tokens:
        log("No auto-refreshable tokens found")
        return

    refreshed = 0
    errors = 0

    for token in tokens:
        status = "NEEDS REFRESH" if token["needs_refresh"] else "OK"
        log(f"Token {token['id']} ({token['email']}): {status} ({token['reason']})")

        if args.check_only or not token["needs_refresh"]:
            continue

        try:
            at = refresh_token(token["email"], token["password"], token.get("proxy_url"))
            update_token_at(db, token["id"], at)
            refreshed += 1
            log(f"Token {token['id']}: REFRESHED")
        except Exception as e:
            errors += 1
            log(f"Token {token['id']}: FAILED — {e}")

    db.close()
    log(f"Done: {refreshed} refreshed, {errors} errors")

    if errors > 0 and refreshed == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
