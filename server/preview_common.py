"""Shared config, paths and sqlite manifest helpers for the preview service.

Stdlib-only. Imported by preview_api.py and reaper.py.
"""
from __future__ import annotations

import os
import re
import sqlite3
import time

# ---------------------------------------------------------------------------
# Paths (overridable via environment / preview.env)
# ---------------------------------------------------------------------------
LIVE_ROOT = os.environ.get("PREVIEW_LIVE_ROOT", "/var/www/preview/live")
STATE_DIR = os.environ.get("PREVIEW_STATE_DIR", "/var/lib/preview")
DB_PATH = os.environ.get("PREVIEW_DB_PATH", os.path.join(STATE_DIR, "manifest.db"))
STAGING_DIR = os.environ.get("PREVIEW_STAGING_DIR", os.path.join(STATE_DIR, "staging"))
PENDING_DIR = os.environ.get("PREVIEW_PENDING_DIR", os.path.join(STATE_DIR, "pending"))
SSH_DIR = os.environ.get("PREVIEW_SSH_DIR", os.path.join(STATE_DIR, ".ssh"))
# Directory holding the custom 404 page (__notfound.html), used only when the
# app serves static content itself (PREVIEW_SERVE_STATIC=1).
STATIC_DIR = os.environ.get("PREVIEW_STATIC_DIR", "/var/www/preview/static")

# When 1, the API process also serves the published preview trees directly
# (so the service is fully self-contained, no nginx needed — used by the
# user-service and docker deployments). When 0 (default), nginx serves the
# static files and only proxies /_api/ to this process.
SERVE_STATIC = os.environ.get("PREVIEW_SERVE_STATIC", "0") == "1"

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------
DEFAULT_DAYS = int(os.environ.get("PREVIEW_DEFAULT_DAYS", "7"))
MAX_DAYS = int(os.environ.get("PREVIEW_MAX_DAYS", "30"))
HASH_BYTES = int(os.environ.get("PREVIEW_HASH_BYTES", "16"))  # ~128 bits of entropy
MAX_UPLOAD_BYTES = int(os.environ.get("PREVIEW_MAX_UPLOAD_BYTES", str(300 * 1024 * 1024)))
MAX_UNZIP_BYTES = int(os.environ.get("PREVIEW_MAX_UNZIP_BYTES", str(1024 * 1024 * 1024)))
MAX_FILES = int(os.environ.get("PREVIEW_MAX_FILES", "8000"))

API_KEY = os.environ.get("PREVIEW_API_KEY", "")
PUBLIC_HOST = os.environ.get("PREVIEW_PUBLIC_HOST", "preview.example.com")
PUBLIC_SCHEME = os.environ.get("PREVIEW_PUBLIC_SCHEME", "https")

# Git archive config (pluggable; archiving is skipped entirely when disabled).
# Previews are pushed to a Gitea/Git repo as an append-only archive.
GIT_ENABLED = os.environ.get("PREVIEW_GIT_ENABLED", "0") == "1"
GIT_REMOTE = os.environ.get("PREVIEW_GIT_REMOTE", "")   # https://git…/owner/repo.git OR git@host:owner/repo.git
GIT_BRANCH = os.environ.get("PREVIEW_GIT_BRANCH", "main")
GIT_REPO_DIR = os.environ.get("PREVIEW_GIT_REPO_DIR", os.path.join(STATE_DIR, "archive-repo"))
# HTTPS transport auth (token is fed via GIT_ASKPASS; never on argv or in .git/config)
GIT_USER = os.environ.get("PREVIEW_GIT_USER", "")
GIT_TOKEN = os.environ.get("PREVIEW_GIT_TOKEN", "")
# SSH transport (used when GIT_REMOTE is scp/ssh style); empty key => root's default identities
GIT_SSH_PORT = os.environ.get("PREVIEW_GIT_SSH_PORT", "22")
GIT_KEY = os.environ.get("PREVIEW_GIT_KEY", "")
GIT_AUTHOR_NAME = os.environ.get("PREVIEW_GIT_AUTHOR_NAME", "preview-archive")
GIT_AUTHOR_EMAIL = os.environ.get("PREVIEW_GIT_AUTHOR_EMAIL", "preview-archive@example.com")

_TOPIC_RE = re.compile(r"[^A-Za-z0-9._-]+")


def slugify_topic(raw: str) -> str:
    """Make a filesystem/URL-safe topic segment."""
    raw = (raw or "").strip()
    raw = _TOPIC_RE.sub("-", raw)
    raw = raw.strip(".-_")
    if not raw:
        raw = "preview"
    return raw[:64]


def ensure_dirs() -> None:
    for d in (LIVE_ROOT, STATE_DIR, STAGING_DIR, PENDING_DIR):
        os.makedirs(d, exist_ok=True)


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db() -> None:
    ensure_dirs()
    conn = connect()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS previews (
                hash         TEXT PRIMARY KEY,
                topic        TEXT NOT NULL,
                created_at   INTEGER NOT NULL,
                updated_at   INTEGER,                        -- last (re)publish time
                expires_at   INTEGER NOT NULL,
                days         INTEGER NOT NULL,
                size_bytes   INTEGER NOT NULL DEFAULT 0,
                file_count   INTEGER NOT NULL DEFAULT 0,
                status       TEXT NOT NULL DEFAULT 'live',   -- live | expired | deleted
                archived     INTEGER NOT NULL DEFAULT 0,     -- 0 no, 1 yes
                archive_name TEXT,
                client_ip    TEXT,
                reclaimed_at INTEGER
            )
            """
        )
        _migrate(conn)
        conn.commit()
    finally:
        conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive, idempotent schema migrations for already-deployed databases."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(previews)")}
    if "updated_at" not in cols:
        conn.execute("ALTER TABLE previews ADD COLUMN updated_at INTEGER")
        conn.execute("UPDATE previews SET updated_at=created_at WHERE updated_at IS NULL")


def public_url(h: str, topic: str) -> str:
    return f"{PUBLIC_SCHEME}://{PUBLIC_HOST}/{h}/{topic}/index.html"


def row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    h, topic = d["hash"], d["topic"]
    d["url"] = public_url(h, topic)
    now = int(time.time())
    d["expired"] = now >= d["expires_at"]
    d["seconds_remaining"] = max(0, d["expires_at"] - now)
    return d
