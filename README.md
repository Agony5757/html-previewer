# html-previewer — ephemeral HTML preview hosting

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
  still live **overwrites it in place and keeps the same URL**, so you can share a
  link once and keep refreshing the content (expiry resets on each update). The
  swap is atomic — a malformed re-upload never destroys the previous good version.
  Pass `?new=1` / `X-New: 1` (or `PREVIEW_NEW=1` in the client) to force a fresh URL.
- **Auto directory index** — if an uploaded folder has no top-level `index.html`,
  a clean, browsable **directory listing** is generated for every folder that
  lacks one (dotfiles excluded). Provide your own `index.html` and it is left
  untouched.
- **Expiry** 1–30 days (default 7). After expiry the live copy is deleted and
  disk reclaimed; any wrong/expired URL returns a custom **404**.
- **Relative links work** — content is served from a normal directory tree.
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

## Client usage
Set `PREVIEW_API_KEY` (and optionally `PREVIEW_API_BASE`) in
`~/.config/preview/config`, then:
```bash
client/preview-publish.sh ./site/ my-site 14          # publish a whole folder, 14-day expiry
client/preview-publish.sh ./report.zip my-report 14   # publish a zip, 14-day expiry
client/preview-publish.sh ./page.html                 # publish a single html, 7 days
client/preview-publish.sh ./site/ my-site             # re-run = update in place (same URL)
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

## API
| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/_api/health` | no | liveness |
| POST | `/_api/publish` | yes | body = raw zip or html; params `topic`,`days`,`type`,`new` (query or `X-…` headers). Same-name (`topic`) re-publishes update the live preview in place unless `new=1`. Folders without `index.html` get an auto-generated directory listing. |
| GET | `/_api/list[?all=1]` | yes | list real previews |
| GET | `/_api/info/{hash}` | yes | one preview record |
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

## Git archive (optional, append-only)
Each publish can be committed into a local working clone and pushed to a Git
repo (e.g. Gitea), browsable in the web UI and never pruned:
```
previews/<YYYY>/<YYYY-MM-DD>__<hash>__<topic>/
    metadata.json
    site/<hash>/<topic>/index.html ...
```
Enable by setting `PREVIEW_GIT_*` in the config (see `deploy/preview.env.example`)
and `PREVIEW_GIT_ENABLED=1`, then restart. While disabled, nothing is
cloned/committed (clean disk). A push failure is non-fatal — the commit stays
local and the reaper retries.

**Why a local expiry/delete never propagates to the remote.** The archive repo
(`PREVIEW_GIT_REPO_DIR`, default `/var/lib/preview/archive-repo`) is a *dedicated*
repository, physically **separate** from the live content tree
(`PREVIEW_LIVE_ROOT`, default `/var/www/preview/live`). Expiry and manual delete
only ever remove paths under the live tree — they never touch the archive repo.
Archiving only ever copies *into* the repo and runs `git add -A` + commit + push,
so the working tree only grows: `git status` never shows deletions and the remote
never receives one. A same-name update is written as a **new** unique snapshot
dir, so even re-publishing one name stays strictly additive (every version is
kept). Because the repo holds the `previews/` archive *only* (not the live tree,
not the source code), no sparse-checkout / partial tracking is needed — the
separation already scopes exactly what is versioned.

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
