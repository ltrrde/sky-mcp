"""tests/test_sky_config.py — 运行配置加载（MCP 监听地址/端口/token）。"""

from __future__ import annotations

import json
import os
import shutil
import sys
import unittest
from contextlib import contextmanager

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in (ROOT, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

from sky_config import (ConfigError, DEFAULTS, default_config, describe,
                        load_config, mcp_url)

TMP_DIR = os.path.join(ROOT, "tests", ".tmp_config")
os.makedirs(TMP_DIR, exist_ok=True)

_ENV_KEYS = ("SKY_CONFIG", "SKY_MCP_HOST", "SKY_MCP_PORT", "SKY_MCP_TOKEN",
             "SKY_MCP_URL", "SKY_MCP_TIMEOUT")


def tearDownModule():
    shutil.rmtree(TMP_DIR, ignore_errors=True)


@contextmanager
def clean_env(**overrides):
    """隔离测试环境变量：清掉所有 SKY_MCP_*/SKY_CONFIG，再设 overrides。"""
    saved = {k: v for k, v in os.environ.items() if k in _ENV_KEYS}
    for key in list(saved):
        os.environ.pop(key, None)
    try:
        os.environ.update({k: str(v) for k, v in overrides.items()})
        yield
    finally:
        for key in list(os.environ):
            if key in _ENV_KEYS:
                os.environ.pop(key, None)
        os.environ.update(saved)


def write_config(name: str, payload) -> str:
    path = os.path.join(TMP_DIR, name)
    with open(path, "w", encoding="utf-8") as f:
        if isinstance(payload, str):
            f.write(payload)
        else:
            json.dump(payload, f, ensure_ascii=False)
    return path


class TestConfigLoad(unittest.TestCase):
    def test_defaults_when_no_file(self):
        with clean_env():
            cfg = load_config(os.path.join(TMP_DIR, "does-not-exist.json"))
        self.assertEqual(cfg["mcp"]["host"], "127.0.0.1")
        self.assertEqual(cfg["mcp"]["port"], 9900)
        self.assertEqual(cfg["mcp"]["token"], DEFAULTS["mcp"]["token"])
        self.assertEqual(cfg["mcp"]["timeout"], 30)
        self.assertEqual(mcp_url(cfg), "http://127.0.0.1:9900")

    def test_file_overrides(self):
        path = write_config("cfg1.json", {"mcp": {
            "host": "10.0.0.2", "port": 9911, "token": "tok-file",
            "timeout": 12}})
        with clean_env():
            cfg = load_config(path)
        self.assertEqual(cfg["mcp"]["host"], "10.0.0.2")
        self.assertEqual(cfg["mcp"]["port"], 9911)
        self.assertEqual(cfg["mcp"]["token"], "tok-file")
        self.assertEqual(cfg["mcp"]["timeout"], 12)
        self.assertEqual(mcp_url(cfg), "http://10.0.0.2:9911")
        self.assertIn(path, cfg["_source"])

    def test_env_overrides_file(self):
        path = write_config("cfg2.json", {"mcp": {"port": 9911,
                                                  "token": "tok-file"}})
        with clean_env(SKY_MCP_HOST="0.0.0.0", SKY_MCP_PORT="9999",
                       SKY_MCP_TOKEN="tok-env", SKY_MCP_TIMEOUT="7"):
            cfg = load_config(path)
        self.assertEqual(cfg["mcp"]["host"], "0.0.0.0")
        self.assertEqual(cfg["mcp"]["port"], 9999)
        self.assertEqual(cfg["mcp"]["token"], "tok-env")
        self.assertEqual(cfg["mcp"]["timeout"], 7)

    def test_url_override_and_listen_host_substitution(self):
        path = write_config("cfg3.json", {"mcp": {"url": "http://example:7777/"}})
        with clean_env():
            cfg = load_config(path)
        self.assertEqual(mcp_url(cfg), "http://example:7777")
        with clean_env(SKY_MCP_HOST="0.0.0.0", SKY_MCP_PORT="9905"):
            cfg = load_config(os.path.join(TMP_DIR, "does-not-exist.json"))
        # 监听 0.0.0.0 时对外连接地址换成 127.0.0.1
        self.assertEqual(mcp_url(cfg), "http://127.0.0.1:9905")

    def test_env_url_beats_file(self):
        path = write_config("cfg4.json", {"mcp": {"port": 9911}})
        with clean_env(SKY_MCP_URL="http://remote:1234"):
            cfg = load_config(path)
        self.assertEqual(mcp_url(cfg), "http://remote:1234")

    def test_malformed_json_raises(self):
        path = write_config("bad.json", "{not json")
        with clean_env():
            with self.assertRaises(ConfigError):
                load_config(path)

    def test_wrong_section_type_raises(self):
        path = write_config("bad2.json", {"mcp": "127.0.0.1:9900"})
        with clean_env():
            with self.assertRaises(ConfigError):
                load_config(path)

    def test_default_config_fallback(self):
        cfg = default_config()
        self.assertEqual(cfg["mcp"]["port"], 9900)
        self.assertEqual(cfg["_source"], "(默认值)")

    def test_describe_does_not_leak_token(self):
        path = write_config("cfg5.json", {"mcp": {
            "port": 9911, "token": "super-secret-token"}})
        with clean_env():
            cfg = load_config(path)
        text = describe(cfg)
        self.assertIn("9911", text)
        self.assertIn("token=有", text)
        self.assertNotIn("super-secret-token", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
