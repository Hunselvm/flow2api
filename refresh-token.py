#!/usr/bin/env python3
"""
Refresh Google session by launching Playwright persistent Chrome,
navigating to Google Flow, triggering session rotation via the
next-auth session endpoint, and extracting the refreshed token.

Usage:
  python3 refresh-token.py --user-data-dir /path/to/browser_data [--timeout 30] [--headed]

Exits 0 with token on stdout, or 1 on failure (expired session, timeout, etc).
"""

import argparse
import datetime
import sys
import time
from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout

COOKIE_NAME = "__Secure-next-auth.session-token"
SESSION_URL = "https://labs.google/fx/api/auth/session"


def get_session_token(context):
    """Extract session token cookie and its expiry from the browser context."""
    cookies = context.cookies(["https://labs.google.com", "https://labs.google"])
    for c in cookies:
        if c["name"] == COOKIE_NAME:
            return c["value"], c.get("expires", -1)
    return None, None


def format_expiry(expiry_ts):
    """Format a cookie expiry timestamp for logging."""
    if not expiry_ts or expiry_ts <= 0:
        return "unknown"
    exp_dt = datetime.datetime.fromtimestamp(expiry_ts, tz=datetime.timezone.utc)
    remaining = exp_dt - datetime.datetime.now(datetime.timezone.utc)
    return f"{exp_dt.isoformat()} ({remaining.days}d {remaining.seconds // 3600}h remaining)"


def main():
    parser = argparse.ArgumentParser(description="Refresh Google session token via Playwright")
    parser.add_argument("--user-data-dir", required=True, help="Chrome user-data-dir (e.g. browser_data/)")
    parser.add_argument("--timeout", type=int, default=30, help="Page load timeout in seconds")
    parser.add_argument("--headed", action="store_true", help="Run with visible browser window (for initial login)")
    args = parser.parse_args()

    timeout_ms = args.timeout * 1000
    chrome_args = ["--no-sandbox", "--proxy-server=http://127.0.0.1:3128"]

    with sync_playwright() as p:
        # Try system Chrome first (has real cookies from manual login, avoids captcha flags)
        try:
            context = p.chromium.launch_persistent_context(
                args.user_data_dir,
                headless=not args.headed,
                channel="chrome",
                args=chrome_args,
            )
        except Exception:
            # Fall back to bundled Chromium
            try:
                context = p.chromium.launch_persistent_context(
                    args.user_data_dir,
                    headless=not args.headed,
                    args=chrome_args,
                )
            except Exception as e:
                print(f"Failed to launch Chrome for {args.user_data_dir}: {e}", file=sys.stderr)
                sys.exit(1)

        try:
            page = context.new_page()

            # Step 1: Navigate to Flow page to establish browser context
            try:
                page.goto("https://labs.google/fx/tools/flow", wait_until="networkidle", timeout=timeout_ms)
            except PwTimeout:
                print(f"Page load timeout for {args.user_data_dir}", file=sys.stderr)
                sys.exit(1)

            # Step 2: Read old cookie for comparison
            old_token, old_expiry = get_session_token(context)
            if not old_token:
                print(
                    f"No session token found before refresh for {args.user_data_dir} — re-login via VNC needed",
                    file=sys.stderr,
                )
                sys.exit(1)

            print(f"Old token expiry: {format_expiry(old_expiry)}", file=sys.stderr)

            # Step 3: Call session endpoint from browser context to trigger rotation
            # The fetch runs inside the page so cookies are sent and Set-Cookie is processed
            session_result = None
            try:
                session_result = page.evaluate("""
                    async () => {
                        try {
                            const resp = await fetch('https://labs.google/fx/api/auth/session', { credentials: 'include' });
                            if (!resp.ok) return { error: resp.status };
                            const data = await resp.json();
                            return { ok: true, hasUser: !!data.user, expires: data.expires || null };
                        } catch (e) {
                            return { error: e.message };
                        }
                    }
                """)
                print(f"Session endpoint response: {session_result}", file=sys.stderr)
            except Exception as e:
                print(f"page.evaluate() failed: {e} — trying direct navigation fallback", file=sys.stderr)

            # Step 4: Fallback — navigate directly to session endpoint
            if not session_result or "error" in session_result:
                try:
                    page.goto(SESSION_URL, wait_until="networkidle", timeout=timeout_ms)
                    body = page.inner_text("body")
                    print(f"Direct session navigation response: {body[:200]}", file=sys.stderr)
                except Exception as e2:
                    print(f"Direct session navigation also failed: {e2}", file=sys.stderr)

            # Step 5: Wait for cookie processing, then re-read
            time.sleep(2)
            new_token, new_expiry = get_session_token(context)

            # Step 6: Detect expired session
            if session_result and isinstance(session_result, dict):
                if session_result.get("ok") and not session_result.get("hasUser"):
                    print(
                        f"Session has no user — session expired for {args.user_data_dir}, re-login via VNC needed",
                        file=sys.stderr,
                    )
                    sys.exit(1)

            if not new_token:
                print(
                    f"Session expired or captcha required for {args.user_data_dir} — re-login via VNC needed",
                    file=sys.stderr,
                )
                sys.exit(1)

            # Step 7: Compare old vs new
            if new_token != old_token:
                print(f"Token ROTATED — new expiry: {format_expiry(new_expiry)}", file=sys.stderr)
            else:
                print(
                    f"Token NOT rotated (same value) — expiry: {format_expiry(new_expiry)}",
                    file=sys.stderr,
                )

            # Success — print token to stdout
            print(new_token)
        finally:
            context.close()


if __name__ == "__main__":
    main()
