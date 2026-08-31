"""Long-running StockWatch sidecar service entry point."""

from __future__ import annotations

import argparse
import os
import signal
import threading
from pathlib import Path

from stockwatch_app.controller import ApplicationController
from stockwatch_app.web import DashboardServer


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="StockWatch local macOS service")
    parser.add_argument("--host", default="127.0.0.1", choices=("127.0.0.1",))
    parser.add_argument("--port", type=int, default=int(os.environ.get("STOCKWATCH_PORT", "8765")))
    parser.add_argument("--support-dir", type=Path)
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--ready-file", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    source_root = (args.source_root or Path(__file__).resolve().parents[1]).resolve()
    controller = ApplicationController(source_root=source_root, support_root=args.support_dir)
    server = DashboardServer(controller, host=args.host, port=args.port)
    stopped = threading.Event()

    def request_stop(_signum=None, _frame=None) -> None:
        stopped.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    controller.start()
    server.start()
    if args.ready_file:
        args.ready_file.parent.mkdir(parents=True, exist_ok=True)
        args.ready_file.write_text(server.url, encoding="utf-8")
    print(f"StockWatch dashboard: {server.url}", flush=True)
    try:
        stopped.wait()
    finally:
        if args.ready_file:
            args.ready_file.unlink(missing_ok=True)
        server.stop()
        controller.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

