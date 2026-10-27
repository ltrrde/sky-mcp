#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sky_adb.py — 光遇（安卓模拟器）ADB 按键/截屏驱动。

纯标准库实现，不依赖 numpy / cv2，便于单独测试。
MCP server（sky-mcp-server.py）、调试 CLI 与单元测试共用本模块。

为什么不是 `adb shell input keyevent`？
    实测（MuMu 12 + 光遇国服 com.netease.sky）：`input keyevent` 注入的事件
    游戏完全无视；而把 Linux input 事件直接写进模拟器的键盘设备
    （`sendevent /dev/input/eventN 1 <code> <1|0>`）游戏会当作真实键盘处理，
    且按键语义与 PC 版完全一致（C 聊天面板、Enter 输入框、ESC 返回、
    W/A/S/D 移动、空格跳、F 交互、1/2 动作…）。

能力:
    按键/长按/组合键（sendevent）、中文输入（ADBKeyboard 广播）、
    截屏（原生 RGBA / PNG 兜底）、前台守卫（mCurrentFocus）、
    设备/包名/键盘节点自动探测。

常用工具:
    python sky_adb.py doctor        # 体检：设备/键盘设备/前台/输入法/截屏耗时
    python sky_adb.py shot out.png  # 截一张图
    python sky_adb.py key c 150     # 注入一次按键（光遇语义）
    python sky_adb.py text 你好      # 往当前输入框打一段中文（先手动打开输入框）
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import struct
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Sequence


class AdbError(RuntimeError):
    """ADB / 设备层错误。消息会直接透传到 MCP 工具结果。"""


# ===================== 常量 =====================

ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG_PATH = os.path.join(ROOT, "adb_config.json")

#: 光遇包名候选：国服网易版在前，国际版兜底
DEFAULT_PACKAGES = ("com.netease.sky", "com.tgc.sky.android", "com.tgc.sky")

#: ADBKeyboard 输入法（中文输入的关键组件）
ADB_IME_COMPONENT = "com.android.adbkeyboard/.AdbIME"
ADB_IME_PACKAGE = "com.android.adbkeyboard"

#: Linux input-event-codes（/usr/include/linux/input-event-codes.h）。
#: 注意：这是 Linux 内核键码，不是 Android KeyEvent 常量（两者不同）。
LINUX_KEYCODES: dict[str, int] = {
    # 字母
    "a": 30, "b": 48, "c": 46, "d": 32, "e": 18, "f": 33, "g": 34, "h": 35,
    "i": 23, "j": 36, "k": 37, "l": 38, "m": 50, "n": 49, "o": 24, "p": 25,
    "q": 16, "r": 19, "s": 31, "t": 20, "u": 22, "v": 47, "w": 17, "x": 45,
    "y": 21, "z": 44,
    # 数字（主键盘）
    "1": 2, "2": 3, "3": 4, "4": 5, "5": 6,
    "6": 7, "7": 8, "8": 9, "9": 10, "0": 11,
    # 功能键
    "escape": 1, "tab": 15, "space": 57, "backspace": 14, "delete": 111,
    "insert": 110, "enter": 28, "num_enter": 96,
    # 修饰键
    "leftctrl": 29, "rightctrl": 97, "leftshift": 42, "rightshift": 54,
    "leftalt": 56, "rightalt": 100, "leftmeta": 125, "rightmeta": 126,
    "capslock": 58,
    # 方向键 / 编辑区
    "up": 103, "down": 108, "left": 105, "right": 106,
    "home": 102, "end": 107, "pageup": 104, "pagedown": 109,
    # 符号
    "minus": 12, "equal": 13, "leftbrace": 26, "rightbrace": 27,
    "semicolon": 39, "apostrophe": 40, "grave": 41, "backslash": 43,
    "comma": 51, "dot": 52, "slash": 53,
    # F1-F12
    **{f"f{i}": 58 + i for i in range(1, 13)},
    # 安卓系统键（sendevent 也能用，但一般走 adb shell input keyevent）
    "back": 158, "menu": 139, "volumeup": 115, "volumedown": 114,
    "power": 116, "wakeup": 143, "sleep": 142,
}

#: 名字别名 → 规范键名
KEY_ALIASES = {
    "return": "enter",
    "esc": "escape",
    "spacebar": "space",
    "control": "leftctrl",
    "ctrl": "leftctrl",
    "shift": "leftshift",
    "alt": "leftalt",
    "option": "leftalt",
    "cmd": "leftmeta",
    "command": "leftmeta",
    "win": "leftmeta",
    "windows": "leftmeta",
    "del": "delete",
}

