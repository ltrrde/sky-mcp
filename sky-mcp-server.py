#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sky-mcp-server.py — 光遇（安卓模拟器）MCP 工具库。

把「按键 / 中文文本输入 / 截图 / OCR / 前台状态」包装成 MCP 工具，
供任意支持 MCP 或 HTTP JSON-RPC 的 AI 客户端调用。
本项目**不包含决策逻辑**（没有 LLM 循环、没有人设、不替 AI 做决定）；
AI 客户端调用工具、自己判断该按什么键、该说什么话。

底座是纯标准库模块 `sky_adb.py`（可单独测试）：
    * 按键：向模拟器键盘节点写 sendevent。实测 `adb shell input keyevent`
      游戏不认，sendevent 认；按键语义与光遇 PC 版一致（c 聊天面板、
      enter 输入框、escape 返回、wasd 移动、space 跳、f 交互、1/2 动作、
      8+space 回家、方向键选弹窗…）。
    * 中文输入：切到 ADBKeyboard 输入法广播 `ADB_INPUT_B64`，不需要剪贴板；
      server 退出时把用户原来的输入法还原。
    * 截屏：`adb exec-out screencap`（raw 优先；无 numpy/cv2 时退回设备侧
      PNG 编码）。
    * 前台守卫：按安卓 mCurrentFocus 判断，游戏不在前台就拒绝按键/输入，
      防止按键落进模拟器里的其他 App。模拟器窗口最小化/被挡住不受影响。

安全设计：
    * 按键时长上限（见 sky_adb.MAX_PRESS_MS）；panic release：启动 / 异常 /
      退出时松开所有可能悬着的键。
    * 线程锁：HTTP 传输是并发（ThreadingHTTPServer），复合动作
      （开输入框 → 打字 → 发送）必须整体串行，否则请求的按键会交错。
    * HTTP bearer token 校验；stdio / HTTP 两种传输。
    * 单条聊天消息长度上限 240 字符。

工具清单：
    status / focus_game / press_key / open_chat / send_chat / type_text /
    read_screen / screenshot

启动方式：
    # HTTP 传输（端口/token 默认读 sky_config.json 的 mcp 段）
    python sky-mcp-server.py --http
    # 临时覆盖端口/token（不改配置文件）
    python sky-mcp-server.py --http --port 9900 --token 1234
    # stdio 传输（MCP 客户端直接拉起）
    python sky-mcp-server.py
    # 查当前生效配置：python sky_config.py

设备参数默认全自动探测（见 sky_adb.AdbConfig：adb 路径 / 设备序列号 /
包名 / 键盘节点 / 输入法都能自动找），多设备或非默认 adb 时再显式指定：
    --adb-serial 127.0.0.1:16384 --adb-path "D:\\...\\adb.exe" --adb-config adb_config.json

可选依赖（缺失时自动降级，不影响按键/输入）：
    numpy + opencv-python：raw 截屏转 PNG、OCR 前处理。缺了就退回 adb
    设备侧 PNG 编码（慢 5-8 倍）。
    PaddleOCR / Tesseract：read_screen 的 OCR 引擎，惰性导入；都没有时
    texts 返回一条"未安装 OCR 引擎"的提示，不会抛异常。
