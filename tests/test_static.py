"""Static frontend checks: served content types, no external URLs, id consistency."""
import re
from pathlib import Path

import requests

from tests.test_web import web  # noqa: F401  (fixture)

STATIC = Path(__file__).resolve().parent.parent / "app" / "web" / "static"
FILES = ["index.html", "app.css", "app.js", "mic-worklet.js"]


def test_static_content_types(web):  # noqa: F811
    for name, want in (("app.css", "text/css"), ("app.js", "javascript"), ("mic-worklet.js", "javascript")):
        r = requests.get(f"{web.base}/static/{name}")
        assert r.status_code == 200, name
        assert want in r.headers["Content-Type"], name
        assert r.headers["X-Content-Type-Options"] == "nosniff"
    r = requests.get(web.base + "/")
    assert r.status_code == 200 and "text/html" in r.headers["Content-Type"]


def test_files_exist():
    for name in FILES:
        assert (STATIC / name).is_file(), name


def test_index_references_only_static_paths():
    html = (STATIC / "index.html").read_text()
    refs = re.findall(r'(?:src|href)="([^"]+)"', html)
    assert refs
    for ref in refs:
        assert ref.startswith("/static/"), ref
        assert (STATIC / ref[len("/static/"):]).is_file(), ref


def test_no_external_urls():
    for name in FILES:
        text = (STATIC / name).read_text()
        assert not re.search(r"https?://", text), name
        assert "//cdn" not in text and "@import" not in text, name


def test_js_ids_exist_in_html():
    html = (STATIC / "index.html").read_text()
    html_ids = set(re.findall(r'\bid="([^"]+)"', html))
    js = (STATIC / "app.js").read_text()
    used = set(re.findall(r'\$\("([^"]+)"\)', js))
    used |= set(re.findall(r'getElementById\("([^"]+)"\)', js))
    assert used
    missing = used - html_ids
    assert not missing, missing
    # ids passed to helper functions as string literals
    for m in re.findall(r'bindRange\("([^"]+)","([^"]+)"', js):
        assert set(m) <= html_ids, m
    for m in re.findall(r'bindCheck\("([^"]+)"', js):
        assert m in html_ids, m
    for rowid in re.findall(r'"(row-[a-z]+)"', js):
        assert rowid in html_ids, rowid


def test_worklet_name_matches():
    assert 'registerProcessor("bmo-mic"' in (STATIC / "mic-worklet.js").read_text()
    assert '"bmo-mic"' in (STATIC / "app.js").read_text()
