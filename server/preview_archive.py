"""Remote-authoritative git archive for published previews (thin local repo).

Architecture: the Git remote (e.g. Gitea) is the ONLY full copy of the
archive. The local repository (GIT_REPO_DIR) is a depth-1, blob-less partial
clone (`--filter=blob:none`): it permanently holds just the tip commit and the
tree hierarchy (kilobytes), never file contents and never history.

Per publish:
  1. build one commit with pure plumbing — live files are hashed straight
     into the object store (`git hash-object`), the entry's tree slot is
     replaced via a temporary index, `git commit-tree` + `update-ref`;
     no working-tree copy of the site is ever materialized;
  2. `git push` it (only new objects travel the wire);
  3. truncate the local repo back to the single tip commit (shallow rewrite +
     reflog expire + `gc --prune=now`), so superseded versions are dropped
     from local disk immediately after the remote confirms them.

Renew/restore streams a single entry back with `git archive --remote`, so an
expired preview costs nothing locally until it is revived. If the remote is
unreachable, pushes fail with a bounded retry backoff and a capped pending
backlog, but serving/expiry keep working.

Append-only guarantee (unchanged): expiry/deletion only ever remove paths
under LIVE_ROOT; archiving only ever adds commits on top of the remote tip.
The remote tip therefore always carries the latest version of every archived
entry, and the full version history lives in the remote commit log.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import tempfile
import time

try:
    import fcntl
except ImportError:  # Windows: no fcntl — degrade the interprocess lock
    import msvcrt

    class fcntl:  # minimal stand-in with the same flock() surface
        LOCK_EX = 0
        LOCK_UN = 1

        @staticmethod
        def flock(fh, op):
            if op == fcntl.LOCK_EX:
                msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
            else:
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)

import preview_common as C

_LOCK_PATH = os.path.join(C.STATE_DIR, "archive.lock")
_BACKOFF_PATH = os.path.join(C.STATE_DIR, "archive-push-backoff")
_ZERO_SHA = "0" * 40


def _log(msg: str) -> None:
    print(f"[archive] {msg}", flush=True)


def archive_name(h: str, topic: str, created_at: int) -> str:
    date = time.strftime("%Y-%m-%d", time.localtime(created_at))
    return f"{date}__{h}__{topic}"


class _LockTimeout(Exception):
    pass


class _FileLock:
    """Interprocess lock shared by the API and the reaper, with a timeout."""

    def __init__(self, path: str, timeout: float):
        self._path = path
        self._timeout = timeout
        self._fh = None

    def __enter__(self):
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        self._fh = open(self._path, "w")
        deadline = time.monotonic() + self._timeout
        while True:
            try:
                fcntl.flock(self._fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                if time.monotonic() >= deadline:
                    self._fh.close()
                    self._fh = None
                    raise _LockTimeout(
                        f"could not acquire {self._path} within {self._timeout:.0f}s")
                time.sleep(0.5)

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


def _kill_group(proc: subprocess.Popen) -> None:
    """Kill a subprocess AND its whole process group (git spawns helpers)."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass


def _git(args: list[str], check: bool = True,
         timeout: int | None = None) -> subprocess.CompletedProcess:
    """Run git in the archive repo; on timeout kill the entire process group."""
    cmd = ["git", "-C", C.GIT_REPO_DIR, *args]
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env=_git_env(), start_new_session=True,
    )
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        out, err = proc.communicate()
        if check:
            raise RuntimeError(
                f"git {' '.join(args[:3])}… timed out after {timeout}s (group killed)")
        return subprocess.CompletedProcess(cmd, proc.returncode, out, err)
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args[:3])}… failed rc={proc.returncode}: {err.strip()[:400]}")
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def _rev_parse(rev: str) -> str | None:
    r = _git(["rev-parse", "--verify", "--quiet", rev], check=False)
    return r.stdout.strip() or None


