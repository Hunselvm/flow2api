#!/usr/bin/env python3
"""
Create a Playwright Chromium persistent profile with a Google session
for the captcha extension browser.

Picks the first eligible no-2FA account, logs in via Playwright,
navigates to labs.google/fx to establish session, then exits.
The resulting profile at PROFILE_DIR can be used with raw Chrome CLI.

Usage:
  xvfb-run --auto-servernum python3 create-captcha-profile.py
  xvfb-run --auto-servernum python3 create-captcha-profile.py --token-id 2
"""

import argparse
import contextlib
import os
import socket
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout

DB_PATH = "/home/max/flow2api/data/flow.db"
PROFILE_DIR = "/home/max/flow2api/captcha_profile"
PROXY_BRIDGE_PATH = "/home/max/flow2api/proxy_bridge.py"
FALLBACK_PROXY = "http://127.0.0.1:3128"


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def find_free_port():
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


@contextlib.contextmanager
def local_proxy_for(upstream_url):
    parsed = urlparse(upstream_url)
    if parsed.hostname in ("127.0.0.1", "localhost"):
        yield upstream_url
        return

    port = find_free_port()
    local_url = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(
        [sys.executable, PROXY_BRIDGE_PATH, upstream_url, "--port", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
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


def pick_account(db, token_id=None):
    query = """
        SELECT id, email, google_password, captcha_proxy_url
        FROM tokens
        WHERE has_2fa = 0
          AND google_password IS NOT NULL
          AND google_password != ''
          AND is_active = 1
    """
    if token_id:
        query += f" AND id = {int(token_id)}"
    query += " LIMIT 1"
    row = db.execute(query).fetchone()
    if not row:
        raise RuntimeError("No eligible account found")
    return {"id": row[0], "email": row[1], "password": row[2], "proxy_url": (row[3] or "").strip() or None}


def create_profile(email, password, proxy_url=None):
    upstream = proxy_url or FALLBACK_PROXY

    with local_proxy_for(upstream) as local_proxy:
        proxy_label = upstream.split("@")[-1] if "@" in upstream else upstream
        log(f"Launching Chromium (via {proxy_label})...")

        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                user_data_dir=PROFILE_DIR,
                headless=False,
                proxy={"server": local_proxy},
                args=[
                    "--no-sandbox",
                    "--disable-blink-features=AutomationControlled",
                ],
                ignore_https_errors=True,
            )
            page = context.pages[0] if context.pages else context.new_page()

            try:
                # Google login
                log(f"Navigating to Google login...")
                page.goto("https://accounts.google.com/signin", timeout=30000)
                page.wait_for_load_state("networkidle", timeout=15000)
                time.sleep(1)

                log(f"Entering email: {email}")
                email_input = page.wait_for_selector('input[type="email"]', timeout=10000)
                email_input.fill(email)
                page.click("#identifierNext")
                time.sleep(3)
                page.wait_for_load_state("networkidle", timeout=15000)

                log(f"Entering password...")
                pw_input = page.wait_for_selector('input[type="password"]:visible', timeout=10000)
                pw_input.fill(password)
                page.click("#passwordNext")
                time.sleep(4)
                page.wait_for_load_state("networkidle", timeout=20000)

                current_url = page.url
                if "challenge" in current_url and "pwd" not in current_url:
                    raise RuntimeError(f"2FA or challenge required: {current_url}")

                log(f"Login OK, navigating to labs.google/fx...")
                page.goto("https://labs.google/fx", timeout=30000, wait_until="networkidle")
                time.sleep(3)

                # Verify we're actually logged in
                if "accounts.google.com" in page.url:
                    raise RuntimeError(f"Redirected to login — session not established: {page.url}")

                log(f"Session established at {page.url}")
                log(f"Saving profile to {PROFILE_DIR}...")

            except PwTimeout as e:
                raise RuntimeError(f"Timeout: {e}")
            finally:
                context.close()

    log(f"Profile saved to {PROFILE_DIR}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--token-id", type=int, help="Use specific token ID")
    parser.add_argument("--db", default=DB_PATH)
    args = parser.parse_args()

    # Clean existing profile
    import shutil
    if os.path.exists(PROFILE_DIR):
        log(f"Removing existing profile at {PROFILE_DIR}...")
        shutil.rmtree(PROFILE_DIR)

    db = sqlite3.connect(args.db)
    account = pick_account(db, args.token_id)
    db.close()

    log(f"Using account: {account['email']} (token {account['id']})")
    create_profile(account["email"], account["password"], account.get("proxy_url"))
    log("Done! Profile ready for captcha browser service.")


if __name__ == "__main__":
    main()
