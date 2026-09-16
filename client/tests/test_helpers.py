import sys
import zipfile
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from html_previewer.client import (build_zip, default_topic, parse_expiry,
                                   slugify_topic)
from html_previewer import config


class ParseExpiryTests(unittest.TestCase):
    def test_days(self):
        self.assertEqual(parse_expiry("7"), {"days": 7})
        self.assertEqual(parse_expiry(" 12 "), {"days": 12})

    def test_ttl_units(self):
        self.assertEqual(parse_expiry("2m"), {"ttl": 120})
        self.assertEqual(parse_expiry("36h"), {"ttl": 129600})
        self.assertEqual(parse_expiry("1s"), {"ttl": 1})
        self.assertEqual(parse_expiry("10d"), {"ttl": 864000})

    def test_none_and_empty(self):
        self.assertIsNone(parse_expiry(None))
        self.assertIsNone(parse_expiry(""))

    def test_bad(self):
        for bad in ("0", "0m", "-3", "7x", "m3", "1.5", "forever"):
            with self.assertRaises(ValueError, msg=bad):
                parse_expiry(bad)


class SlugifyTopicTests(unittest.TestCase):
    # Parity with slugify_topic() in server/preview_common.py
    def test_cases(self):
        cases = [
            ("my-site", "my-site"),
            ("My Site!", "My-Site"),
            ("hello_world", "hello_world"),
            ("a  b", "a-b"),
            (".hidden..-", "hidden"),
            ("", "preview"),
            ("站点", "preview"),
            ("报告 2026", "2026"),
            ("a" * 100, "a" * 64),
            ("v1.2_beta", "v1.2_beta"),
        ]
        for raw, want in cases:
            self.assertEqual(slugify_topic(raw), want, msg=raw)


class DefaultTopicTests(unittest.TestCase):
    def test_derivation(self):
        with tempfile.TemporaryDirectory() as td:
            site = Path(td) / "my site"
            site.mkdir()
            self.assertEqual(default_topic(site), "my site")
            page = Path(td) / "report.html"
            page.touch()
            self.assertEqual(default_topic(page), "report")
            bundle = Path(td) / "bundle.zip"
            bundle.touch()
            self.assertEqual(default_topic(bundle), "bundle")


class BuildZipTests(unittest.TestCase):
    def _make_site(self, root: Path):
        (root / "assets").mkdir(parents=True)
        (root / "pages").mkdir()
        (root / "index.html").write_text("<h1>hi</h1>", encoding="utf-8")
        (root / "assets" / "style.css").write_text("body{}", encoding="utf-8")
        (root / "pages" / "about.html").write_text("<p>x</p>", encoding="utf-8")

    def test_contents_at_archive_root(self):
        with tempfile.TemporaryDirectory() as td:
            site = Path(td) / "site"
            site.mkdir()
            self._make_site(site)
            data = build_zip(site)
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                names = set(zf.namelist())
                # Files must sit at the archive root (no "site/" wrapper,
                # no backslashes even when built on Windows).
                self.assertEqual(names,
                                 {"index.html", "assets/style.css",
                                  "pages/about.html"})
                self.assertEqual(zf.read("index.html").decode(), "<h1>hi</h1>")
                self.assertEqual(zf.read("pages/about.html").decode(), "<p>x</p>")


class ConfigTests(unittest.TestCase):
    def test_parse_config(self):
        text = """
# comment
PREVIEW_API_KEY=abc123
export PREVIEW_API_BASE=https://preview.example.com
QUOTED='with spaces'
DOUBLE="dq"
invalid line ignored
EMPTY=
"""
        cfg = config.parse_config(text)
        self.assertEqual(cfg["PREVIEW_API_KEY"], "abc123")
        self.assertEqual(cfg["PREVIEW_API_BASE"], "https://preview.example.com")
        self.assertEqual(cfg["QUOTED"], "with spaces")
        self.assertEqual(cfg["DOUBLE"], "dq")
        self.assertEqual(cfg["EMPTY"], "")

    def test_save_config_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            cfgfile = Path(td) / "nested" / "config"
            path = config.save_config(base="https://x.example", key="k1",
                                      path=cfgfile)
            self.assertEqual(path, cfgfile)
            cfg = config.load_config(cfgfile)
            self.assertEqual(cfg["PREVIEW_API_BASE"], "https://x.example")
            self.assertEqual(cfg["PREVIEW_API_KEY"], "k1")
            # update preserves unrelated lines, replaces values in place
            cfgfile.write_text(
                "# my notes\nOTHER=keep\nPREVIEW_API_KEY=old\n",
                encoding="utf-8")
            config.save_config(key="k2", path=cfgfile)
            cfg = config.load_config(cfgfile)
            self.assertEqual(cfg["PREVIEW_API_KEY"], "k2")
            self.assertEqual(cfg["OTHER"], "keep")
            self.assertIn("# my notes", cfgfile.read_text(encoding="utf-8"))

    def test_resolve_precedence(self):
        with tempfile.TemporaryDirectory() as td:
            cfgfile = Path(td) / "config"
            cfgfile.write_text(
                "PREVIEW_API_BASE=https://file.example/\n"
                "PREVIEW_API_KEY=filekey\n", encoding="utf-8")
            env = {"PREVIEW_API_BASE": "https://env.example",
                   "PREVIEW_API_KEY": "envkey"}
            with mock.patch.dict(os.environ, env):
                r = config.resolve(config_path=cfgfile)
                self.assertEqual(r["base"], "https://env.example")
                self.assertEqual(r["key"], "envkey")
                r = config.resolve(base="https://flag.example", key="flagkey",
                                   config_path=cfgfile)
                self.assertEqual(r["base"], "https://flag.example")
                self.assertEqual(r["key"], "flagkey")
            env2 = {k: v for k, v in os.environ.items()
                    if k not in ("PREVIEW_API_BASE", "PREVIEW_API_KEY")}
            with mock.patch.dict(os.environ, env2, clear=True):
                r = config.resolve(config_path=cfgfile)
                self.assertEqual(r["base"], "https://file.example")
                self.assertEqual(r["key"], "filekey")

    def test_save_config_requires_something(self):
        with self.assertRaises(ValueError):
            config.save_config(path="/nonexistent/x")


if __name__ == "__main__":
    unittest.main()
