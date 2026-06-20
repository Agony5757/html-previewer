"""Best-effort archiving of published previews to a Gitea repo via git push.

Stdlib-only (drives the system `git` binary over SSH). Archiving is a *pluggable*
concern: when PREVIEW_GIT_ENABLED != 1 we do nothing (and create no local repo,
so disk stays clean until Gitea is wired up). When enabled, each publish copies
the served tree into a local working clone under
  previews/<YYYY>/<YYYY-MM-DD>__<hash>__<topic>/{metadata.json, site/...}
then `git add` + `git commit` + `git push` to the Gitea remote.

Append-only guarantee (why a local delete never reaches the remote)
-------------------------------------------------------------------
The archive repo (GIT_REPO_DIR, e.g. /var/lib/preview/archive-repo) is a
*dedicated* repository that is physically SEPARATE from the live content tree
(LIVE_ROOT, e.g. /var/www/preview/live). Expiry/deletion only ever rmtree paths
under LIVE_ROOT (see reaper.py / _handle_delete) — they never touch the archive
repo. Here we only ever copy *into* the repo and run `git add -A` + commit +
push, so the remote only grows and full history is retained: a reclaim of live
content never produces a deletion on the remote. Because the repo holds the
previews/ archive *only* (not the live tree, not the source code), no
sparse-checkout / partial-tracking is needed — the separation already scopes
exactly what is versioned.

Update vs. new (mirrors the live same-name-update model)
--------------------------------------------------------
Each entry path is keyed by the preview's ORIGINAL created_at + hash + topic, so
it is stable for the life of that hash. An in-place same-name update (same live
hash) rewrites the same entry -> git records a MODIFIED change; a fresh hash
(e.g. re-publishing a name whose previous preview has EXPIRED) lands in a new
entry -> git records a NEW addition. Old versions are never lost — they remain
in the commit history.

Resilience: a commit is always made locally (fast, offline-safe); the push may
fail (network) in which case the commit stays local and the reaper retries the
push later (flush_pending).
"""
from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
import time

import preview_common as C

_LOCK_PATH = os.path.join(C.STATE_DIR, "archive.lock")


def _log(msg: str) -> None:
    print(f"[archive] {msg}", flush=True)


def archive_name(h: str, topic: str, created_at: int) -> str:
    date = time.strftime("%Y-%m-%d", time.localtime(created_at))
    return f"{date}__{h}__{topic}"


