"""Command-line interface: the ``preview-publish`` command.

Mirrors the interface of the classic ``client/preview-publish.sh`` so muscle
memory carries over, and adds the remaining API endpoints (--info, --expiry,
--health, --save-config) plus --open and --json.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import webbrowser
from pathlib import Path

from . import __version__
from .client import PreviewClient, PreviewError, parse_expiry
from .config import resolve, save_config

PROG = "preview-publish"


def _print_json(payload) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def _fmt_size(n) -> str:
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return "?"


def _fmt_span(seconds) -> str:
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return "?"
    for size, name in ((86400, "day"), (3600, "hour"), (60, "minute")):
        if seconds >= size:
            n = seconds // size
            return f"{n} {name}{'s' if n != 1 else ''}"
    return f"{seconds} seconds"


def _fmt_remaining(seconds) -> str:
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return "?"
    if seconds <= 0:
        return "expired"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h left"
    if hours:
        return f"{hours}h {minutes}m left"
    return f"{minutes}m left"


def _fmt_ts(ts) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(ts)))
    except (TypeError, ValueError, OSError):
        return "?"


def _build_parser() -> argparse.ArgumentParser:
    epilog = """\
expiry formats:
  7        7 days
  2m       2 minutes (also Ns / Nh / Nd)

examples:
  preview-publish ./site/ my-site 14
  preview-publish ./page.html
  preview-publish --dry-run ./site/ my-site
  preview-publish --list
  preview-publish --delete <hash>
  preview-publish --expiry <hash> --action extend --days 30