#: 组合键/长按时稳妥的写法：shift-space、ctrl-v 之类
MODIFIER_KEYS = ("leftctrl", "rightctrl", "leftshift", "rightshift",
                 "leftalt", "rightalt", "leftmeta", "rightmeta")

#: panic_release 要兜底松开的键（防止 adb 半路被 kill 把键按住）
PANIC_RELEASE_KEYS = tuple(sorted(set(
    list(MODIFIER_KEYS)
    + ["w", "a", "s", "d", "space", "f", "q", "e", "tab", "c", "enter",
       "escape", "1", "2", "3", "4", "5", "6", "7", "8", "9", "0",
       "up", "down", "left", "right"]
)))

#: 按键时长封顶（毫秒）。旧固件的 2s 上限思路保留：任何调用都不能把键
#: 按住太久；1000ms 足够走完游戏里最长的"长按"
MAX_PRESS_MS = 2000


def normalize_key(key: str) -> str:
    k = str(key).strip().lower()
    k = KEY_ALIASES.get(k, k)
    if k in LINUX_KEYCODES:
        return k
    raise AdbError(
        f"未知按键: {key!r}。可用键: {', '.join(sorted(LINUX_KEYCODES))}")


# ===================== 配置 =====================

@dataclass
class AdbConfig:
    """执行层配置。所有字段都有自动探测兜底，正常情况下不用手写配置。"""

    adb_path: str | None = None
    serial: str | None = None
    package: str | None = None            # 光遇包名；None=自动探测
    keyboard_device: str | None = None    # /dev/input/eventN；None=自动探测
    ime_component: str = ADB_IME_COMPONENT
    ime_restore: bool = True              # 退出时把输入法还给用户原来的
    foreground_ttl: float = 0.6           # 前台查询缓存秒数
    shell_timeout: float = 20.0
    startup_release: bool = True          # 启动时先松开所有可能悬着的键
    verbose: bool = False

    @classmethod
    def from_file(cls, path: str | None = None) -> "AdbConfig":
        """从 adb_config.json 读配置；环境变量优先，文件缺失则全自动。"""
        data: dict[str, Any] = {}
        cfg_path = path or os.environ.get("SKY_ADB_CONFIG") or DEFAULT_CONFIG_PATH
        if cfg_path and os.path.exists(cfg_path):
            try:
                with open(cfg_path, "r", encoding="utf-8") as f:
                    data = json.load(f) or {}
            except (OSError, ValueError) as exc:
                raise AdbError(f"读取配置文件失败 {cfg_path}: {exc}") from exc
        if not isinstance(data, dict):
            raise AdbError(f"配置文件格式错误（应为 JSON 对象）: {cfg_path}")

        def pick(env: str, key: str, default=None):
            if env in os.environ and os.environ[env].strip():
                return os.environ[env].strip()
            return data.get(key, default)

        ime = data.get("ime") or {}
        return cls(
            adb_path=pick("SKY_ADB_PATH", "adb_path"),
            serial=pick("SKY_ADB_SERIAL", "serial")
            or os.environ.get("ANDROID_SERIAL") or None,
            package=pick("SKY_GAME_PACKAGE", "package"),
            keyboard_device=pick("SKY_KEYBOARD_DEVICE", "keyboard_device"),
            ime_component=ime.get("component") or ADB_IME_COMPONENT,
            ime_restore=bool(ime.get("restore", True)),
            foreground_ttl=float(data.get("foreground_ttl", 0.6)),
            shell_timeout=float(pick("SKY_ADB_TIMEOUT", "shell_timeout", 20.0)),
            startup_release=bool(data.get("startup_release", True)),
            verbose=bool(data.get("verbose", False)),
        )


# ===================== ADB 设备 =====================

@dataclass
class ScreenSize:
    width: int
    height: int

    def as_dict(self) -> dict[str, int]:
        return {"width": self.width, "height": self.height}


