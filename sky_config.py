#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sky_config.py — MCP 工具库运行配置（监听地址 / 端口 / token）。

把 MCP server 的监听参数集中到 ``sky_config.json``（已 gitignore）：

    {
      "mcp": {"host": "127.0.0.1", "port": 9900, "token": "1234", "timeout": 30}
    }

优先级：环境变量 > sky_config.json > 内置默认值。
模板见 ``sky_config.example.json``；命令行 ``--host/--port/--token`` 仍可临时覆盖。

支持的环境变量：
  SKY_CONFIG, SKY_MCP_HOST, SKY_MCP_PORT, SKY_MCP_TOKEN, SKY_MCP_URL,
  SKY_MCP_TIMEOUT

说明：本项目的决策（说什么、按什么键）由外部 AI 客户端负责，
这里没有 LLM/模型/key 配置。
"""

from __future__ import annotations

import copy
import json
import os

ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG_PATH = os.path.join(ROOT, "sky_config.json")
EXAMPLE_CONFIG_PATH = os.path.join(ROOT, "sky_config.example.json")

DEFAULTS: dict = {
    "mcp": {
        "host": "127.0.0.1",
        "port": 9900,
        "token": "1234",
        "timeout": 30,
    },
}


class ConfigError(ValueError):
    """配置文件格式错误 / 读取失败。"""


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _read_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except OSError as exc:
        raise ConfigError(f"读不到配置文件 {path}: {exc}") from exc
    except ValueError as exc:
        raise ConfigError(f"配置文件不是合法 JSON: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"配置文件顶层必须是对象: {path}")
    mcp = data.get("mcp")
    if mcp is not None and not isinstance(mcp, dict):
        raise ConfigError(f"配置项 'mcp' 必须是对象: {path}")
    return data


def load_config(path: str | None = None) -> dict:
    """读取并合并配置。返回的 dict 一定包含完整的 mcp 字段。"""
    cfg_path = path or os.environ.get("SKY_CONFIG") or DEFAULT_CONFIG_PATH
    data: dict = {}
    if cfg_path and os.path.exists(cfg_path):
        data = _read_json(cfg_path)
    cfg = _deep_merge(DEFAULTS, data)

    env = os.environ
    if env.get("SKY_MCP_HOST"):
        cfg["mcp"]["host"] = env["SKY_MCP_HOST"]
    if env.get("SKY_MCP_URL"):
        cfg["mcp"]["url"] = env["SKY_MCP_URL"]
    if env.get("SKY_MCP_PORT"):
        try:
            cfg["mcp"]["port"] = int(env["SKY_MCP_PORT"])
        except ValueError:
            raise ConfigError(
                f"SKY_MCP_PORT 不是整数: {env['SKY_MCP_PORT']!r}") from None
    if env.get("SKY_MCP_TOKEN"):
        cfg["mcp"]["token"] = env["SKY_MCP_TOKEN"]
    if env.get("SKY_MCP_TIMEOUT"):
        try:
            cfg["mcp"]["timeout"] = float(env["SKY_MCP_TIMEOUT"])
        except ValueError:
            raise ConfigError(
                f"SKY_MCP_TIMEOUT 不是数字: {env['SKY_MCP_TIMEOUT']!r}") from None

    cfg["_source"] = cfg_path if (cfg_path and os.path.exists(cfg_path)) else "(默认值)"
    return cfg


def default_config() -> dict:
    """内置默认配置（配置文件缺失/损坏时兜底，保证服务还能起来）。"""
    cfg = _deep_merge(DEFAULTS, {})
    cfg["_source"] = "(默认值)"
    return cfg


def mcp_url(cfg: dict) -> str:
    """MCP server 的 JSON-RPC URL。

    优先 ``mcp.url``（可指向远端/反代），否则用 host + port 拼。
    """
    mcp = cfg.get("mcp") or {}
    url = str(mcp.get("url") or "").strip()
    if url:
        return url.rstrip("/")
    host = str(mcp.get("host") or "127.0.0.1").strip()
    port = int(mcp.get("port") or 9900)
    if host in ("0.0.0.0", "::", ""):
        host = "127.0.0.1"   # 监听地址不一定是可连接地址
    return f"http://{host}:{port}"


def describe(cfg: dict) -> str:
    """一行配置摘要（不打印 token 明文）。"""
    mcp = cfg.get("mcp") or {}
    token = str(mcp.get("token") or "")
    return (
        f"MCP 监听={mcp.get('host')}:{mcp.get('port')}  "
        f"token={'有' if token else '无'}  "
        f"timeout={mcp.get('timeout')}s  来源={cfg.get('_source')}"
    )


def main(argv: list[str] | None = None) -> int:
    """`python sky_config.py` 打印当前生效配置（排查"配置没读到"）。"""
    import argparse

    parser = argparse.ArgumentParser(description="光遇 MCP 工具库运行配置检查")
    parser.add_argument("--config", help="配置文件路径（默认 sky_config.json）")
    args = parser.parse_args(argv)
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"配置错误: {exc}")
        return 2
    print(describe(cfg))
    shown = {k: v for k, v in cfg.items() if not k.startswith("_")}
    print(json.dumps(shown, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
