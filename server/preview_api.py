#!/usr/bin/env python3
"""Preview upload/management API (stdlib only).

Listens on 127.0.0.1:8787 behind nginx (location ^~ /_api/). Auth via the
X-API-Key header (constant-time compare against PREVIEW_API_KEY).

Endpoints
  GET    /_api/health                      -> {ok:true}                (no auth)
  POST   /_api/publish                     -> publish a zip or single html
             query/header params:
               topic   (?topic= or X-Topic)   default "preview"
               days    (?days=  or X-Days)     default 7, max 30
               ttl     (?ttl=   or X-TTL)      expiry in SECONDS (overrides days)
               type    (?type=  or X-Type)     zip|html (auto-detected if absent)
               new     (?new=   or X-New)      1 to force a brand-new URL
               dry_run (?dry_run= or X-Dry-Run) 1 to predict only, write nothing
             body: raw bytes (zip archive, or html document)
             Same-name update (default): if a preview is already live with the
             same topic AND not past its expiry, its content is replaced in place
             (the URL stays the same and the expiry resets). A name whose previous
             preview has expired gets a brand-new hash/URL instead. new=1 always
             forces a fresh hash. If a zip has no top-level index.html, a browsable
             directory-listing index.html is auto-generated for every folder that
             lacks one.
  GET    /_api/check                       -> dry-run: would this name update or
             create? params topic, new (query or X-…). Changes nothing.
  GET    /_api/list[?all=1]                -> JSON listing of real previews
  GET    /_api/info/<hash>                 -> one preview's record
  POST   /_api/expiry/<hash>               -> admin expiry control (params:
             action=extend|expire|renew (query or X-Action), days (query/X-Days).
             extend: prolong a live preview by `days` (default 30) on top of its
             current expiry. expire: reclaim it now and mark expired (archive
             kept). renew: revive an expired one for `days` (default 7) from now,
             restoring the content from the git archive if it was already
             reclaimed (409 if it was never archived).
  DELETE /_api/preview/<hash>              -> reclaim now (git archive kept)

When PREVIEW_SERVE_STATIC=1 the process also serves the published preview trees
itself (public, no key) at /<hash>/<topic>/... so the service is fully
self-contained (no nginx) — used by the user-service and docker deployments.
With nginx in front (default), nginx serves static files and proxies /_api/.
"""
from __future__ import annotations

import base64
import hmac
import html
import io
import json
import mimetypes
import os
import secrets
import shutil
import struct
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

import preview_archive
import preview_common as C

ZIP_MAGIC = b"PK\x03\x04"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _now() -> int:
    return int(time.time())


def _parse_days(raw, default: int):
    """Parse a positive integer day count.

    Returns `default` when the value is absent/blank, the parsed int when it is a
    valid positive integer, or None when it is present but invalid (caller errors).
    """
    if raw is None or str(raw).strip() == "":
        return default
    try:
        d = int(str(raw).strip())
    except ValueError:
        return None
    return d if d >= 1 else None


def _check_key(headers) -> bool:
    if not C.API_KEY:
        return False
    supplied = headers.get("X-API-Key", "") or ""
    return hmac.compare_digest(supplied, C.API_KEY)


def _new_hash(conn) -> str:
    """Mint an unguessable, time-unique preview id.

    The id is RANDOM (never derived from the upload's *content*, so re-publishing
    an expired name always yields a brand-new id) and folds in a high-resolution
    timestamp so two publishes can never collide even in principle; it is still
    verified unique against every existing row (any status) before use.
    """
    while True:
        raw = os.urandom(C.HASH_BYTES) + struct.pack(">Q", time.time_ns())
        h = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
        # keep it clean for URLs / reserved prefixes
        if h[0] in "-_":
            continue
        if not conn.execute("SELECT 1 FROM previews WHERE hash=?", (h,)).fetchone():
            return h