class AdbDevice:
    """一个安卓模拟器设备的执行层句柄。

    典型用法::

        dev = AdbDevice()            # 自动找 adb / 设备 / 包名 / 键盘设备
        dev.ensure_ready()
        dev.press_key("c", 150)      # 打开聊天面板
        pix, w, h = dev.screencap_raw()

    测试时可以传 ``runner`` 注入假 adb 调用（见 tests/test_sky_adb.py）。
    """

    def __init__(self, config: AdbConfig | None = None, runner=None):
        self.cfg = config or AdbConfig.from_file()
        self._runner = runner or self._default_runner
        self._resolved = False
        self._resolving = False
        self._adb = self.cfg.adb_path
        self._serial = self.cfg.serial
        self._package: str | None = self.cfg.package
        self._keyboard_device = self.cfg.keyboard_device
        self._screen: ScreenSize | None = None
        self._input_lock = threading.Lock()
        self._fg_lock = threading.Lock()
        self._fg_cache: tuple[float, dict[str, Any]] | None = None
        self._pressed: set[int] = set()
        self._ime_original: str | None = None
        self._ime_switched = False
        self._closed = False
        self._log = (lambda msg: None) if not self.cfg.verbose else self._print_log

    # ---------- 日志 / 底层执行 ----------

    @staticmethod
    def _print_log(msg: str) -> None:
        sys.stderr.write(f"[sky-adb] {msg}\n")
        sys.stderr.flush()

    def _default_runner(self, argv: list[str], timeout: float,
                        binary: bool) -> bytes:
        try:
            proc = subprocess.run(
                argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=timeout, check=False,
            )
        except FileNotFoundError as exc:
            raise AdbError(
                f"找不到 adb 可执行文件: {argv[0]!r}。"
                "请安装 Android platform-tools，把 adb 加进 PATH，"
                "或在 adb_config.json 里填 adb_path。") from exc
        except subprocess.TimeoutExpired as exc:
            raise AdbError(f"adb 命令超时({timeout:.0f}s): {' '.join(argv[1:])}") from exc
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", "replace").strip()
            out = proc.stdout.decode("utf-8", "replace").strip()
            raise AdbError(
                f"adb 命令失败(exit {proc.returncode}): {' '.join(argv[1:])}\n"
                f"{err or out}")
        return proc.stdout

    def adb(self, args: Sequence[str], *, binary: bool = False,
            timeout: float | None = None) -> bytes:
        if not self._resolved and not self._resolving:
            self.resolve()
        self._ensure_adb_path()
        argv = [self._adb]
        if self._serial:
            argv += ["-s", self._serial]
        argv += list(args)
        self._log("$ " + " ".join(argv))
        return self._runner(argv, timeout or self.cfg.shell_timeout, binary)

    def shell(self, command: str, *, binary: bool = False,
              timeout: float | None = None) -> bytes:
        return self.adb(["shell", command], binary=binary, timeout=timeout)

    def shell_text(self, command: str, timeout: float | None = None) -> str:
        return self.shell(command, timeout=timeout).decode("utf-8", "replace")

    def _ensure_adb_path(self) -> None:
        if self._adb:
            return
        self._adb = self._find_adb()
        if not self._adb:
            raise AdbError(
                "找不到 adb。装好 Android platform-tools 后把 adb 加入 PATH，"
                "或设置环境变量 SKY_ADB_PATH / 在 adb_config.json 填 adb_path。")

    @staticmethod
    def _find_adb() -> str | None:
        import shutil
        found = shutil.which("adb")
        if found:
            return found
        sdk = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
        if sdk:
            for name in ("adb.exe", "adb"):
                p = os.path.join(sdk, "platform-tools", name)
                if os.path.isfile(p):
                    return p
        # MuMu 12 常见自带 adb
        for p in (r"C:\Program Files\Netease\MuMuPlayer-12.0\shell\adb.exe",
                  r"C:\Program Files\MuMuVMMVbox\adb.exe"):
            if os.path.isfile(p):
                return p
        return None

    # ---------- 设备发现 / 就绪 ----------

    def list_devices(self) -> list[dict[str, str]]:
        self._ensure_adb_path()
        argv = [self._adb, "devices", "-l"]
        try:
            raw = self._runner(argv, 10.0, False).decode("utf-8", "replace")
        except FileNotFoundError as exc:
            raise AdbError(
                f"找不到 adb 可执行文件: {self._adb!r}。"
                "请安装 Android platform-tools，把 adb 加进 PATH，"
                "或在 adb_config.json 里填 adb_path。") from exc
        devices: list[dict[str, str]] = []
        for line in raw.splitlines()[1:]:
            line = line.strip()
            if not line or line.startswith("*"):
                continue
            parts = line.split()
            if len(parts) < 2 or parts[1] != "device":
                continue
            info: dict[str, str] = {"serial": parts[0], "state": parts[1]}
            for token in parts[2:]:
                if ":" in token:
                    k, v = token.split(":", 1)
                    info[k] = v
            devices.append(info)
        return devices

    def resolve(self, force: bool = False) -> "AdbDevice":
        """定位 adb / 设备序列号 / 游戏包名 / 键盘设备。可重复调用。"""
        if self._resolved and not force:
            return self
        if self._resolving:   # adb() 里隐式调用 resolve() 时防止递归
            return self
        self._resolving = True
        try:
            self._ensure_adb_path()
            devices = self.list_devices()
            if self._serial:
                matched = [d for d in devices if d["serial"] == self._serial]
                if not matched:
                    online = ", ".join(d["serial"] for d in devices) or "无"
                    raise AdbError(
                        f"找不到设备 {self._serial!r}（在线: {online}）。"
                        "模拟器没开或 adb 端口没连上？")
            else:
                online = [d for d in devices if d["state"] == "device"]
                if not online:
                    raise AdbError(
                        "没有在线的 adb 设备。先启动模拟器，并确认 "
                        "`adb devices` 能看到它。")
                if len(online) > 1:
                    names = ", ".join(d["serial"] for d in online)
                    raise AdbError(
                        f"检测到多个 adb 设备（{names}），请设置环境变量 "
                        "SKY_ADB_SERIAL 或在 adb_config.json 里指定 serial。")
                self._serial = online[0]["serial"]
            self._package = self._package or self._detect_package()
            self._keyboard_device = (self._keyboard_device
                                     or self._detect_keyboard_device())
            self._resolved = True
            self._log(f"设备就绪: serial={self._serial} package={self._package} "
                      f"keyboard={self._keyboard_device}")
            return self
        finally:
            self._resolving = False

    def ensure_ready(self) -> "AdbDevice":
        self.resolve()
        if self.cfg.startup_release:
            try:
                self.panic_release()
                self.cfg.startup_release = False
            except AdbError:
                pass
        return self

    def _detect_package(self) -> str:
        try:
            listing = self.shell_text("pm list packages")
        except AdbError:
            listing = ""
        installed = {line.strip().removeprefix("package:") for line in listing.splitlines()}
        for pkg in DEFAULT_PACKAGES:
            if pkg in installed:
                return pkg
        for pkg in installed:
            if "sky" in pkg.lower():
                return pkg
        # 找不到也返回国服包名：至少报错信息能看出目标
        return DEFAULT_PACKAGES[0]

    def _detect_keyboard_device(self) -> str:
        """从 `getevent -pl` 里挑出带全键盘能力的 /dev/input/eventN。"""
        try:
            raw = self.shell_text("getevent -pl", timeout=10.0)
        except AdbError:
            raw = ""
        best: tuple[int, str] | None = None
        for block in re.split(r"\n(?=add device )", raw):
            m = re.search(r"add device \d+:\s*(/dev/input/event\d+)", block)
            if not m:
                continue
            dev = m.group(1)
            name_m = re.search(r'name:\s*"([^"]*)"', block)
            name = (name_m.group(1) if name_m else "").lower()
            has_letters = bool(re.search(r"KEY_A\b", block)) and \
                bool(re.search(r"KEY_W\b", block))
            if not has_letters:
                continue
            score = 0
            if "xiaomi input" in name:
                score += 100
            elif "input" in name:
                score += 50
            if "joystick" in name or "gamepad" in name:
                score -= 100
            if best is None or score > best[0]:
                best = (score, dev)
        if best is None:
            raise AdbError(
                "找不到键盘输入设备（需要能写 /dev/input/eventN 做按键注入）。"
                "可手动在 adb_config.json 里指定 keyboard_device，"
                "如 \"/dev/input/event4\"。")
        return best[1]

    # ---------- 屏幕 / 截屏 ----------

    def screen_size(self, refresh: bool = False) -> ScreenSize:
        """当前方向的屏幕尺寸（像素）。

        `wm size` 报的是物理尺寸，横屏（rotation 1/3）要交换宽高——
        MuMu 实测 `wm size` = 1800x3200，但游戏实际画面/截屏是 3200x1800。
        """
        if self._screen and not refresh:
            return self._screen
        text = self.shell_text("wm size")
        m = re.search(r"Override size:\s*(\d+)x(\d+)", text) or \
            re.search(r"Physical size:\s*(\d+)x(\d+)", text)
        if not m:
            raise AdbError(f"解析屏幕尺寸失败: {text.strip()!r}")
        w, h = int(m.group(1)), int(m.group(2))
        try:
            disp = self.shell_text(
                "dumpsys window displays 2>/dev/null | grep -E 'mRotation='")
            mr = re.search(r"mRotation=(\d)", disp)
            if mr and int(mr.group(1)) in (1, 3):
                w, h = h, w
        except AdbError:
            pass
        self._screen = ScreenSize(w, h)
        return self._screen

    def screencap_raw(self) -> tuple[bytes, int, int, str]:
        """原生截屏（无 PNG 压缩，最快）。返回 (像素 bytes, 宽, 高, 像素格式)。

        像素格式为 "rgba" 或 "bgra"；配合 numpy 时:
            np.frombuffer(pix, np.uint8).reshape(h, w, 4)
        """
        data = self.adb(["exec-out", "screencap"], binary=True,
                        timeout=max(15.0, self.cfg.shell_timeout))
        if len(data) < 16:
            raise AdbError(f"截屏数据太短({len(data)} bytes)，设备异常？")
        # Android 10+ 头部 16 字节：w,h,format,colorspace；老系统 12 字节
        parsed = None
        for header_len in (16, 12):
            if len(data) < header_len:
                continue
            w, h, fmt = struct.unpack("<III", data[:12])
            if not (0 < w <= 10000 and 0 < h <= 10000):
                continue
            if len(data) - header_len != w * h * 4:
                continue
            parsed = (w, h, fmt, header_len)
            break
        if parsed is None:
            raise AdbError(
                f"截屏格式无法识别（{len(data)} bytes）。"
                "可改用 screencap_png() 兜底。")
        w, h, fmt, header_len = parsed
        pix = data[header_len:]
        # PixelFormat: 1=RGBA_8888 2=RGBX_8888 5=BGRA_8888
        if fmt in (1, 2):
            pixel_format = "rgba"
        elif fmt == 5:
            pixel_format = "bgra"
        else:
            raise AdbError(f"不支持的截屏像素格式 format={fmt}（期望 RGBA/RGBX/BGRA）")
        return pix, w, h, pixel_format

    def screencap_png(self) -> bytes:
        """兜底截屏：PNG 编码在设备侧做，慢 5-8 倍，但兼容性最好。"""
        return self.adb(["exec-out", "screencap", "-p"], binary=True,
                        timeout=max(30.0, self.cfg.shell_timeout * 2))

    # ---------- 前台应用 / 生命周期 ----------

    def foreground(self, use_cache: bool = True) -> dict[str, Any]:
        """当前前台窗口信息（安卓层，与模拟器窗口是否最小化无关）。"""
        now = time.time()
        with self._fg_lock:
            if use_cache and self._fg_cache and \
                    now - self._fg_cache[0] < self.cfg.foreground_ttl:
                return self._fg_cache[1]
        info: dict[str, Any] = {"window": "", "package": "", "activity": "",
                                "is_game": False}
        try:
            text = self.shell_text(
                "dumpsys window 2>/dev/null | grep -E 'mCurrentFocus|mFocusedApp'",
                timeout=10.0)
        except AdbError:
            text = ""
        for line in text.splitlines():
            m = re.search(r"Window\{\S+\s+u\d+\s+([^/\s}]+)(?:/([^\s}]+))?", line)
            if m:
                info["package"] = m.group(1)
                info["activity"] = m.group(2) or ""
                info["window"] = line.strip()
                break
        if not info["package"]:
            m = re.search(r"ActivityRecord\{\S+\s+u\d+\s+([^/\s}]+)/([^\s}]+)", text)
            if m:
                info["package"] = m.group(1)
                info["activity"] = m.group(2)
                info["window"] = text.strip()
        info["is_game"] = self._is_game_package(info["package"])
        with self._fg_lock:
            self._fg_cache = (now, info)
        return info

    def _is_game_package(self, package: str) -> bool:
        target = self._package or ""
        if not package:
            return False
        if target and package == target:
            return True
        return any(package == p or package.startswith(p + ".") for p in DEFAULT_PACKAGES)

    def is_game_foreground(self) -> bool:
        return bool(self.foreground().get("is_game"))

    def require_game_foreground(self) -> dict[str, Any]:
        """游戏不在前台时拒绝按键——和旧版 Windows 前台守卫同职责，但查的是
        安卓前台应用：模拟器窗口最小化/被挡住都不影响，只有切到别的 App 才拦。"""
        fg = self.foreground(use_cache=False)
        if not fg.get("is_game"):
            who = fg.get("package") or "未知应用"
            raise AdbError(
                f"Sky 不在前台，拒绝按键（安全保护：防止按键落进别的应用）。"
                f"当前前台: {who}。把模拟器里的光遇切回来即可。")
        return fg

    def screen_on(self) -> None:
        text = self.shell_text("dumpsys power 2>/dev/null | grep -E 'mWakefulness='")
        if "Awake" not in text:
            try:
                self.adb(["shell", "input", "keyevent", "KEYCODE_WAKEUP"])
            except AdbError:
                pass

    def launch_game(self) -> str:
        pkg = self._package or self._detect_package()
        self.screen_on()
        if self.is_game_foreground():
            return f"already foreground: {pkg}"
        try:
            self.shell(f"monkey -p {pkg} -c android.intent.category.LAUNCHER 1",
                       timeout=20.0)
            return f"launched via monkey: {pkg}"
        except AdbError as exc:
            raise AdbError(f"启动光遇失败（{pkg}）: {exc}") from exc

    # ---------- 键盘注入（sendevent） ----------

    @property
    def keyboard_device(self) -> str:
        if not self._keyboard_device:
            self.resolve()
        assert self._keyboard_device
        return self._keyboard_device

    @staticmethod
    def _keycode(name: str | int) -> int:
        if isinstance(name, int):
            return name
        return LINUX_KEYCODES[normalize_key(name)]

    def _event_cmds(self, code: int, down: bool, sync: bool = True) -> str:
        dev = self.keyboard_device
        cmds = [f"sendevent {dev} 1 {code} {1 if down else 0}"]
        if sync:
            cmds.append(f"sendevent {dev} 0 0 0")
        return "; ".join(cmds)

    def _run_events(self, script: str, timeout: float | None = None) -> None:
        """把一串 sendevent 脚本交给设备执行；失败时尽力松开所有键。"""
        try:
            self.shell(script, timeout=timeout)
        except AdbError:
            try:
                self.panic_release()
            except AdbError:
                pass
            raise

    def _hold_script(self, codes: Sequence[int], duration_ms: int) -> str:
        duration = min(max(0, int(duration_ms)), MAX_PRESS_MS)
        dev = self.keyboard_device
        parts: list[str] = []
        for code in codes:
            parts.append(f"sendevent {dev} 1 {code} 1")
            parts.append(f"sendevent {dev} 0 0 0")
        if duration > 0:
            parts.append(f"sleep {duration / 1000:.3f}")
        for code in reversed(list(codes)):
            parts.append(f"sendevent {dev} 1 {code} 0")
            parts.append(f"sendevent {dev} 0 0 0")
        return "; ".join(parts)

    def press_key(self, key: str, duration_ms: int = 80,
                  require_foreground: bool = True) -> None:
        """按一下键（可长按），按键语义与光遇 PC 版一致。"""
        code = self._keycode(key)
        if require_foreground:
            self.require_game_foreground()
        duration = min(max(1, int(duration_ms)), MAX_PRESS_MS)
        with self._input_lock:
            self._pressed.add(code)
            try:
                self._run_events(self._hold_script([code], duration),
                                 timeout=duration / 1000 + self.cfg.shell_timeout)
            finally:
                self._pressed.discard(code)

    def combo(self, keys: Sequence[str], duration_ms: int = 50,
              require_foreground: bool = True) -> None:
        """组合键，如 ("leftctrl", "v") / ("leftshift", "space")。"""
        codes = [self._keycode(k) for k in keys]
        if not codes:
            return
        if require_foreground:
            self.require_game_foreground()
        duration = min(max(1, int(duration_ms)), MAX_PRESS_MS)
        with self._input_lock:
            self._pressed.update(codes)
            try:
                self._run_events(self._hold_script(codes, duration),
                                 timeout=duration / 1000 + self.cfg.shell_timeout)
            finally:
                self._pressed.difference_update(codes)

    def key_down(self, key: str, require_foreground: bool = True) -> None:
        code = self._keycode(key)
        if require_foreground:
            self.require_game_foreground()
        with self._input_lock:
            self._pressed.add(code)
            self._run_events(self._event_cmds(code, True))

    def key_up(self, key: str) -> None:
        code = self._keycode(key)
        with self._input_lock:
            self._pressed.discard(code)
            self._run_events(self._event_cmds(code, False))

    def panic_release(self) -> None:
        """松开所有可能悬着的键。启动时/异常后各来一遍，杜绝"Ctrl 卡住"。"""
        dev = self.keyboard_device
        parts: list[str] = []
        codes = set(self._pressed)
        codes.update(LINUX_KEYCODES[k] for k in PANIC_RELEASE_KEYS)
        for code in sorted(codes):
            parts.append(f"sendevent {dev} 1 {code} 0")
        parts.append(f"sendevent {dev} 0 0 0")
        self.shell("; ".join(parts), timeout=15.0)
        self._pressed.clear()

    # ---------- 文本输入（ADBKeyboard IME） ----------

    def default_ime(self) -> str:
        return self.shell_text(
            "settings get secure default_input_method").strip()

    def adb_ime_installed(self) -> bool:
        try:
            listing = self.shell_text("ime list -s")
        except AdbError:
            return False
        return any(self.cfg.ime_component.split("/")[0] in line
                   for line in listing.splitlines())

    def adb_ime_active(self) -> bool:
        try:
            return self.default_ime() == self.cfg.ime_component
        except AdbError:
            return False

    def ensure_adb_ime(self) -> None:
        """切到 ADBKeyboard 输入法（中文输入的前提）。只切一次，退出时还原。"""
        if not self.adb_ime_installed():
            raise AdbError(
                "模拟器里没装 ADBKeyboard 输入法，无法输入中文。\n"
                "装法: 下载 ADBKeyboard.apk 后执行 "
                "`adb install -r ADBKeyboard.apk`，再 "
                "`adb shell ime enable com.android.adbkeyboard/.AdbIME`。\n"
                "详见 README「中文输入」一节。")
        if self.adb_ime_active():
            return
        current = self.default_ime()
        if current and self._ime_original is None:
            self._ime_original = current
        self.shell(f"ime enable {self.cfg.ime_component}; "
                   f"ime set {self.cfg.ime_component}")
        self._ime_switched = True
        for _ in range(20):
            if self.adb_ime_active():
                return
            time.sleep(0.1)
        raise AdbError(f"切换输入法到 {self.cfg.ime_component} 失败")

    def restore_ime(self) -> None:
        if not self._ime_switched or not self._ime_original:
            return
        if not self.cfg.ime_restore:
            return
        try:
            self.shell(f"ime set {self._ime_original}")
        except AdbError:
            pass
        self._ime_switched = False

    def clear_text(self) -> None:
        """清空当前聚焦输入框。ADBKeyboard 的 ADB_CLEAR_TEXT 广播。

        为什么需要：光遇的聊天输入框会记住上次没发出去的草稿（实测点开
        面板/输入框后草稿还在），不先清空就把新消息**粘在旧草稿后面**，
        AI 会发出上下文错乱的句子。
        """
        self.ensure_adb_ime()
        with self._input_lock:
            self.shell("am broadcast -a ADB_CLEAR_TEXT", timeout=15.0)

    def type_text(self, text: str, clear: bool = True) -> str:
        """把一段文本打进当前聚焦的输入框（中文/emoji 均可）。

        走 ADBKeyboard 的广播接口，输入法切到 com.android.adbkeyboard/.AdbIME；
        这是安卓上没有 root 时输入 Unicode 的可靠办法。
        ``clear=True`` 时先清空输入框（默认）——光遇会保留未发送的草稿。
        """
        if not text:
            return "no text"
        if len(text) > 1000:
            raise AdbError("单条文本太长（超过 1000 字符）")
        self.ensure_adb_ime()
        encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
        with self._input_lock:
            if clear:
                self.shell("am broadcast -a ADB_CLEAR_TEXT", timeout=15.0)
            try:
                self.shell(f"am broadcast -a ADB_INPUT_B64 --es msg {encoded}",
                           timeout=15.0)
            except AdbError:
                # 旧版 ADBKeyboard 只支持 ADB_INPUT_TEXT（纯 ASCII 兜底）
                safe = text.replace("'", "'\\''")
                self.shell(f"am broadcast -a ADB_INPUT_TEXT --es msg '{safe}'",
                           timeout=15.0)
        return f"typed {len(text)} chars via ADBKeyboard"

    # ---------- 状态 / 清理 ----------

    def status(self) -> dict[str, Any]:
        self.resolve()
        info: dict[str, Any] = {
            "mode": "android-emulator",
            "platform": sys.platform,
            "adb": self._adb,
            "serial": self._serial,
            "package": self._package,
            "input_backend": "adb-sendevent",
            "keyboard_device": self._keyboard_device,
            "screen": self.screen_size().as_dict(),
            "ime": {
                "component": self.cfg.ime_component,
                "active": self.adb_ime_active(),
                "adbkeyboard_installed": self.adb_ime_installed(),
            },
        }
        try:
            info["foreground"] = self.foreground()
        except AdbError as exc:
            info["foreground"] = {"error": str(exc)}
        try:
            info["device_model"] = self.shell_text(
                "getprop ro.product.model").strip()
            info["android_version"] = self.shell_text(
                "getprop ro.build.version.release").strip()
        except AdbError:
            pass
        return info

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.restore_ime()
        except AdbError:
            pass
        try:
            self.panic_release()
        except AdbError:
            pass

    def __enter__(self) -> "AdbDevice":
        return self.ensure_ready()

    def __exit__(self, *exc) -> None:
        self.close()


