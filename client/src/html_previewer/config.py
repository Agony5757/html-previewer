"""Cross-platform config discovery for the html-previewer client.

Precedence (highest first):
  1. explicit values passed by the caller (CLI flags)
  2. environment variables (PREVIEW_API_KEY / PREVIEW_API_BASE)
  3. a key=value config file

The config file uses plain ``KEY=VALUE`` lines (an optional ``export`` prefix
and surrounding quotes are tolerated), so the same file works for both the
classic ``preview-publish.sh`` client and this Python client. On Linux/macOS
the default location matches the shell client: ``~/.config/preview/config``.
On Windows it is ``%APPDATA%\\preview\\config``.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ENV_KEY = "PREVIEW_API_KEY"
ENV_BASE = "PREVIEW_API_BASE"
ENV_CONFIG = "PREVIEW_CONFIG"
DEFAULT_BASE = "https://preview.example.com"


def default_config_path() -> Path:
    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / "preview" / "config"
        return Path.home() / ".config" / "preview" / "config"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "preview" / "config"
    return Path.home() / ".config" / "preview" / "config"


def parse_config(text: str) -> dict:
    cfg = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            cfg[key] = value
    return cfg


def load_config(path: Path | str | None = None) -> dict:
    path = Path(path) if path is not None else default_config_path()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    return parse_config(text)


def save_config(base: str | None = None, key: str | None = None,
                path: Path | str | None = None) -> Path:
    """Write/update PREVIEW_API_BASE and PREVIEW_API_KEY in the config file.

    Existing lines for these keys are replaced in place; unrelated lines are
    preserved. The file is created (0600 where supported) if missing.
    """
    path = Path(path) if path is not None else default_config_path()
    updates = {}
    if base:
        updates[ENV_BASE] = base
    if key:
        updates[ENV_KEY] = key
    if not updates:
        raise ValueError("nothing to save: pass base and/or key")

    lines: list[str] = []
    seen = set()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        pass
    out = []
    for line in lines:
        stripped = line.strip()
        parsed = parse_config(stripped)
        if len(parsed) == 1:
            (name, _old), = parsed.items()
            if name in updates:
                out.append(f"{name}={updates[name]}")
                seen.add(name)
                continue
        out.append(line)
    for name, value in updates.items():
        if name not in seen:
            out.append(f"{name}={value}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass  # Windows: ACLs already restrict to the user profile
    return path


def resolve(base: str | None = None, key: str | None = None,
            config_path: Path | str | None = None) -> dict:
    """Resolve (base_url, api_key, config_path_used) from flags/env/config."""
    path = Path(config_path) if config_path is not None else default_config_path()
    file_cfg = load_config(path)
    base = base or os.environ.get(ENV_BASE) or file_cfg.get(ENV_BASE, DEFAULT_BASE)
    key = key or os.environ.get(ENV_KEY) or file_cfg.get(ENV_KEY)
    return {"base": base.rstrip("/"), "key": key, "config_path": path}
