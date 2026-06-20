# html-previewer — 临时 HTML 预览托管

[English](README.md) · **简体中文**

通过**不可猜测的能力 URL（capability URL）**发布短期 HTML 预览，支持自动过期 +
磁盘回收、为没有 `index.html` 的文件夹自动生成目录列表，并可选地以追加方式归档到
Git 仓库。

```
https://preview.example.com/{hash}/{topic}/index.html
                             │       └ 人类可读的标签
                             └ 不可猜测的令牌（secrets.token_urlsafe，约 128 位）
```

> 把 `preview.example.com` 换成你自己的域名（system 安装），或使用
> `http://localhost:8787`（user / docker 安装）。一切都通过环境变量配置——
> 代码里不写死任何个人信息。

## 功能特性
- **不可猜测的 URL** = 唯一的访问控制（能力 URL，无需登录）。
- **支持文件夹** —— 可发布整个目录、一个 `.zip`，或单个 `.html`。
- **同名更新（默认）** —— 当一个名字（`topic`）对应的预览**仍在线且未过期**时，
  再次以该名字发布会**原地覆盖并保持同一个 URL**（过期时间重置），因此你可以只分享
  一次链接、之后不断刷新内容。若该名字之前的预览**已过期**，则会分配一个**全新的
  URL**。覆盖是原子的——一次损坏的重传绝不会破坏之前的正常版本。加 `?new=1` /
  `X-New: 1`（或客户端 `PREVIEW_NEW=1`）可强制总是生成新 URL。
- **Dry-run（预演）** —— 上传前先询问这次发布会“原地更新”还是“新建一个 URL”：
  `client/preview-publish.sh --dry-run <path>` 或 `GET /_api/check?topic=…`。
- **灵活的过期时间** —— 1–30 **天**（默认 7），或用 **`ttl`**（秒）设置不足一天的
  短期预览（客户端支持 `2m`、`36h`、`10d` 等）。
- **自动目录索引** —— 若上传的文件夹没有顶层 `index.html`，会为每个缺少索引页的
  目录生成一个干净、可浏览的**目录列表**（忽略点文件）。若你自带 `index.html`，
  则保持原样不动。
- **过期** —— 过期后在线副本被删除、磁盘被回收（每小时的 reaper）；任何错误/过期
  的 URL 都返回自定义 **404**。
- **相对链接可用** —— 内容以普通目录树的形式提供。
- **管理员首页** —— 站点根路径（`/`）是一个小型后台：粘贴 API 密钥即可列出你的
  **在线**与**已过期**预览页面（中英双语）。
- **可选的自动归档** —— 每次发布都可追加提交到 Git 仓库（只增不删）。
- **API 密钥** —— 用于发布，以及列出预览的*真实*目录。
- 自给自足：**仅依赖 Python 标准库**，无任何外部运行时依赖。

## 部署 —— 三选一（`deploy/install.sh <mode>`）

| 模式 | 命令 | 权限 | 前端 |
|---|---|---|---|
| **用户服务** | `bash deploy/install.sh user` | 无（当前用户） | 应用自己提供一切 |
| **系统服务** | `sudo bash deploy/install.sh system` | root | nginx + systemd |
| **Docker** | `bash deploy/install.sh docker` | docker | 应用自己提供一切 |

三者共用同一份代码，只是文件位置不同、以及是否由 nginx 在前面承接。当应用自己提供
内容时（`user`/`docker`）会设置 `PREVIEW_SERVE_STATIC=1`。

### 1）用户服务（一键，无需 sudo）
```bash
bash deploy/install.sh user
```
安装一个 `systemd --user` 单元；所有东西都在 `~/.local/share/preview` 和
`~/.config/preview` 之下。进程同时提供 API **和**预览，监听
`http://127.0.0.1:8787`。命令会打印生成的 API 密钥。可用
`PREVIEW_PORT=9000 bash deploy/install.sh user` 覆盖端口。

> 想在注销后继续运行：`sudo loginctl enable-linger "$USER"`。

### 2）系统服务（nginx + systemd，多租户机器）
```bash
sudo PREVIEW_DOMAIN=preview.example.com bash deploy/install.sh system
```
把代码安装到 `/opt/preview`，配置写到 `/etc/preview/preview.env`（0600，保存 API
密钥），一个独立的 nginx vhost，`preview-api` 服务以及每小时的 `preview-reaper`
定时器。幂等——重复运行会保留 API 密钥并复用现有配置里的域名。然后：
1. **DNS**：添加 A 记录 `preview.example.com -> <你的服务器IP>`。
2. **TLS**：`sudo PREVIEW_DOMAIN=preview.example.com bash deploy/obtain-cert.sh`
   （certbot --nginx，自动加 443 + 跳转）。

