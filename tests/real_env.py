"""tests/real_env.py — 真机测试环境（无任何 fake/mock）。

所有测试都直接操作当前正在运行的安卓模拟器（MuMu 12）+ 光遇国服 + 真实
rapidocr OCR 引擎。环境不满足时 **直接报环境错误**（不会 skip、不会假通过）。

要求（缺一不可）：
  1) 模拟器已启动，`adb devices` 能看到设备（默认 127.0.0.1:16384）；
  2) 光遇已登录，并且能进到可操作场景（测试会真实按键/开关聊天面板）；
  3) ADBKeyboard 已安装（中文输入必需）；
  4) 真实 OCR 引擎 rapidocr-onnxruntime + numpy/opencv 可导入。

本机开发环境可能把 OCR 依赖解包在工作区 `.deps/`（gitignore），
这里会在导入失败时自动把 `.deps` 加入 sys.path（仍是真实运行时，不是 mock）。

测试会真实操作游戏：
  - 开关聊天面板、聚焦/退出聊天输入框；
  - MCP 测试会真发 1 条带时间戳的测试消息（用于验证发送回执与回读）；
  - 前台守卫测试会切到系统设置再切回游戏。
测试结束（atexit）会松开所有按键并还原输入法。
"""

from __future__ import annotations

import atexit
import importlib.util
import os
import shutil
import subprocess
import sys
import time
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _path in (ROOT, HERE):
    if _path not in sys.path:
        sys.path.insert(0, _path)


def ensure_ocr_runtime() -> None:
    """确保真实 OCR 依赖可导入；本机开发兜底用工作区 .deps/。"""
    try:
        import rapidocr_onnxruntime  # noqa: F401
        import cv2  # noqa: F401
        import numpy  # noqa: F401
        return
    except Exception as first_error:  # pragma: no cover - 环境错误路径
        deps = os.path.join(ROOT, ".deps")
        if os.path.isdir(deps):
            if deps not in sys.path:
                sys.path.insert(0, deps)
            try:
                import rapidocr_onnxruntime  # noqa: F401
                import cv2  # noqa: F401
                import numpy  # noqa: F401
                return
            except Exception:
                pass
        raise RuntimeError(
            "真机测试需要真实 OCR 引擎：rapidocr-onnxruntime + numpy + opencv。\n"
            "安装：python -m pip install -r requirements.txt\n"
            "（本机开发也可 python tools\\ocr_env.py 解包到 .deps/）\n"
            f"导入错误：{first_error}")


