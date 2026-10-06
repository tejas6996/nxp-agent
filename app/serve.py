"""
Start the web UI and open it in the browser.

    uv run app/serve.py                  # http://localhost:8000 (this computer only)
    uv run app/serve.py --host 0.0.0.0   # also reachable from other computers on the network
"""

import argparse
import threading
import webbrowser

import uvicorn

from app.settings import get_settings


def main() -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="News digest web UI")
    parser.add_argument("--host", default="127.0.0.1", help="Interface to listen on (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=settings.app_port, help="Port (default: APP_PORT)")
    parser.add_argument("--no-browser", action="store_true", help="Don't open the browser")
    args = parser.parse_args()

    url = f"http://localhost:{args.port}"
    print(f"News Digest UI running at {url}  (press Ctrl+C to stop)")
    if not args.no_browser:
        threading.Timer(1.5, webbrowser.open, [url]).start()
    uvicorn.run("app.main:app", host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