### 3）Docker
```bash
cd deploy/docker && docker compose up -d --build
# API 密钥：
docker compose logs | grep 'API key'
```
单个自给自足的容器提供 `http://localhost:8787`；内容与状态持久化在 `preview-data`
卷中。可在 `docker-compose.yml`（或 `.env`）里设置固定的 `PREVIEW_API_KEY`，以避免
自动生成。

## API 密钥 —— 你的管理凭证
**`PREVIEW_API_KEY` 是所有特权操作的唯一凭证**：发布、删除、列出预览的真实目录，
以及登录管理员首页。**请妥善保存**——任何人拿到它都能发布和管理预览。它只在安装时
显示一次，并以 `0600` 权限保存在服务配置里；它绝不会出现在 URL 或日志中。

**忘了，或想轮换它？** 没有找回途径——在配置里设置新值并重启服务：

| 模式 | 修改 | 然后重启 |
|---|---|---|
| 系统 | `/etc/preview/preview.env` 里的 `PREVIEW_API_KEY=…` | `sudo systemctl restart preview-api` |
| 用户 | `~/.config/preview/preview.env` 里的 `PREVIEW_API_KEY=…` | `systemctl --user restart preview-api` |
| Docker | `docker-compose.yml`/`.env`（或 `-e` 参数）里的 `PREVIEW_API_KEY=…`；如有则删除已保存的 `…/state/api_key` | `docker compose up -d` |

轮换后别忘了同步客户端：把新的 `PREVIEW_API_KEY` 写入 `~/.config/preview/config`，
这样 `preview-publish.sh`（以及管理员页面）才能继续工作。

## 管理员首页
站点根路径（例如 `https://preview.example.com/`）提供一个小型**管理后台**。粘贴
API 密钥即可列出你的**在线**与**已过期 / 失效**预览页面（名称、链接、创建/过期时间
+ 剩余时间、大小、文件数、是否已归档、状态），并带有中/EN 语言切换。每一行都带有
一键过期控制：**延期30天** 为在线预览延长有效期，**立即过期** 立刻回收该预览，
**续期7天** 让已过期的预览重新上线（若内容已被回收，则从 git 归档中恢复）。该密钥是管理
凭证，仅保存在当前浏览器标签页（`sessionStorage`）；点 **Forget（清除）** 即可清掉。
没有有效密钥时，后台不会泄露任何信息。

## 客户端用法
在 `~/.config/preview/config` 里设置 `PREVIEW_API_KEY`（以及可选的
`PREVIEW_API_BASE`），然后：
```bash
client/preview-publish.sh ./site/ my-site 14          # 发布整个文件夹，14 天过期
client/preview-publish.sh ./report.zip my-report 14   # 发布一个 zip，14 天过期
client/preview-publish.sh ./page.html                 # 发布单个 html，7 天
client/preview-publish.sh ./site/ my-site 2m          # 短期：2 分钟过期（ttl）
client/preview-publish.sh ./site/ my-site             # 在线期间重跑 = 原地更新（同一 URL）
client/preview-publish.sh --dry-run ./site/ my-site   # 预演：会原地更新还是新建 URL？
PREVIEW_NEW=1 client/preview-publish.sh ./site/ my-site   # 强制改为生成全新 URL
client/preview-publish.sh --list                      # 列出在线预览
client/preview-publish.sh --delete <hash>             # 立即回收
```
客户端会在本地打包文件夹（优先用 `zip`，否则回退到 `python3`）。等价的原始 curl：
```bash
curl -H "X-API-Key: $KEY" -H "X-Topic: my-report" -H "X-Days: 14" \
     --data-binary @report.zip https://preview.example.com/_api/publish
```