def _ensure_repo() -> None:
    """Guarantee a thin (depth-1, blob-less) clone of the remote archive tip."""
    eff = _effective_remote()
    gitdir = os.path.join(C.GIT_REPO_DIR, ".git")
    if not os.path.isdir(gitdir):
        os.makedirs(C.GIT_REPO_DIR, exist_ok=True)
        subprocess.run(["git", "init", "-b", C.GIT_BRANCH, C.GIT_REPO_DIR],
                       capture_output=True, text=True, check=True)
        _git(["remote", "add", "origin", eff])
        heads = _git(["ls-remote", "--heads", "origin"], check=False, timeout=60)
        if heads.stdout.strip():
            _git(["fetch", "--depth", "1", "--filter=blob:none",
                  "origin", C.GIT_BRANCH], timeout=600)
            _git(["update-ref", f"refs/remotes/origin/{C.GIT_BRANCH}", "FETCH_HEAD"])
            _git(["update-ref", f"refs/heads/{C.GIT_BRANCH}", "FETCH_HEAD"])
        else:
            _log("remote archive is empty; starting a fresh history")
    else:
        cur = _git(["remote", "get-url", "origin"], check=False)
        if cur.returncode != 0:
            _git(["remote", "add", "origin", eff])
        elif cur.stdout.strip() != eff:
            _git(["remote", "set-url", "origin", eff])
    # Promisor config is set by clone; set it explicitly for init+fetch repos.
    _git(["config", "remote.origin.promisor", "true"])
    _git(["config", "remote.origin.partialclonefilter", "blob:none"])
    # Bound pack-objects memory on small boxes.
    for key, val in (("pack.threads", "1"),
                     ("pack.windowMemory", "128m"),
                     ("pack.deltaCacheSize", "128m")):
        _git(["config", key, val])
    _git(["config", "user.name", C.GIT_AUTHOR_NAME])
    _git(["config", "user.email", C.GIT_AUTHOR_EMAIL])


def _entry_rel(h: str, topic: str, created_at: int,
               stored_name: str | None = None) -> str:
    name = stored_name or archive_name(h, topic, created_at)
    year = time.strftime("%Y", time.localtime(created_at))
    return f"previews/{year}/{name}"


def _live_files(h: str) -> list[tuple[str, str]]:
    """[(absolute_path, repo_rel_path)] for every file under LIVE_ROOT/<h>,
    mapped to its slot under previews/<year>/<name>/site/ in the repo tree."""
    root = os.path.join(C.LIVE_ROOT, h)
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for fn in sorted(filenames):
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            out.append((full, rel))
    return out