"""
    ap = argparse.ArgumentParser(
        prog=PROG,
        description="Publish short-lived HTML previews to a html-previewer service.",
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("path", nargs="?", help="folder, .zip, or single .html to publish")
    ap.add_argument("topic", nargs="?", help="URL label (default: derived from path)")
    ap.add_argument("expiry_spec", nargs="?", metavar="EXPIRY",
                    help="N days, or N{s,m,h,d} e.g. 2m (default: server default)")
    ap.add_argument("--version", action="version", version=f"{PROG} {__version__}")

    g = ap.add_argument_group("actions (instead of publishing)")
    g.add_argument("--dry-run", action="store_true",
                   help="predict update-in-place vs new URL (no upload)")
    g.add_argument("--list", action="store_true", help="list live previews")
    g.add_argument("--list-all", action="store_true",
                   help="list all previews incl. expired/deleted")
    g.add_argument("--info", metavar="HASH", help="show one preview's record")
    g.add_argument("--delete", metavar="HASH", help="reclaim a preview now")
    g.add_argument("--expiry", metavar="HASH",
                   help="admin expiry control (requires --action)")
    g.add_argument("--action", choices=("extend", "expire", "renew"),
                   help="expiry action for --expiry (days via --days)")
    g.add_argument("--days", type=int, metavar="N",
                   help="days for --action extend/renew (default: 30/7)")
    g.add_argument("--health", action="store_true", help="check server liveness")
    g.add_argument("--save-config", action="store_true",
                   help="save --base/--key (or env) to the config file and exit")

    g = ap.add_argument_group("options")
    g.add_argument("--new", action="store_true",
                   default=os.environ.get("PREVIEW_NEW") == "1",
                   help="force a brand-new URL instead of same-name update "
                        "(env: PREVIEW_NEW=1)")
    g.add_argument("--open", action="store_true",
                   help="open the published URL in a browser")
    g.add_argument("--json", action="store_true", dest="as_json",
                   help="print raw JSON responses")
    g.add_argument("--base", metavar="URL", help="API base URL (env: PREVIEW_API_BASE)")
    g.add_argument("--key", metavar="KEY", help="API key (env: PREVIEW_API_KEY)")
    g.add_argument("--config", metavar="FILE",
                   help="config file path (default: platform config dir)")
    return ap


# ---- output -------------------------------------------------------------

def _print_publish(res: dict) -> None:
    action = "updated in place" if res.get("updated") else "created"
    print(f"published topic={res.get('topic')} ({action})")
    print(f"  url:     {res.get('url')}")
    print(f"  expires: {_fmt_ts(res.get('expires_at'))} "
          f"(in {_fmt_span(res.get('ttl_seconds'))})")
    print(f"  size:    {_fmt_size(res.get('size_bytes'))}, "
          f"{res.get('file_count', '?')} files")
    if not res.get("has_index_html"):
        note = ("auto-generated directory index" if res.get("auto_index")
                else "WARNING: no index.html")
        print(f"  index:   {note}")


def _print_check(res: dict) -> None:
    print(f"dry-run: topic={res.get('topic')}")
    if res.get("action") == "update":
        print(f"  would UPDATE in place (same URL): {res.get('url')}")
    else:
        print("  would CREATE a new URL")


def _print_list(previews: list) -> None:
    if not previews:
        print("no previews")
        return
    for p in previews:
        print(f"{p.get('hash')}  {p.get('topic')}  {p.get('status')}"
              f"  ({_fmt_remaining(p.get('seconds_remaining'))},"
              f" {_fmt_size(p.get('size_bytes'))},"
              f" {p.get('file_count', '?')} files)")
        print(f"  {p.get('url')}")


def _print_preview(p: dict, title: str) -> None:
    print(title)
    for k in ("hash", "topic", "url", "status", "days", "size_bytes",
              "file_count", "archived"):
        if k in p:
            print(f"  {k + ':':12} {p[k]}")
    print(f"  expires:     {_fmt_ts(p.get('expires_at'))} "
          f"({_fmt_remaining(p.get('seconds_remaining'))})")


# ---- command ------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.expiry and not args.action:
        print(f"{PROG}: --expiry requires --action extend|expire|renew",
              file=sys.stderr)
        return 2
    if args.action and not args.expiry:
        print(f"{PROG}: --action requires --expiry HASH", file=sys.stderr)
        return 2

    cfg = resolve(base=args.base, key=args.key, config_path=args.config)

    if args.save_config:
        path = save_config(base=cfg["base"], key=cfg["key"],
                           path=cfg["config_path"])
        print(f"saved config to {path}")
        return 0

    client = PreviewClient(cfg["base"], api_key=cfg["key"])

    try:
        if args.health:
            res = client.health()
            if args.as_json:
                _print_json(res)
            else:
                print(f"ok: {cfg['base']} is alive")
            return 0

        if args.list or args.list_all:
            previews = client.list_previews(all=args.list_all)
            if args.as_json:
                _print_json({"previews": previews})
            else:
                _print_list(previews)
            return 0

        if args.info:
            res = client.info(args.info)
            if args.as_json:
                _print_json(res)
            else:
                _print_preview(res.get("preview", {}), f"preview {args.info}")
            return 0

        if args.delete:
            res = client.delete(args.delete)
            if args.as_json:
                _print_json(res)
            else:
                print(f"deleted {res.get('hash')} "
                      f"(live copy reclaimed; git archive kept)")
            return 0

        if args.expiry:
            res = client.expiry(args.expiry, args.action, days=args.days)
            if args.as_json:
                _print_json(res)
            else:
                _print_preview(res.get("preview", {}),
                               f"{args.action}: preview {args.expiry}")
            return 0

        # default action: publish (or dry-run)
        if not args.path:
            print(f"{PROG}: a path is required to publish "
                  f"(or use --list/--delete/--info/...)", file=sys.stderr)
            return 2
        path = Path(args.path)
        if not path.exists():
            print(f"{PROG}: no such file or directory: {path}", file=sys.stderr)
            return 1

        if args.dry_run:
            res = client.publish(path, topic=args.topic, dry_run=True,
                                 new=args.new)
            if args.as_json:
                _print_json(res)
            else:
                _print_check(res)
            return 0

        parse_expiry(args.expiry_spec)  # fail fast on a bad spec, before zipping
        res = client.publish(path, topic=args.topic, expiry=args.expiry_spec,
                             new=args.new)
        if args.as_json:
            _print_json(res)
        else:
            _print_publish(res)
        if args.open and res.get("url"):
            webbrowser.open(res["url"])
        return 0

    except PreviewError as e:
        print(f"{PROG}: {e}", file=sys.stderr)
        return 1
    except ValueError as e:
        print(f"{PROG}: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print(f"{PROG}: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