class _FileLock:
    """Interprocess lock shared by the API and the reaper."""

    def __init__(self, path: str):
        self._path = path
        self._fh = None

    def __enter__(self):
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        self._fh = open(self._path, "w")
        fcntl.flock(self._fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        try:
            fcntl.flock(self._fh, fcntl.LOCK_UN)
        finally:
            self._fh.close()


def _is_https() -> bool:
    return C.GIT_REMOTE.startswith(("http://", "https://"))


def _effective_remote() -> str:
    """For HTTPS, embed the username so git only prompts for the password (token)."""
    r = C.GIT_REMOTE
    if _is_https() and C.GIT_USER:
        scheme, rest = r.split("://", 1)
        host_part = rest.split("/", 1)[0]
        if "@" not in host_part:
            r = f"{scheme}://{C.GIT_USER}@{rest}"
    return r


def _ensure_askpass() -> str:
    path = os.path.join(C.STATE_DIR, "git-askpass.sh")
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            f.write('#!/bin/sh\necho "$PREVIEW_GIT_TOKEN"\n')
        os.chmod(path, 0o700)
    return path


def _git_env() -> dict:
    env = os.environ.copy()
    env.update(
        GIT_TERMINAL_PROMPT="0",
        GIT_AUTHOR_NAME=C.GIT_AUTHOR_NAME,
        GIT_AUTHOR_EMAIL=C.GIT_AUTHOR_EMAIL,
        GIT_COMMITTER_NAME=C.GIT_AUTHOR_NAME,
        GIT_COMMITTER_EMAIL=C.GIT_AUTHOR_EMAIL,
    )
    if _is_https():
        if C.GIT_TOKEN:
            env["GIT_ASKPASS"] = _ensure_askpass()
            env["PREVIEW_GIT_TOKEN"] = C.GIT_TOKEN
    else:
        known_hosts = os.path.join(C.SSH_DIR, "known_hosts")
        os.makedirs(C.SSH_DIR, exist_ok=True)
        ssh = (
            "ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new "
            f"-o UserKnownHostsFile={known_hosts} -o ConnectTimeout=20 -p {C.GIT_SSH_PORT}"
        )
        if C.GIT_KEY:
            ssh += f" -i {C.GIT_KEY} -o IdentitiesOnly=yes"
        env["GIT_SSH_COMMAND"] = ssh
    return env


def _git(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", C.GIT_REPO_DIR, *args],
        capture_output=True, text=True, timeout=600, env=_git_env(), check=False,
    ) if not check else subprocess.run(
        ["git", "-C", C.GIT_REPO_DIR, *args],
        capture_output=True, text=True, timeout=600, env=_git_env(), check=True,
    )


def _ensure_repo() -> None:
    eff = _effective_remote()
    gitdir = os.path.join(C.GIT_REPO_DIR, ".git")
    if not os.path.isdir(gitdir):
        os.makedirs(C.GIT_REPO_DIR, exist_ok=True)
        subprocess.run(["git", "init", "-b", C.GIT_BRANCH, C.GIT_REPO_DIR],
                       capture_output=True, text=True, check=True)
        _git(["remote", "add", "origin", eff])
    else:
        # keep the remote URL in sync with config
        cur = _git(["remote", "get-url", "origin"], check=False)
        if cur.returncode != 0:
            _git(["remote", "add", "origin", eff])
        elif cur.stdout.strip() != eff:
            _git(["remote", "set-url", "origin", eff])
    _git(["config", "user.name", C.GIT_AUTHOR_NAME])
    _git(["config", "user.email", C.GIT_AUTHOR_EMAIL])


def _push() -> bool:
    res = _git(["push", "origin", C.GIT_BRANCH], check=False)
    if res.returncode != 0:
        _log(f"git push failed rc={res.returncode}: {res.stderr.strip()[:500]}")
        return False
    return True


def archive_preview(h: str, topic: str, created_at: int, meta: dict) -> dict:
    """Copy + commit + push one preview.

    Returns {"committed": bool, "pushed": bool, "name": str|None}.
    """
    out = {"committed": False, "pushed": False, "name": None}
    if not C.GIT_ENABLED:
        _log("git archiving disabled (PREVIEW_GIT_ENABLED!=1); skipping")
        return out
    if not C.GIT_REMOTE:
        _log("PREVIEW_GIT_REMOTE not set; skipping")
        return out

    # The entry path is keyed by the preview's ORIGINAL created_at + hash + topic,
    # so it is STABLE for the life of that hash: an in-place same-name update
    # (same hash) rewrites the same entry => git sees a MODIFIED change, while a
    # fresh hash (e.g. re-publishing an expired name) lands in a brand-new entry
    # => git sees a NEW addition. Either way the remote only grows and full
    # version history is retained in the commit log (append-only history).
    name = archive_name(h, topic, created_at)
    year = time.strftime("%Y", time.localtime(created_at))
    try:
        with _FileLock(_LOCK_PATH):
            _ensure_repo()
            entry = os.path.join(C.GIT_REPO_DIR, "previews", year, name)
            site = os.path.join(entry, "site")
            os.makedirs(entry, exist_ok=True)
            # Replace site/ wholesale so the commit diff reflects exactly this
            # version (added/modified/removed files), rather than merging stale
            # files from a previous version on top of the new one.
            if os.path.isdir(site):
                shutil.rmtree(site, ignore_errors=True)
            src = os.path.join(C.LIVE_ROOT, h)
            shutil.copytree(src, site)
            with open(os.path.join(entry, "metadata.json"), "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)

            _git(["add", "-A"])
            staged = _git(["diff", "--cached", "--quiet"], check=False)
            if staged.returncode == 0:
                _log(f"no changes to commit for {h}")
            else:
                exp = time.strftime("%Y-%m-%d", time.localtime(meta.get("expires_at", created_at)))
                verb = "update" if meta.get("updated") else "archive"
                _git(["commit", "-m", f"{verb} {topic} {h} (expires {exp})"])
                out["committed"] = True
                out["name"] = name
            out["pushed"] = _push()
    except subprocess.CalledProcessError as e:
        _log(f"git error for {h}: {e.stderr.strip()[:500] if e.stderr else e}")
        return out
    except Exception as e:  # noqa: BLE001
        _log(f"archive error for {h}: {e}")
        return out

    if out["pushed"]:
        _log(f"archived {h} -> Gitea ({year}/{name})")
    elif out["committed"]:
        _log(f"committed {h} locally; push pending (reaper will retry)")
    return out


def restore_from_archive(h: str, topic: str, created_at: int,
                         stored_name: str | None = None) -> bool:
    """Copy a reclaimed preview's content back into LIVE_ROOT from the archive.

    The admin 'renew' action calls this when the live tree was already reclaimed
    by the reaper (or an explicit expire/delete): the append-only archive retains
    the full snapshot under previews/<year>/<name>/site/, so we copy it back to
    LIVE_ROOT/<hash>/ (which restores the served /<hash>/<topic>/ tree). Returns
    True on success (including when the live copy is already present), False when
    archiving is disabled or the archived entry is missing — nothing to restore.
    """
    if not C.GIT_ENABLED:
        return False
    dest = os.path.join(C.LIVE_ROOT, h)
    name = stored_name or archive_name(h, topic, created_at)
    year = time.strftime("%Y", time.localtime(created_at))
    site = os.path.join(C.GIT_REPO_DIR, "previews", year, name, "site")
    try:
        with _FileLock(_LOCK_PATH):
            if os.path.isdir(dest):
                return True
            if not os.path.isdir(site):
                return False
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copytree(site, dest)
        _log(f"restored {h} from archive ({year}/{name})")
        return True
    except Exception as e:  # noqa: BLE001
        _log(f"restore error for {h}: {e}")
        return False


def flush_pending() -> int:
    """Retry pushing any locally-committed-but-unpushed archives.

    Returns the number of preview rows newly marked archived (0 if nothing to do).
    """
    if not C.GIT_ENABLED or not os.path.isdir(os.path.join(C.GIT_REPO_DIR, ".git")):
        return 0
    with _FileLock(_LOCK_PATH):
        ok = _push()
    if not ok:
        return 0
    try:
        conn = C.connect()
        cur = conn.execute(
            "UPDATE previews SET archived=1 "
            "WHERE archive_name IS NOT NULL AND archived=0"
        )
        conn.commit()
        n = cur.rowcount
        conn.close()
        return n
    except Exception as e:  # noqa: BLE001
        _log(f"db mark error: {e}")
        return 0
