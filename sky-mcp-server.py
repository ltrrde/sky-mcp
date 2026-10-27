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
import difflib
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
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from sky_adb import (
    MAX_PRESS_MS,
    AdbConfig,
    AdbDevice,
    AdbError,
    normalize_key as _normalize_adb_key,
)
from sky_chat import (bubble_region, chat_region, panel_looks_open,
                      parse_chat_items, parse_floating_bubbles)
from sky_config import ConfigError, default_config, load_config as load_sky_config
from sky_ocr import OcrEngine, OcrError

SERVER_INFO = {"name": "sky-mcp-server", "version": "0.5.0-ocr-chat"}
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

#: read_screen 全屏 OCR 的「文字过少」回退阈值（需求 R3-1）：
#: 角色移动/转场帧上 default 预处理可能只读出 1~2 个碎片，此时用
#: preprocess=none 再试一次；回退生效时结果里 fallback_used=true。
OCR_FALLBACK_MIN_TEXTS = 2


class SkyError(RuntimeError):
    """执行层错误。消息直接进 MCP tools/call 的 isError 文本，必须人能看懂。

    ``code`` 是给 AI 客户端做错误分类用的（R5）：
      foreground / ime / device / ocr / io / invalid / unknown
    """

    def __init__(self, message: str, code: str = "unknown"):
        super().__init__(message)
        self.code = code

    def as_payload(self) -> dict[str, Any]:
        return {"ok": False, "error_type": self.code, "error": str(self)}


def now_iso() -> str:
    """带本地时区的 ISO8601 时间（截帧/发送时间戳用）。"""
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def new_frame_id() -> str:
    """一次截屏的唯一 id（同一次 read_screen 里文本与图片共用）。"""
    return f"{int(time.time() * 1000):x}-{secrets.token_hex(4)}"


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
    #: OCR 引擎：auto / paddleocr / rapidocr / tesseract / none（默认读 SKY_OCR_ENGINE）
    ocr_engine: str | None = None
    #: HTTP 启动时后台预热 OCR（首次加载模型很慢，别让第一个工具调用卡住）
    ocr_prewarm: bool = True


@dataclass(frozen=True)
class CapturedFrame:
    """一次截屏的结果 + 元数据（R4：image_size/captured_at/frame_id）。"""

    png: bytes
    width: int
    height: int
    frame_id: str
    captured_at: str


