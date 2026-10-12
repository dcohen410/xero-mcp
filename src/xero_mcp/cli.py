"""Command line: `xero-mcp configure | login | logout | status | serve`."""

from __future__ import annotations

import argparse
import os
import sys

from . import __version__, auth
from . import config as config_mod


def _configure(args: argparse.Namespace) -> int:
    cfg = config_mod.load()
    if args.client_id:
        cfg.client_id = args.client_id.strip()
    if args.port:
        cfg.port = args.port
    if args.local_dir:
        cfg.local_dirs = [os.path.expanduser(p) for p in args.local_dir]
    if args.approve_mode:
        cfg.approve_mode = args.approve_mode
    path = config_mod.save(cfg)
    print(f"Saved settings to {path}")
    if not cfg.client_id:
        print("Still needed: --client-id (from your Xero app at developer.xero.com).")
        return 1
    print(f"Make sure your Xero app's redirect URI is exactly {cfg.redirect_uri}")
    print("Next: run `xero-mcp login`.")
    return 0


def _login(args: argparse.Namespace) -> int:
    cfg = config_mod.load()
    org = auth.login(cfg, organisation=args.organisation, open_browser=not args.no_browser)
    cfg.tenant_id, cfg.tenant_name = org["tenant_id"], org["tenant_name"]
    config_mod.save(cfg)
    print(f"Signed in to Xero. Organisation: {cfg.tenant_name}")
    return 0


def _logout(_: argparse.Namespace) -> int:
    cfg = config_mod.load()
    auth.clear_refresh_token(cfg)
    cfg.tenant_id, cfg.tenant_name = "", ""
    config_mod.save(cfg)
    print("Signed out and removed the saved Xero sign-in.")
    return 0


def _status(_: argparse.Namespace) -> int:
    cfg = config_mod.load()
    print(f"xero-mcp {__version__}")
    print(f"Settings file:  {config_mod.config_path()}")
    print(f"Client ID:      {cfg.client_id or '(not set)'}")
    print(f"Redirect URI:   {cfg.redirect_uri}")
    print(f"Organisation:   {cfg.tenant_name or '(none; run xero-mcp login)'}")
    print(f"Signed in:      {'yes' if auth.load_refresh_token(cfg) else 'no'}")
    print(f"Local folders:  {', '.join(cfg.local_dirs) or '(none)'}")
    print(f"Approving:      {cfg.approve_mode}")
    return 0


def _serve(_: argparse.Namespace) -> int:
    from .server import run

    run()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="xero-mcp", description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("configure", help="save your Xero app's client ID and options")
    p.add_argument("--client-id", help="Client ID of your Xero 'Mobile or desktop app'")
    p.add_argument("--port", type=int, help=f"local sign-in port (default {config_mod.DEFAULT_PORT})")
    p.add_argument("--local-dir", action="append",
                   help="a folder on this computer the server may read and write (repeatable; default ~/Downloads)")
    p.add_argument("--approve-mode", choices=config_mod.APPROVE_MODES,
                   help="'disabled' (default): drafts only; 'confirm': Claude may approve bills after you confirm")
    p.set_defaults(func=_configure)

    p = sub.add_parser("login", help="sign in to Xero in your browser and pick the organisation")
    p.add_argument("--organisation", help="name (or part of it) of the organisation to use")
    p.add_argument("--no-browser", action="store_true", help="print the sign-in link instead of opening it")
    p.set_defaults(func=_login)

    sub.add_parser("logout", help="sign out and delete the saved sign-in").set_defaults(func=_logout)
    sub.add_parser("status", help="show settings and sign-in state").set_defaults(func=_status)
    sub.add_parser("serve", help="run the MCP server over stdio (what Claude runs)").set_defaults(func=_serve)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        args.func = _serve
    try:
        return args.func(args)
    except (auth.NotSignedIn, RuntimeError) as exc:
        print(exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
