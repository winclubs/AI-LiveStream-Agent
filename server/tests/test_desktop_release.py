import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DESKTOP = ROOT / "apps" / "desktop-ui"


def test_desktop_dependencies_are_exact_and_lockfile_matches():
    package = json.loads((DESKTOP / "package.json").read_text(encoding="utf-8"))
    lock = json.loads((DESKTOP / "package-lock.json").read_text(encoding="utf-8"))
    assert package["devDependencies"] == {
        "electron": "44.3.0",
        "electron-builder": "26.15.3",
    }
    assert lock["lockfileVersion"] == 3
    assert lock["packages"][""]["devDependencies"] == package["devDependencies"]


def test_desktop_main_gates_window_and_routes_data_to_user_data():
    source = (DESKTOP / "main.js").read_text(encoding="utf-8")
    assert 'app.getPath("userData")' in source
    assert "LIVE_AGENT_DATA_DIR: dataDir" in source
    assert "LIVE_AGENT_HOST: host" in source
    assert "LIVE_AGENT_PORT: String(port)" in source
    assert "if (await ensureBackend()) {\n    createWindow();" in source
    assert "meta.data_dir" in source
    assert "await ensureBackend();\n  createWindow();" not in source


def test_external_python_runtime_preflight_passes_current_environment(tmp_path):
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["LIVE_AGENT_DATA_DIR"] = str(tmp_path / "desktop-data")
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "runtime_preflight.py")],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    metadata = json.loads(result.stdout.strip().splitlines()[-1])
    assert metadata["ok"] is True
    assert metadata["architecture"] == "64bit"
    assert metadata["version"].startswith(("3.12.", "3.13."))


def test_desktop_package_contains_operational_assets_and_readiness_gate():
    package = json.loads((DESKTOP / "package.json").read_text(encoding="utf-8"))
    resources = {(item["from"], item["to"]) for item in package["build"]["extraResources"]}
    assert ("../../README.md", "README.md") in resources
    assert ("../../.env.example", ".env.example") in resources
    assert ("../../docs", "docs") in resources
    source = (DESKTOP / "main.js").read_text(encoding="utf-8")
    assert 'readyUrl: `${origin}/readyz`' in source
    assert '"-m", "server.run"' in source
    assert "shutdownOwnedBackend" in source
    assert (ROOT / "scripts" / "backup_data.py").is_file()
    assert (ROOT / "scripts" / "restore_data.py").is_file()
    assert (ROOT / "docs" / "operations.md").is_file()
