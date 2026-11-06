"""Command-line entry for both TUIs (edu_tui.py / sim_tui.py / Codrone-TUI.py call into here)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .backend import MockBackend
from .config import load_config


def build_app(kind: str, args: argparse.Namespace):
    cfg = load_config()
    demo = bool(args.demo)
    if kind == "edu":
        from .edu_app import EduApp as App

        if demo:
            backend = MockBackend("edu")
        else:
            from .backend.edu import EduBackend

            backend = EduBackend()
    else:
        from .sim_app import SimApp as App

        if demo:
            backend = MockBackend("sim")
        else:
            from .backend.sim import SimBackend

            s = cfg["sim"]
            backend = SimBackend(verify=not args.no_verify and bool(s.get("verify", True)),
                                 cm_per_s=float(s.get("cm_per_s", 80.0)),
                                 vertical_cm_per_s=float(s.get("vertical_cm_per_s", 60.0)))
    return App(
        backend,
        config=cfg,
        port=args.port,
        autoconnect=bool(args.connect),
        demo=demo,
        theme=args.theme,
        field_path=Path(args.field) if args.field else None,
        command_log=False if args.no_log else None,
    )


def parse_args(kind: str, argv: list[str] | None = None) -> argparse.Namespace:
    desc = {"edu": "CoDrone EDU terminal flight console", "sim": "CoDrone Simulator terminal flight console"}[kind]
    ap = argparse.ArgumentParser(prog=f"{kind}_tui.py", description=desc)
    ap.add_argument("--port", help="EDU: serial port, e.g. COM5 · Sim: host:port (default 127.0.0.1:5000)")
    ap.add_argument("--connect", action="store_true", help="pair immediately on start")
    ap.add_argument("--demo", action="store_true", help="use a mock drone (no hardware, nothing flies)")
    ap.add_argument("--theme", help="start theme, e.g. btop, catppuccin-mocha, tokyo-night")
    ap.add_argument("--field", help="path to a field.toml (ADC field layout)")
    ap.add_argument("--no-log", action="store_true", help="do not write typed commands to logs/")
    if kind == "sim":
        ap.add_argument("--no-verify", action="store_true",
                        help="do not probe the simulator on connect (use if it never answers range queries)")
    else:
        ap.set_defaults(no_verify=False)
    return ap.parse_args(argv)


def safety_net(backend) -> None:
    """Last resort once the UI is gone (crash, closed window): never leave a drone airborne."""
    try:
        if backend.connected and backend.flying:
            print("TUI is exiting while the drone is flying -- landing it.", file=sys.stderr)
            backend.land()
    except BaseException as exc:  # noqa: BLE001
        print(f"could not land automatically ({type(exc).__name__}: {exc}) -- land it by hand!", file=sys.stderr)
    try:
        if backend.connected:
            backend.disconnect()
    except BaseException:  # noqa: BLE001
        pass


def main(kind: str, argv: list[str] | None = None) -> int:
    args = parse_args(kind, argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    app = build_app(kind, args)
    try:
        app.run()
    finally:
        safety_net(app.backend)
    return 0