class AdbSkyController:
    """把所有 MCP 工具动作落到 AdbDevice 上。

    生产默认自己创建真实 AdbDevice；测试/特殊部署可以注入自定义实例：

        controller = AdbSkyController(ServerConfig(), device=my_adb_device)
        controller = AdbSkyController(ServerConfig(), ocr_engine=my_ocr_engine)

    本仓库的真机测试（tests/test_*_real.py）都是直接注入真实设备 + 真实 rapidocr。
    """

    def __init__(self, config: ServerConfig | None = None,
                 device: AdbDevice | None = None,
                 ocr_engine: Any = None):
        self.config = config or ServerConfig()
        self._device = device
        # 复合动作（open_chat/send_chat 的"开输入框→打字→发送"）串行执行；
        # 单按键的并发安全由 AdbDevice 自己的 _input_lock 保证。
        self._action_lock = threading.RLock()
        # OCR 引擎（sky_ocr.OcrEngine；测试可注入假引擎 duck-type）
        self._ocr_impl: Any = ocr_engine
        self._ocr_init_lock = threading.Lock()
        # 最近发出的聊天文本（read_chat 判断 is_self 用；10 分钟窗口）
        self._sent_history: list[tuple[str, float]] = []
        self._send_lock = threading.Lock()
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
            raise SkyError(str(exc), code="device") from exc
        return dev

    def _require_foreground(self, dev: AdbDevice | None = None) -> dict[str, Any]:
        """前台守卫：游戏不在前台就拒绝（错误消息里带「前台」字样，
        方便 AI 客户端识别"用户切走了，等会儿再试"；错误码 foreground）。"""
        dev = dev or self._ready_device()
        try:
            return dev.require_game_foreground()
        except AdbError as exc:
            text = str(exc)
            # ADBKeyboard 崩溃时前台会变成 "Application Error: com.android.adbkeyboard"；
            # 这时不是"用户切走了"，按 ime 报错让客户端知道该重启输入法。
            code = "ime" if "adbkeyboard" in text.lower() else "foreground"
            raise SkyError(text, code=code) from exc

    def _remember_sent(self, message: str) -> None:
        """记录最近发出的聊天（read_chat 用它把"无名字裸行"认成自己发的）。"""
        with self._send_lock:
            self._sent_history.append((message, time.time()))
            self._sent_history = self._sent_history[-50:]

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
            # 按键类错误也做分类（前台守卫 / IME / 设备），R5 客户端据此重试
            text = str(exc)
            code = ("foreground" if "前台" in text
                    else ("ime" if "adbkeyboard" in text.lower() else "device"))
            raise SkyError(text, code=code) from exc

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
            raise SkyError("message must be a non-empty string", code="invalid")
        if len(message) > MAX_CHAT_CHARS:
            raise SkyError(
                f"message is too long; keep it under {MAX_CHAT_CHARS} characters",
                code="invalid")
        return message

    @staticmethod
    def _reply_receipt(message: str, *, send: bool, backend: str,
                       action: str) -> dict[str, Any]:
        """发送/输入回执（R5）：AI 客户端据此判断是否发出、是否需要重试。"""
        return {
            "ok": True,
            "action": action,               # sent | typed
            "sent": bool(send),
            "sent_at": now_iso(),
            "chars": len(message),
            "truncated": len(message) >= MAX_CHAT_CHARS,
            "message": message,
            "backend": backend,
        }

    @staticmethod
    def _wrap_ime_error(exc: AdbError) -> "SkyError":
        """ADBKeyboard/输入法类失败单独给 error_type=ime，方便客户端提示装输入法。"""
        text = str(exc)
        if "ADBKeyboard" in text or "输入法" in text:
            return SkyError(text, code="ime")
        if "前台" in text:
            return SkyError(text, code="foreground")
        return SkyError(text, code="device")

    def type_text(
        self,
        message: str,
        send: bool = True,
        enter_tap_ms: int = 35,
        backend: str | None = None,
        require_foreground: bool = True,
    ) -> dict[str, Any]:
        """往「已经打开的」聊天输入框打字（中文走 ADBKeyboard），可选回车发送。

        返回回执 JSON（R5）：ok/action/sent/sent_at/chars/truncated/message/backend。
        """
        message = self._validate_message(message)
        resolved = self.resolve_backend(backend)
        tap_ms = self._clamp_ms(enter_tap_ms)
        with self._action_lock:
            # 打字 = 往当前焦点输入框塞文本；游戏不在前台时必须拒绝，
            # 否则文本会进别的 App（和按键的前台守卫同一个理由）。
            # 先自愈 IME 再查前台：ADBKeyboard 崩溃对话框会挡住前台判定。
            dev = self._ready_device()
            try:
                dev.ensure_ime_healthy()
            except AdbError as exc:
                raise self._wrap_ime_error(exc) from exc
            if require_foreground:
                self._require_foreground(dev)
            try:
                if not dev.wait_input_connection(2.0):
                    raise SkyError(
                        "聊天输入框没有拿到输入焦点（ADBKeyboard 无法提交文字）："
                        "请先打开聊天输入框再打字。", code="ime")
                dev.type_text(message)
                if send:
                    # IME 广播提交文字和游戏输入框刷新有延迟：立刻回车会把
                    # "还没上屏"的消息当成空消息发出去（真机实测）。
                    time.sleep(0.12)
                    dev.press_key("enter", tap_ms)
            except AdbError as exc:
                raise self._wrap_ime_error(exc) from exc
        if send:
            self._remember_sent(message)
        action = "sent" if send else "typed"
        return self._reply_receipt(message, send=send, backend=resolved,
                                   action=action)

    def send_chat(
        self,
        message: str,
        open_key: str = "enter",
        open_delay_ms: int = 500,
        assume_open: bool = False,
        send: bool = True,
        enter_tap_ms: int = 35,
        backend: str | None = None,
        verify: bool = False,
    ) -> dict[str, Any]:
        """开聊天输入框 → 输入一条消息（中文可用）→ 可选回车发送。

        和旧版语义一致：``assume_open=True`` 表示输入框已经由人手动打开，
        这时不再按 open_key（避免短按 enter 又把输入框关掉）。
        返回回执 JSON（R5），字段同 type_text。

        ``verify=True`` 时发完会再做一次 read_chat 回读校验（额外约 4~5s）：
        回执里带 ``verified``；为 False 时带 ``verify_error="send_key"``，
        用于区分「发送键没生效/消息没上屏」和「其实发出去了但 OCR 认不出」。
        """
        message = self._validate_message(message)
        resolved = self.resolve_backend(backend)
        open_keys = self._parse_keys(open_key)
        tap_ms = self._clamp_ms(enter_tap_ms)
        try:
            delay = max(0.0, float(open_delay_ms)) / 1000.0
        except (TypeError, ValueError):
            raise SkyError(
                f"open_delay_ms 必须是数字: {open_delay_ms!r}",
                code="invalid") from None
        with self._action_lock:
            # 先自愈 IME（ADBKeyboard 崩溃对话框会挡住前台判定），再查前台。
            dev = self._ready_device()
            try:
                dev.ensure_ime_healthy()
            except AdbError as exc:
                raise self._wrap_ime_error(exc) from exc
            # 再查前台，别在别的 App 上按 enter 开输入框；后面的每次
            # 按键 / 打字也各自带守卫（用户中途切走会立刻报错）。
            self._require_foreground(dev)
            try:
                if not assume_open:
                    if len(open_keys) == 1:
                        dev.press_key(open_keys[0], tap_ms)
                    else:
                        dev.combo(open_keys, tap_ms)
                    time.sleep(delay)
                # 等输入框真正拿到 InputConnection：没有连接时广播会让
                # ADBKeyboard 崩溃且消息被静默丢弃（真机踩坑）。
                if not dev.wait_input_connection(3.0):
                    if not assume_open:
                        # 再补按一次 open key（可能上一按被动画/卡顿吃掉）
                        if len(open_keys) == 1:
                            dev.press_key(open_keys[0], tap_ms)
                        else:
                            dev.combo(open_keys, tap_ms)
                        time.sleep(delay)
                    if not dev.wait_input_connection(2.0):
                        raise SkyError(
                            "聊天输入框没有拿到输入焦点（ADBKeyboard 无法提交"
                            "文字，可能游戏卡顿/输入框被遮挡）。重试或先手动"
                            "打开聊天输入框。", code="ime")
                dev.type_text(message)
                if send:
                    # 同上：等 IME 把文字提交进游戏输入框再按回车发送
                    time.sleep(0.12)
                    dev.press_key("enter", tap_ms)
            except AdbError as exc:
                raise self._wrap_ime_error(exc) from exc
        receipt = self._reply_receipt(
            message, send=send, backend=resolved,
            action="sent" if send else "typed")
        if send and verify:
            verified = self._verify_sent(message)
            receipt["verified"] = verified
            if verified is False:
                # 消息没在面板/气泡里出现：发送键没生效或界面没刷新
                receipt["verify_error"] = "send_key"
        if send:
            self._remember_sent(message)
        return receipt

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

    def capture_frame(self) -> CapturedFrame:
        """截一帧并附带元数据（R4：image_size / captured_at / frame_id）。

        同一次 read_screen / read_chat 里 OCR 与返回的图片必须共用同一帧，
        所以截屏只做一次，把 png 和元数据一起往后传。
        """
        data = self.capture_png()
        width, height = self._png_size(data)
        if width <= 0 or height <= 0:
            raise SkyError("截屏 PNG 尺寸解析失败", code="device")
        return CapturedFrame(png=data, width=width, height=height,
                             frame_id=new_frame_id(), captured_at=now_iso())

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
                    f"截屏数据不完整（{frame.size}/{expected} 字节）",
                    code="device")
            frame = frame[:expected].reshape((height, width, 4))
            code = (cv2.COLOR_RGBA2BGR if pixel_format == "rgba"
                    else cv2.COLOR_BGRA2BGR)
            image = cv2.cvtColor(frame, code)
            image = self._resize_capture(image)
            ok, buffer = cv2.imencode(
                ".png", image, [cv2.IMWRITE_PNG_COMPRESSION, 1])
            if not ok:
                raise SkyError("PNG 编码失败", code="device")
            return buffer.tobytes()
        except SkyError:
            raise
        except Exception as exc:
            return self._screencap_png_fallback(f"raw 帧处理失败: {exc}")

    def _as_frame(self, png: bytes | None = None,
                  frame: CapturedFrame | None = None) -> CapturedFrame:
        """统一取帧：优先外部传入（保证同帧复用），否则现场截一张。"""
        if frame is not None:
            return frame
        if png is None:
            return self.capture_frame()
        width, height = self._png_size(png)
        if width <= 0 or height <= 0:
            raise SkyError("PNG 尺寸解析失败", code="invalid")
        return CapturedFrame(png=png, width=width, height=height,
                             frame_id=new_frame_id(), captured_at=now_iso())

    @staticmethod
    def _png_size(data: bytes) -> tuple[int, int]:
        """从 PNG 头解析尺寸（不依赖任何图像库）。"""
        if (len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n"
                and data[12:16] == b"IHDR"):
            width, height = struct.unpack(">II", data[16:24])
            if 0 < width <= 20000 and 0 < height <= 20000:
                return width, height
        return 0, 0

    def screenshot_base64(self, png: bytes | None = None) -> str:
        """截屏 → PNG base64（MCP image content 用）。"""
        data = png if png is not None else self.capture_png()
        return base64.b64encode(data).decode("ascii")

    def save_png(self, frame: CapturedFrame,
                 save_path: str | None) -> str | None:
        """可选：把当前帧另存为 PNG（R4 save_path）。返回绝对路径或 None。"""
        if not save_path:
            return None
        try:
            path = os.path.abspath(os.path.expanduser(str(save_path)))
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(path, "wb") as handle:
                handle.write(frame.png)
            return path
        except OSError as exc:
            raise SkyError(f"保存截图失败: {exc}", code="io") from exc

    # ===================== OCR（sky_ocr）/ 聊天（sky_chat） =====================

    @property
    def ocr_engine(self):
        """惰性创建 OCR 引擎；测试可注入任意 duck-type（status/run/prewarm）。"""
        if self._ocr_impl is None:
            with self._ocr_init_lock:
                if self._ocr_impl is None:
                    try:
                        self._ocr_impl = OcrEngine(self.config.ocr_engine)
                    except Exception as exc:  # 构造失败也要让 tools 能报诊断
                        raise SkyError(
                            f"OCR 引擎初始化失败: {exc}", code="ocr") from exc
        return self._ocr_impl

    def ocr_status(self) -> dict[str, Any]:
        """OCR 状态（绝不抛异常：status / health / 工具降级都要能用）。"""
        fallback: dict[str, Any] = {
            "ocr": "none", "ocr_engine": "none", "ocr_device": "unknown",
            "ocr_error": "", "ocr_ready": False, "ocr_load_ms": 0.0,
            "ocr_available": [], "ocr_chain": ["paddleocr", "rapidocr",
                                              "tesseract"],
        }
        try:
            status = self.ocr_engine.status()
        except Exception as exc:
            fallback["ocr_error"] = f"OCR 引擎初始化失败: {exc}"
            return fallback
        if not isinstance(status, dict):
            return fallback
        merged = dict(fallback)
        merged.update(status)
        return merged

    def prewarm_ocr(self) -> dict[str, Any]:
        """加载 OCR 模型（HTTP 启动后由后台线程调用，避免首调阻塞）。

        加载后再用「真实尺寸的空白图」各跑一遍 full 与 chat 区域：onnxruntime
        首帧按输入形状做图优化/内存分配，不预热的话第一次 read_screen 会额外
        多 0.5~1.5s（R3 的 ≤3s 就被这次抖动顶破）。预热图是纯色 PNG，很快。
        """
        try:
            status = self.ocr_engine.prewarm()
            try:
                width = max(320, int(self.config.screenshot_max_width))
                height = max(180, int(self.config.screenshot_max_height))
                warm_full = self._warmup_png(width, height)
                self.ocr_engine.run(warm_full, preprocess="default")
                region = chat_region((width, height))
                self.ocr_engine.run(warm_full, region=region, preprocess="chat")
            except Exception as exc:  # 预热推理失败不影响正事，下次真调用再报
                log(f"OCR 预热推理失败（忽略）: {exc}")
            log(f"OCR 预热完成: engine={status.get('ocr')} "
                f"ready={status.get('ocr_ready')} "
                f"load_ms={status.get('ocr_load_ms')}")
            return status
        except Exception as exc:
            log(f"OCR 预热失败（read_screen/read_chat 仍返回诊断）: {exc}")
            return self.ocr_status()

    @staticmethod
    def _warmup_png(width: int = 240, height: int = 80) -> bytes:
        """生成一张纯白 PNG（纯标准库），用于按真实尺寸预热推理。"""
        import zlib

        width, height = max(8, int(width)), max(8, int(height))
        row = b"\x00" + b"\xff\xff\xff" * width
        raw = row * height

        def chunk(tag: bytes, data: bytes) -> bytes:
            return (struct.pack(">I", len(data)) + tag + data
                    + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

        header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
        return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
                + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))

    def _region_for(self, region: Any,
                    size: tuple[int, int]) -> tuple[Any, str]:
        """工具参数 region → (sky_ocr 绝对裁剪区, 规范名)。"""
        name = str(region or "full").strip().lower()
        if name in ("full", "all", ""):
            return None, "full"
        if name == "chat":
            return chat_region(size), "chat"
        if name == "bubble":
            return bubble_region(size), "bubble"
        raise SkyError(
            f"未知 region={region!r}（可用: full / chat / bubble）",
            code="invalid")

    def _ocr_frame(self, frame: CapturedFrame, region: Any,
                   preprocess: str | None = None):
        """对一帧做 OCR。返回 (OcrResult|None, region名, preprocess, error)。

        ``preprocess`` 显式传入时覆盖默认值（R3-1 的 none 回退用）。
        """
        region_px, region_name = self._region_for(
            region, (frame.width, frame.height))
        if preprocess is None:
            preprocess = ("chat" if region_name in ("chat", "bubble")
                          else "default")
        try:
            result = self.ocr_engine.run(
                frame.png, region=region_px, preprocess=preprocess)
            return result, region_name, preprocess, ""
        except OcrError as exc:
            return None, region_name, preprocess, str(exc)
        except Exception as exc:  # 兜住引擎内部意外，工具不 500
            return None, region_name, preprocess, f"OCR 失败: {exc}"

    #: 聊天输入框条带（相对坐标）：面板/输入 UI 打开时，底部左下会出现
    #: 输入框 + 占位符「聊天......」。聊天列表为空（附近没人说话）时，
    #: 仅靠聊天 ROI 的文字启发式判不出面板状态，用这条带兜底。
    _CHAT_INPUT_STRIP_REL = (0.0, 0.88, 0.42, 0.12)

    def _chat_input_visible(self, frame: CapturedFrame) -> tuple[bool | None, float]:
        """检测底部聊天输入框条带是否可见。返回 (可见?, 这次 OCR 的 ocr_ms)。"""
        top = int(frame.height * self._CHAT_INPUT_STRIP_REL[1])
        region = (
            0,
            top,
            max(1, int(frame.width * self._CHAT_INPUT_STRIP_REL[2])),
            max(1, frame.height - top),
        )
        try:
            result = self.ocr_engine.run(
                frame.png, region=region, preprocess="chat")
        except Exception:
            return None, 0.0
        for item in result.items:
            normalized = re.sub(r"[\s\W_]+", "", item.text or "")
            if (normalized.startswith("聊天")
                    or normalized in ("发送", "语音输入", "说点什么")):
                return True, float(result.ocr_ms)
        return False, float(result.ocr_ms)

    def read_screen(self, png: bytes | None = None, *,
                    region: str = "full",
                    save_path: str | None = None,
                    frame: CapturedFrame | None = None) -> dict[str, Any]:
        """截屏 + OCR（R1/R3/R4）。

        引擎不可用时不抛异常：``texts=[]`` 且 ``ocr_error`` 给出可操作原因，
        AI 客户端可回退本地 OCR 或改用视觉模型。
        """
        frame = self._as_frame(png, frame)
        status = self.ocr_status()
        result, region_name, preprocess, ocr_error = self._ocr_frame(
            frame, region)
        ocr_ms = float(result.ocr_ms) if result else 0.0
        fallback_attempted = False
        fallback_used = False
        # R3-1：全屏 default 只读出 1~2 个碎片（角色移动/转场帧）时，用
        # preprocess=none 再试一次；0 文字（纯图形场景）不触发，避免白白多跑一趟。
        # 只有「回退后文字更多」才采用回退结果（fallback_used=true）；
        # 尝试过但没变好也会记 fallback_attempted（ocr_ms 已含两遍耗时）。
        if (result is not None and region_name == "full"
                and preprocess == "default"
                and 0 < len(result.items) <= OCR_FALLBACK_MIN_TEXTS):
            fallback_attempted = True
            alt, _alt_name, alt_pre, _alt_err = self._ocr_frame(
                frame, region, preprocess="none")
            if alt is not None:
                ocr_ms += float(alt.ocr_ms)
                if len(alt.items) > len(result.items):
                    result, preprocess, fallback_used = alt, alt_pre, True
                    ocr_error = ""
        payload: dict[str, Any] = {
            "ocr": status.get("ocr", "none"),
            "ocr_engine": status.get("ocr_engine", status.get("ocr", "none")),
            "ocr_device": status.get("ocr_device", "unknown"),
            "ocr_error": ocr_error or str(status.get("ocr_error") or ""),
            "ocr_ready": bool(status.get("ocr_ready", False)),
            "image_size": {"width": frame.width, "height": frame.height},
            "captured_at": frame.captured_at,
            "frame_id": frame.frame_id,
            "region": region_name,
            "preprocess": preprocess,
            "fallback_used": fallback_used,
            "fallback_attempted": fallback_attempted,
            "ocr_ms": round(ocr_ms, 1),
            "texts": result.texts() if result else [],
        }
        saved = self.save_png(frame, save_path)
        if saved:
            payload["saved_path"] = saved
        return payload

    #: 聊天界面里的通用称呼/菜单词：OCR 只剩这些碎片时不是一条真消息
    _CHAT_NOISE_TEXTS = frozenset({
        "陌生人", "光之子", "玩家", "旅人",
        "图像", "音频", "控制", "账号", "好友", "商店", "遇境", "精灵",
        "活动", "邮箱", "好友码", "王国客服", "更多", "聊天", "发送",
        "语音输入", "图像设置", "聊天记录",
    })

    @staticmethod
    def _norm_chat_text(text: Any) -> str:
        """聊天文本归一化：去空白/标点/符号，保留中英文数字（用于去重与相似）。"""
        return re.sub(r"[\s\W_]+", "", str(text or "")).lower()

    @classmethod
    def _looks_like_own(cls, text: str, own_texts: list[str]) -> bool:
        """判断一条消息是不是最近自己发出的（光遇自己消息不显示名字）。"""
        norm = cls._norm_chat_text(text)
        if not norm:
            return False
        for sent in own_texts:
            other = cls._norm_chat_text(sent)
            if not other:
                continue
            if norm == other:
                return True
            if len(norm) >= 2 and len(other) >= 2:
                if difflib.SequenceMatcher(None, norm, other).ratio() >= 0.72:
                    return True
                common = sum(1 for char in set(norm) if char in other)
                if common / max(1, len(set(norm))) >= 0.8:
                    return True
        return False

    def _verify_sent(self, message: str) -> bool | None:
        """发后回读校验：自己的消息是否出现在面板/气泡里（R5 发送键失效检测）。

        返回 True=找到、False=没找到、None=无法判断（OCR 不可用/读屏失败）。
        只做候选匹配（字符命中 ≥0.6 或序列相似 ≥0.8），不要求逐字一致。
        """
        # 发送键按下后气泡/面板渲染有延迟，先等 UI 上屏再截帧（真机实测 0.8s 足够）
        time.sleep(0.8)
        try:
            chat = self.read_chat(limit=10)
        except Exception:
            return None
        target = self._norm_chat_text(message)
        if not target:
            return None
        best = 0.0
        for item in chat.get("messages") or []:
            text = self._norm_chat_text(item.get("text") or "")
            if not text:
                continue
            if target in text or text in target:
                return True
            if difflib.SequenceMatcher(None, target, text).ratio() >= 0.8:
                return True
            common = sum(1 for char in set(target) if char in text)
            best = max(best, common / max(1, len(set(target))))
        return best >= 0.6

    def _clean_chat_message(self, message: dict[str, Any],
                            own_texts: list[str]) -> dict[str, Any] | None:
        """过滤名字碎片/UI 噪音，并补齐 is_self（bubble 模式原本不标）。"""
        text = str(message.get("text") or "").strip()
        if len(text) < 2:
            return None
        if text == str(message.get("speaker") or ""):
            return None
        if text in self._CHAT_NOISE_TEXTS:
            return None
        if not self._norm_chat_text(text):
            return None
        if not message.get("is_self") and self._looks_like_own(text, own_texts):
            message["is_self"] = True
        return message

    def _merge_chat_messages(self, groups: list[list[dict[str, Any]]],
                             own_texts: list[str],
                             limit: int) -> list[dict[str, Any]]:
        """合并 panel/bubble 两路结果：同文本去重（panel 的 speaker 优先），按 y 排序。"""
        merged: list[dict[str, Any]] = []
        index: dict[str, int] = {}
        for messages in groups:
            for raw in messages:
                message = self._clean_chat_message(dict(raw), own_texts)
                if message is None:
                    continue
                key = self._norm_chat_text(message["text"])
                if key in index:
                    existing = merged[index[key]]
                    if message.get("speaker") and not existing.get("speaker"):
                        existing["speaker"] = message["speaker"]
                        existing["kind"] = "panel"
                    existing["is_self"] = bool(
                        existing.get("is_self") or message.get("is_self"))
                    existing["confidence"] = max(
                        float(existing.get("confidence") or 0.0),
                        float(message.get("confidence") or 0.0))
                    continue
                index[key] = len(merged)
                merged.append(message)
        merged.sort(key=lambda item: (int(item.get("y") or 0),
                                      int(item.get("x") or 0)))
        if limit and len(merged) > limit:
            merged = merged[-limit:]
        return merged

    def _recent_sent_texts(self) -> list[str]:
        now = time.time()
        with self._send_lock:
            self._sent_history = [(text, ts) for text, ts in self._sent_history
                                  if now - ts < 600.0]
            return [text for text, _ in self._sent_history]

    def read_chat(self, png: bytes | None = None, *,
                  limit: Any = 20,
                  mode: str = "auto",
                  save_path: str | None = None,
                  frame: CapturedFrame | None = None) -> dict[str, Any]:
        """读取游戏内聊天（R2）：聊天面板 + 世界气泡两路 OCR，合并成结构化消息。

        - mode=auto：同时读聊天面板（带名字的日志行）和世界气泡（发送后的消息
          会以气泡叠在输入框上方），两路合并、按文本去重、按 y 排序；
        - mode=panel / bubble：只读对应区域；
        - 返回字段：messages / ocr / ocr_device / ocr_error / ocr_ready /
          ocr_ms / image_size / captured_at / frame_id / region / mode / panel_open。
        """
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            raise SkyError(f"limit 必须是整数: {limit!r}", code="invalid") from None
        limit = min(max(1, limit), 100)
        mode = str(mode or "auto").strip().lower()
        if mode not in ("auto", "panel", "bubble"):
            raise SkyError(
                f"未知 mode={mode!r}（可用: auto / panel / bubble）",
                code="invalid")
        frame = self._as_frame(png, frame)
        status = self.ocr_status()
        own_texts = self._recent_sent_texts()
        screen_size = (frame.width, frame.height)

        panel_messages: list[dict[str, Any]] = []
        bubble_messages: list[dict[str, Any]] = []
        groups: list[list[dict[str, Any]]] = []
        used: list[str] = []
        errors: list[str] = []
        ocr_ms = 0.0
        panel_open: bool | None = None
        fullscreen_scan = False

        if mode in ("auto", "panel"):
            result, _name, _pre, err = self._ocr_frame(frame, "chat")
            if err:
                errors.append(err)
            if result is not None:
                texts = result.texts()
                ocr_ms += float(result.ocr_ms)
                used.append("chat")
                panel_open = panel_looks_open(texts, screen_size)
                panel_messages = parse_chat_items(
                    texts, own_texts=own_texts, mode="panel", limit=limit,
                    image_size=screen_size)
        # 聊天列表为空时（附近没人说话），文字启发式返回 None/False；
        # 用底部输入框占位「聊天......」再确认一次，避免误报"面板没开"。
        if panel_open is not True and mode in ("auto", "panel"):
            visible, extra_ms = self._chat_input_visible(frame)
            if extra_ms:
                ocr_ms += extra_ms
                used.append("inputbar")
            if visible is True:
                panel_open = True
        if mode in ("auto", "bubble"):
            result, _name, _pre, err = self._ocr_frame(frame, "bubble")
            if err:
                errors.append(err)
            if result is not None:
                ocr_ms += float(result.ocr_ms)
                used.append("bubble")
                bubble_messages = parse_chat_items(
                    result.texts(), own_texts=own_texts,
                    mode="bubble", limit=limit, image_size=screen_size)
        groups = [panel_messages, bubble_messages]

        # R2-1：气泡不只在左下 ROI，也可能飘在角色头顶（全屏任意位置）。
        # - mode=bubble：总是补一次全屏扫描（固定 ROI 保证不了头顶气泡）；
        # - mode=auto：只有「左下气泡一路没读到」或「最近 120s 发送的文本还没被
        #   匹配到」时才补扫描，避免每次 read_chat 都多花一次全屏 OCR。
        partial = self._merge_chat_messages(groups, own_texts, limit)
        now = time.time()
        with self._send_lock:
            recent_own = [text for text, ts in self._sent_history
                          if now - ts < 120.0]
        unmatched_recent = [
            text for text in recent_own
            if not any(self._looks_like_own(message.get("text", ""), [text])
                       for message in partial)
        ]
        bubble_found = any(message.get("kind") == "bubble"
                           for message in partial)
        need_fullscreen = (mode == "bubble"
                           or (mode == "auto"
                               and (not bubble_found or bool(unmatched_recent))))
        floating_messages: list[dict[str, Any]] = []
        if need_fullscreen:
            result, _name, _pre, err = self._ocr_frame(frame, "full")
            if err:
                errors.append(err)
            if result is not None:
                ocr_ms += float(result.ocr_ms)
                used.append("fullscreen")
                fullscreen_scan = True
                floating_messages = parse_floating_bubbles(
                    result.texts(), own_texts=own_texts,
                    image_size=screen_size, limit=limit)

        messages = self._merge_chat_messages(
            [panel_messages, bubble_messages, floating_messages],
            own_texts, limit)

        payload: dict[str, Any] = {
            "messages": messages,
            "ocr": status.get("ocr", "none"),
            "ocr_engine": status.get("ocr_engine", status.get("ocr", "none")),
            "ocr_device": status.get("ocr_device", "unknown"),
            "ocr_error": "; ".join(errors) or str(status.get("ocr_error") or ""),
            "ocr_ready": bool(status.get("ocr_ready", False)),
            "ocr_ms": round(ocr_ms, 1),
            "image_size": {"width": frame.width, "height": frame.height},
            "captured_at": frame.captured_at,
            "frame_id": frame.frame_id,
            "region": "+".join(used) if used else "none",
            "mode": mode,
            "panel_open": panel_open,
            # R2-1：给客户端诊断「气泡 ROI 到底扫哪里 / 是否做了全屏兜底扫描」
            "bubble_region_px": list(bubble_region(screen_size)),
            "fullscreen_scan": fullscreen_scan,
        }
        saved = self.save_png(frame, save_path)
        if saved:
            payload["saved_path"] = saved
        return payload

    # ===================== 自检 =====================

    def status(self) -> dict[str, Any]:
        """执行层自检 JSON（AI 客户端启动时先调它看设备是否就绪）。

        字段平铺在顶层：mode / platform / input_backend / adb / serial /
        package / keyboard_device / screen / foreground / ime / device_model /
        android_version / ocr / ocr_engine / ocr_device / ocr_error /
        ocr_ready / ocr_load_ms / ocr_available / ocr_chain /
        screenshot_max / device_ready。

        设备层出错时不抛异常，而是塞 ``error`` 字段——客户端能直接看到
        具体原因（adb 没连上 / 没有设备 / 没装输入法…）。
        前台状态只体现在 foreground.is_game。
        """
        backend = self.resolve_backend()
        ocr = self.ocr_status()
        info: dict[str, Any] = {
            "mode": "android-emulator",
            "platform": sys.platform,
            "input_backend": backend,
            # R1：OCR 引擎名 + 诊断（哪一步失败、装了没、是否已预热）
            "ocr": ocr.get("ocr", "none"),
            "ocr_engine": ocr.get("ocr", "none"),
            "ocr_device": ocr.get("ocr_device", "unknown"),
            "ocr_error": str(ocr.get("ocr_error") or ""),
            "ocr_ready": bool(ocr.get("ocr_ready", False)),
            "ocr_load_ms": float(ocr.get("ocr_load_ms") or 0.0),
            "ocr_available": ocr.get("ocr_available", []),
            "ocr_chain": ocr.get("ocr_chain", []),
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
        "description": "Open Sky chat, type a message through the ADBKeyboard IME (Chinese and emoji supported), and optionally send it with Enter. Returns a JSON receipt: ok/action/sent/sent_at/chars/truncated/message/backend; errors carry error_type (foreground/ime/device).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "Chat message under 240 characters."},
                "open_key": {"type": "string", "description": "Key used to open the chat input.", "default": "enter"},
                "open_delay_ms": {"type": "integer", "description": "Delay after opening chat before typing (IME needs time to focus the input).", "default": 500},
                "assume_open": {"type": "boolean", "description": "Set true when the chat input is already open; no open key is sent.", "default": False},
                "send": {"type": "boolean", "description": "Set false to type without pressing Enter.", "default": True},
                "enter_tap_ms": {"type": "integer", "description": "Short Enter tap duration.", "default": 35},
                "backend": {"type": "string", "description": "Input backend override: adb or auto.", "default": "adb"},
                "verify": {"type": "boolean", "description": "After sending, OCR the chat again to verify the message appeared (adds ~4-5s). Receipt then has verified; verified=false carries verify_error=send_key.", "default": False},
            },
            "required": ["message"],
        },
    },
    {
        "name": "type_text",
        "description": "Type text into an already-open Sky chat input through the ADBKeyboard IME and optionally send. Use this when the user manually opened the input box. Returns the same JSON receipt as send_chat.",
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
        "description": "Capture the emulator screen and OCR visible text. Returns ocr engine info, ocr_error diagnostics, ocr_ms, image_size/captured_at/frame_id, fallback_used (auto retry with preprocess=none when a full-frame default pass finds <=2 fragments), optional saved PNG, and texts with boxes. region=full|chat|bubble selects the OCR area.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "region": {"type": "string", "enum": ["full", "chat", "bubble"], "description": "OCR region: full frame (default), chat panel, or in-world chat bubbles.", "default": "full"},
                "save_path": {"type": "string", "description": "Optional: save the captured PNG to this path (parent dirs created)."},
            },
        },
    },
    {
        "name": "read_chat",
        "description": "Read in-game chat and return structured messages (speaker/text/confidence/y/is_self/kind). Auto mode OCRs the chat panel, the left-bottom bubble ROI, and (when needed) a full-screen scan for floating bubbles above characters; merges/dedupes by text and sorts by y. Returns ocr/ocr_error/ocr_ms/image_size/captured_at/frame_id/panel_open/bubble_region_px/fullscreen_scan as well.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Max recent messages (1-100).", "default": 20},
                "mode": {"type": "string", "enum": ["auto", "panel", "bubble"], "description": "auto (panel then bubble), panel only, or bubble only.", "default": "auto"},
                "save_path": {"type": "string", "description": "Optional: save the captured PNG to this path."},
            },
        },
    },
    {
        "name": "screenshot",
        "description": "Capture the emulator screen and return it as a PNG image. Also returns image metadata (image_size/captured_at/frame_id) and optional saved path.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "save_path": {"type": "string", "description": "Optional: save the captured PNG to this path (parent dirs created)."},
            },
        },
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
                arguments.get("open_delay_ms", 500),
                arguments.get("assume_open", False),
                arguments.get("send", True),
                arguments.get("enter_tap_ms", 35),
                arguments.get("backend"),
                arguments.get("verify", False),
            )
            return {"content": text_content(
                json.dumps(result, ensure_ascii=False))}
        if name == "type_text":
            result = self.controller.type_text(
                arguments["message"],
                arguments.get("send", True),
                arguments.get("enter_tap_ms", 35),
                arguments.get("backend"),
                arguments.get("require_foreground", True),
            )
            return {"content": text_content(
                json.dumps(result, ensure_ascii=False))}
        if name == "read_screen":
            # 只截一次屏：OCR 和返回的图片共用同一帧（同一 frame_id）
            frame = self.controller.capture_frame()
            result = self.controller.read_screen(
                frame=frame,
                region=arguments.get("region", "full"),
                save_path=arguments.get("save_path"),
            )
            text = json.dumps(result, ensure_ascii=False, indent=2)
            return {"content": [
                {"type": "text", "text": text},
                {"type": "image",
                 "data": base64.b64encode(frame.png).decode("ascii"),
                 "mimeType": "image/png"},
            ]}
        if name == "read_chat":
            frame = self.controller.capture_frame()
            result = self.controller.read_chat(
                frame=frame,
                limit=arguments.get("limit", 20),
                mode=arguments.get("mode", "auto"),
                save_path=arguments.get("save_path"),
            )
            return {"content": text_content(
                json.dumps(result, ensure_ascii=False, indent=2))}
        if name == "screenshot":
            frame = self.controller.capture_frame()
            meta = {
                "image_size": {"width": frame.width, "height": frame.height},
                "captured_at": frame.captured_at,
                "frame_id": frame.frame_id,
            }
            saved = self.controller.save_png(frame, arguments.get("save_path"))
            if saved:
                meta["saved_path"] = saved
            return {"content": [
                {"type": "text",
                 "text": json.dumps(meta, ensure_ascii=False)},
                {"type": "image",
                 "data": base64.b64encode(frame.png).decode("ascii"),
                 "mimeType": "image/png"},
            ]}
        raise SkyError(f"Unknown tool: {name}", code="invalid")

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
        except SkyError as exc:
            # 工具级错误走 isError + 结构化 error_type（R5：客户端可分类重试）
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {"content": text_content(json.dumps(
                    exc.as_payload(), ensure_ascii=False)),
                    "isError": True},
            }
        except AdbError as exc:
            payload = {"ok": False, "error_type": "device", "error": str(exc)}
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {"content": text_content(json.dumps(
                    payload, ensure_ascii=False)), "isError": True},
            }
        except Exception as exc:
            # 工具级错误走 isError（客户端应检查该字段，否则会当正常结果）
            payload = {"ok": False, "error_type": "unknown", "error": str(exc)}
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {"content": text_content(json.dumps(
                    payload, ensure_ascii=False)), "isError": True},
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
    # R3：启动时后台预热 OCR（第一次加载模型很慢，别让首个工具调用卡住）。
    # 用后台线程 + status.ocr_ready 暴露进度；--no-ocr-prewarm 可关闭。
    if config.ocr_prewarm:
        def _prewarm():
            try:
                server.controller.prewarm_ocr()
            except Exception as exc:  # 预热失败不影响服务，read_* 会给诊断
                log(f"OCR 预热异常: {exc}")

        threading.Thread(target=_prewarm, name="ocr-prewarm",
                         daemon=True).start()
        log("OCR 预热线程已启动（status.ocr_ready 可查进度）")
    else:
        log("OCR 预热已关闭：首次 read_screen/read_chat 会等待模型加载")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/health":
                self.send_error(404)
                return
            ocr = server.controller.ocr_status()
            body = json.dumps({
                "status": "ok",
                "server": SERVER_INFO,
                "mode": "android-emulator",
                "ocr": ocr.get("ocr", "none"),
                "ocr_device": ocr.get("ocr_device", "unknown"),
                "ocr_ready": bool(ocr.get("ocr_ready", False)),
                "ocr_error": str(ocr.get("ocr_error") or ""),
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
    parser.add_argument(
        "--ocr-engine",
        choices=["auto", "paddleocr", "rapidocr", "tesseract", "none"],
        default=os.environ.get("SKY_OCR_ENGINE", "auto"),
        help="OCR 引擎（默认 auto：paddleocr→rapidocr→tesseract；也可用环境变量 SKY_OCR_ENGINE）。")
    parser.add_argument(
        "--no-ocr-prewarm", action="store_true",
        help="HTTP 启动时不后台预热 OCR（默认预热，避免首个 read_screen 卡住）。")
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
            ocr_engine=args.ocr_engine,
            ocr_prewarm=not args.no_ocr_prewarm,
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
