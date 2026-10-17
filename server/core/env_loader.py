# -*- coding: utf-8 -*-
"""
最小 ``.env`` 加载器 (server.core.env_loader)

存在理由（上线诊断 RISK-4）：
``.env.example`` 曾声明「本项目不会自动读取 .env」。开发期无妨，但现场
商用部署有两个真实风险：

1. 运维把 ``.env`` 放进安装目录以为生效，实际**全部静默失效** ——
   服务以默认配置（回环绑定、无鉴权、无 CORS 白名单）启动，不报任何错；
2. 关键安全项（``API_AUTH_TOKEN`` / ``ENFORCE_REMOTE_AUTH`` /
   ``CORS_ALLOW_ORIGINS``）恰恰写在 ``.env`` 里，等于默认不生效。

设计取舍：**不引入 python-dotenv**，自实现约 40 行。原因：
    - python-dotenv 不在 server/requirements.txt 中，为一个 40 行功能
      引入新依赖 = 新增供应链面（与刚加的 pip-audit 目标相悖）；
    - 语义需求极窄（KEY=VALUE + 注释 + 引号），无需完整 dotenv 规范
      （多层转义、变量插值、``export`` 语义等）。

三条硬性不变量：
    1. **进程环境变量永远优先**，绝不覆盖（CI / 服务管理器注入的令牌
       与密钥优先级高于磁盘文件）；
    2. 文件缺失 / 不可读 / 语法错误 / 路径是目录 —— 一律**静默跳过**，
       绝不阻断服务启动；
    3. 可用 ``LIVE_AGENT_DISABLE_DOTENV=1`` 完全关闭。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger("LiveAgent.EnvLoader")

#: 关闭开关的取值（与项目其他布尔开关保持一致）
_TRUTHY = ("1", "true", "yes")

#: 默认文件名
DEFAULT_DOTENV_NAME = ".env"

#: 无法识别的行前缀（跳过而非报错，保证一行错误不会带走后续所有键）
_ASSIGNMENT_SEPARATORS = ("=",)


def _strip_quotes(value: str) -> str:
    """剥离值两侧的成对引号；引号内的 `#` 不是注释。"""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def parse_dotenv(content: str) -> dict[str, str]:
    """解析 ``.env`` 文本为键值对。

    支持：``KEY=VALUE``、``export KEY=VALUE``、``#`` 注释、空行、
    成对引号包裹的值。**不**支持变量插值与多层转义（dotenv 完整规范
    的子集，够用且无歧义）。

    无法解析的行被跳过而非抛出，避免单行笔误导致整份配置静默失效。
    """
    result: dict[str, str] = {}
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        # 兼容 shell 的 export 前缀
        if line.startswith("export "):
            line = line[len("export "):].lstrip()

        separator = None
        for candidate in _ASSIGNMENT_SEPARATORS:
            idx = line.find(candidate)
            if idx > 0:
                separator = candidate
                break
        if separator is None:
            # 无 '=' 的行：跳过（注释外的裸文本通常是说明性残留）
            continue

        key = line[:line.index(separator)].strip()
        raw_value = line[line.index(separator) + len(separator):].strip()
        if not key or not key.replace("_", "").isalnum():
            continue

        # 未加引号时，尾部注释应被剥离
        if raw_value[:1] not in ("'", '"'):
            hash_pos = raw_value.find(" #")
            if hash_pos >= 0:
                raw_value = raw_value[:hash_pos].rstrip()

        result[key] = _strip_quotes(raw_value)
    return result


def load_dotenv_if_present(
    path: str | os.PathLike[str] | None = None,
    *,
    override: bool = False,
    environ: dict[str, str] | None = None,
) -> dict[str, str]:
    """加载 ``.env`` 到 ``environ``（默认 ``os.environ``）。

    返回实际写入的键值对（便于调用方日志与测试断言）。

    永不抛异常：任何 I/O 或解析问题都退化为「什么都不做」。
    """
    target = environ if environ is not None else os.environ

    if os.getenv("LIVE_AGENT_DISABLE_DOTENV", "").strip().lower() in _TRUTHY:
        return {}

    env_path = Path(path) if path is not None else Path(DEFAULT_DOTENV_NAME)
    try:
        if not env_path.is_file():
            return {}
        content = env_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning(".env 加载跳过 (%s): %s", env_path, exc)
        return {}

    try:
        parsed = parse_dotenv(content)
    except Exception as exc:  # 防御性：解析器本身也不得成为启动阻断点
        logger.warning(".env 解析跳过 (%s): %s", env_path, exc)
        return {}

    applied: dict[str, str] = {}
    for key, value in parsed.items():
        if not override and key in target:
            # 进程环境变量优先：绝不覆盖 CI / 服务管理器注入的值
            continue
        target[key] = value
        applied[key] = value

    if applied:
        logger.info(".env 已加载 %d 项配置（来源: %s）", len(applied), env_path)
    return applied


def load_project_dotenv(base_dir: str | os.PathLike[str] | None = None,
                        override: bool = False) -> dict[str, str]:
    """从项目根目录加载 ``.env``（``server`` 包的标准入口用法）。

    ``LIVE_AGENT_ENV_FILE`` 可指定自定义路径 —— 便携部署与企业受控配置目录
    常需要把配置放在项目根之外。该变量本身也只从进程环境读取，
    不会被 ``.env`` 覆盖（避免自引用与路径劫持）。
    """
    custom = os.getenv("LIVE_AGENT_ENV_FILE", "").strip()
    if custom:
        return load_dotenv_if_present(custom, override=override)

    root = Path(base_dir) if base_dir is not None else Path(__file__).resolve().parents[2]
    return load_dotenv_if_present(root / DEFAULT_DOTENV_NAME, override=override)