## API
| 方法 | 路径 | 鉴权 | 用途 |
|---|---|---|---|
| GET | `/_api/health` | 否 | 存活检查 |
| POST | `/_api/publish` | 是 | 请求体 = 原始 zip 或 html；参数 `topic`、`days`、`ttl`、`type`、`new`、`dry_run`（查询串或 `X-…` 头）。同 `topic` 且在线未过期的预览会被原地更新（除非 `new=1`）；已过期的名字得到新 URL。`ttl`（秒）覆盖 `days`。`dry_run=1` 只预测。 |
| GET | `/_api/check` | 是 | 预演：发布 `topic`（参数 `topic`、`new`）会原地更新还是新建 URL？不改变任何东西。 |
| GET | `/_api/list[?all=1]` | 是 | 列出真实预览 |
| GET | `/_api/info/{hash}` | 是 | 单条预览记录 |
| POST | `/_api/expiry/{hash}` | 是 | 管理员过期控制；参数 `action` = `extend`（为在线预览延长 `days` 天，默认 30）、`expire`（立即回收并标记为已过期，保留归档）或 `renew`（为已过期的预览续期 `days` 天，默认 7；若内容已被回收则从 git 归档中恢复）。`days` 通过查询串或 `X-Days` 传入。 |
| DELETE | `/_api/preview/{hash}` | 是 | 立即删除在线副本（保留 git 归档） |

## 架构
| 部件 | 系统安装 | 用户 / Docker 安装 |
|---|---|---|
| 静态预览服务 | nginx vhost | Python 应用（`PREVIEW_SERVE_STATIC=1`） |
| API 服务 | systemd `preview-api` @ `127.0.0.1:8787` | `systemd --user` 单元 / 容器 |
| 过期 reaper | systemd 定时器（每小时） | `systemd --user` 定时器 / 启动时 |
| 在线内容 | `/var/www/preview/live/{hash}/{topic}/…` | `…/preview/live/…` 或 `/data/live` |
| 清单（sqlite）+ 状态 | `/var/lib/preview/` | `…/preview/state` 或 `/data/state` |
| 配置 + API 密钥 | `/etc/preview/preview.env`（0600） | `~/.config/preview/preview.env` 或 `/data` |

## Git 归档（可选，只增不删）
每次发布都可以提交到一个本地工作副本并推送到 Git 仓库（例如 Gitea），可在网页里
浏览且永不被裁剪：
```
previews/<YYYY>/<YYYY-MM-DD>__<hash>__<topic>/
    metadata.json
    site/<hash>/<topic>/index.html ...
```
在配置里设置 `PREVIEW_GIT_*`（见 `deploy/preview.env.example`）和
`PREVIEW_GIT_ENABLED=1`，然后重启即可启用。禁用时不克隆/不提交（磁盘干净）。推送
失败不致命——提交先留在本地，reaper 之后会重试。

**在线更新 vs 新 URL，与 git 保持一致。** 归档条目的路径由预览的原始 `created_at` +
hash + topic 决定，因此在该 hash 的整个生命周期内是稳定的。一次原地同名更新（同一个
在线 hash）会重写同一个条目 → git 记录为 **MODIFIED（修改）**；一个新的 hash（重新
发布一个之前已**过期**的名字，或 `new=1`）会落到一个新条目 → git 记录为 **NEW
（新增）**。旧版本永不丢失——它们都保留在提交历史中。

**为什么本地的过期/删除绝不会传播到远端。** 归档仓库
（`PREVIEW_GIT_REPO_DIR`，默认 `/var/lib/preview/archive-repo`）是一个*专用*仓库，
在物理上与在线内容树（`PREVIEW_LIVE_ROOT`，默认 `/var/www/preview/live`）**分离**。
过期与手动删除只会移除在线树下的路径——它们绝不会触碰归档仓库。归档只会往仓库里
*拷入*，并执行 `git add -A` + 提交 + 推送，因此远端只增不减、完整历史被保留：回收
在线内容绝不会在远端产生删除。由于该仓库**只**保存 `previews/` 归档（不含在线树，
也不含源码），因此无需 sparse-checkout / 部分追踪——这种分离本身就精确地界定了被
版本化的范围。

## 运维（系统安装）
```bash
systemctl status preview-api preview-reaper.timer
journalctl -u preview-api -f
systemctl start preview-reaper.service     # 按需运行 reaper
```
用户安装：把命令换成 `systemctl --user` / `journalctl --user` 即可。

## 配置参考
所有开关都是环境变量（见 `deploy/preview.env.example`）。常用：
`PREVIEW_API_KEY`、`PREVIEW_BIND`、`PREVIEW_PORT`、`PREVIEW_PUBLIC_HOST`、
`PREVIEW_PUBLIC_SCHEME`、`PREVIEW_SERVE_STATIC`、`PREVIEW_LIVE_ROOT`、
`PREVIEW_STATE_DIR`、`PREVIEW_STATIC_DIR`、`PREVIEW_DEFAULT_DAYS`、
`PREVIEW_MAX_DAYS`，以及 `PREVIEW_GIT_*` 归档设置。
