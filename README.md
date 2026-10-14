# html-previewer — ephemeral HTML preview hosting

**English** · [简体中文](README.zh-CN.md)

Publish short-lived HTML previews behind **unguessable capability URLs**, with
automatic expiry + disk reclaim, an auto-generated directory listing for folders
without an `index.html`, and optional append-only archiving to a Git repo.

```
https://preview.example.com/{hash}/{topic}/index.html
                             │       └ human-readable label
                             └ unguessable token (secrets.token_urlsafe, ~128 bit)
```

> Replace `preview.example.com` with your own domain (system install) or use
> `http://localhost:8787` (user / docker install). Everything is configured via
> environment variables — no personal values are baked into the code.

## Features
- **Unguessable URL** = the only access control (capability URL; no login).
- **Folders welcome** — publish a whole directory, a `.zip`, or a single `.html`.
- **Same-name update (default)** — re-publishing under a name (`topic`) that is
  still live **and not past its expiry** overwrites it in place and **keeps the
  same URL** (expiry resets), so you can share a link once and keep refreshing
  the content. A name whose previous preview has **expired** instead gets a
  brand-new URL. The swap is atomic — a malformed re-upload never destroys the
  previous good version. Pass `?new=1` / `X-New: 1` (or `PREVIEW_NEW=1` in the
  client) to always force a fresh URL.
- **Dry-run** — ask whether a publish would *update in place* or *create a new
  URL* before uploading: `client/preview-publish.sh --dry-run <path>` or
  `GET /_api/check?topic=…`.
- **Flexible expiry** — 1–30 **days** (default 7), or sub-day **`ttl`** in
  seconds for short-lived previews (client accepts `2m`, `36h`, `10d`, …).
- **Auto directory index** — if an uploaded folder has no top-level `index.html`,
  a clean, browsable **directory listing** is generated for every folder that
  lacks one (dotfiles excluded). Provide your own `index.html` and it is left
  untouched.
- **Expiry** — after expiry the live copy is deleted and disk reclaimed (hourly
  reaper); any wrong/expired URL returns a custom **404**.
- **Relative links work** — content is served from a normal directory tree.
- **Admin homepage** — the site root (`/`) is a small dashboard: paste the API
  key to list your **active** and **expired** preview pages (bilingual 中/EN).
- **Optional auto-archive** of each publish to a Git repo (append-only).
- **API key** for publishing and for listing the *real* directory of previews.
- Self-contained: **stdlib-only Python**, no external runtime dependencies.

## Deployment — pick one (`deploy/install.sh <mode>`)

| Mode | Command | Privileges | Front end |
|---|---|---|---|
| **User service** | `bash deploy/install.sh user` | none (your user) | the app serves everything |
| **System service** | `sudo bash deploy/install.sh system` | root | nginx + systemd |
| **Docker** | `bash deploy/install.sh docker` | docker | the app serves everything |

All three share the same code; they differ only in where files live and whether
nginx fronts the app. When the app serves content itself (`user`/`docker`) it
sets `PREVIEW_SERVE_STATIC=1`.

### 1) User service (one-click, no sudo)
```bash
bash deploy/install.sh user
```
Installs a `systemd --user` unit; everything lives under `~/.local/share/preview`
and `~/.config/preview`. The process serves the API **and** the previews on
`http://127.0.0.1:8787`. The command prints the generated API key. Override the
port with `PREVIEW_PORT=9000 bash deploy/install.sh user`.

> To keep it running after logout: `sudo loginctl enable-linger "$USER"`.

### 2) System service (nginx + systemd, multi-tenant box)
```bash
sudo PREVIEW_DOMAIN=preview.example.com bash deploy/install.sh system
```
Installs code to `/opt/preview`, config to `/etc/preview/preview.env` (0600,
holds the API key), an isolated nginx vhost, the `preview-api` service and the
hourly `preview-reaper` timer. Idempotent — a re-run preserves the API key and
reuses the domain from the existing config. Then:
1. **DNS**: add A record `preview.example.com -> <YOUR_SERVER_IP>`.
2. **TLS**: `sudo PREVIEW_DOMAIN=preview.example.com bash deploy/obtain-cert.sh`
   (certbot --nginx, adds 443 + redirect).

### 3) Docker
```bash
cd deploy/docker && docker compose up -d --build
# API key:
docker compose logs | grep 'API key'
```
A single self-contained container serves `http://localhost:8787`; content and
state persist in the `preview-data` volume. Set a fixed `PREVIEW_API_KEY` in
`docker-compose.yml` (or a `.env`) to avoid the auto-generated one.

