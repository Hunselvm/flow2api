#!/usr/bin/env python3
"""
Local proxy bridge: unauthenticated localhost proxy that forwards to an
authenticated upstream (e.g. Smartproxy residential).

Usage:
    python3 proxy_bridge.py <upstream_proxy_url> [--port 3128]

Example:
    python3 proxy_bridge.py http://user:pass@gate.smartproxy.com:7777
    python3 proxy_bridge.py http://user:pass@gate.smartproxy.com:7777 --port 18080
"""

import asyncio
import base64
import sys
import signal
import argparse
import os
from urllib.parse import urlparse


def parse_upstream(url: str):
    """Parse upstream proxy URL into (host, port, auth_header)."""
    parsed = urlparse(url)
    host = parsed.hostname
    port = parsed.port or 8080
    auth_header = None
    if parsed.username:
        creds = f"{parsed.username}:{parsed.password or ''}"
        b64 = base64.b64encode(creds.encode()).decode()
        auth_header = f"Proxy-Authorization: Basic {b64}\r\n"
    return host, port, auth_header


async def handle_connect(reader, writer, upstream_host, upstream_port, upstream_auth):
    """Handle HTTPS CONNECT tunnel."""
    try:
        # Read the initial request line + headers
        request_line = await asyncio.wait_for(reader.readline(), timeout=30)
        if not request_line:
            writer.close()
            return

        request_str = request_line.decode("utf-8", errors="replace")
        headers = []
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout=30)
            if line in (b"\r\n", b"\n", b""):
                break
            headers.append(line)

        if request_str.upper().startswith("CONNECT"):
            # HTTPS tunnel: connect to upstream and forward CONNECT
            try:
                up_reader, up_writer = await asyncio.wait_for(
                    asyncio.open_connection(upstream_host, upstream_port),
                    timeout=15,
                )
            except Exception:
                writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                writer.close()
                return

            # Forward CONNECT to upstream with auth
            up_writer.write(request_line)
            if upstream_auth:
                up_writer.write(upstream_auth.encode())
            for h in headers:
                up_writer.write(h)
            up_writer.write(b"\r\n")
            await up_writer.drain()

            # Read upstream response
            up_response = await asyncio.wait_for(up_reader.readline(), timeout=15)
            # Forward all upstream headers back to client
            response_headers = [up_response]
            while True:
                h = await asyncio.wait_for(up_reader.readline(), timeout=15)
                response_headers.append(h)
                if h in (b"\r\n", b"\n", b""):
                    break
            for h in response_headers:
                writer.write(h)
            await writer.drain()

            # Bidirectional pipe
            await asyncio.gather(
                _pipe(reader, up_writer),
                _pipe(up_reader, writer),
            )
        else:
            # HTTP request: forward with auth header injected
            try:
                up_reader, up_writer = await asyncio.wait_for(
                    asyncio.open_connection(upstream_host, upstream_port),
                    timeout=15,
                )
            except Exception:
                writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                writer.close()
                return

            up_writer.write(request_line)
            if upstream_auth:
                up_writer.write(upstream_auth.encode())
            for h in headers:
                up_writer.write(h)
            up_writer.write(b"\r\n")
            await up_writer.drain()

            # Pipe response back
            await asyncio.gather(
                _pipe(reader, up_writer),
                _pipe(up_reader, writer),
            )
    except (asyncio.TimeoutError, ConnectionError, OSError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def _pipe(reader, writer):
    """Pipe data from reader to writer until EOF."""
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError, asyncio.CancelledError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def main():
    parser = argparse.ArgumentParser(description="Local proxy bridge")
    parser.add_argument("upstream", nargs="?", default=None, help="Upstream proxy URL (e.g. http://user:pass@host:port). Falls back to UPSTREAM_PROXY_URL env var.")
    parser.add_argument("--port", type=int, default=3128, help="Local listen port (default: 3128)")
    parser.add_argument("--host", default="127.0.0.1", help="Local listen host (default: 127.0.0.1)")
    args = parser.parse_args()

    upstream_url = args.upstream or os.environ.get('UPSTREAM_PROXY_URL')
    if not upstream_url:
        parser.error('Upstream proxy URL required: pass as argument or set UPSTREAM_PROXY_URL env var')
    upstream_host, upstream_port, upstream_auth = parse_upstream(upstream_url)
    print(f"Proxy bridge: {args.host}:{args.port} -> {upstream_host}:{upstream_port} (auth={'yes' if upstream_auth else 'no'})")

    server = await asyncio.start_server(
        lambda r, w: handle_connect(r, w, upstream_host, upstream_port, upstream_auth),
        args.host,
        args.port,
    )

    # Graceful shutdown
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.ensure_future(_shutdown(server)))

    print(f"Listening on {args.host}:{args.port}")
    async with server:
        await server.serve_forever()


async def _shutdown(server):
    print("\nShutting down proxy bridge...")
    server.close()
    await server.wait_closed()


if __name__ == "__main__":
    asyncio.run(main())
