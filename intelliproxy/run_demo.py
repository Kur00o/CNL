#!/usr/bin/env python3
"""
One-command demo: starts the local test origin AND the proxy, then opens the dashboard.

    python3 run_demo.py

  test origin    http://127.0.0.1:9000     (and https://localhost:9443, self-signed)
  proxy          http://127.0.0.1:8888     <- point clients here
  dashboard      http://127.0.0.1:8888/

Press Ctrl-C to stop; the proxy prints its final cache-analysis report.
"""
import argparse
import threading
import webbrowser

import origin_server
import proxy


def main():
    ap = argparse.ArgumentParser(description="Start test origin + IntelliProxy for a demonstration")
    ap.add_argument("--proxy-port", type=int, default=8888)
    ap.add_argument("--origin-port", type=int, default=9000)
    ap.add_argument("--https-port", type=int, default=9443, help="0 disables the HTTPS test origin")
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    try:
        _, https_srv = origin_server.start_origin(a.origin_port, a.https_port)
    except OSError as exc:
        print(f"Cannot start the test origin on port {a.origin_port}: {exc}\n"
              f"Use --origin-port to pick another port, or stop the old demo first.")
        return 1
    print(f"Test origin: http://127.0.0.1:{a.origin_port}/   (hit counters: /__hits)")
    if https_srv:
        print(f"             https://localhost:{a.https_port}/   (self-signed; use curl -k)")
    if not a.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(f"http://127.0.0.1:{a.proxy_port}/")).start()
    return proxy.main(["--port", str(a.proxy_port), "-v"])


if __name__ == "__main__":
    raise SystemExit(main())
