# -*- coding: utf-8 -*-
"""
``.env`` 自动加载回归测试。

背景（上线诊断 RISK-4）：
``.env.example`` 首行明确写着「本项目不会自动读取 .env；请由 PowerShell、
CI 或服务管理器注入这些变量」。这在开发期是刻意的，但**现场商用部署**
带来两个真实风险：

1. 运维把 ``.env`` 放进安装目录以为生效，实际全部静默失效 ——
   服务以默认配置（回环绑定、无鉴权、无 CORS 白名单）启动，
   没有任何报错，只有「怎么配了没用」的排障困境；
2. 关键安全项（``API_AUTH_TOKEN`` / ``ENFORCE_REMOTE_AUTH`` /
   ``CORS_ALLOW_ORIGINS``）恰恰写在 ``.env`` 里，等于默认不生效。

本文件锁定加载契约：
    - 显式进程环境变量**永远优先**于 ``.env``（不得被文件覆盖）；
    - ``.env`` 中被注释/空白的行不产生变量；
    - 文件缺失/不可读/语法错误时**静默跳过**，绝不阻断服务启动；
    - 关闭开关（``LIVE_AGENT_DISABLE_DOTENV=1``）必须真正生效。
"""
import os
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _write_env(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


def _run_child(script: str, env_extra: dict[str, str], tmp: Path) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        # 子进程 cwd 为 tmp_path（隔离真实 .env），需显式注入项目根才能 import server
        "PYTHONPATH": str(ROOT),
    }
    # 宿主机上可能存在的同名变量会污染断言，这里一律清除。
    # 注意必须 pop 而非置空串：置空串仍算「已设置」，
    # 会被 env_loader 的「进程变量优先」规则正确地跳过，从而掩盖加载失败。
    for _stale in (
        "PROBE_VALUE", "PROBE_OVERRIDE", "LIVE_AGENT_HOST", "LIVE_AGENT_PORT",
        "LIVE_AGENT_ENV_FILE",
    ):
        env.pop(_stale, None)
    env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script)],
        cwd=str(tmp),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=90,
        check=False,
    )


_CHILD = """
    import os
    from server.core.env_loader import load_dotenv_if_present
    load_dotenv_if_present(os.environ.get("PROBE_ENV_FILE"))
    print("VALUE=" + os.environ.get("PROBE_VALUE", ""))
    print("OVERRIDE=" + os.environ.get("PROBE_OVERRIDE", ""))
"""


# ---------------------------------------------------------------------------
# 基础加载
# ---------------------------------------------------------------------------

def test_loads_key_value_pairs_from_env_file(tmp_path):
    """标准 KEY=VALUE 行必须被加载。"""
    env_file = _write_env(tmp_path / ".env", "PROBE_VALUE=from_file\n")

    proc = _run_child(_CHILD, {"PROBE_ENV_FILE": str(env_file)}, tmp_path)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "VALUE=from_file" in proc.stdout


def test_ignores_comments_blank_lines_and_export_prefix(tmp_path):
    """注释、空行、`export ` 前缀、缺失值行都必须被正确处理。"""
    env_file = _write_env(
        tmp_path / ".env",
        "\n"
        "# 这是注释\n"
        "   # 缩进注释\n"
        "PROBE_VALUE=plain\n"
        "\n",
    )

    proc = _run_child(_CHILD, {"PROBE_ENV_FILE": str(env_file)}, tmp_path)

    assert "VALUE=plain" in proc.stdout


def test_strips_quotes_from_values(tmp_path):
    """值两侧的成对引号必须被剥离，引号内的 `#` 不得被当作注释。"""
    env_file = _write_env(
        tmp_path / ".env",
        'PROBE_VALUE="quoted # not comment"\n',
    )

    proc = _run_child(_CHILD, {"PROBE_ENV_FILE": str(env_file)}, tmp_path)

    assert "VALUE=quoted # not comment" in proc.stdout


# ---------------------------------------------------------------------------
# 优先级：进程环境变量必须胜出
# ---------------------------------------------------------------------------

