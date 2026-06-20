# Copilot instructions — html-previewer

Ephemeral HTML preview hosting: publish a folder / `.zip` / single `.html` behind an
unguessable capability URL (`/{hash}/{topic}/index.html`), with automatic expiry +
disk reclaim and optional append-only Git archiving. **Stdlib-only Python — no
third-party runtime dependencies.**

## Hard constraints (read before changing code)
- **Stdlib only.** The server must run on a bare `python3` with no `pip install`.
  Do not add third-party imports to `server/*.py`. (The Docker image adds only the
  `git` binary, used solely for optional archiving.)
- **All configuration is environment variables**, centralized in
  `server/preview_common.py` (every knob is an `os.environ.get(...)`). Add new config
  there, not scattered across modules. The deployed `preview.env` is the source of truth;
  document new vars in `deploy/preview.env.example`.
- **Never commit secrets.** `preview.env`, `*.key`, `api_key` are git-ignored. The API
  key is the sole credential and must never appear in URLs or logs.

## Build / run / validate
There is **no build step, no test suite, and no configured linter** — don't invent one.
- Syntax-check after edits: `python3 -m py_compile server/*.py`
- Run locally, self-contained (serves API **and** previews itself):
  ```bash
  PREVIEW_API_KEY=dev PREVIEW_PORT=18811 PREVIEW_SERVE_STATIC=1 \
    PREVIEW_LIVE_ROOT=/tmp/pv/live PREVIEW_STATE_DIR=/tmp/pv/state \
    PREVIEW_STATIC_DIR=/tmp/pv/static PREVIEW_PUBLIC_HOST=localhost:18811 \
    PREVIEW_PUBLIC_SCHEME=http python3 server/preview_api.py
  ```
- **Validate a change end-to-end** (the project's substitute for tests — run these
  against the server above):
  ```bash
  curl -s localhost:18811/_api/health
  echo '<h1>hi</h1>' | curl -s -H 'X-API-Key: dev' -H 'X-Topic: smoke' --data-binary @- localhost:18811/_api/publish
  curl -s -H 'X-API-Key: dev' localhost:18811/_api/list
  PREVIEW_LIVE_ROOT=/tmp/pv/live PREVIEW_STATE_DIR=/tmp/pv/state python3 server/reaper.py
  ```
- Install / deploy (three modes, one codebase): `bash deploy/install.sh {user|system|docker}`.

## Architecture (the parts that span files)
Four cooperating stdlib modules in `server/`, deployed **flat** (no package): they
`import preview_common as C` / `import preview_archive` by bare name, so they must run
from the same directory (install scripts copy all four `.py` files side by side).

- `preview_api.py` — `ThreadingHTTPServer` on `127.0.0.1:8787`. Auth = `X-API-Key`
  header, constant-time `hmac.compare_digest`. Routes live under the reserved `/_api/`
  prefix; everything else is (optionally) static preview content.
- `preview_common.py` — config, paths, and the single sqlite `previews` table (WAL).
  Schema changes go in `_migrate()` and must be **additive + idempotent** (it runs on
  every startup against already-deployed DBs).
- `preview_archive.py` — best-effort archiving by driving the system `git` binary.
- `reaper.py` — expiry sweeper (systemd timer, hourly): rmtree expired live trees, mark
  `status='expired'`, retry pending pushes, sweep stale staging dirs.

**Publish flow (`_handle_publish`)** is the core invariant to preserve: read body →
extract into a hidden `LIVE_ROOT/.staging/...` dir *on the same filesystem* → atomic
`os.rename` swap into `LIVE_ROOT/{hash}` (`_swap_into_place`) → write the sqlite row →
spawn a daemon thread to archive. Consequences a change must not break: a malformed
re-upload can never destroy the live version, and readers never see a half-written tree.

**Two serving modes:** `PREVIEW_SERVE_STATIC=1` (user/docker installs — the Python
process serves previews) vs `=0` (system install — nginx serves static files and only
proxies `/_api/`). Behavior must stay equivalent across both; nginx rules in
`deploy/preview.nginx.conf` mirror the app's static handling.

## Project-specific conventions
- **Same-name-update vs new-URL** is the central behavioral model. Re-publishing a
  `topic` that is **still live and not past its expiry** overwrites it in place (URL
  stable, expiry resets); an **expired** name mints a fresh hash/URL. `?new=1` forces a
  new URL; `?dry_run=1` / `GET /_api/check` predict without writing. Driven by
  `_find_live_match` + `_predict_action`.
- **Lazy expiry:** because the reaper is hourly, a row can be *past* `expires_at` while
  still `status='live'`. "Live" for update-matching means `status='live' AND expires_at
  > now` — never treat `status` alone as authoritative for expiry.
- **Capability URL is the only access control.** Hashes are minted random (never derived
  from content) + a ns timestamp, verified unique (`_new_hash`).
- **Security invariants** — keep them when touching upload/serving code: zip-slip & zip-
  bomb guards in `_safe_extract`; dotfiles/dotdirs are never served (app skips them,
  nginx `deny`s them); 403→404 mapping so a wrong/expired/indexless path never reveals
  whether something exists.
- **Archive is append-only and a *separate* repo** from the live tree: expiry/delete
  only ever rmtree under `LIVE_ROOT` and never touch the archive repo. The archive entry
  path is keyed on the preview's **original** `created_at + hash + topic` so a same-name
  update rewrites it (git MODIFIED) and a fresh hash adds a new entry (git NEW).
- **Expiry units:** `days` (1–30, default 7) or sub-day `ttl` seconds (overrides days).
- Keep generic placeholders (`preview.example.com`, `<owner>/<repo>`) in code/docs — no
  real domains, IPs, or owners are baked in.

## When editing server code, update all deploy surfaces
The install scripts copy a fixed set of files. If you add a server module or asset,
update every relevant target: `deploy/install-user.sh`, `deploy/install-system.sh`, and
`deploy/docker/Dockerfile` (+ `entrypoint.sh`). New endpoints should be reflected in the
`README.md` API table and the `preview_api.py` module docstring.