def load_server_module():
    """按路径加载 sky-mcp-server.py（文件名带连字符，不能直接 import）。"""
    ensure_ocr_runtime()
    spec = importlib.util.spec_from_file_location(
        "sky_mcp_server_real", os.path.join(ROOT, "sky-mcp-server.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _adb_executable(config) -> str | None:
    if config.adb_path and os.path.isfile(config.adb_path):
        return config.adb_path
    found = shutil.which("adb")
    if found:
        return found
    sdk = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
    if sdk:
        candidate = os.path.join(sdk, "platform-tools",
                                 "adb.exe" if os.name == "nt" else "adb")
        if os.path.isfile(candidate):
            return candidate
    return None


def _ensure_device_visible(adb: str | None, serial: str | None) -> None:
    """模拟器 adb 端口经常在 adb server 重启后掉线，先 connect 一次。"""
    if not serial or ":" not in serial:
        return
    command = [adb or "adb", "connect", serial]
    try:
        subprocess.run(command, capture_output=True, timeout=15)
    except Exception:
        pass  # 失败也别吞掉真正的错误，后面 ensure_ready() 会给可读原因


class RealEnvironment:
    """真机测试共享环境：真实 AdbDevice + AdbSkyController + McpServer。"""

    def __init__(self) -> None:
        self.server_mod = load_server_module()
        from sky_adb import AdbConfig, AdbDevice

        self.adb_config = AdbConfig.from_file()
        adb = _adb_executable(self.adb_config)
        _ensure_device_visible(adb, self.adb_config.serial)
        self.device = AdbDevice(self.adb_config)
        self.device.ensure_ready()

        self.config = self.server_mod.ServerConfig(
            input_backend="adb",
            adb_config=self.adb_config,
            ocr_engine="rapidocr",
            ocr_prewarm=False,
        )
        self.controller = self.server_mod.AdbSkyController(
            self.config, device=self.device)
        self.mcp = self.server_mod.McpServer(self.controller)

        # 真实 OCR 预热（真模型加载 + 真尺寸推理预热）
        self.controller.prewarm_ocr()
        self.ocr_status = self.controller.ocr_status()
        if self.ocr_status.get("ocr") != "rapidocr":
            raise RuntimeError(
                "真实 OCR 引擎未就绪：status="
                f"{self.ocr_status.get('ocr')!r}，"
                f"ocr_error={self.ocr_status.get('ocr_error')!r}")
        # 截图/OCR 都是真实链路，确认一次模型加载耗时已记录
        if not self.ocr_status.get("ocr_ready"):
            raise RuntimeError("OCR 预热失败：ocr_ready=False")

        # 确保游戏在前台（真实拉起/唤醒）
        try:
            self.device.require_game_foreground()
        except Exception:
            self.controller.focus_game()
            self.device.require_game_foreground()

        self.screen = self.device.screen_size()
        self.fixtures = {
            "world_banner": os.path.join(
                HERE, "fixtures", "sky_world_banner.jpg"),
            "chat_panel": os.path.join(
                HERE, "fixtures", "sky_chat_panel.jpg"),
            "chat_input": os.path.join(
                HERE, "fixtures", "sky_chat_input.jpg"),
            "chat_region": os.path.join(
                HERE, "fixtures", "sky_chat_region.jpg"),
        }
        for name, path in self.fixtures.items():
            if not os.path.isfile(path):
                raise RuntimeError(f"真机测试 fixture 缺失：{name} -> {path}")

    # ---------- 生命周期 ----------

    def close(self) -> None:
        """松开所有键并还原本机输入法（真实设备清理）。"""
        try:
            self.controller.close()
        except Exception:
            pass

    # ---------- 聊天界面状态（全部用真实按键 + 真实 OCR 观察） ----------

    def chat_state(self) -> dict[str, Any]:
        return self.controller.read_chat(limit=5, mode="panel")

    def ensure_input_closed(self) -> None:
        """退出聊天输入焦点（ESC 只在输入框聚焦时退出，不会开菜单）。"""
        if self.device.input_connection_active():
            self.controller.press_key("escape", 200)
            time.sleep(0.4)

    def ensure_panel_open(self, attempts: int = 8) -> dict[str, Any]:
        self.ensure_input_closed()
        state = self.chat_state()
        if state.get("panel_open") is True:
            return state
        for _ in range(attempts):
            self.controller.press_key("c", 40)
            time.sleep(0.9)
            state = self.chat_state()
            if state.get("panel_open") is True:
                return state
        raise AssertionError(
            "无法通过真实按键打开聊天面板：read_chat.panel_open 仍不是 True。"
            f"最后一次 read_chat={ {k: state.get(k) for k in
            ('region', 'panel_open', 'ocr_error')} }")

    def ensure_panel_closed(self, attempts: int = 8) -> None:
        self.ensure_input_closed()
        state = self.chat_state()
        if state.get("panel_open") is not True:
            return
        for _ in range(attempts):
            self.controller.press_key("c", 40)
            time.sleep(0.9)
            state = self.chat_state()
            if state.get("panel_open") is not True:
                return
        raise AssertionError(
            "无法通过真实按键关闭聊天面板：read_chat.panel_open 仍为 True")

    def ensure_game_foreground(self) -> None:
        """确保游戏在前台；若被别的 App 挡住就真实 focus_game 拉回来。"""
        try:
            self.device.require_game_foreground()
            return
        except Exception:
            pass
        self.controller.focus_game()
        deadline = time.time() + 12.0
        while time.time() < deadline:
            try:
                self.device.require_game_foreground()
                return
            except Exception:
                time.sleep(0.5)
        raise AssertionError("focus_game 未能把光遇拉回前台")

    def reset_chat(self) -> None:
        """把聊天 UI 恢复到「面板关闭、输入未聚焦」的干净状态。

        清理阶段不抛异常（避免掩盖真正的测试失败），但会先确保游戏在前台，
        否则残留的其它 App 会让下一条用例级联报错。
        """
        try:
            self.ensure_game_foreground()
            self.ensure_input_closed()
            self.ensure_panel_closed()
        except Exception:
            pass


_env: RealEnvironment | None = None


def get() -> RealEnvironment:
    """获取（惰性初始化）真机测试环境；失败时抛带操作指引的 RuntimeError。"""
    global _env
    if _env is not None:
        return _env
    try:
        _env = RealEnvironment()
    except RuntimeError:
        raise
    except Exception as exc:  # 环境类错误统一包装成可读提示
        raise RuntimeError(
            "真机测试环境未就绪：\n"
            f"  {type(exc).__name__}: {exc}\n"
            "请确认：模拟器已启动、光遇已登录进场景、adb 能看到设备、"
            "ADBKeyboard 已安装、rapidocr 依赖可导入（python tools\\ocr_env.py）。"
        ) from exc
    atexit.register(_env.close)
    return _env


def preflight() -> RealEnvironment:
    """run_all.py 在跑测试前调用：环境问题给出可操作提示并退出。"""
    return get()