## The API key — your admin credential
The **`PREVIEW_API_KEY` is the sole credential** for every privileged action:
publishing, deleting, listing the real directory of previews, and signing in to
the admin homepage. **Save it somewhere safe** — anyone who has it can publish and
manage previews. It is shown once at install time and stored (mode `0600`) in the
service config; it is never printed in URLs or logs.

**Forgot it, or want to rotate it?** There is no recovery — set a new value in the
config and restart the service:

| Mode | Edit | Then restart |
|---|---|---|
| System | `PREVIEW_API_KEY=…` in `/etc/preview/preview.env` | `sudo systemctl restart preview-api` |
| User | `PREVIEW_API_KEY=…` in `~/.config/preview/preview.env` | `systemctl --user restart preview-api` |
| Docker | `PREVIEW_API_KEY=…` in `docker-compose.yml`/`.env` (or the `-e` flag); delete the stored `…/state/api_key` if present | `docker compose up -d` |

After rotating, update the client too: set the new `PREVIEW_API_KEY` in
`~/.config/preview/config` so `preview-publish.sh` (and the admin page) keep working.

## Admin homepage
The site root (e.g. `https://preview.example.com/`) serves a small **admin
dashboard**. Paste the API key and it lists your **active** and **expired /
inactive** preview pages (name, link, created/expires + time remaining, size,
files, archived, status), with a 中/EN language toggle. Each row has one-click
expiry controls: **Extend 30d** prolongs a live preview, **Expire now** reclaims it
immediately, and **Renew 7d** revives an expired one (restored from the git archive
if its content was already reclaimed). The key is the admin
credential and is kept only in that browser tab (`sessionStorage`); use **Forget**
to clear it. The dashboard reveals nothing without a valid key.

## Client usage
Set `PREVIEW_API_KEY` (and optionally `PREVIEW_API_BASE`) in
`~/.config/preview/config`, then:
```bash
client/preview-publish.sh ./site/ my-site 14          # publish a whole folder, 14-day expiry
client/preview-publish.sh ./report.zip my-report 14   # publish a zip, 14-day expiry
client/preview-publish.sh ./page.html                 # publish a single html, 7 days
client/preview-publish.sh ./site/ my-site 2m          # short-lived: 2-minute expiry (ttl)
client/preview-publish.sh ./site/ my-site             # re-run while live = update in place (same URL)
client/preview-publish.sh --dry-run ./site/ my-site   # predict: would update in place, or new URL?
PREVIEW_NEW=1 client/preview-publish.sh ./site/ my-site   # force a brand-new URL instead
client/preview-publish.sh --list                      # list live previews
client/preview-publish.sh --delete <hash>             # reclaim now
```
The client zips a folder client-side (uses `zip`, falls back to `python3`).
Raw curl equivalent:
```bash
curl -H "X-API-Key: $KEY" -H "X-Topic: my-report" -H "X-Days: 14" \
     --data-binary @report.zip https://preview.example.com/_api/publish
```

### Python client (Windows / Linux / macOS)
The same client ships as a dependency-free Python package — install it from
PyPI (`pip install html-previewer`) or directly from the repo
(`pip install ./client`) — and get the `preview-publish` command plus a small
library:
```bash
preview-publish --base https://preview.example.com --key $KEY --save-config
preview-publish ./site/ my-site 14        # same interface as the shell client
preview-publish --list / --delete <hash>  # plus --info, --expiry, --open, --json
```
```python
from html_previewer import PreviewClient
c = PreviewClient("https://preview.example.com", api_key=key)
print(c.publish("./site", topic="my-site", expiry="14")["url"])
```
Config: `%APPDATA%\preview\config` on Windows, `~/.config/preview/config`
elsewhere (same shell-style file as above), or `PREVIEW_API_KEY` /
`PREVIEW_API_BASE` env vars. See [client/README.md](client/README.md) for the
full documentation, and `.github/workflows/pypi.yml` for the PyPI release
pipeline (Trusted Publishing).

