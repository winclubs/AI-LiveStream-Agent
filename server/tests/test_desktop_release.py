import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DESKTOP = ROOT / "apps" / "desktop-ui"


def test_desktop_dependencies_are_exact_and_lockfile_matches():
    """依赖必须为精确版本（无 ^ / ~ / * / latest）且 lockfile 与之一致。

    历史实现直接断言依赖集合字面量，导致每新增一个依赖都要改测试 ——
    这会诱导「改测试而不是改契约」。改为断言**真正的意图**：
    1. 每个依赖都是精确版本；
    2. lockfile lockfileVersion == 3；
    3. lockfile 中的依赖集合与 package.json 严格相等。
    """
    package = json.loads((DESKTOP / "package.json").read_text(encoding="utf-8"))
    lock = json.loads((DESKTOP / "package-lock.json").read_text(encoding="utf-8"))

    declared = {**package.get("devDependencies", {}), **package.get("dependencies", {})}
    assert declared, "桌面端必须显式声明依赖"

    for name, spec in declared.items():
        assert spec in {"latest", ""} or spec[0] not in "^~><=*xX", (
            f"依赖 {name}={spec} 必须是精确版本：桌面端走 npm ci 锁文件安装，"
            f"范围版本会让构建结果随 registry 漂移"
        )

    # 商用发行必需能力：自动更新依赖 + fuses 加固配置
    assert "electron-updater" in declared, "缺少自动更新依赖，商用版本无法热修"
    fuses = package["build"].get("electronFuses")
    assert isinstance(fuses, dict) and fuses, "缺少 Electron fuses 加固配置"
    assert fuses.get("runAsNode") is False, "必须禁用 ELECTRON_RUN_AS_NODE（可被任意本地进程劫持为调试后门）"
    assert fuses.get("enableNodeOptionsEnvironmentVariable") is False, "必须禁用 NODE_OPTIONS（可注入代码）"
    assert fuses.get("enableNodeCliInspectArguments") is False, "必须禁用命令行调试参数（可被远程附加）"
    assert fuses.get("onlyLoadAppFromAsar") is True, "必须只从 ASAR 加载（阻断路径替换攻击）"

    assert lock["lockfileVersion"] == 3
    lock_root = lock["packages"][""]
    lock_declared = {**lock_root.get("devDependencies", {}), **lock_root.get("dependencies", {})}
    assert lock_declared == declared, "package-lock.json 与 package.json 依赖不一致，npm ci 会直接失败"


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


def test_desktop_package_excludes_tests_from_installer():
    """安装包不得携带后端测试套件（体积虚胖 + 测试夹具外泄）。"""
    package = json.loads((DESKTOP / "package.json").read_text(encoding="utf-8"))
    by_from = {item["from"]: item for item in package["build"]["extraResources"]}

    server_res = by_from["../../server"]
    assert any("!**/tests/**" in f for f in server_res.get("filter", [])), (
        "server 资源必须显式排除 tests/ 目录"
    )


def test_desktop_main_blocks_navigation_and_external_windows():
    """桌面壳必须限制在本机引擎页面，且外链不得在应用窗口内打开。

    历史实现对导航与 window.open 完全不设防：渲染层一旦被注入或出现外链
    跳转，整个桌面壳（含 file:// 权限与后端控制能力）会被带到任意站点。
    """
    source = (DESKTOP / "main.js").read_text(encoding="utf-8")

    assert "setWindowOpenHandler" in source, "必须拦截 window.open 请求"
    assert 'action: "deny"' in source, "外链窗口必须在应用内拒绝"
    assert "will-navigate" in source, "必须拦截顶层导航"
    assert "will-attach-webview" in source, "必须禁止挂载 webview"
    assert 'action: "deny"' in source

    # 预加载脚本只暴露最小 IPC 桥，且绝不泄漏 ipcRenderer
    preload = (DESKTOP / "preload.js").read_text(encoding="utf-8")
    assert "contextBridge" in preload
    assert "ipcRenderer" in preload
    assert "exposeInMainWorld" in preload
    assert 'exposeInMainWorld("desktopBridge"' in preload
    assert 'require("fs")' not in preload, "预加载不得暴露文件系统能力"
    assert 'require("child_process")' not in preload, "预加载不得暴露子进程能力"


def test_autoupdate_failure_never_blocks_streaming_startup():
    """自动更新异常绝不能阻断开播（直播节点必须能离线运行）。"""
    source = (DESKTOP / "main.js").read_text(encoding="utf-8")
    assert "initAutoUpdater()" in source
    # 初始化被 try/catch 包裹，失败只告警
    assert "自动更新初始化异常" in source or "auto-updater] 初始化异常" in source

    updater = (DESKTOP / "autoupdate.js").read_text(encoding="utf-8")
    assert "app.isPackaged" in updater, "开发态必须禁用自动更新"
    assert "不影响开播" in updater, "更新失败必须显式标注为不影响开播"