def _find_live_match(conn, topic: str, now: int):
    """The preview a same-name publish would update: still live AND not past its
    expiry. (The reaper is lazy/hourly, so a row can be past expires_at while
    still status='live' — such a logically-expired name must NOT be updated.)"""
    return conn.execute(
        "SELECT * FROM previews WHERE topic=? AND status='live' AND expires_at > ? "
        "ORDER BY created_at DESC LIMIT 1",
        (topic, now),
    ).fetchone()


def _predict_action(conn, topic: str, force_new: bool, now: int) -> dict:
    """Decide whether a publish of `topic` would update in place or create anew,
    without changing anything (drives the dry-run / check endpoint)."""
    match = None if force_new else _find_live_match(conn, topic, now)
    if match:
        return {
            "action": "update",
            "hash": match["hash"],
            "url": C.public_url(match["hash"], match["topic"]),
            "expires_at": match["expires_at"],
            "expires_at_iso": time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(match["expires_at"])),
            "seconds_remaining": max(0, match["expires_at"] - now),
        }
    prev = conn.execute(
        "SELECT hash, status, expires_at FROM previews WHERE topic=? "
        "ORDER BY created_at DESC LIMIT 1",
        (topic,),
    ).fetchone()
    if force_new:
        reason = "forced new (new=1)"
    elif prev is None:
        reason = "no existing preview with this name"
    elif now >= prev["expires_at"]:
        reason = "previous preview with this name has expired"
    else:
        reason = "previous preview with this name is not live"
    out = {"action": "create", "reason": reason}
    if prev is not None:
        out["previous"] = {
            "hash": prev["hash"],
            "status": prev["status"],
            "expired": now >= prev["expires_at"],
        }
    return out


def _safe_extract(zf: zipfile.ZipFile, dest: str) -> tuple[int, int]:
    """Extract with zip-slip / bomb protection. Returns (file_count, total_bytes)."""
    dest_abs = os.path.realpath(dest)
    total = 0
    count = 0
    for info in zf.infolist():
        if info.is_dir():
            continue
        name = info.filename
        if name.startswith("/") or ".." in name.replace("\\", "/").split("/"):
            raise ValueError(f"unsafe path in zip: {name}")
        target = os.path.realpath(os.path.join(dest_abs, name))
        if not (target == dest_abs or target.startswith(dest_abs + os.sep)):
            raise ValueError(f"path escapes target: {name}")
        total += info.file_size
        count += 1
        if total > C.MAX_UNZIP_BYTES:
            raise ValueError("uncompressed size exceeds limit")
        if count > C.MAX_FILES:
            raise ValueError("too many files in archive")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with zf.open(info) as src, open(target, "wb") as out:
            shutil.copyfileobj(src, out)
    return count, total


def _flatten_single_wrapper(topic_dir: str) -> None:
    """If the content sits inside one wrapper subdir, lift it up one level so
    that index.html ends up directly under {topic}/ (keeps relative links sane).
    """
    if os.path.exists(os.path.join(topic_dir, "index.html")):
        return
    entries = os.listdir(topic_dir)
    if len(entries) != 1:
        return
    sub = os.path.join(topic_dir, entries[0])
    if not os.path.isdir(sub):
        return
    if not os.path.exists(os.path.join(sub, "index.html")):
        return
    for item in os.listdir(sub):
        shutil.move(os.path.join(sub, item), os.path.join(topic_dir, item))
    os.rmdir(sub)


def _dir_stats(path: str) -> tuple[int, int]:
    count = 0
    total = 0
    for root, _dirs, files in os.walk(path):
        for fn in files:
            count += 1
            try:
                total += os.path.getsize(os.path.join(root, fn))
            except OSError:
                pass
    return count, total


def _fmt_size(n: int) -> str:
    f = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024 or unit == "TB":
            return f"{int(f)} B" if unit == "B" else f"{f:.1f} {unit}"
        f /= 1024
    return f"{n} B"