## API
| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/_api/health` | no | liveness |
| POST | `/_api/publish` | yes | body = raw zip or html; params `topic`,`days`,`ttl`,`type`,`new`,`dry_run` (query or `X-…` headers). A live, not-yet-expired same-`topic` preview is updated in place (unless `new=1`); an expired name gets a new URL. `ttl` (seconds) overrides `days`. `dry_run=1` predicts only. |
| GET | `/_api/check` | yes | dry-run: would publishing `topic` (params `topic`,`new`) update in place or create a new URL? Changes nothing. |
| GET | `/_api/list[?all=1]` | yes | list real previews |
| GET | `/_api/info/{hash}` | yes | one preview record |
| POST | `/_api/expiry/{hash}` | yes | admin expiry control; param `action` = `extend` (prolong a live preview by `days`, default 30), `expire` (reclaim now + mark expired; archive kept), or `renew` (revive an expired one for `days`, default 7, restoring from the git archive if already reclaimed). `days` via query or `X-Days`. |
| DELETE | `/_api/preview/{hash}` | yes | delete live copy now (git archive kept) |

## Architecture
| Piece | System install | User / Docker install |
|---|---|---|
| Static preview serving | nginx vhost | the Python app (`PREVIEW_SERVE_STATIC=1`) |
| API service | systemd `preview-api` @ `127.0.0.1:8787` | `systemd --user` unit / container |
| Expiry reaper | systemd timer (hourly) | `systemd --user` timer / on boot |
| Live content | `/var/www/preview/live/{hash}/{topic}/…` | `…/preview/live/…` or `/data/live` |
| Manifest (sqlite) + state | `/var/lib/preview/` | `…/preview/state` or `/data/state` |
| Config + API key | `/etc/preview/preview.env` (0600) | `~/.config/preview/preview.env` or `/data` |

## Git archive (optional, append-only, remote-authoritative)
Each publish is committed and pushed to a Git repo (e.g. Gitea), browsable in
the web UI and never pruned:
```
previews/<YYYY>/<YYYY-MM-DD>__<hash>__<topic>/
    metadata.json
    site/<topic>/index.html ...
```
Enable by setting `PREVIEW_GIT_*` in the config (see `deploy/preview.env.example`)
and `PREVIEW_GIT_ENABLED=1`, then restart. While disabled, nothing is
cloned/committed (clean disk). A push failure is non-fatal — the commit stays
local (capped) and the reaper retries with backoff.

**The remote is the only full copy; local storage stays thin.** The local
archive repo (`PREVIEW_GIT_REPO_DIR`) is a depth-1, blob-less partial clone:
it permanently holds just the tip commit and tree names (~KBs), never file
contents and never history. Each publish hashes the live files straight into
git objects (no working-tree copy), pushes, and then truncates the local repo
back to the single tip commit — so superseded versions leave local disk as
soon as the remote confirms them. Renewing an expired preview streams just
that entry back with `git archive --remote` (needs a reachable remote and a
server that supports `upload-archive`; Gitea does). When the remote is
unreachable, renew returns a clear error and publishing keeps working; the
unpushed backlog is capped (`PREVIEW_GIT_PENDING_MAX_COMMITS`).

**Live updates vs. new URLs, mirrored in git.** The entry path is keyed by the
preview's original `created_at` + hash + topic, so it is stable for the life of
that hash. An in-place same-name update (same live hash) rewrites the same entry
→ git records a **MODIFIED** change; a fresh hash (re-publishing a name whose
previous preview has **expired**, or `new=1`) lands in a new entry → git records
a **NEW** addition. Old versions are never lost — they remain in the remote's
commit history.

**Why a local expiry/delete never propagates to the remote.** The archive repo
is a *dedicated* repository, physically **separate** from the live content tree
(`PREVIEW_LIVE_ROOT`, default `/var/www/preview/live`). Expiry and manual delete
only ever remove paths under the live tree — they never touch the archive repo.
Archiving only ever adds commits on top of the remote tip, so the remote only
grows and full history is retained: a reclaim of live content never produces a
deletion on the remote.

## Operations (system install)
```bash
systemctl status preview-api preview-reaper.timer
journalctl -u preview-api -f
systemctl start preview-reaper.service     # run the reaper on demand
```
User install: same with `systemctl --user` / `journalctl --user`.

## Configuration reference
All knobs are environment variables (see `deploy/preview.env.example`). Notable:
`PREVIEW_API_KEY`, `PREVIEW_BIND`, `PREVIEW_PORT`, `PREVIEW_PUBLIC_HOST`,
`PREVIEW_PUBLIC_SCHEME`, `PREVIEW_SERVE_STATIC`, `PREVIEW_LIVE_ROOT`,
`PREVIEW_STATE_DIR`, `PREVIEW_STATIC_DIR`, `PREVIEW_DEFAULT_DAYS`,
`PREVIEW_MAX_DAYS`, and the `PREVIEW_GIT_*` archive settings.