"""

from __future__ import annotations

import argparse
import base64
import importlib
import json
import os
import re
import secrets
import struct
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from sky_adb import (
    MAX_PRESS_MS,
    AdbConfig,
    AdbDevice,
    AdbError,
    normalize_key as _normalize_adb_key,
)
from sky_config import ConfigError, default_config, load_config as load_sky_config

SERVER_INFO = {"name": "sky-mcp-server", "version": "0.4.0-toolkit"}
PROTOCOL_VERSION = "2025-03-26"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}

#: 现在只有一条执行路径：adb。auto = 自动（就是 adb）。
SUPPORTED_BACKENDS = ("adb", "auto")

#: 旧 PC 时代的输入后端。名字保留在这里只为了给旧调用方一个明确错误，
#: 而不是悄悄 fallback 到别的实现（那样按键会"没反应"却查不出原因）。
REMOVED_BACKENDS: dict[str, str] = {
    "pyautogui": "桌面鼠标键盘注入",
    "pydirectinput": "DirectInput 按键注入",
    "gamepad": "虚拟 Xbox 手柄",
    "arduino": "串口 HID 键盘板",
}

#: 聊天消息长度上限（和旧版一致，避免一条消息把输入框/广播打爆）
MAX_CHAT_CHARS = 240


class SkyError(RuntimeError):
    """执行层错误。消息直接进 MCP tools/call 的 isError 文本，必须人能看懂。"""


def log(message: str) -> None:
    sys.stderr.write(f"[sky-mcp] {message}\n")
    sys.stderr.flush()


def load_optional(module_name: str):
    """惰性导入可选依赖；装没装/能不能加载都不影响主流程。"""
    try:
        return importlib.import_module(module_name)
    except Exception:
        return None


def normalize_key(key: str) -> str:
    """外部按键名 → Linux keycode 表里的规范名（错误转成 SkyError）。"""
    try:
        return _normalize_adb_key(key)
    except AdbError as exc:
        raise SkyError(str(exc)) from exc


@dataclass
class ServerConfig:
    """MCP server 配置。设备层参数单独放在 adb_config（AdbConfig）里。"""

    host: str = "127.0.0.1"
    port: int = 9800
    token: str | None = None
    input_backend: str = "adb"
    screenshot_scale: float = 1.0
    screenshot_max_width: int = 1920
    screenshot_max_height: int = 1080
    adb_config: AdbConfig | None = None


class AdbSkyController:
    """把所有 MCP 工具动作落到 AdbDevice 上。

    测试时可以注入假设备（tests/fake_adb.FakeAdb + AdbDevice）::

        dev = AdbDevice(AdbConfig(adb_path="fake-adb"), runner=FakeAdb())
        controller = AdbSkyController(ServerConfig(), device=dev)
    """

    def __init__(self, config: ServerConfig | None = None,
                 device: AdbDevice | None = None):
        self.config = config or ServerConfig()
        self._device = device
        # 复合动作（open_chat/send_chat 的"开输入框→打字→发送"）串行执行；
        # 单按键的并发安全由 AdbDevice 自己的 _input_lock 保证。
        self._action_lock = threading.RLock()
        # OCR 引擎 / 设备名缓存
        self._ocr_engine: Any = None
        self._ocr_name = "none"
        self._ocr_device = "unknown"
        # 截屏依赖（numpy/cv2）惰性探测：只探一次，失败后一直走 PNG 兜底
        self._cv2: Any = None
        self._np: Any = None
        self._capture_deps_checked = False

    # ===================== 设备 / 生命周期 =====================

    @property
    def device(self) -> AdbDevice:
        """惰性创建 AdbDevice（构造本身不发 adb 命令，第一次用才连）。"""
        if self._device is None:
            try:
                adb_config = self.config.adb_config or AdbConfig.from_file()
                self._device = AdbDevice(adb_config)
            except AdbError as exc:
                raise SkyError(str(exc)) from exc
        return self._device

    def _ready_device(self) -> AdbDevice:
        """确保 adb / 设备 / 包名 / 键盘节点都定位好（可重复调用）。"""
        dev = self.device
        try:
            dev.ensure_ready()
        except AdbError as exc:
            raise SkyError(str(exc)) from exc
        return dev

    def _require_foreground(self, dev: AdbDevice | None = None) -> dict[str, Any]:
        """前台守卫：游戏不在前台就拒绝（错误消息里带「前台」字样，
        方便 AI 客户端识别"用户切走了，等会儿再试"）。"""
        dev = dev or self._ready_device()
        try:
            return dev.require_game_foreground()
        except AdbError as exc:
            raise SkyError(str(exc)) from exc

    def close(self) -> None:
        """退出清理：松开所有悬键 + 还原用户原来的输入法。"""
        with self._action_lock:
            if self._device is None:
                return
            try:
                self._device.close()
            except Exception as exc:  # 清理失败不掩盖真正退出原因
                log(f"关闭设备层时出错（忽略）: {exc}")

    # ===================== 输入后端 / 按键 =====================

    def resolve_backend(self, backend: str | None = None) -> str:
        """解析输入后端。只支持 adb / auto；旧 PC 后端给明确报错。"""
        value = str(backend or self.config.input_backend or "auto").strip().lower()
        if value in REMOVED_BACKENDS:
            raise SkyError(
                f"输入后端 {value!r}（{REMOVED_BACKENDS[value]}）已移除："
                "执行层现在只走「安卓模拟器 + adb」。请把 backend 改成 "
                "\"adb\"（或 \"auto\"）。")
        if value not in SUPPORTED_BACKENDS:
            raise SkyError(
                f"不支持的输入后端: {value!r}。可用: adb, auto"
                "（旧 PC/串口时代的所有后端已移除）。")
        return "adb"

    @staticmethod
    def _clamp_ms(duration_ms: Any) -> int:
        """按键时长夹到 [1, MAX_PRESS_MS]，防止一次调用把键按住太久。"""
        try:
            value = int(duration_ms)
        except (TypeError, ValueError):
            raise SkyError(f"duration_ms 必须是整数: {duration_ms!r}") from None
        return min(max(1, value), MAX_PRESS_MS)

    @staticmethod
    def _parse_keys(key: str) -> list[str]:
        """解析单个键名或组合键表达式（如 ``shift-space`` / ``ctrl+v``）。

        组合键交给 AdbDevice.combo 一次按住再依次松开。
        """
        expr = str(key).strip()
        if not expr:
            raise SkyError("key 不能为空")
        if "+" in expr or "-" in expr:
            parts = [normalize_key(part) for part in re.split(r"[+\-]", expr)
                     if part.strip()]
            if not parts:
                raise SkyError(f"无法解析按键表达式: {key!r}")
            return parts
        return [normalize_key(expr)]

    def _tap_keys(self, keys: list[str], duration_ms: int) -> None:
        """按一下键（组合键走 combo），统一把 AdbError 转成 SkyError。"""
        dev = self._ready_device()
        try:
            if len(keys) == 1:
                dev.press_key(keys[0], duration_ms)
            else:
                dev.combo(keys, duration_ms)
        except AdbError as exc:
            raise SkyError(str(exc)) from exc

    def press_key(self, key: str, duration_ms: int = 80,
                  backend: str | None = None) -> str:
        """按一下键（可长按 / 组合键）。按键语义与光遇 PC 版一致。"""
        resolved = self.resolve_backend(backend)
        keys = self._parse_keys(key)
        duration = self._clamp_ms(duration_ms)
        self._tap_keys(keys, duration)
        return f"pressed {'+'.join(keys)} for {int(duration)}ms via {resolved}"

    def open_chat(self, key: str = "enter", duration_ms: int = 35,
                  backend: str | None = None) -> str:
        """短按聊天键（通常是 enter）打开输入框/聊天面板。

        时长要短：enter 长按会变成语音/长按聊天之类的手感。
        """
        resolved = self.resolve_backend(backend)
        keys = self._parse_keys(key)
        duration = self._clamp_ms(duration_ms)
        self._tap_keys(keys, duration)
        return f"tapped {'+'.join(keys)} for {int(duration)}ms via {resolved}"

    # ===================== 文本输入 =====================

    @staticmethod
    def _validate_message(message: Any) -> str:
        if not isinstance(message, str) or not message.strip():
            raise SkyError("message must be a non-empty string")
        if len(message) > MAX_CHAT_CHARS:
            raise SkyError(
                f"message is too long; keep it under {MAX_CHAT_CHARS} characters")
        return message

    def type_text(
        self,
        message: str,
        send: bool = True,
        enter_tap_ms: int = 35,
        backend: str | None = None,
        require_foreground: bool = True,
    ) -> str:
        """往「已经打开的」聊天输入框打字（中文走 ADBKeyboard），可选回车发送。"""
        message = self._validate_message(message)
        resolved = self.resolve_backend(backend)
        tap_ms = self._clamp_ms(enter_tap_ms)
        with self._action_lock:
            # 打字 = 往当前焦点输入框塞文本；游戏不在前台时必须拒绝，
            # 否则文本会进别的 App（和按键的前台守卫同一个理由）。
            if require_foreground:
                self._require_foreground()
            dev = self._ready_device()
            try:
                dev.type_text(message)
                if send:
                    dev.press_key("enter", tap_ms)
            except AdbError as exc:
                raise SkyError(str(exc)) from exc
        action = "sent" if send else "typed"
        return f"{action} text: {message} via {resolved}"

    def send_chat(
        self,
        message: str,
        open_key: str = "enter",
        open_delay_ms: int = 180,
        assume_open: bool = False,
        send: bool = True,
        enter_tap_ms: int = 35,
        backend: str | None = None,
    ) -> str:
        """开聊天输入框 → 输入一条消息（中文可用）→ 可选回车发送。

        和旧版语义一致：``assume_open=True`` 表示输入框已经由人手动打开，
        这时不再按 open_key（避免短按 enter 又把输入框关掉）。
        """
        message = self._validate_message(message)
        resolved = self.resolve_backend(backend)
        open_keys = self._parse_keys(open_key)
        tap_ms = self._clamp_ms(enter_tap_ms)
        try:
            delay = max(0.0, float(open_delay_ms)) / 1000.0
        except (TypeError, ValueError):
            raise SkyError(f"open_delay_ms 必须是数字: {open_delay_ms!r}") from None
        with self._action_lock:
            # 先查一次前台，别在别的 App 上按 enter 开输入框；后面的每次
            # 按键 / 打字也各自带守卫（用户中途切走会立刻报错）。
            self._require_foreground()
            dev = self._ready_device()
            try:
                if not assume_open:
                    if len(open_keys) == 1:
                        dev.press_key(open_keys[0], tap_ms)
                    else:
                        dev.combo(open_keys, tap_ms)
                    time.sleep(delay)
                dev.type_text(message)
                if send:
                    dev.press_key("enter", tap_ms)
            except AdbError as exc:
                raise SkyError(str(exc)) from exc
        action = "sent" if send else "typed"
        return f"{action} chat: {message} via {resolved}"

    # ===================== 前台 / 焦点 =====================

    def focus_game(self) -> dict[str, Any]:
        """唤醒屏幕；游戏不在前台就拉起（monkey 启动），再确认前台。

        返回前台信息 JSON：模拟器窗口最小化/被挡都不影响，只有安卓层切到
        别的 App 才会拦。
        """
        with self._action_lock:
            dev = self._ready_device()
            try:
                action = dev.launch_game()
            except AdbError as exc:
                raise SkyError(str(exc)) from exc
            # monkey 启动是异步的：给游戏几秒钟起来再检查前台。
            if action.startswith("launched"):
                deadline = time.time() + 8.0
                while time.time() < deadline and not dev.is_game_foreground():
                    time.sleep(0.25)
            foreground = self._require_foreground(dev)
            return {
                "mode": "android-emulator",
                "serial": getattr(dev, "_serial", None),
                "package": getattr(dev, "_package", None),
                "focused": True,
                "action": action,
                "foreground": foreground,
            }

    def ensure_game_foreground(self) -> dict[str, Any]:
        """只校验、不拉起：游戏不在前台就报错（错误消息含「前台」）。"""
        foreground = self._require_foreground()
        return {
            "focused": True,
            "serial": getattr(self._device, "_serial", None),
            "package": getattr(self._device, "_package", None),
            "foreground": foreground,
        }

    # ===================== 截屏 / OCR =====================

    def _capture_libs(self) -> tuple[Any, Any]:
        """惰性加载 numpy/cv2（可选依赖；没装则截屏走 PNG 兜底）。"""
        if not self._capture_deps_checked:
            self._capture_deps_checked = True
            self._cv2 = load_optional("cv2")
            self._np = load_optional("numpy")
            if self._cv2 is None or self._np is None:
                log("未安装 numpy/opencv（可选依赖）：截图改走 adb screencap -p "
                    "兜底（pip install numpy opencv-python 可启用 raw 截屏）")
        return self._cv2, self._np

    def _screencap_png_fallback(self, reason: str) -> bytes:
        """设备侧 PNG 编码兜底：慢 5-8 倍，但不需要任何 Python 图像库。"""
        log(f"{reason}；改用 adb screencap -p 兜底")
        try:
            return self._ready_device().screencap_png()
        except AdbError as exc:
            raise SkyError(str(exc)) from exc

    def _resize_capture(self, image):
        """按配置缩放截图（screenshot_scale 先缩，再限制到 max 宽高，不放大）。"""
        cv2 = self._cv2
        height, width = image.shape[:2]
        scale = float(self.config.screenshot_scale or 1.0)
        if abs(scale - 1.0) > 1e-6:
            scale = min(max(scale, 0.2), 4.0)
            interp = cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR
            image = cv2.resize(
                image, (max(1, int(width * scale)), max(1, int(height * scale))),
                interpolation=interp)
        height, width = image.shape[:2]
        max_width = max(1, int(self.config.screenshot_max_width))
        max_height = max(1, int(self.config.screenshot_max_height))
        limit = min(max_width / width, max_height / height, 1.0)
        if limit < 1.0:
            image = cv2.resize(
                image, (max(1, int(width * limit)), max(1, int(height * limit))),
                interpolation=cv2.INTER_AREA)
        return image

    def capture_png(self) -> bytes:
        """截一张游戏画面，返回 PNG bytes。

        raw 帧（RGBA/BGRA）+ cv2 编码优先；numpy/cv2 缺失或 raw 通道失败时
        退回设备侧 PNG（screencap -p）。
        """
        cv2, np = self._capture_libs()
        if cv2 is None or np is None:
            return self._screencap_png_fallback("缺少 numpy/cv2")
        dev = self._ready_device()
        try:
            pixels, width, height, pixel_format = dev.screencap_raw()
        except AdbError as exc:
            return self._screencap_png_fallback(f"raw 截屏失败: {exc}")
        try:
            frame = np.frombuffer(pixels, dtype=np.uint8)
            expected = width * height * 4
            if frame.size < expected:
                raise SkyError(
                    f"截屏数据不完整（{frame.size}/{expected} 字节）")
            frame = frame[:expected].reshape((height, width, 4))
            code = (cv2.COLOR_RGBA2BGR if pixel_format == "rgba"
                    else cv2.COLOR_BGRA2BGR)
            image = cv2.cvtColor(frame, code)
            image = self._resize_capture(image)
            ok, buffer = cv2.imencode(
                ".png", image, [cv2.IMWRITE_PNG_COMPRESSION, 1])
            if not ok:
                raise SkyError("PNG 编码失败")
            return buffer.tobytes()
        except SkyError:
            raise
        except Exception as exc:
            return self._screencap_png_fallback(f"raw 帧处理失败: {exc}")

    def screenshot_base64(self, png: bytes | None = None) -> str:
        """截屏 → PNG base64（MCP image content 用）。"""
        data = png if png is not None else self.capture_png()
        return base64.b64encode(data).decode("ascii")

    @staticmethod
    def _png_size(data: bytes) -> tuple[int, int]:
        """从 PNG 头解析尺寸（不依赖任何图像库）。"""
        if (len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n"
                and data[12:16] == b"IHDR"):
            width, height = struct.unpack(">II", data[16:24])
            if 0 < width <= 20000 and 0 < height <= 20000:
                return width, height
        return 0, 0

    def read_screen(self, png: bytes | None = None) -> dict[str, Any]:
        """截屏 + OCR，返回可见文本列表（OCR 引擎缺失时返回提示，不抛异常）。"""
        data = png if png is not None else self.capture_png()
        width, height = self._png_size(data)
        path = None
        try:
            with tempfile.NamedTemporaryFile(
                    prefix="sky-mcp-", suffix=".png", delete=False) as tmp:
                tmp.write(data)
                path = tmp.name
            return {
                "ocr": self._detect_ocr_name(),
                "ocr_device": self._ocr_device,
                "image_size": {"width": width, "height": height},
                "texts": self._run_ocr(path),
            }
        finally:
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass

    def _detect_ocr_name(self) -> str:
        """探测 OCR 引擎（PaddleOCR 优先，Tesseract 兜底），算出来的名字缓存。"""
        if self._ocr_engine is not None:
            return self._ocr_name
        try:
            os.environ.setdefault("PADDLE_PDX_ENABLE_MKLDNN_BYDEFAULT", "0")
            os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
            from paddleocr import PaddleOCR
            try:
                import paddle
                if paddle.device.is_compiled_with_cuda():
                    paddle.set_device("gpu:0")
                self._ocr_device = paddle.get_device()
            except Exception:
                self._ocr_device = "unknown"

            self._ocr_engine = PaddleOCR(
                use_angle_cls=False, lang="ch", det_limit_side_len=1920)
            self._ocr_name = f"paddleocr-mobile-{self._ocr_device}"
            return self._ocr_name
        except Exception:
            pass
        try:
            import pytesseract
            pytesseract.get_tesseract_version()
            self._ocr_engine = pytesseract
            self._ocr_name = "tesseract"
            return self._ocr_name
        except Exception:
            pass
        self._ocr_name = "none"
        return self._ocr_name

    def _enhance_image(self, image_path: str) -> str:
        """OCR 前放大 2 倍 + 提对比度 + 锐化；cv2 缺失时原图直出。

        返回处理后的临时图片路径（无需处理时返回原路径，调用方负责清理）。
        """
        cv2, np = self._capture_libs()
        if cv2 is None or np is None:
            return image_path
        try:
            # np.fromfile + imdecode：Windows 下 cv2.imread 对中文路径不友好
            raw = np.fromfile(image_path, dtype=np.uint8)
            image = cv2.imdecode(raw, cv2.IMREAD_COLOR)
            if image is None:
                return image_path
            height, width = image.shape[:2]
            image = cv2.resize(
                image, (max(1, width * 2), max(1, height * 2)),
                interpolation=cv2.INTER_LINEAR)
            image = cv2.convertScaleAbs(image, alpha=1.5, beta=0)  # 对比度
            blur = cv2.GaussianBlur(image, (0, 0), 1.0)
            image = cv2.addWeighted(image, 1.5, blur, -0.5, 0)     # 锐化(USM)
            ok, buffer = cv2.imencode(".png", image)
            if not ok:
                return image_path
            enhanced_path = image_path + ".enhanced.png"
            with open(enhanced_path, "wb") as handle:
                handle.write(buffer.tobytes())
            return enhanced_path
        except Exception:
            return image_path

    def _run_ocr(self, image_path: str) -> list[dict[str, Any]]:
        engine = self._detect_ocr_name()
        if engine.startswith("paddleocr"):
            return self._run_paddle_ocr(image_path)
        if engine == "tesseract":
            return self._run_tesseract_ocr(image_path)
        return [{
            "text": "未安装 OCR 引擎（PaddleOCR / Tesseract 都不可用）",
            "confidence": 0, "x": 0, "y": 0,
        }]

    def _run_paddle_ocr(self, image_path: str) -> list[dict[str, Any]]:
        """PaddleOCR。兼容 PaddleOCR 3.x（结果 dict）和 2.x（line 列表）两种格式。

        坐标从放大 2 倍的图上算出来，这里统一除 2 还原到截图坐标系。
        """
        enhanced_path = self._enhance_image(image_path)
        try:
            try:
                result = self._ocr_engine.ocr(enhanced_path, cls=True)
            except Exception:
                result = self._ocr_engine.ocr(enhanced_path)
        finally:
            if enhanced_path != image_path:
                try:
                    os.unlink(enhanced_path)
                except OSError:
                    pass
        if not result:
            return []

        texts: list[dict[str, Any]] = []
        if isinstance(result[0], dict):
            for page in result:
                rec_texts = page.get("rec_texts") or []
                rec_scores = page.get("rec_scores") or []
                rec_boxes = page.get("rec_polys") or page.get("dt_polys") or []
                for index, text in enumerate(rec_texts):
                    points = rec_boxes[index] if index < len(rec_boxes) else []
                    if hasattr(points, "tolist"):
                        points = points.tolist()
                    xs = [p[0] for p in points] if points else [0]
                    ys = [p[1] for p in points] if points else [0]
                    texts.append({
                        "text": str(text),
                        "confidence": (float(rec_scores[index])
                                       if index < len(rec_scores) else 0),
                        "x": int(min(xs) / 2),
                        "y": int(min(ys) / 2),
                    })
            return texts

        for line in result[0] or []:
            box, (text, confidence) = line[0], line[1]
            texts.append({
                "text": str(text),
                "confidence": float(confidence),
                "x": int(min(p[0] for p in box) / 2),
                "y": int(min(p[1] for p in box) / 2),
            })
        return texts

    def _run_tesseract_ocr(self, image_path: str) -> list[dict[str, Any]]:
        enhanced_path = self._enhance_image(image_path)
        try:
            # pytesseract 自己会处理文件/像素转换，这里不直接依赖图像库
            text = self._ocr_engine.image_to_string(
                enhanced_path, lang="chi_sim+eng")
        finally:
            if enhanced_path != image_path:
                try:
                    os.unlink(enhanced_path)
                except OSError:
                    pass
        return [
            {"text": line.strip(), "confidence": 0.5, "x": 0, "y": 0}
            for line in text.splitlines()
            if line.strip()
        ]

    # ===================== 自检 =====================

    def status(self) -> dict[str, Any]:
        """执行层自检 JSON（AI 客户端启动时先调它看设备是否就绪）。

        字段平铺在顶层：mode / platform / input_backend / adb / serial /
        package / keyboard_device / screen / foreground / ime / device_model /
        android_version / ocr / ocr_device / screenshot_max / device_ready。

        设备层出错时不抛异常，而是塞 ``error`` 字段——客户端能直接看到
        具体原因（adb 没连上 / 没有设备 / 没装输入法…）。
        前台状态只体现在 foreground.is_game。
        """
        backend = self.resolve_backend()
        info: dict[str, Any] = {
            "mode": "android-emulator",
            "platform": sys.platform,
            "input_backend": backend,
            "ocr": self._detect_ocr_name(),
            "ocr_device": self._ocr_device,
            "screenshot_max": {
                "width": int(self.config.screenshot_max_width),
                "height": int(self.config.screenshot_max_height),
            },
        }
        try:
            dev = self._ready_device()
            device_info = dev.status()
        except (SkyError, AdbError) as exc:
            info["device_ready"] = False
            info["error"] = str(exc)
            return info
        info.update(device_info)
        # 设备层报的是实现细节（adb-sendevent），对外统一报 "adb"
        info["mode"] = "android-emulator"
        info["input_backend"] = backend
        info["device_ready"] = True
        ime = info.get("ime")
        if isinstance(ime, dict):
            # 补充"当前默认输入法"：启动自检要能看出 ADBKeyboard 是否已生效
            try:
                ime.setdefault("current", dev.default_ime())
            except AdbError:
                pass
        return info


def text_content(text: str) -> list[dict[str, str]]:
    return [{"type": "text", "text": text}]


#: MCP 工具表。工具名 / 参数名保持旧版不变，只有描述里的"PC"改成"模拟器"。
TOOLS = [
    {
        "name": "status",
        "description": "Check the adb execution layer: device, input backend, IME, foreground app, OCR engine, and screenshot limits.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "focus_game",
        "description": "Wake the emulator screen and bring Sky to the foreground (launch the game if it is not running); returns foreground info as JSON.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "press_key",
        "description": "Press a keyboard key in Sky running in the Android emulator. Keys use the same semantics as the PC version: WASD movement, space jump/fly, F interact, Q honk, Tab flight mode, C chat panel, escape back.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Key name such as w, a, s, d, space, f, q, e, tab, c, enter, escape, right, or a combo like shift-space."},
                "duration_ms": {"type": "integer", "description": "Hold duration in milliseconds (capped at 2000).", "default": 80},
                "backend": {"type": "string", "description": "Input backend override: adb or auto. The old PC backends are removed.", "default": "adb"},
            },
            "required": ["key"],
        },
    },
    {
        "name": "open_chat",
        "description": "Short-tap the Sky chat key (usually enter). Keep the duration short so Enter does not become voice/chat-hold behavior.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Chat key, usually enter.", "default": "enter"},
                "duration_ms": {"type": "integer", "description": "Tap duration in milliseconds.", "default": 35},
                "backend": {"type": "string", "description": "Input backend override: adb or auto.", "default": "adb"},
            },
        },
    },
    {
        "name": "send_chat",
        "description": "Open Sky chat, type a message through the ADBKeyboard IME (Chinese and emoji supported), and optionally send it with Enter.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "Chat message under 240 characters."},
                "open_key": {"type": "string", "description": "Key used to open the chat input.", "default": "enter"},
                "open_delay_ms": {"type": "integer", "description": "Delay after opening chat before typing.", "default": 180},
                "assume_open": {"type": "boolean", "description": "Set true when the chat input is already open; no open key is sent.", "default": False},
                "send": {"type": "boolean", "description": "Set false to type without pressing Enter.", "default": True},
                "enter_tap_ms": {"type": "integer", "description": "Short Enter tap duration.", "default": 35},
                "backend": {"type": "string", "description": "Input backend override: adb or auto.", "default": "adb"},
            },
            "required": ["message"],
        },
    },
    {
        "name": "type_text",
        "description": "Type text into an already-open Sky chat input through the ADBKeyboard IME and optionally send. Use this when the user manually opened the input box.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "Chat message under 240 characters."},
                "send": {"type": "boolean", "description": "Set false to type without pressing Enter.", "default": True},
                "enter_tap_ms": {"type": "integer", "description": "Short Enter tap duration.", "default": 35},
                "backend": {"type": "string", "description": "Input backend override: adb or auto.", "default": "adb"},
                "require_foreground": {"type": "boolean", "description": "Require Sky to be the foreground app before typing.", "default": True},
            },
            "required": ["message"],
        },
    },
    {
        "name": "read_screen",
        "description": "Capture the emulator screen and OCR visible text.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "screenshot",
        "description": "Capture the emulator screen and return it as a PNG image.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


class McpServer:
    def __init__(self, controller: AdbSkyController):
        self.controller = controller

    def handle_tool_call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "status":
            return {"content": text_content(json.dumps(
                self.controller.status(), ensure_ascii=False, indent=2))}
        if name == "focus_game":
            return {"content": text_content(json.dumps(
                self.controller.focus_game(), ensure_ascii=False, indent=2))}
        if name == "press_key":
            result = self.controller.press_key(
                arguments["key"],
                arguments.get("duration_ms", 80),
                arguments.get("backend"),
            )
            return {"content": text_content(result)}
        if name == "open_chat":
            result = self.controller.open_chat(
                arguments.get("key", "enter"),
                arguments.get("duration_ms", 35),
                arguments.get("backend"),
            )
            return {"content": text_content(result)}
        if name == "send_chat":
            result = self.controller.send_chat(
                arguments["message"],
                arguments.get("open_key", "enter"),
                arguments.get("open_delay_ms", 180),
                arguments.get("assume_open", False),
                arguments.get("send", True),
                arguments.get("enter_tap_ms", 35),
                arguments.get("backend"),
            )
            return {"content": text_content(result)}
        if name == "type_text":
            result = self.controller.type_text(
                arguments["message"],
                arguments.get("send", True),
                arguments.get("enter_tap_ms", 35),
                arguments.get("backend"),
                arguments.get("require_foreground", True),
            )
            return {"content": text_content(result)}
        if name == "read_screen":
            # 只截一次屏：OCR 和返回的图片共用同一帧
            png = self.controller.capture_png()
            result = self.controller.read_screen(png=png)
            text = json.dumps(result, ensure_ascii=False, indent=2)
            return {"content": [
                {"type": "text", "text": text},
                {"type": "image",
                 "data": self.controller.screenshot_base64(png=png),
                 "mimeType": "image/png"},
            ]}
        if name == "screenshot":
            data = self.controller.screenshot_base64()
            return {"content": [
                {"type": "image", "data": data, "mimeType": "image/png"}]}
        raise SkyError(f"Unknown tool: {name}")

    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        method = message.get("method")
        msg_id = message.get("id")
        try:
            if method == "initialize":
                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "protocolVersion": PROTOCOL_VERSION,
                        "capabilities": {"tools": {}},
                        "serverInfo": SERVER_INFO,
                    },
                }
            if method == "notifications/initialized":
                return None
            if method == "tools/list":
                return {"jsonrpc": "2.0", "id": msg_id,
                        "result": {"tools": TOOLS}}
            if method == "tools/call":
                params = message.get("params") or {}
                result = self.handle_tool_call(
                    params.get("name", ""), params.get("arguments") or {})
                result.setdefault("isError", False)
                return {"jsonrpc": "2.0", "id": msg_id, "result": result}
            if method == "ping":
                return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32601, "message": f"Unknown method: {method}"},
            }
        except Exception as exc:
            # 工具级错误走 isError（客户端应检查该字段，否则会当正常结果）
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {"content": text_content(f"Error: {exc}"),
                           "isError": True},
            }


def run_stdio(server: McpServer) -> None:
    log("启动 stdio 传输（换行分隔的 JSON-RPC）")
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        response = server.handle(message)
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()


def require_authorized(handler: BaseHTTPRequestHandler,
                       config: ServerConfig) -> bool:
    if not config.token:
        return True
    auth = handler.headers.get("Authorization", "")
    expected = f"Bearer {config.token}"
    if auth == expected:
        return True
    handler.send_response(401)
    handler.send_header("Content-Type", "application/json")
    handler.end_headers()
    handler.wfile.write(json.dumps(
        {"error": "missing or invalid bearer token"}).encode("utf-8"))
    return False


def run_http(server: McpServer, config: ServerConfig) -> None:
    if config.host not in LOCAL_HOSTS and not config.token:
        raise SystemExit(
            "拒绝在未设置 --token 的情况下绑定非本机地址（httpx 远程调用需鉴权）。")
    # 启动时就把 OCR 引擎探一遍：第一次 OCR 加载模型很慢，别让首个工具调用卡住
    server.controller._detect_ocr_name()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/health":
                self.send_error(404)
                return
            body = json.dumps({
                "status": "ok",
                "server": SERVER_INFO,
                "mode": "android-emulator",
                "ocr": server.controller._detect_ocr_name(),
                "ocr_device": server.controller._ocr_device,
            }).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if not require_authorized(self, config):
                return
            length = int(self.headers.get("Content-Length", "0"))
            try:
                message = json.loads(
                    self.rfile.read(length).decode("utf-8") or "{}")
            except json.JSONDecodeError:
                self.send_error(400, "Invalid JSON")
                return
            response = server.handle(message)
            if response is None:
                self.send_response(204)
                self.end_headers()
                return
            payload = json.dumps(response, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type",
                             "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, fmt: str, *args):
            log(fmt % args)

    httpd = ThreadingHTTPServer((config.host, config.port), Handler)
    log(f"HTTP 传输已启动: http://{config.host}:{config.port}")
    if config.token:
        log("HTTP bearer token 已启用")
    httpd.serve_forever()


def _load_runtime_config(path: str | None) -> dict:
    """读 sky_config.json（mcp 段的端口/token 是默认值来源）。

    配置文件坏了不阻塞启动：打警告后按内置默认值继续，CLI 还能覆盖。
    """
    try:
        return load_sky_config(path)
    except ConfigError as exc:
        log(f"运行配置读取失败，改用默认值: {exc}")
        return default_config()


def parse_args() -> argparse.Namespace:
    # 先解析 --config，才能用 sky_config.json 里的 host/port/token 当默认值
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--config", default=os.environ.get("SKY_CONFIG"))
    known, _ = pre.parse_known_args()
    sky_cfg = _load_runtime_config(known.config)
    mcp_cfg = sky_cfg.get("mcp") or {}

    parser = argparse.ArgumentParser(
        description="MCP tool server for controlling Sky running in an "
                    "Android emulator (adb execution layer).")
    parser.add_argument(
        "--config", default=known.config,
        help="运行配置文件（默认 sky_config.json；内含 mcp 监听端口/token）。")
    parser.add_argument(
        "--http", nargs="?", const="true", default=None,
        help="Run HTTP JSON-RPC transport. Optional legacy form: --http 9800.")
    parser.add_argument(
        "--host", default=str(mcp_cfg.get("host") or "127.0.0.1"),
        help="HTTP host（默认取 sky_config.json 的 mcp.host）。"
             "Use 0.0.0.0 for LAN clients such as Polaris.")
    parser.add_argument(
        "--port", type=int, default=int(mcp_cfg.get("port") or 9900),
        help="HTTP port（默认取 sky_config.json 的 mcp.port）。")
    parser.add_argument(
        "--token", default=mcp_cfg.get("token") or None,
        help="Bearer token for HTTP POST requests（默认取 sky_config.json 的 mcp.token）。")
    parser.add_argument(
        "--print-token", action="store_true",
        help="Generate and print a random token, then exit.")
    # ---- adb 设备参数（默认全自动探测，这三个只用于多设备/自定义安装）----
    parser.add_argument(
        "--adb-path", default=None,
        help="adb 可执行文件路径。默认自动探测 PATH / ANDROID_HOME / MuMu 自带 adb。")
    parser.add_argument(
        "--adb-serial", default=None,
        help="adb 设备序列号，如 127.0.0.1:16384。默认自动探测（多设备时必须指定）。")
    parser.add_argument(
        "--adb-config", default=None,
        help="adb 配置文件路径（默认 adb_config.json；也可用环境变量 SKY_ADB_CONFIG）。")
    parser.add_argument(
        "--input-backend", choices=["auto", "adb"],
        default=os.environ.get("SKY_INPUT_BACKEND", "auto"),
        help="输入后端。现在只有 adb/auto 两种（旧 PC 后端已移除）。")
    parser.add_argument(
        "--screenshot-scale", type=float,
        default=float(os.environ.get("SKY_SCREENSHOT_SCALE", "1.0")),
        help="截图缩放系数（1.0=原始尺寸）。")
    parser.add_argument(
        "--screenshot-max-width", type=int,
        default=int(os.environ.get("SKY_SCREENSHOT_MAX_WIDTH", "1920")),
        help="截图最大宽度（只缩小不放大）。")
    parser.add_argument(
        "--screenshot-max-height", type=int,
        default=int(os.environ.get("SKY_SCREENSHOT_MAX_HEIGHT", "1080")),
        help="截图最大高度（只缩小不放大）。")
    args = parser.parse_args()
    if args.http and args.http != "true":
        args.port = int(args.http)
    args.http = bool(args.http)
    args.sky_config = sky_cfg
    return args


def _build_adb_config(args: argparse.Namespace) -> AdbConfig:
    """读 adb_config.json（或 --adb-config 指定的文件），再用命令行值覆盖。"""
    try:
        adb_config = (AdbConfig.from_file(args.adb_config)
                      if args.adb_config else AdbConfig.from_file())
    except AdbError as exc:
        raise SkyError(str(exc)) from exc
    if args.adb_path:
        adb_config.adb_path = args.adb_path
    if args.adb_serial:
        adb_config.serial = args.adb_serial
    return adb_config


def main() -> None:
    args = parse_args()
    if args.print_token:
        print(secrets.token_urlsafe(24))
        return
    log(f"运行配置: {args.sky_config.get('_source')}")
    try:
        adb_config = _build_adb_config(args)
        config = ServerConfig(
            host=args.host,
            port=args.port,
            token=args.token,
            input_backend=args.input_backend,
            screenshot_scale=args.screenshot_scale,
            screenshot_max_width=args.screenshot_max_width,
            screenshot_max_height=args.screenshot_max_height,
            adb_config=adb_config,
        )
        controller = AdbSkyController(config)
        # 启动时就校验后端，坏参数（旧 PC 后端名等）立刻给中文报错，
        # 而不是等第一个工具调用才在 stdio 里默默报错。
        controller.resolve_backend(config.input_backend)
        server = McpServer(controller)
    except (SkyError, AdbError) as exc:
        log(f"启动失败: {exc}")
        raise SystemExit(2)
    try:
        if args.http:
            run_http(server, config)
        else:
            run_stdio(server)
    except KeyboardInterrupt:
        log("收到 Ctrl+C，退出中…")
    finally:
        # 退出清理：松开所有悬键 + 还原用户原来的输入法
        controller.close()


if __name__ == "__main__":
    main()
