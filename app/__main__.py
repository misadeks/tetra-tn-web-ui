"""CLI entry point.

    python -m app                 # run the servers + dashboard
    python -m app --simulate      # also launch the bundled fake stack
    python -m app --config x.toml # use a specific config file
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from .config import load_config
from .dashboard import run_dashboard
from .hub import Hub
from .stack_servers import poll_state_loop, run_control_server, run_telemetry_server


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="app", description="TNMM Demo UI — BlueStation MS interface server")
    p.add_argument("--config", default="config.toml", help="path to config.toml")
    p.add_argument("--simulate", action="store_true", help="also start the bundled fake stack")
    p.add_argument("--chaos", action="store_true", help="with --simulate: enable chaos (drops/failures)")
    p.add_argument("--control-port", type=int, help="override control port")
    p.add_argument("--telemetry-port", type=int, help="override telemetry port")
    p.add_argument("--dashboard-port", type=int, help="override dashboard port")
    p.add_argument(
        "--tls", action="store_true",
        help="serve the dashboard over HTTPS with an auto-generated self-signed "
             "cert (cached in ~/.tnmm_ui/tls). REQUIRED for microphone/uplink "
             "voice when opening the UI from another device over the LAN, since "
             "browsers only allow the mic in a secure (HTTPS/localhost) context.")
    p.add_argument("--tls-cert", metavar="FILE", help="PEM certificate for HTTPS (implies --tls)")
    p.add_argument("--tls-key", metavar="FILE", help="PEM private key for HTTPS (implies --tls)")
    p.add_argument(
        "--log-ws", nargs="?", const="", default=None, metavar="FILE",
        help="log every stack WebSocket frame (control + telemetry, both "
             "directions) to FILE as JSONL. Omit FILE for an auto-named file; "
             "frames also print at DEBUG (-v).")
    p.add_argument(
        "--log-ws-console", action="store_true",
        help="print every stack WebSocket frame (JSONL) to the console. Can be "
             "used with or without --log-ws.")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


async def _run_fake_stack(cfg, chaos: bool) -> None:
    # Import lazily so the app has no hard dependency on the simulator.
    import fake_stack
    await fake_stack.run(
        control_url=_ws_url(cfg.command),
        telemetry_url=_ws_url(cfg.telemetry),
        credentials=cfg.command.credentials,
        chaos=chaos,
    )


def _ws_url(chan) -> str:
    scheme = "wss" if chan.use_tls else "ws"
    host = "127.0.0.1" if chan.host in ("0.0.0.0", "") else chan.host
    return f"{scheme}://{host}:{chan.port}"


async def main_async(args: argparse.Namespace) -> None:
    cfg = load_config(args.config)
    if args.control_port:
        cfg.command.port = args.control_port
    if args.telemetry_port:
        cfg.telemetry.port = args.telemetry_port
    if args.dashboard_port:
        cfg.dashboard.port = args.dashboard_port

    hub = Hub()
    hub.registration_type = cfg.registration.registration_type

    # Optional HTTPS for the dashboard so the mic (uplink voice) works when the
    # UI is opened from another device over the LAN — browsers only expose
    # getUserMedia in a secure (HTTPS/localhost) context.
    ssl_context = None
    want_tls = args.tls or args.tls_cert or args.tls_key
    if want_tls:
        from . import tls
        if args.tls_cert or args.tls_key:
            if not (args.tls_cert and args.tls_key):
                raise SystemExit("--tls-cert and --tls-key must be given together")
            cert_path, key_path = Path(args.tls_cert), Path(args.tls_key)
        else:
            cert_path, key_path = tls.ensure_self_signed()
        ssl_context = tls.build_ssl_context(cert_path, key_path)

    wire = None
    if args.log_ws is not None or args.log_ws_console:
        from .wirelog import WireLog, default_path
        # A file is only opened when --log-ws is given; --log-ws-console alone
        # prints to the console without writing a file.
        path = None
        if args.log_ws is not None:
            path = args.log_ws or default_path()
        wire = WireLog(path, console=args.log_ws_console)
        hub.wire_log = wire
    tasks = [
        asyncio.create_task(run_control_server(hub, cfg.command), name="control"),
        asyncio.create_task(run_telemetry_server(hub, cfg.telemetry), name="telemetry"),
        asyncio.create_task(run_dashboard(hub, cfg.dashboard, ssl_context), name="dashboard"),
        asyncio.create_task(poll_state_loop(hub, cfg.dashboard.poll_interval), name="poller"),
    ]

    scheme = "https" if ssl_context is not None else "http"
    print("\n  TNMM Demo UI")
    print(f"  dashboard : {scheme}://{cfg.dashboard.host}:{cfg.dashboard.port}")
    print(f"  control   : {cfg.command.host}:{cfg.command.port}  (stack dials in)")
    print(f"  telemetry : {cfg.telemetry.host}:{cfg.telemetry.port}  (stack dials in)")
    if ssl_context is not None:
        print("  tls       : on (self-signed OK; accept the browser warning)")
    if args.simulate:
        print("  fake stack: enabled (--simulate)")
        await asyncio.sleep(0.5)  # let servers bind first
        tasks.append(asyncio.create_task(_run_fake_stack(cfg, args.chaos), name="fake-stack"))
    if wire is not None:
        dest = []
        if wire.path:
            dest.append(wire.path)
        if wire.console:
            dest.append("console")
        print(f"  ws log    : {', '.join(dest)}  (all stack frames, JSONL)")
    print("  Ctrl+C to stop.\n")

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass
    finally:
        if wire is not None:
            wire.close()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print("\nstopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
