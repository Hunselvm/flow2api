"""
Local proxy bridge: listens on localhost:18080 (no auth) and forwards
all requests to the upstream authenticated proxy.

Chrome --proxy-server=http://127.0.0.1:18080 → this bridge → upstream proxy with auth

Uses asyncio for concurrency. Handles both HTTP and CONNECT (HTTPS) methods.
"""
import asyncio
import base64
import sys

LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 18080

# Read upstream from argv or env
UPSTREAM_HOST = None
UPSTREAM_PORT = None
UPSTREAM_AUTH = None  # base64-encoded "user:pass"


def parse_upstream(url: str):
    """Parse proxy URL like http://user:pass@host:port"""
    global UPSTREAM_HOST, UPSTREAM_PORT, UPSTREAM_AUTH
    url = url.strip()
    if url.startswith("http://"):
        url = url[7:]
    elif url.startswith("https://"):
        url = url[8:]

    auth = None
    if "@" in url:
        creds, url = url.rsplit("@", 1)
        auth = base64.b64encode(creds.encode()).decode()

    host, port = url.split(":")
    UPSTREAM_HOST = host
    UPSTREAM_PORT = int(port)
    UPSTREAM_AUTH = auth


async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def handle_client(client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter):
    try:
        # Read the first line to get the method
        first_line = await asyncio.wait_for(client_reader.readline(), timeout=30)
        if not first_line:
            client_writer.close()
            return

        # Read remaining headers
        headers = [first_line]
        while True:
            line = await asyncio.wait_for(client_reader.readline(), timeout=10)
            headers.append(line)
            if line == b"\r\n" or line == b"\n" or not line:
                break

        # Connect to upstream proxy
        up_reader, up_writer = await asyncio.wait_for(
            asyncio.open_connection(UPSTREAM_HOST, UPSTREAM_PORT),
            timeout=15
        )

        # Inject Proxy-Authorization header
        modified_headers = []
        for h in headers:
            modified_headers.append(h)
            # After the first line (request line), inject auth header
            if h == first_line and UPSTREAM_AUTH:
                modified_headers.append(
                    f"Proxy-Authorization: Basic {UPSTREAM_AUTH}\r\n".encode()
                )

        # Send modified request to upstream
        up_writer.write(b"".join(modified_headers))
        await up_writer.drain()

        # Bidirectional pipe
        await asyncio.gather(
            pipe(client_reader, up_writer),
            pipe(up_reader, client_writer),
        )
    except Exception:
        pass
    finally:
        try:
            client_writer.close()
        except Exception:
            pass


async def main():
    if len(sys.argv) < 2:
        print("Usage: python proxy_bridge.py <upstream_proxy_url>")
        print("  e.g.: python proxy_bridge.py http://user:pass@host:port")
        sys.exit(1)

    parse_upstream(sys.argv[1])
    print(f"Proxy bridge: 127.0.0.1:{LISTEN_PORT} → {UPSTREAM_HOST}:{UPSTREAM_PORT} (auth={'yes' if UPSTREAM_AUTH else 'no'})")

    server = await asyncio.start_server(handle_client, LISTEN_HOST, LISTEN_PORT)
    print(f"Listening on {LISTEN_HOST}:{LISTEN_PORT}")

    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
