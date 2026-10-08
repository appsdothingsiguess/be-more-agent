import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _text(name):
    return (ROOT / name).read_text()


def test_bash_syntax():
    for f in ("setup.sh", "start_agent.sh"):
        assert subprocess.run(["bash", "-n", str(ROOT / f)]).returncode == 0


def test_help_exits_zero():
    r = subprocess.run(["bash", str(ROOT / "setup.sh"), "--help"], capture_output=True, text=True)
    assert r.returncode == 0 and "--dry-run" in r.stdout


def test_setup_has_no_model_downloads():
    t = _text("setup.sh").lower()
    for bad in ("ollama", "whisper", "piper", "moondream", "curl", "wget", "huggingface", ".gguf"):
        assert bad not in t, bad


def test_requirements_thin():
    t = _text("requirements.txt").lower()
    pkgs = [l for l in t.splitlines() if l.strip() and not l.startswith("#")]
    joined = "\n".join(pkgs)
    for bad in ("ollama", "sounddevice", "numpy", "scipy", "duckduckgo", "openwakeword"):
        assert bad not in joined, bad
    assert "requests" in joined and "pillow" in joined


def test_service_template():
    t = _text("tools/bmo-agent.service")
    assert "User=" in t and "network-online.target" in t and "Restart=always" in t
    code = "\n".join(l for l in t.splitlines() if not l.lstrip().startswith("#"))
    assert "token" not in code.lower()
    assert "Bearer" not in t


def test_dry_run_writes_nothing(tmp_path):
    def snapshot():
        return sorted(
            str(p) for p in ROOT.rglob("*")
            if ".git" not in p.parts and "__pycache__" not in p.parts and ".pytest_cache" not in p.parts
        )

    before = snapshot()
    venv = tmp_path / "v"
    r = subprocess.run(
        ["bash", str(ROOT / "setup.sh"), "--dry-run", "--skip-apt", "--venv", str(venv)],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
    assert "[dry-run]" in r.stdout and "-m venv" in r.stdout
    assert snapshot() == before
    assert not venv.exists()