# ===================== 便于脚本用的单例 =====================

_default_device: AdbDevice | None = None
_device_lock = threading.Lock()


def get_device(config: AdbConfig | None = None, fresh: bool = False) -> AdbDevice:
    """进程内共享 AdbDevice（MCP server / detector 各自一份即可）。"""
    global _default_device
    with _device_lock:
        if _default_device is None or fresh:
            _default_device = AdbDevice(config)
        return _default_device


# ===================== CLI =====================

def _cmd_doctor(dev: AdbDevice) -> int:
    print("== adb 设备 ==")
    for d in dev.list_devices():
        print(" ", d)
    print("== 就绪 ==")
    dev.resolve()
    info = dev.status()
    for key in ("adb", "serial", "package", "keyboard_device", "device_model",
                "android_version"):
        print(f"  {key}: {info.get(key)}")
    print(f"  screen: {info.get('screen')}")
    print(f"  foreground: {info.get('foreground')}")
    print(f"  adb keyboard installed: {info['ime']['adbkeyboard_installed']}")
    print(f"  adb keyboard active   : {info['ime']['active']}")
    print("== 截屏耗时 ==")
    for mode in ("raw", "png"):
        t0 = time.time()
        try:
            if mode == "raw":
                pix, w, h, fmt = dev.screencap_raw()
                print(f"  raw : {w}x{h} {fmt} {len(pix)//1024}KB "
                      f"{(time.time()-t0)*1000:.0f}ms")
            else:
                data = dev.screencap_png()
                print(f"  png : {len(data)//1024}KB {(time.time()-t0)*1000:.0f}ms")
        except AdbError as exc:
            print(f"  {mode} 失败: {exc}")
    return 0


