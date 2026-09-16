# html-previewer (Python client)

Cross-platform command-line client and library for
[html-previewer](https://github.com/Agony5757/html-previewer) — a self-hosted
service that publishes short-lived HTML previews behind **unguessable
capability URLs**, with automatic expiry and disk reclaim.

Works on **Linux, macOS and Windows**. Pure **stdlib** — zero runtime
dependencies. It is a faithful, feature-complete port of the classic
`preview-publish.sh`, so existing habits and scripts carry over.

## Install

```bash
pip install html-previewer
```

This installs the `preview-publish` command (alias: `html-previewer`).

## Configure

Point the client at your server once:

```bash
preview-publish --base https://preview.example.com --key YOUR_API_KEY --save-config
```

Or just export the variables: `PREVIEW_API_BASE` and `PREVIEW_API_KEY`
(equivalently, put `PREVIEW_API_KEY=…` lines in the config file —
`~/.config/preview/config` on Linux/macOS, `%APPDATA%\preview\config` on
Windows; the same shell-style file the bash client reads).

## Use

```bash
preview-publish ./site/ my-site 14      # publish a folder, 14-day expiry
preview-publish ./report.zip            # publish a zip (topic from filename)
preview-publish ./page.html             # publish a single html page
preview-publish ./site/ my-site 2m      # short-lived: 2 minutes
preview-publish --dry-run ./site/ my    # predict: update in place, or new URL?
preview-publish --new ./site/ my-site   # force a brand-new URL
preview-publish --list                  # list live previews (--list-all: everything)
preview-publish --info <hash>           # one preview's record
preview-publish --delete <hash>         # reclaim now
preview-publish --expiry <hash> --action extend --days 30
preview-publish --open ./site/ my-site  # publish, then open in a browser
preview-publish --health                # is the server alive?
```

Re-publishing under a `topic` that is still live **updates it in place and
keeps the same URL** (expiry resets) — share a link once, keep refreshing the
content. A topic whose preview already expired gets a brand-new URL. Add
`--json` to any command for machine-readable output.

## Library

```python
from html_previewer import PreviewClient

c = PreviewClient("https://preview.example.com", api_key="…")
res = c.publish("./site", topic="my-site", expiry="14")   # expiry: "14" or "2m"
print(res["url"])

for p in c.list_previews():
    print(p["topic"], p["url"], p["seconds_remaining"])

c.check("my-site")          # dry-run: update in place vs new URL
c.info(res["hash"])
c.expiry(res["hash"], "extend", days=30)   # extend | expire | renew
c.delete(res["hash"])
```

## Development

```bash
cd client
python -m pip install -e .[test]
python -m pytest tests -v
```

The test suite includes an end-to-end test that boots the real server from
the repository checkout; it is skipped automatically when the server sources
are absent (e.g. in the sdist).

## License

MIT — see [LICENSE](LICENSE).
