"""TLS helpers: self-signed certificate via openssl, and the server SSL context."""
from __future__ import annotations

import json
import logging
import os
import socket
import ssl
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)


def local_sans(extra=()) -> list[str]:
    host = socket.gethostname()
    sans = ["DNS:bmo-pi", "DNS:bmo-pi.local", "DNS:localhost"]
    if host:
        sans += [f"DNS:{host}", f"DNS:{host}.local"]
    ips = {"127.0.0.1"}
    try:
        out = subprocess.run(["hostname", "-I"], capture_output=True, text=True,
                             timeout=5).stdout
        for tok in out.split():
            try:
                socket.inet_pton(socket.AF_INET, tok)
                ips.add(tok)
            except OSError:
                pass
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        for info in socket.getaddrinfo(host, None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    sans += [f"IP:{ip}" for ip in sorted(ips)]
    sans += [f"DNS:{h}" for h in extra]
    seen: list[str] = []
    for s in sans:
        if s not in seen:
            seen.append(s)
    return seen


def ensure_self_signed(cert, key, sans, run=subprocess.run) -> None:
    cert, key = Path(cert).expanduser(), Path(key).expanduser()
    sans = sorted(set(sans))
    meta = cert.parent / "sans.json"
    try:
        same = json.loads(meta.read_text()) == sans
    except (OSError, ValueError):
        same = False
    if same and cert.is_file() and key.is_file():
        return
    for d in {cert.parent, key.parent}:
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(d, 0o700)
    tmp_cert, tmp_key = cert.with_name(cert.name + ".tmp"), key.with_name(key.name + ".tmp")
    run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
         "-nodes", "-days", "825", "-subj", "/CN=bmo-pi",
         "-addext", "subjectAltName=" + ",".join(sans),
         "-keyout", str(tmp_key), "-out", str(tmp_cert)],
        check=True, capture_output=True, timeout=60)
    os.chmod(tmp_key, 0o600)
    os.replace(tmp_key, key)
    os.replace(tmp_cert, cert)
    meta.write_text(json.dumps(sans))
    log.info("Generated self-signed certificate %s", cert)


def build_context(webcfg) -> ssl.SSLContext | None:
    mode = webcfg.tls
    if mode == "off":
        return None
    cert = Path(webcfg.cert_file).expanduser()
    key = Path(webcfg.key_file).expanduser()
    if mode == "self_signed":
        ensure_self_signed(cert, key, local_sans(webcfg.tls_hostnames))
    elif mode != "files":
        raise ValueError(f"unknown tls mode: {mode}")
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(str(cert), str(key))
    return ctx
