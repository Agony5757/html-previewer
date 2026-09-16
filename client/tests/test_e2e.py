"""End-to-end tests: boot the real server (from the repo checkout) and drive
the client library + CLI against it. Skipped when the server sources are not
present (e.g. inside the sdist/wheel).
"""
import io
import json
import contextlib
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
import zipfile
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from html_previewer import PreviewClient, PreviewError
from html_previewer import cli

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVER = REPO_ROOT / "server" / "preview_api.py"
API_KEY = "e2e-test-key-123"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _fetch(url: str):
    with urllib.request.urlopen(url, timeout=10) as r:
        return r.status, r.read()


@unittest.skipUnless(SERVER.exists(), f"server sources not found: {SERVER}")
class EndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="preview-e2e-")
        tmp = Path(cls.tmp.name)
        cls.port = _free_port()
        env = {
            **os.environ,
            "PREVIEW_API_KEY": API_KEY,
            "PREVIEW_BIND": "127.0.0.1",
            "PREVIEW_PORT": str(cls.port),
            "PREVIEW_SERVE_STATIC": "1",
            "PREVIEW_PUBLIC_HOST": f"127.0.0.1:{cls.port}",
            "PREVIEW_PUBLIC_SCHEME": "http",
            "PREVIEW_LIVE_ROOT": str(tmp / "live"),
            "PREVIEW_STATE_DIR": str(tmp / "state"),
            "PREVIEW_STATIC_DIR": str(tmp / "static"),
            "PREVIEW_GIT_ENABLED": "0",
        }
        cls.proc = subprocess.Popen(
            [sys.executable, str(SERVER)], env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.client = PreviewClient(cls.base, api_key=API_KEY)
        deadline = time.monotonic() + 15
        while True:
            if cls.proc.poll() is not None:
                out = cls.proc.stdout.read() if cls.proc.stdout else ""
                raise RuntimeError(f"server died during startup:\n{out}")
            try:
                status, _ = _fetch(f"{cls.base}/_api/health")
                if status == 200:
                    break
            except urllib.error.URLError:
                pass
            if time.monotonic() > deadline:
                cls.tearDownClass()
                raise RuntimeError("server did not come up within 15s")
            time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        if cls.proc.poll() is None:
            cls.proc.terminate()
            try:
                cls.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                cls.proc.kill()
        cls.tmp.cleanup()

    # -- helpers ------------------------------------------------------------

    def _site(self, name: str, *, with_index: bool = True) -> Path:
        root = Path(self.tmp.name) / "sites" / name
        (root / "assets").mkdir(parents=True, exist_ok=True)
        (root / "pages").mkdir(parents=True, exist_ok=True)
        (root / "assets" / "style.css").write_text("body{color:red}",
                                                   encoding="utf-8")
        (root / "pages" / "about.html").write_text("<p>about</p>",
                                                   encoding="utf-8")
        if with_index:
            (root / "index.html").write_text(
                "<h1>hello e2e</h1>", encoding="utf-8")
        return root

    def _unique(self, prefix: str) -> str:
        return f"{prefix}-{self.id().rsplit('.', 1)[-1].lower()}"

    # -- tests ----------------------------------------------------------------

    def test_health(self):
        self.assertIs(self.client.health()["ok"], True)

    def test_publish_folder_fetch_and_relative_links(self):
        topic = self._unique("site")
        res = self.client.publish(self._site(topic), topic=topic, expiry="14")
        self.assertTrue(res["ok"])
        self.assertEqual(res["action"], "create")
        self.assertFalse(res["updated"])
        self.assertTrue(res["has_index_html"])
        self.assertEqual(res["days"], 14)
        url = res["url"]
        self.assertEqual(url, f"{self.base}/{res['hash']}/{topic}/index.html")
        status, body = _fetch(url)
        self.assertEqual(status, 200)
        self.assertIn(b"hello e2e", body)
        status, body = _fetch(f"{self.base}/{res['hash']}/{topic}/pages/about.html")
        self.assertEqual((status, body), (200, b"<p>about</p>"))
        status, body = _fetch(f"{self.base}/{res['hash']}/{topic}/assets/style.css")
        self.assertEqual((status, body), (200, b"body{color:red}"))

    def test_same_name_update_keeps_url(self):
        topic = self._unique("upd")
        site = self._site(topic)
        first = self.client.publish(site, topic=topic)
        site.joinpath("index.html").write_text("<h1>v2</h1>", encoding="utf-8")
        second = self.client.publish(site, topic=topic, expiry="3")
        self.assertTrue(second["updated"])
        self.assertEqual(second["action"], "update")
        self.assertEqual(second["hash"], first["hash"])
        self.assertEqual(second["url"], first["url"])
        self.assertEqual(second["days"], 3)
        _, body = _fetch(first["url"])
        self.assertIn(b"v2", body)

    def test_force_new_mints_new_url(self):
        topic = self._unique("force")
        site = self._site(topic)
        first = self.client.publish(site, topic=topic)
        second = self.client.publish(site, topic=topic, new=True)
        self.assertFalse(second["updated"])
        self.assertNotEqual(second["hash"], first["hash"])
        self.assertNotEqual(second["url"], first["url"])

    def test_dry_run_and_check(self):
        topic = self._unique("dry")
        site = self._site(topic)
        res = self.client.publish(site, topic=topic, dry_run=True)
        self.assertEqual(res["action"], "create")
        self.client.publish(site, topic=topic)
        res = self.client.publish(site, topic=topic, dry_run=True)
        self.assertEqual(res["action"], "update")
        self.assertEqual(res["hash"], self.client.check(topic)["hash"])
        self.assertEqual(self.client.check(topic, new=True)["action"], "create")

    def test_publish_single_html(self):
        page = Path(self.tmp.name) / "page.html"
        page.write_text("<h1>single</h1>", encoding="utf-8")
        res = self.client.publish(page, expiry="2m")
        self.assertEqual(res["ttl_seconds"], 120)
        status, body = _fetch(res["url"])
        self.assertEqual((status, body), (200, b"<h1>single</h1>"))

    def test_publish_zip_file(self):
        topic = self._unique("zip")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("index.html", "<h1>zipped</h1>")
            zf.writestr("deep/nested.txt", "deep")
        zpath = Path(self.tmp.name) / "bundle.zip"
        zpath.write_bytes(buf.getvalue())
        res = self.client.publish(zpath, topic=topic)
        status, body = _fetch(res["url"])
        self.assertEqual((status, body), (200, b"<h1>zipped</h1>"))

    def test_autoindex_for_folder_without_index(self):
        topic = self._unique("auto")
        res = self.client.publish(self._site(topic, with_index=False),
                                  topic=topic)
        # the server synthesizes the listing as index.html
        self.assertTrue(res["auto_index"])
        self.assertTrue(res["has_index_html"])
        status, body = _fetch(f"{self.base}/{res['hash']}/{topic}/")
        self.assertEqual(status, 200)
        self.assertIn(b"pages/", body)

    def test_list_info_delete(self):
        topic = self._unique("lifecycle")
        res = self.client.publish(self._site(topic), topic=topic)
        h = res["hash"]
        self.assertIn(h, [p["hash"] for p in self.client.list_previews()])
        info = self.client.info(h)["preview"]
        self.assertEqual((info["hash"], info["topic"], info["status"]),
                         (h, topic, "live"))
        out = self.client.delete(h)
        self.assertEqual(out["status"], "deleted")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            _fetch(res["url"])
        self.assertEqual(cm.exception.code, 404)
        self.assertNotIn(h, [p["hash"] for p in self.client.list_previews()])
        self.assertIn(h, [p["hash"] for p in self.client.list_previews(all=True)])

    def test_expiry_extend_expire_renew(self):
        topic = self._unique("expiry")
        res = self.client.publish(self._site(topic), topic=topic)
        h = res["hash"]
        before = res["expires_at"]
        extended = self.client.expiry(h, "extend", days=5)["preview"]
        self.assertGreaterEqual(extended["expires_at"],
                                before + 5 * 86400 - 60)
        expired = self.client.expiry(h, "expire")["preview"]
        self.assertEqual(expired["status"], "expired")
        # no git archive configured -> nothing to restore from -> 409
        with self.assertRaises(PreviewError) as cm:
            self.client.expiry(h, "renew")
        self.assertEqual(cm.exception.status, 409)

    def test_bad_api_key(self):
        bad = PreviewClient(self.base, api_key="wrong")
        with self.assertRaises(PreviewError) as cm:
            bad.list_previews()
        self.assertEqual(cm.exception.status, 401)
        nokey = PreviewClient(self.base, api_key=None)
        with self.assertRaises(PreviewError):
            nokey.list_previews()

    def test_unsupported_path(self):
        junk = Path(self.tmp.name) / "junk.txt"
        junk.write_text("x", encoding="utf-8")
        with self.assertRaises(PreviewError):
            self.client.publish(junk)
        with self.assertRaises(PreviewError):
            self.client.publish(Path(self.tmp.name) / "missing")

    # -- CLI-level tests ----------------------------------------------------

    def _cli(self, *argv):
        buf = io.StringIO()
        code = 0
        with redirect_stdout(buf):
            code = cli.main([*argv, "--base", self.base, "--key", API_KEY])
        return code, buf.getvalue()

    def test_cli_publish_list_delete(self):
        topic = self._unique("cli")
        site = self._site(topic)
        code, out = self._cli(str(site), topic, "14")
        self.assertEqual(code, 0)
        self.assertIn(topic, out)
        self.assertIn(self.base, out)
        # the URL printed is fetchable
        url = [l.strip() for l in out.splitlines() if "url:" in l][0].split(None, 1)[1]
        self.assertEqual(_fetch(url)[0], 200)
        code, out = self._cli("--dry-run", str(site), topic)
        self.assertEqual(code, 0)
        self.assertIn("UPDATE in place", out)
        code, out = self._cli("--list", "--json")
        self.assertEqual(code, 0)
        self.assertIn(topic, [p["topic"] for p in json.loads(out)["previews"]])
        code, out = self._cli("--list")
        self.assertEqual(code, 0)
        self.assertIn(topic, out)
        h = json.loads(self._cli("--list", "--json")[1])["previews"][0]["hash"]
        code, out = self._cli("--delete", h)
        self.assertEqual(code, 0)
        self.assertIn("deleted", out)
        code, out = self._cli("--health")
        self.assertEqual((code, out.strip()), (0, f"ok: {self.base} is alive"))

    def test_cli_expiry_requires_action(self):
        code, out = self._cli("--expiry", "deadbeef")
        self.assertEqual(code, 2)

    def test_cli_bad_expiry_fails_fast(self):
        code, out = self._cli(str(self._site(self._unique("badexp"))),
                              self._unique("badexp"), "9z")
        self.assertEqual(code, 2)

    def test_cli_bad_key_reports_error(self):
        buf = io.StringIO()
        import contextlib
        with contextlib.redirect_stderr(buf):
            code = cli.main(["--list", "--base", self.base, "--key", "nope"])
        self.assertEqual(code, 1)
        self.assertIn("401", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
