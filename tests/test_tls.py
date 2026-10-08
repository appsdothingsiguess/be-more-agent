import json
import socket
import ssl
import stat
import subprocess

import pytest
import requests

from app.config import Config
from app.web import tls
from app.web.server import WebServer
from tests.test_web import PIN, FakeController

SANS = ["DNS:bmo-pi", "DNS:localhost", "IP:127.0.0.1"]


def san_text(cert):
    return subprocess.run(["openssl", "x509", "-noout", "-ext", "subjectAltName", "-in", str(cert)],
                          capture_output=True, text=True, check=True).stdout


def test_generate_sans_and_modes(tmp_path):
    cert, key = tmp_path / "t" / "cert.pem", tmp_path / "t" / "key.pem"
    tls.ensure_self_signed(cert, key, SANS)
    out = san_text(cert)
    assert "DNS:bmo-pi" in out and "IP Address:127.0.0.1" in out
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    assert stat.S_IMODE(cert.parent.stat().st_mode) == 0o700
    assert json.loads((cert.parent / "sans.json").read_text()) == sorted(SANS)


def test_no_regen_then_regen(tmp_path):
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    tls.ensure_self_signed(cert, key, SANS)
    first = cert.read_bytes()
    calls = []
    tls.ensure_self_signed(cert, key, list(reversed(SANS)), run=lambda *a, **k: calls.append(a))
    assert not calls and cert.read_bytes() == first
    tls.ensure_self_signed(cert, key, SANS + ["DNS:other.example"])
    assert cert.read_bytes() != first and "other.example" in san_text(cert)
    cert.unlink()
    tls.ensure_self_signed(cert, key, SANS + ["DNS:other.example"])
    assert cert.exists()


def test_local_sans():
    s = tls.local_sans(["x.example"])
    for want in ("DNS:bmo-pi", "DNS:bmo-pi.local", "IP:127.0.0.1", "DNS:x.example"):
        assert want in s


def test_build_context_modes(tmp_path):
    cfg = Config()
    assert tls.build_context(cfg.web) is None
    cfg.web.tls = "self_signed"
    cfg.web.cert_file, cfg.web.key_file = str(tmp_path / "c.pem"), str(tmp_path / "k.pem")
    ctx = tls.build_context(cfg.web)
    assert ctx.minimum_version == ssl.TLSVersion.TLSv1_2
    cfg.web.tls = "files"
    assert tls.build_context(cfg.web) is not None
    cfg.web.cert_file = str(tmp_path / "missing.pem")
    with pytest.raises(OSError):
        tls.build_context(cfg.web)


@pytest.fixture
def tlsweb(tmp_path):
    cfg = Config()
    cfg.web.host = "127.0.0.1"
    cfg.web.port = 0
    cfg.web.tls = "self_signed"
    cfg.web.tls_port = 0
    cfg.web.cert_file, cfg.web.key_file = str(tmp_path / "c.pem"), str(tmp_path / "k.pem")
    cfg.runtime_dir = str(tmp_path / "rt")
    srv = WebServer(cfg, FakeController(cfg), pin=PIN)
    srv.start()
    srv.cert = str(tmp_path / "c.pem")
    yield srv
    srv.stop()


def test_https_fetch_and_secure_cookie(tlsweb):
    w = tlsweb
    assert w.tls_active and w.url == f"https://127.0.0.1:{w.port}" and len(w.urls) == 2
    r = requests.get(w.url + "/", verify=w.cert)
    assert r.status_code == 200 and "BMO" in r.text
    r = requests.post(w.url + "/api/login", json={"pin": PIN}, verify=w.cert)
    assert r.status_code == 200 and "Secure" in r.headers["Set-Cookie"]


def test_redirect(tlsweb):
    w = tlsweb
    r = requests.get(f"http://127.0.0.1:{w.http_port}/some/path?a=1", allow_redirects=False)
    assert r.status_code == 301
    assert r.headers["Location"] == f"https://127.0.0.1:{w.port}/some/path?a=1"


def test_garbage_does_not_block(tlsweb):
    w = tlsweb
    stalled = socket.create_connection(("127.0.0.1", w.port))
    junk = socket.create_connection(("127.0.0.1", w.port))
    junk.sendall(b"GET / HTTP/1.1\r\n\r\n")
    try:
        r = requests.get(w.url + "/", verify=w.cert, timeout=5)
        assert r.status_code == 200
    finally:
        stalled.close()
        junk.close()


def test_plain_cookie_not_secure():
    cfg = Config()
    cfg.web.host, cfg.web.port = "127.0.0.1", 0
    srv = WebServer(cfg, FakeController(cfg), pin=PIN)
    srv.start()
    try:
        r = requests.post(f"http://127.0.0.1:{srv.port}/api/login", json={"pin": PIN})
        assert "Secure" not in r.headers["Set-Cookie"]
    finally:
        srv.stop()


def test_fallback_to_http(tmp_path):
    cfg = Config()
    cfg.web.host, cfg.web.port = "127.0.0.1", 0
    cfg.web.tls = "files"
    cfg.web.cert_file = cfg.web.key_file = str(tmp_path / "nope.pem")
    srv = WebServer(cfg, FakeController(cfg), pin=PIN)
    srv.start()
    try:
        assert not srv.tls_active and srv.url.startswith("http://")
        assert requests.get(srv.url + "/").status_code == 200
    finally:
        srv.stop()