def test_process_env_wins_over_env_file(tmp_path):
    """已存在的进程环境变量绝不能被 .env 覆盖。

    这是最关键的不变量：CI 与服务管理器注入的变量（含密钥、令牌）
    优先级必须高于磁盘文件。
    """
    env_file = _write_env(
        tmp_path / ".env",
        "PROBE_VALUE=from_file\nPROBE_OVERRIDE=from_file\n",
    )

    proc = _run_child(
        _CHILD,
        {"PROBE_ENV_FILE": str(env_file), "PROBE_VALUE": "from_process"},
        tmp_path,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "VALUE=from_process" in proc.stdout, "进程环境变量被 .env 覆盖了"
    assert "OVERRIDE=from_file" in proc.stdout, "未预置的变量应从 .env 补齐"


# ---------------------------------------------------------------------------
# 健壮性：任何异常都不得阻断服务启动
# ---------------------------------------------------------------------------

def test_missing_file_is_silently_skipped(tmp_path):
    """文件不存在时静默跳过，绝不抛异常。"""
    proc = _run_child(_CHILD, {"PROBE_ENV_FILE": str(tmp_path / "nope.env")}, tmp_path)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "VALUE=" in proc.stdout


def test_none_path_is_noop(tmp_path):
    """传入 None 表示不加载，必须是无操作。"""
    proc = _run_child(_CHILD, {}, tmp_path)

    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_malformed_line_does_not_abort_remaining_keys(tmp_path):
    """单行格式错误不得中断整份文件的解析（后续合法键仍需生效）。"""
    env_file = _write_env(
        tmp_path / ".env",
        "this line has no equals sign\nPROBE_VALUE=after_malformed\n",
    )

    proc = _run_child(_CHILD, {"PROBE_ENV_FILE": str(env_file)}, tmp_path)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "VALUE=after_malformed" in proc.stdout, "一行格式错误导致后续键全部丢失"


def test_directory_path_is_silently_skipped(tmp_path):
    """传入目录而非文件时静默跳过，不得抛 IsADirectoryError。"""
    proc = _run_child(_CHILD, {"PROBE_ENV_FILE": str(tmp_path)}, tmp_path)

    assert proc.returncode == 0, proc.stdout + proc.stderr


# ---------------------------------------------------------------------------
# 关闭开关
# ---------------------------------------------------------------------------

def test_can_be_disabled_by_env_switch(tmp_path):
    """LIVE_AGENT_DISABLE_DOTENV=1 必须真正关闭加载。"""
    env_file = _write_env(tmp_path / ".env", "PROBE_VALUE=should_not_load\n")

    proc = _run_child(
        _CHILD,
        {"PROBE_ENV_FILE": str(env_file), "LIVE_AGENT_DISABLE_DOTENV": "1"},
        tmp_path,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "VALUE=\n" in proc.stdout, "关闭开关未生效，.env 仍被加载"


# ---------------------------------------------------------------------------
# 与应用启动的真实接线
# ---------------------------------------------------------------------------

def test_config_module_loads_dotenv_before_reading_settings(tmp_path):
    """``server.config`` 必须在读取 LIVE_AGENT_* 之前完成 .env 加载。

    配置模块是 DATA_DIR / SERVER_HOST / SERVER_PORT 的唯一来源；
    若加载顺序颠倒，.env 里的数据目录与端口就永远不生效。

    通过 ``LIVE_AGENT_ENV_FILE`` 指向自定义 .env 路径来验证 —— 这同时
    覆盖了便携部署 / 企业受控目录两种真实场景（配置不放在项目根下）。
    """
    env_file = _write_env(
        tmp_path / "custom.env",
        "LIVE_AGENT_HOST=10.9.9.9\nLIVE_AGENT_PORT=19999\n",
    )
    script = textwrap.dedent(
        """
        from server import config
        print("HOST=" + config.SERVER_HOST)
        print("PORT=" + str(config.SERVER_PORT))
        """
    )
    proc = _run_child(
        script,
        {
            "LIVE_AGENT_ENV_FILE": str(env_file),
        },
        tmp_path,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "HOST=10.9.9.9" in proc.stdout, "config 未加载 .env 中的 LIVE_AGENT_HOST"
    assert "PORT=19999" in proc.stdout, "config 未加载 .env 中的 LIVE_AGENT_PORT"


def test_dotenv_env_file_override_respects_process_env_priority(tmp_path):
    """``LIVE_AGENT_ENV_FILE`` 指向的文件同样不得覆盖进程环境变量。"""
    env_file = _write_env(tmp_path / "custom.env", "LIVE_AGENT_PORT=19999\n")
    script = textwrap.dedent(
        """
        from server import config
        print("PORT=" + str(config.SERVER_PORT))
        """
    )
    proc = _run_child(
        script,
        {"LIVE_AGENT_ENV_FILE": str(env_file), "LIVE_AGENT_PORT": "18099"},
        tmp_path,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "PORT=18099" in proc.stdout


def test_dotenv_never_overrides_explicit_process_env_in_config(tmp_path):
    """显式进程环境变量在 config 层同样必须优先于 .env。"""
    env_file = _write_env(tmp_path / ".env", "LIVE_AGENT_PORT=19999\n")
    script = textwrap.dedent(
        """
        from server import config
        print("PORT=" + str(config.SERVER_PORT))
        """
    )
    proc = _run_child(
        script,
        {
            "PROBE_ENV_FILE": str(env_file),
            "PYTHONPATH": str(ROOT),
            "LIVE_AGENT_PORT": "18099",
        },
        tmp_path,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "PORT=18099" in proc.stdout