def _cmd_shot(dev: AdbDevice, path: str) -> int:
    """CLI 截图用 PNG 兜底通道，少一层 numpy 依赖。"""
    data = dev.screencap_png()
    with open(path, "wb") as f:
        f.write(data)
    print(f"saved {path} ({len(data)} bytes)")
    return 0


def _cmd_key(dev: AdbDevice, key: str, ms: int) -> int:
    dev.ensure_ready()
    dev.press_key(key, ms)
    print(f"pressed {key} {ms}ms")
    return 0


def _cmd_text(dev: AdbDevice, text: str) -> int:
    dev.ensure_ready()
    print(dev.type_text(text))
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    parser = argparse.ArgumentParser(description="光遇模拟器 ADB 执行层工具")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor", help="体检：设备/输入/截屏")
    p_shot = sub.add_parser("shot", help="截屏保存为 PNG")
    p_shot.add_argument("path")
    p_key = sub.add_parser("key", help="注入一次按键")
    p_key.add_argument("key")
    p_key.add_argument("--ms", type=int, default=80)
    p_text = sub.add_parser("text", help="往当前聚焦输入框打字（中文走 ADBKeyboard）")
    p_text.add_argument("text")
    args = parser.parse_args(argv)

    dev = AdbDevice()
    try:
        dev.ensure_ready()
        if args.cmd == "doctor":
            return _cmd_doctor(dev)
        if args.cmd == "shot":
            return _cmd_shot(dev, args.path)
        if args.cmd == "key":
            return _cmd_key(dev, args.key, args.ms)
        if args.cmd == "text":
            return _cmd_text(dev, args.text)
    finally:
        dev.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