_AUTOINDEX_CSS = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { margin:0; font-family: ui-sans-serif, system-ui, -apple-system, "Segoe UI",
  Roboto, Helvetica, Arial, sans-serif; background:#0b0d10; color:#e6e8eb; }
main { max-width: 860px; margin: 0 auto; padding: 40px 20px 64px; }
h1 { font-size: 18px; font-weight: 600; margin: 0 0 18px; word-break: break-all; }
h1 code { background:#161a1f; padding:3px 8px; border-radius:6px; color:#9cc4ff; font-size:15px; }
ul.listing { list-style:none; margin:0; padding:0; border:1px solid #1e242b; border-radius:10px; overflow:hidden; }
ul.listing li { display:flex; align-items:center; gap:10px; padding:10px 14px; border-top:1px solid #161b21; }
ul.listing li:first-child { border-top:none; }
ul.listing li:hover { background:#11151a; }
ul.listing a { color:#e6e8eb; text-decoration:none; flex:1; word-break:break-all; }
ul.listing a:hover { color:#9cc4ff; text-decoration:underline; }
li.dir a { color:#9cc4ff; font-weight:600; }
li.dir a::before { content:"\\1F4C1  "; }
li.file a::before { content:"\\1F4C4  "; }
.meta { color:#7a8290; font-size:12px; white-space:nowrap; }
li.empty { color:#7a8290; padding:14px; }
footer { color:#5c636e; font-size:12px; margin-top:16px; text-align:right; }
"""


def _render_autoindex(topic: str, rel: str, entries: list, is_root: bool) -> bytes:
    crumb = topic if not rel else f"{topic}/{rel}"
    rows = []
    if not is_root:
        rows.append('<li class="dir"><a href="../">../</a>'
                    '<span class="meta">parent directory</span></li>')
    for name, is_dir, size in entries:
        label = name + ("/" if is_dir else "")
        href = html.escape(label, quote=True)
        meta = "dir" if is_dir else _fmt_size(size)
        cls = "dir" if is_dir else "file"
        rows.append(f'<li class="{cls}"><a href="{href}">{html.escape(label)}</a>'
                    f'<span class="meta">{meta}</span></li>')
    body = "\n".join(rows) or '<li class="empty">(empty folder)</li>'
    doc = (
        "<!doctype html>\n<html lang=\"en\">\n<head>\n"
        "<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        "<meta name=\"robots\" content=\"noindex,nofollow\">\n"
        f"<title>Index of {html.escape(crumb)}</title>\n"
        f"<style>{_AUTOINDEX_CSS}</style>\n</head>\n<body>\n<main>\n"
        f"<h1>Index of <code>{html.escape(crumb)}</code></h1>\n"
        f"<ul class=\"listing\">\n{body}\n</ul>\n"
        f"<footer>{len(entries)} item(s) · auto-generated directory listing</footer>\n"
        "</main>\n</body>\n</html>\n"
    )
    return doc.encode("utf-8")


def _generate_autoindex(topic_dir: str, topic: str) -> int:
    """Synthesize a browsable directory listing when the uploaded tree has no
    landing page: create an index.html in every directory (recursively) that
    lacks one. Dotfiles/dotdirs are skipped (they are never served). Returns
    the number of index pages generated.
    """
    # Snapshot the directory set first so the index.html files we create do
    # not perturb the traversal.
    dirs = [topic_dir]
    for root, subdirs, _files in os.walk(topic_dir):
        subdirs[:] = sorted(d for d in subdirs if not d.startswith("."))
        dirs.extend(os.path.join(root, d) for d in subdirs)

    generated = 0
    for d in dirs:
        if os.path.exists(os.path.join(d, "index.html")) or \
           os.path.exists(os.path.join(d, "index.htm")):
            continue
        entries = []
        for name in sorted(os.listdir(d)):
            if name.startswith("."):
                continue
            full = os.path.join(d, name)
            is_dir = os.path.isdir(full)
            entries.append((name, is_dir, 0 if is_dir else _safe_size(full)))
        rel = os.path.relpath(d, topic_dir)
        rel = "" if rel == "." else rel
        page = _render_autoindex(topic, rel, entries, is_root=(d == topic_dir))
        with open(os.path.join(d, "index.html"), "wb") as f:
            f.write(page)
        generated += 1
    return generated


def _safe_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


_FALLBACK_404 = (
    b"<!doctype html><meta charset=utf-8><title>404 Not Found</title>"
    b"<body style='font-family:sans-serif;background:#0b0d10;color:#e6e8eb;"
    b"display:flex;height:100vh;align-items:center;justify-content:center;margin:0'>"
    b"<div style='text-align:center'><h1 style='font-size:64px;margin:0;color:#5b9dff'>404</h1>"
    b"<p>This preview link is invalid or has expired.</p></div></body>"
)


# ---------------------------------------------------------------------------
# request handler
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "preview/1.0"

    # ---- response helpers ----
    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _err(self, code: int, msg: str) -> None:
        self._json(code, {"ok": False, "error": msg})

    def log_message(self, fmt, *args):  # quieter logs to journald
        print("[api] " + (fmt % args), flush=True)

    @staticmethod
    def _route(path: str) -> str:
        p = urlsplit(path).path
        if p.startswith("/_api/"):
            p = p[len("/_api"):]
        return p.rstrip("/") or "/"

    def _params(self) -> dict:
        q = parse_qs(urlsplit(self.path).query)
        out = {k: v[0] for k, v in q.items()}
        for hk, pk in (("X-Topic", "topic"), ("X-Days", "days"),
                       ("X-Type", "type"), ("X-New", "new"),
                       ("X-TTL", "ttl"), ("X-Dry-Run", "dry_run"),
                       ("X-Action", "action")):
            if pk not in out and self.headers.get(hk):
                out[pk] = self.headers.get(hk)
        return out

    # ---- verbs ----
    def do_GET(self):
        raw = urlsplit(self.path).path
        route = self._route(self.path)
        if route == "/health":
            return self._json(200, {"ok": True, "service": "preview", "time": _now()})
        # Public static serving of published previews (self-contained mode).
        if C.SERVE_STATIC and not raw.startswith("/_api/"):
            return self._serve_static(raw)
        if not _check_key(self.headers):
            return self._err(401, "missing or invalid X-API-Key")
        if route == "/list":
            return self._handle_list()
        if route == "/check":
            return self._handle_check()
        if route.startswith("/info/"):
            return self._handle_info(route[len("/info/"):])
        return self._err(404, "unknown endpoint")

    def do_HEAD(self):
        # Allow HEAD on static content so browsers/curl -I work in standalone mode.
        if C.SERVE_STATIC and not urlsplit(self.path).path.startswith("/_api/"):
            return self._serve_static(urlsplit(self.path).path, head=True)
        self.send_response(200)
        self.end_headers()

    def do_DELETE(self):
        if not _check_key(self.headers):
            return self._err(401, "missing or invalid X-API-Key")
        route = self._route(self.path)
        if route.startswith("/preview/"):
            return self._handle_delete(route[len("/preview/"):])
        return self._err(404, "unknown endpoint")

    def do_POST(self):
        if not _check_key(self.headers):
            return self._err(401, "missing or invalid X-API-Key")
        route = self._route(self.path)
        if route == "/publish":
            return self._handle_publish()
        if route.startswith("/expiry/"):
            return self._handle_expiry(route[len("/expiry/"):])
        return self._err(404, "unknown endpoint")

    # ---- handlers ----
    def _serve_404(self, head: bool = False) -> None:
        try:
            with open(os.path.join(C.STATIC_DIR, "__notfound.html"), "rb") as f:
                page = f.read()
        except OSError:
            page = _FALLBACK_404
        self.send_response(404)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.end_headers()
        if not head:
            self.wfile.write(page)

    def _serve_static(self, raw_path: str, head: bool = False) -> None:
        rel = unquote(raw_path).lstrip("/")
        segments = [p for p in rel.split("/") if p]
        # Never serve dotfiles or allow path traversal.
        if any(p == ".." or p.startswith(".") for p in segments):
            return self._serve_404(head)
        root_abs = os.path.realpath(C.LIVE_ROOT)
        target = os.path.realpath(os.path.join(root_abs, rel))
        if target != root_abs and not target.startswith(root_abs + os.sep):
            return self._serve_404(head)
        if os.path.isdir(target):
            idx = os.path.join(target, "index.html")
            if not os.path.isfile(idx):
                return self._serve_404(head)
            target = idx
        if not os.path.isfile(target):
            return self._serve_404(head)
        ctype = mimetypes.guess_type(target)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in (
            "application/javascript", "application/json", "image/svg+xml",
        ):
            ctype += "; charset=utf-8"
        try:
            size = os.path.getsize(target)
        except OSError:
            return self._serve_404(head)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        if head:
            return
        with open(target, "rb") as f:
            shutil.copyfileobj(f, self.wfile)

    def _read_body(self) -> bytes | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._err(400, "bad Content-Length")
            return None
        if length <= 0:
            self._err(400, "empty body")
            return None
        if length > C.MAX_UPLOAD_BYTES:
            self._err(413, "upload too large")
            return None
        return self.rfile.read(length)

    def _handle_publish(self):
        params = self._params()
        topic = C.slugify_topic(params.get("topic", "preview"))

        # Same-name-update is the default: re-publishing under a name that is
        # already live (and not past its expiry) overwrites it in place (stable
        # URL). Opt out with ?new=1 / X-New: 1 to force a brand-new URL.
        force_new = str(params.get("new", "")).strip().lower() in ("1", "true", "yes")
        dry_run = str(params.get("dry_run", "")).strip().lower() in ("1", "true", "yes")

        # Expiry: ttl (seconds) takes precedence over days (back-compat). ttl
        # enables sub-day previews (e.g. a 2-minute doc).
        ttl_raw = params.get("ttl")
        if ttl_raw is not None:
            try:
                ttl = int(ttl_raw)
            except ValueError:
                return self._err(400, "ttl must be an integer number of seconds")
            if ttl < 1 or ttl > C.MAX_DAYS * 86400:
                return self._err(400, f"ttl must be between 1 and {C.MAX_DAYS * 86400} seconds")
            days_store = ttl // 86400
        else:
            try:
                days = int(params.get("days", C.DEFAULT_DAYS))
            except ValueError:
                return self._err(400, "days must be an integer")
            if days < 1 or days > C.MAX_DAYS:
                return self._err(400, f"days must be between 1 and {C.MAX_DAYS}")
            ttl = days * 86400
            days_store = days

        body = self._read_body()
        if body is None:
            return

        ptype = params.get("type")
        if not ptype:
            ptype = "zip" if body[:4] == ZIP_MAGIC else "html"
        if ptype not in ("zip", "html"):
            return self._err(400, "type must be zip or html")

        # Dry-run: report what a real publish WOULD do (update vs create) and
        # change nothing. (The body was drained above; for a body-less check use
        # GET /_api/check?topic=...)
        if dry_run:
            conn = C.connect()
            try:
                pred = _predict_action(conn, topic, force_new, _now())
            finally:
                conn.close()
            return self._json(200, {"ok": True, "dry_run": True, "topic": topic,
                                    "force_new": force_new, **pred})

        # Decide the target hash: reuse the live, not-yet-expired preview that
        # shares this name (so its URL stays stable and the content is replaced),
        # otherwise mint a fresh unguessable hash. A name whose previous preview
        # has expired (even if the reaper has not reclaimed it yet) gets a NEW one.
        match_now = _now()
        conn = C.connect()
        try:
            match = None if force_new else _find_live_match(conn, topic, match_now)
            if match:
                h = match["hash"]
                is_update = True
                created = match["created_at"]
            else:
                h = _new_hash(conn)
                is_update = False
                created = None  # set to publish time below
        finally:
            conn.close()

        # Build the new tree in a private staging dir on the SAME filesystem as
        # LIVE_ROOT, then swap it into place with an atomic rename. This means a
        # malformed re-upload can never destroy the previous good version, and a
        # reader never sees a half-written tree. The staging dir is hidden (dot
        # prefix) so it is never served (nginx denies dotpaths; the app skips them).
        staging_parent = os.path.join(C.LIVE_ROOT, ".staging")
        os.makedirs(staging_parent, exist_ok=True)
        stage_root = os.path.join(staging_parent, f"{h}.{secrets.token_hex(6)}")
        stage_topic_dir = os.path.join(stage_root, topic)
        os.makedirs(stage_topic_dir, exist_ok=True)

        try:
            if ptype == "zip":
                if body[:4] != ZIP_MAGIC:
                    raise ValueError("body is not a zip archive")
                with zipfile.ZipFile(io.BytesIO(body)) as zf:
                    _safe_extract(zf, stage_topic_dir)
                _flatten_single_wrapper(stage_topic_dir)
            else:  # html
                with open(os.path.join(stage_topic_dir, "index.html"), "wb") as f:
                    f.write(body)
        except Exception as e:  # noqa: BLE001
            shutil.rmtree(stage_root, ignore_errors=True)
            return self._err(400, f"failed to process upload: {e}")

        # No landing page? Synthesize a browsable directory listing so the
        # folder's contents are reachable via links.
        auto_index = False
        if not os.path.exists(os.path.join(stage_topic_dir, "index.html")):
            try:
                auto_index = _generate_autoindex(stage_topic_dir, topic) > 0
            except Exception as e:  # noqa: BLE001
                print(f"[api] autoindex error for {h}: {e}", flush=True)

        has_index = os.path.exists(os.path.join(stage_topic_dir, "index.html"))
        count, size = _dir_stats(stage_root)

        final_dir = os.path.join(C.LIVE_ROOT, h)
        try:
            self._swap_into_place(stage_root, final_dir, staging_parent)
        except Exception as e:  # noqa: BLE001
            shutil.rmtree(stage_root, ignore_errors=True)
            return self._err(500, f"failed to publish: {e}")

        now = _now()
        expires = now + ttl
        if not is_update:
            created = now
        client_ip = self.headers.get("X-Real-IP", self.client_address[0])

        conn = C.connect()
        try:
            if is_update:
                # Keep the original created_at (stable identity); refresh the
                # rest. archived=0 + archive_name=NULL re-queues archiving — the
                # archive entry is keyed on created_at+hash so it is rewritten in
                # place (git MODIFIED), not duplicated.
                conn.execute(
                    """UPDATE previews
                       SET topic=?, updated_at=?, expires_at=?, days=?,
                           size_bytes=?, file_count=?, status='live',
                           archived=0, archive_name=NULL, reclaimed_at=NULL,
                           client_ip=?
                       WHERE hash=?""",
                    (topic, now, expires, days_store, size, count, client_ip, h),
                )
            else:
                conn.execute(
                    """INSERT INTO previews
                       (hash, topic, created_at, updated_at, expires_at, days,
                        size_bytes, file_count, status, archived, client_ip)
                       VALUES (?,?,?,?,?,?,?,?, 'live', 0, ?)""",
                    (h, topic, created, now, expires, days_store, size, count, client_ip),
                )
            conn.commit()
        finally:
            conn.close()

        meta = {
            "hash": h, "topic": topic,
            "created_at": created, "updated_at": now,
            "expires_at": expires, "days": days_store, "ttl_seconds": ttl,
            "size_bytes": size, "file_count": count, "type": ptype,
            "updated": is_update,
        }
        # Archive in the background (best-effort; no-op while git archiving
        # disabled). Pass the ORIGINAL created_at so the archive entry path is
        # stable per hash: a same-name update rewrites it (git MODIFIED), a fresh
        # hash creates a new entry (git NEW).
        threading.Thread(
            target=self._archive_async, args=(h, topic, created, meta), daemon=True
        ).start()

        return self._json(200, {
            "ok": True,
            "hash": h,
            "topic": topic,
            "url": C.public_url(h, topic),
            "updated": is_update,
            "action": "update" if is_update else "create",
            "created_at": created,
            "updated_at": now,
            "expires_at": expires,
            "expires_at_iso": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(expires)),
            "days": days_store,
            "ttl_seconds": ttl,
            "size_bytes": size,
            "file_count": count,
            "has_index_html": has_index,
            "auto_index": auto_index,
            "archive_enabled": C.GIT_ENABLED,
        })

    @staticmethod
    def _swap_into_place(stage_root: str, final_dir: str, trash_parent: str) -> None:
        """Atomically replace final_dir with the freshly-built stage_root.

        Both live on the same filesystem (under LIVE_ROOT) so os.rename is
        atomic. Any previous tree is moved aside first and removed only after
        the swap succeeds; on failure it is restored.
        """
        backup = None
        if os.path.lexists(final_dir):
            backup = os.path.join(trash_parent, f"old-{secrets.token_hex(6)}")
            os.rename(final_dir, backup)
        try:
            os.rename(stage_root, final_dir)
        except Exception:
            if backup is not None and not os.path.lexists(final_dir):
                os.rename(backup, final_dir)
            raise
        if backup is not None:
            shutil.rmtree(backup, ignore_errors=True)

    @staticmethod
    def _archive_async(h, topic, created, meta):
        try:
            res = preview_archive.archive_preview(h, topic, created, meta)
            if res.get("committed"):
                conn = C.connect()
                conn.execute(
                    "UPDATE previews SET archived=?, archive_name=? WHERE hash=?",
                    (1 if res.get("pushed") else 0, res.get("name"), h),
                )
                conn.commit()
                conn.close()
        except Exception as e:  # noqa: BLE001
            print(f"[api] archive error for {h}: {e}", flush=True)

    def _handle_check(self):
        """Dry-run: report whether publishing a given name would update an
        existing live preview or create a new one — without changing anything."""
        params = self._params()
        topic = C.slugify_topic(params.get("topic", "preview"))
        force_new = str(params.get("new", "")).strip().lower() in ("1", "true", "yes")
        now = _now()
        conn = C.connect()
        try:
            pred = _predict_action(conn, topic, force_new, now)
        finally:
            conn.close()
        return self._json(200, {"ok": True, "dry_run": True, "topic": topic,
                                "force_new": force_new, **pred})

    def _handle_list(self):
        include_all = self._params().get("all") in ("1", "true", "yes")
        conn = C.connect()
        try:
            if include_all:
                rows = conn.execute(
                    "SELECT * FROM previews ORDER BY created_at DESC"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM previews WHERE status='live' ORDER BY created_at DESC"
                ).fetchall()
        finally:
            conn.close()
        items = [C.row_to_dict(r) for r in rows]
        return self._json(200, {"ok": True, "count": len(items), "previews": items})

    def _handle_info(self, h: str):
        conn = C.connect()
        try:
            row = conn.execute("SELECT * FROM previews WHERE hash=?", (h,)).fetchone()
        finally:
            conn.close()
        if not row:
            return self._err(404, "no such preview")
        return self._json(200, {"ok": True, "preview": C.row_to_dict(row)})

    def _handle_delete(self, h: str):
        if not h or "/" in h or ".." in h:
            return self._err(400, "bad hash")
        conn = C.connect()
        try:
            row = conn.execute("SELECT * FROM previews WHERE hash=?", (h,)).fetchone()
            if not row:
                return self._err(404, "no such preview")
            shutil.rmtree(os.path.join(C.LIVE_ROOT, h), ignore_errors=True)
            conn.execute(
                "UPDATE previews SET status='deleted', reclaimed_at=? WHERE hash=?",
                (_now(), h),
            )
            conn.commit()
        finally:
            conn.close()
        return self._json(200, {"ok": True, "hash": h, "status": "deleted",
                                "note": "live copy removed; git archive (if any) kept"})

    def _handle_expiry(self, h: str):
        """Admin expiry control for one preview (drives the admin homepage buttons).

        action=extend  prolong a live preview's expiry by `days` (default 30),
                       added on top of its current expiry so time is never lost.
        action=expire  expire it right now: reclaim the live copy immediately
                       (don't wait for the hourly reaper) and mark it 'expired'.
                       The git archive (if any) is kept, so it can be renewed.
        action=renew   revive an expired/inactive preview for `days` (default 7),
                       a fresh window from now. If the live copy was already
                       reclaimed it is restored from the git archive; if it was
                       never archived there is nothing to restore -> 409.
        """
        if not h or "/" in h or ".." in h:
            return self._err(400, "bad hash")
        params = self._params()
        action = str(params.get("action", "")).strip().lower()
        if action not in ("extend", "expire", "renew"):
            return self._err(400, "action must be extend, expire, or renew")

        now = _now()
        conn = C.connect()
        try:
            row = conn.execute("SELECT * FROM previews WHERE hash=?", (h,)).fetchone()
            if not row:
                return self._err(404, "no such preview")

            if action == "expire":
                shutil.rmtree(os.path.join(C.LIVE_ROOT, h), ignore_errors=True)
                conn.execute(
                    "UPDATE previews SET status='expired', expires_at=?, "
                    "reclaimed_at=?, updated_at=? WHERE hash=?",
                    (now, now, now, h),
                )
                conn.commit()
                new_row = conn.execute(
                    "SELECT * FROM previews WHERE hash=?", (h,)).fetchone()
                return self._json(200, {"ok": True, "action": "expire",
                                        "preview": C.row_to_dict(new_row)})

            # extend / renew both end with the preview live and a future expiry.
            default_days = 30 if action == "extend" else 7
            days = _parse_days(params.get("days"), default_days)
            if days is None:
                return self._err(400, "days must be a positive integer")
            if action == "extend":
                # Add on top of the current expiry (never shorten); fall back to
                # `now` if it had already slipped past expiry.
                new_expires = max(int(row["expires_at"]), now) + days * 86400
            else:  # renew: a fresh window from now
                new_expires = now + days * 86400
            days_store = max(1, (new_expires - now + 86399) // 86400)

            # Make sure the served content exists; restore it from the append-only
            # git archive if the reaper already reclaimed the live tree.
            live_dir = os.path.join(C.LIVE_ROOT, h)
            if not os.path.isdir(live_dir):
                restored = preview_archive.restore_from_archive(
                    h, row["topic"], row["created_at"], row["archive_name"])
                if not restored:
                    return self._err(
                        409,
                        "content was already reclaimed and is not in the git "
                        "archive; re-publish it instead of renewing")

            conn.execute(
                "UPDATE previews SET status='live', expires_at=?, days=?, "
                "reclaimed_at=NULL, updated_at=? WHERE hash=?",
                (new_expires, days_store, now, h),
            )
            conn.commit()
            new_row = conn.execute(
                "SELECT * FROM previews WHERE hash=?", (h,)).fetchone()
            return self._json(200, {"ok": True, "action": action,
                                    "preview": C.row_to_dict(new_row)})
        finally:
            conn.close()


def main():
    C.init_db()
    if not C.API_KEY:
        print("[api] FATAL: PREVIEW_API_KEY is not set", flush=True)
        raise SystemExit(1)
    addr = (os.environ.get("PREVIEW_BIND", "127.0.0.1"), int(os.environ.get("PREVIEW_PORT", "8787")))
    httpd = ThreadingHTTPServer(addr, Handler)
    print(f"[api] listening on {addr[0]}:{addr[1]} (git archive={'on' if C.GIT_ENABLED else 'off'})", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
