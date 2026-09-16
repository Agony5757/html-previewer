"""Client library for the html-previewer ephemeral preview hosting service.

Stdlib-only, works on Linux, macOS and Windows. Talks to the service's
``/_api/`` HTTP endpoints using the X-API-Key header.

Typical use::

    from html_previewer import PreviewClient

    c = PreviewClient("https://preview.example.com", api_key="…")
    result = c.publish("./site", topic="my-site", expiry="14")  # or "2m"
    print(result["url"])
"""
from __future__ import annotations

import io
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

__all__ = [
    "PreviewClient",
    "PreviewError",
    "build_zip",
    "default_topic",
    "parse_expiry",
    "slugify_topic",
]


class PreviewError(Exception):
    """A request to the preview service failed."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


# Mirrors slugify_topic() in server/preview_common.py so the client can reason
# about the final topic segment (and print it) exactly like the server will.
_TOPIC_RE = re.compile(r"[^A-Za-z0-9._-]+")


def slugify_topic(raw: str) -> str:
    raw = (raw or "").strip()
    raw = _TOPIC_RE.sub("-", raw)
    raw = raw.strip(".-_")
    if not raw:
        raw = "preview"
    return raw[:64]


_EXPIRY_RE = re.compile(r"^(\d+)([smhd]?)$", re.IGNORECASE)
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_expiry(spec: str | None):
    """Parse an expiry spec into publish parameters.

    ``"14"``  -> 14 days (X-Days)
    ``"2m"``  -> 2 minutes as seconds (X-TTL); also Ns / Nh / Nd
    ``None``  -> server default (no header sent)
    Returns ``{"days": n}``, ``{"ttl": seconds}`` or ``None``.
    """
    if spec is None or spec == "":
        return None
    m = _EXPIRY_RE.match(str(spec).strip())
    if not m:
        raise ValueError(f"bad expiry {spec!r}: use N for days, or Ns/Nm/Nh/Nd (e.g. 2m)")
    n, unit = int(m.group(1)), m.group(2).lower()
    if n < 1:
        raise ValueError(f"bad expiry {spec!r}: must be at least 1")
    if not unit:
        return {"days": n}
    return {"ttl": n * _UNIT_SECONDS[unit]}


def default_topic(path: Path | str) -> str:
    """Derive the default topic from a folder/file name (matches the shell client)."""
    p = Path(path)
    if p.is_dir():
        return p.name
    return p.stem or p.name


def build_zip(folder: Path | str) -> bytes:
    """Zip the *contents* of a folder so files sit at the archive root.

    ``index.html`` lands directly at the zip root (and thus under
    ``{hash}/{topic}/`` on the server). Arcnames always use forward slashes
    so archives built on Windows unpack correctly on the server.
    """
    folder = Path(folder)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _dirs, files in os.walk(folder):
            for name in files:
                full = os.path.join(root, name)
                arcname = os.path.relpath(full, folder).replace(os.sep, "/")
                zf.write(full, arcname)
    return buf.getvalue()


class PreviewClient:
    """HTTP client for a html-previewer service."""

    def __init__(self, base_url: str, api_key: str | None = None,
                 timeout: float = 120.0):
        self.base = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    # -- transport ---------------------------------------------------------

    def _url(self, path: str, params: dict | None = None) -> str:
        url = f"{self.base}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        return url

    def _request(self, method: str, path: str, *, params: dict | None = None,
                 headers: dict | None = None, data: bytes | None = None) -> dict:
        req = urllib.request.Request(self._url(path, params), data=data,
                                     method=method)
        if self.api_key:
            req.add_header("X-API-Key", self.api_key)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                body = json.loads(e.read().decode("utf-8"))
                detail = body.get("error", "")
            except Exception:  # noqa: BLE001 - best-effort error decoration
                pass
            msg = f"HTTP {e.code} {e.reason}"
            if detail:
                msg = f"{msg}: {detail}"
            raise PreviewError(msg, status=e.code) from None
        except urllib.error.URLError as e:
            raise PreviewError(f"cannot reach {self.base}: {e.reason}") from None

    def _require_key(self) -> str:
        if not self.api_key:
            raise PreviewError(
                "missing API key: set PREVIEW_API_KEY (env or config file), "
                "pass --key, or run with --save-config"
            )
        return self.api_key

    # -- API operations ----------------------------------------------------

    def health(self) -> dict:
        return self._request("GET", "/_api/health")

    def check(self, topic: str, new: bool = False) -> dict:
        """Dry-run: would publishing this topic update in place or create a new URL?"""
        self._require_key()
        topic = slugify_topic(topic)
        params = {"topic": topic}
        if new:
            params["new"] = "1"
        return self._request("GET", "/_api/check", params=params)

    def publish(self, path: Path | str, topic: str | None = None,
                expiry: str | None = None, new: bool = False,
                dry_run: bool = False) -> dict:
        """Publish a folder, a .zip, or a single .html file.

        topic    URL label; defaults to the folder/file name
        expiry   N days ("14") or N{s,m,h,d} ("2m"); None = server default
        new      force a brand-new URL instead of a same-name in-place update
        dry_run  only predict the outcome (no upload happens)
        """
        self._require_key()
        p = Path(path)
        if not p.exists():
            raise PreviewError(f"no such file or directory: {p}")

        if p.is_dir():
            ptype = "zip"
        elif p.suffix.lower() == ".zip":
            ptype = "zip"
        elif p.suffix.lower() in (".html", ".htm"):
            ptype = "html"
        else:
            raise PreviewError(
                f"unsupported type (use a folder, .zip, or .html): {p}"
            )

        topic = slugify_topic(topic if topic is not None else default_topic(p))

        if dry_run:
            return self.check(topic, new=new)

        data = build_zip(p) if p.is_dir() else p.read_bytes()

        headers = {"X-Topic": topic, "X-Type": ptype}

        expiry_hdr = parse_expiry(expiry)
        if expiry_hdr:
            for k, v in expiry_hdr.items():
                headers[f"X-{k.capitalize()}"] = str(v)
        if new:
            headers["X-New"] = "1"
        return self._request("POST", "/_api/publish", headers=headers, data=data)

    def list_previews(self, all: bool = False) -> list:
        """List previews (live only by default; everything with all=True)."""
        self._require_key()
        params = {"all": "1"} if all else None
        return self._request("GET", "/_api/list", params=params).get("previews", [])

    def info(self, hash: str) -> dict:
        self._require_key()
        return self._request("GET", f"/_api/info/{hash}")

    def delete(self, hash: str) -> dict:
        """Reclaim a preview now (the git archive, if any, is kept)."""
        self._require_key()
        return self._request("DELETE", f"/_api/preview/{hash}")

    def expiry(self, hash: str, action: str, days: int | None = None) -> dict:
        """Admin expiry control: action = extend | expire | renew."""
        self._require_key()
        if action not in ("extend", "expire", "renew"):
            raise PreviewError("action must be extend, expire, or renew")
        params = {"action": action}
        if days is not None:
            params["days"] = str(days)
        return self._request("POST", f"/_api/expiry/{hash}", params=params)