def _commit_entry(h: str, topic: str, created_at: int, meta: dict) -> str | None:
    """Build one commit whose tree equals the tip tree with this preview's
    entry replaced by the current live content. Pure plumbing: no checkout,
    no working-tree copy. Returns the commit sha, or None if nothing changed."""
    entry = _entry_rel(h, topic, created_at)
    head_tree = _rev_parse(f"refs/heads/{C.GIT_BRANCH}^{{tree}}")
    head_commit = _rev_parse(f"refs/heads/{C.GIT_BRANCH}")
    with tempfile.TemporaryDirectory(dir=C.STATE_DIR, prefix="archive-idx-") as tmp:
        env = {**_git_env(), "GIT_INDEX_FILE": os.path.join(tmp, "index")}

        def gix(args: list[str], input: str | None = None) -> str:
            proc = subprocess.Popen(
                ["git", "-C", C.GIT_REPO_DIR, *args],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, env=env, start_new_session=True)
            out, err = proc.communicate(input, timeout=600)
            if proc.returncode != 0:
                raise RuntimeError(
                    f"git {' '.join(args[:3])}… failed rc={proc.returncode}: "
                    f"{err.strip()[:300]}")
            return out

        records: list[str] = []
        if head_tree:
            gix(["read-tree", head_tree])
            # Drop the entry's previous version wholesale so the new tree
            # reflects exactly this version (no stale files merge in).
            for path in gix(["ls-tree", "-r", "--name-only", "-z",
                             f"refs/heads/{C.GIT_BRANCH}", "--", entry]).split("\0"):
                if path:
                    records.append(f"0 {_ZERO_SHA}\t{path}")
        files = _live_files(h)
        if files:
            shas = gix(["hash-object", "-w", "--stdin-paths"],
                       input="".join(f"{full}\n" for full, _ in files)).splitlines()
            for (full, rel), sha in zip(files, (s.strip() for s in shas)):
                records.append(f"100644 {sha}\t{entry}/site/{rel}")
        md = os.path.join(tmp, "metadata.json")
        with open(md, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        msha = gix(["hash-object", "-w", "--", md]).strip()
        records.append(f"100644 {msha}\t{entry}/metadata.json")

        gix(["update-index", "-z", "--index-info"],
            input="".join(rec + "\0" for rec in records))
        tree = gix(["write-tree"]).strip()
        if head_tree and tree == head_tree:
            return None
        exp = time.strftime("%Y-%m-%d", time.localtime(meta.get("expires_at", created_at)))
        verb = "update" if meta.get("updated") else "archive"
        parent = ["-p", head_commit] if head_commit else []
        commit = gix(["commit-tree", tree, *parent,
                      "-m", f"{verb} {topic} {h} (expires {exp})"]).strip()
        _git(["update-ref", f"refs/heads/{C.GIT_BRANCH}", commit])
        return commit


def _pending_commits() -> int:
    tip = _rev_parse(f"refs/heads/{C.GIT_BRANCH}")
    if tip is None:
        return 0
    base = _rev_parse(f"refs/remotes/origin/{C.GIT_BRANCH}")
    rng = f"{base}..{tip}" if base else tip
    r = _git(["rev-list", "--count", rng], check=False)
    try:
        return int(r.stdout.strip() or 0)
    except ValueError:
        return 0


def _truncate_history(tip: str) -> None:
    """Cut the local repo back to a single-commit history rooted at `tip`.

    Only call after a push confirmed the remote holds `tip`. Rewrites the
    shallow boundary to the tip, then expires reflogs and prunes so every
    superseded commit/object leaves local disk.

    Never runs `git gc`/`git repack`: this is a promisor (blob-less) repo, and
    repack both refuses to drop promisor packs and needs every reachable blob
    locally. Objects created here stay loose, so plain `git prune` reclaims
    superseded ones; the tiny fetched tree-only pack is left untouched.
    """
    gitdir = _git(["rev-parse", "--absolute-git-dir"]).stdout.strip()
    with open(os.path.join(gitdir, "shallow"), "w") as f:
        f.write(tip + "\n")
    _git(["update-ref", f"refs/remotes/origin/{C.GIT_BRANCH}", tip])
    _git(["reflog", "expire", "--expire=now", "--all"])
    _git(["prune", "--expire=now"], timeout=300)


def _push() -> bool:
    res = _git(["push", "origin", C.GIT_BRANCH], check=False,
               timeout=C.GIT_PUSH_TIMEOUT)
    if res.returncode != 0:
        _log(f"git push failed rc={res.returncode}: {res.stderr.strip()[:500]}")
        return False
    return True


def _mark_push_failure() -> None:
    try:
        with open(_BACKOFF_PATH, "w") as f:
            f.write(str(int(time.time())))
    except OSError:
        pass


def _backoff_active() -> bool:
    try:
        with open(_BACKOFF_PATH) as f:
            ts = int(f.read().strip())
    except (OSError, ValueError):
        return False
    return (time.time() - ts) < C.GIT_PUSH_RETRY_BACKOFF


def archive_preview(h: str, topic: str, created_at: int, meta: dict) -> dict:
    """Commit + push one preview; drop superseded history locally on success.

    Returns {"committed": bool, "pushed": bool, "name": str|None}.
    """
    out = {"committed": False, "pushed": False, "name": None}
    if not C.GIT_ENABLED:
        _log("git archiving disabled (PREVIEW_GIT_ENABLED!=1); skipping")
        return out
    if not C.GIT_REMOTE:
        _log("PREVIEW_GIT_REMOTE not set; skipping")
        return out
    try:
        with _FileLock(_LOCK_PATH, timeout=600):
            _ensure_repo()
            pending = _pending_commits()
            if pending >= C.GIT_PENDING_MAX_COMMITS:
                _log(f"pending archive backlog at cap ({pending} commits); "
                     f"skipping new commits until it drains")
                return out
            commit = _commit_entry(h, topic, created_at, meta)
            if commit is None:
                _log(f"no changes to commit for {h}")
            else:
                out["committed"] = True
                out["name"] = archive_name(h, topic, created_at)
            if _push():
                out["pushed"] = True
                _truncate_history(
                    _rev_parse(f"refs/heads/{C.GIT_BRANCH}") or commit or "")
                try:
                    os.remove(_BACKOFF_PATH)
                except OSError:
                    pass
            else:
                _mark_push_failure()
    except _LockTimeout:
        _log(f"archive lock busy; skipping archive for {h}")
        return out
    except Exception as e:  # noqa: BLE001
        _log(f"archive error for {h}: {e}")
        return out

    if out["pushed"]:
        _log(f"archived {h} -> remote (history stays remote-only)")
    elif out["committed"]:
        _log(f"committed {h} locally; push pending (reaper retries with backoff)")
    return out


def _extract_remote_path(path: str, destdir: str) -> None:
    """Stream one archive path from the remote and untar it into destdir.

    `git archive --remote` extracts server-side, so only this entry's bytes
    cross the wire — no clone, no history download.
    """
    strip = len(path.split("/"))
    tar = subprocess.Popen(
        ["tar", "-x", "--strip-components", str(strip), "-C", destdir],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE, text=True, start_new_session=True)
    git = subprocess.Popen(
        ["git", "-C", C.GIT_REPO_DIR, "archive",
         f"--remote={_effective_remote()}", "HEAD", path],
        stdout=tar.stdin, stderr=subprocess.PIPE, text=True,
        env=_git_env(), start_new_session=True)
    try:
        _, git_err = git.communicate(timeout=C.GIT_RESTORE_TIMEOUT)
    except subprocess.TimeoutExpired:
        _kill_group(git)
        _kill_group(tar)
        raise RuntimeError(f"git archive --remote timed out after {C.GIT_RESTORE_TIMEOUT}s")
    try:
        # communicate() closes our stdin copy -> tar sees EOF and finishes
        _, tar_err = tar.communicate(timeout=300)
    except subprocess.TimeoutExpired:
        _kill_group(tar)
        raise RuntimeError("tar extraction stalled")
    if git.returncode != 0:
        raise RuntimeError(f"git archive --remote failed: {(git_err or '').strip()[:300]}")
    if tar.returncode != 0:
        raise RuntimeError(f"tar failed: {(tar_err or '').strip()[:300]}")


def restore_from_archive(h: str, topic: str, created_at: int,
                         stored_name: str | None = None) -> bool:
    """Revive a reclaimed preview by streaming its entry from the git remote.

    Returns True on success (including when the live copy is already present),
    False when archiving is disabled or the entry was never archived.
    """
    if not C.GIT_ENABLED:
        return False
    dest = os.path.join(C.LIVE_ROOT, h)
    entry = _entry_rel(h, topic, created_at, stored_name)
    staging = os.path.join(C.LIVE_ROOT, f".restore-{h[:12]}-{os.getpid()}")
    try:
        with _FileLock(_LOCK_PATH, timeout=C.GIT_LOCK_TIMEOUT):
            if os.path.isdir(dest):
                return True
            _ensure_repo()  # trees are local: the existence check is offline
            probe = _git(["cat-file", "-e", f"refs/heads/{C.GIT_BRANCH}:{entry}/site"],
                         check=False)
            if probe.returncode != 0:
                return False  # never archived at the tip
        # Stream OUTSIDE the lock: extraction can take minutes on a slow link
        # and must not block publishes or concurrent renews of other hashes.
        shutil.rmtree(staging, ignore_errors=True)
        os.makedirs(staging, exist_ok=True)
        _extract_remote_path(f"{entry}/site", staging)
        if not os.path.isdir(dest):
            os.rename(staging, dest)
        _log(f"restored {h} from remote archive ({entry})")
        return True
    except _LockTimeout:
        _log(f"restore for {h}: archive lock busy ({C.GIT_LOCK_TIMEOUT:.0f}s)")
        return False
    except Exception as e:  # noqa: BLE001
        _log(f"restore error for {h}: {e}")
        return False
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def flush_pending() -> int:
    """Retry pushing locally-committed-but-unpushed archives (with backoff).

    Returns the number of preview rows newly marked archived (0 if nothing
    was pushed).
    """
    if not C.GIT_ENABLED or not os.path.isdir(os.path.join(C.GIT_REPO_DIR, ".git")):
        return 0
    if _backoff_active():
        _log("push retry suppressed by backoff (recent push failure)")
        return 0
    try:
        with _FileLock(_LOCK_PATH, timeout=60):
            if _pending_commits() == 0:
                return 0
            if not _push():
                _mark_push_failure()
                return 0
            _truncate_history(_rev_parse(f"refs/heads/{C.GIT_BRANCH}") or "")
    except _LockTimeout:
        return 0
    except Exception as e:  # noqa: BLE001
        _log(f"flush error: {e}")
        return 0
    try:
        os.remove(_BACKOFF_PATH)
    except OSError:
        pass
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
