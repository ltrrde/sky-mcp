"""tests/fake_adb.py — 假 adb 执行器，给 unittest 用，不需要真模拟器。

AdbDevice(config, runner=FakeAdb()) 之后所有 adb 调用都走这里：
记录命令、返回仿真输出（设备列表 / getevent / 截屏 / dumpsys / IME …）。
"""

from __future__ import annotations

import struct
import zlib

SERIAL = "127.0.0.1:16384"

DEVICES_L = """List of devices attached
127.0.0.1:16384        device product:r9q model:SM_G9900 device:r9q transport_id:2
"""

DEVICES_EMPTY = "List of devices attached\n"

DEVICES_TWO = """List of devices attached
127.0.0.1:16384        device product:r9q model:SM_G9900 device:r9q transport_id:2
emulator-5554          device product:sdk_gphone64_x86_64 device:emu64xa transport_id:3
"""

GETEVENT_PL = """add device 1: /dev/input/event5
  name:     "Xiaomi Joystick"
    KEY (0001): BTN_THUMBL            BTN_THUMBR            BTN_DPAD_UP
                BTN_DPAD_DOWN
add device 2: /dev/input/event4
  name:     "Xiaomi Input"
    KEY (0001): KEY_ESC               KEY_1                 KEY_2
                KEY_3                 KEY_4                 KEY_5
                KEY_Q                 KEY_W                 KEY_E
                KEY_A                 KEY_S                 KEY_D
                KEY_F                 KEY_C                 KEY_ENTER
                KEY_LEFTCTRL          KEY_LEFTALT           KEY_LEFTMETA
                KEY_SPACE             KEY_UP                KEY_DOWN
                KEY_LEFT              KEY_RIGHT
    KEY (0002): REL_X                 REL_Y
add device 3: /dev/input/event3
  name:     "Xiaomi mouse integration"
    KEY (0001): KEY_VOLUMEDOWN        KEY_VOLUMEUP          BTN_MOUSE
"""

PACKAGES = """package:com.android.adbkeyboard
package:com.android.settings
package:com.netease.sky
package:com.mumu.launcher
"""

WINDOW_GAME = """  mCurrentFocus=Window{d822bb2 u0 com.netease.sky/com.tgc.sky.netease.GameActivity_Netease}
  mFocusedApp=ActivityRecord{48b3714 u0 com.netease.sky/com.tgc.sky.netease.GameActivity_Netease t1025}
"""

WINDOW_OTHER = """  mCurrentFocus=Window{4b8e8bb u0 com.sohu.inputmethod.sogou.chuizi/com.sohu.inputmethod.sogou.SogouIME}
  mFocusedApp=ActivityRecord{48b3714 u0 com.netease.sky/com.tgc.sky.netease.GameActivity_Netease t1025}
"""

IMES = """com.sohu.inputmethod.sogou.chuizi/com.sohu.inputmethod.sogou.SogouIME
com.android.adbkeyboard/.AdbIME
"""

POWER_AWAKE = "  mWakefulness=Awake\n"
# 真机 MuMu 实测：wm size 是竖屏物理尺寸，dumpsys window displays mRotation=1，
# 游戏实际截屏为 3200x1800（横屏），screen_size() 需要交换宽高。
WM_SIZE = "Physical size: 1800x3200\n"
WINDOW_DISPLAYS = "    mRotation=1 mDeferredRotationPauseCount=0\n"


def make_raw_screencap(width: int = 8, height: int = 4,
                       rgba: tuple[int, int, int, int] = (10, 20, 30, 255),
                       header_len: int = 16) -> bytes:
    """构造与真机同格式的截图数据（RGBA_8888）。"""
    header = struct.pack("<III", width, height, 1)
    if header_len == 16:
        header += struct.pack("<I", 1)
    pixel = bytes(rgba)
    return header + pixel * (width * height)


def make_png(width: int = 8, height: int = 4,
             rgba: tuple[int, int, int, int] = (10, 20, 30, 255)) -> bytes:
    """构造一张合法的最小 PNG（给 adb `screencap -p` 兜底通道用）。"""
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    row = b"\x00" + bytes(rgba) * width
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(row * height))
            + chunk(b"IEND", b""))


class FakeAdb:
    """可配置的假 adb。runner 签名与 AdbDevice._default_runner 一致。"""

    def __init__(self, serial: str = SERIAL, devices_output: str = DEVICES_L,
                 focus: str = "game", packages: str = PACKAGES,
                 imes: str = IMES, adb_ime_installed: bool = True,
                 raw_frame: bytes | None = None,
                 png_frame: bytes | None = None, adb_present: bool = True):
        self.serial = serial
        self.devices_output = devices_output
        self.focus = focus
        self.packages = packages
        self.imes = imes if adb_ime_installed else imes.replace(
            "com.android.adbkeyboard/.AdbIME\n", "")
        self.adb_ime_installed = adb_ime_installed
        self.raw_frame = raw_frame if raw_frame is not None else make_raw_screencap()
        self.png_frame = png_frame if png_frame is not None else make_png()
        self.adb_present = adb_present
        self.default_ime = "com.sohu.inputmethod.sogou.chuizi/com.sohu.inputmethod.sogou.SogouIME"
        self.calls: list[list[str]] = []
        self.shell_scripts: list[str] = []
        self.broadcasts: list[str] = []

    # ---- 便捷断言 ----

    def commands(self) -> list[str]:
        return [" ".join(call) for call in self.calls]

    def sendevent_scripts(self) -> list[str]:
        return [s for s in self.shell_scripts if "sendevent" in s]

    def __call__(self, argv: list[str], timeout: float, binary: bool) -> bytes:
        self.calls.append(list(argv))
        if not self.adb_present:
            raise FileNotFoundError(argv[0])
        args = list(argv[1:])
        # 设备发现走 `adb devices -l`（不带 -s）；其余命令带 [-s serial]
        if args and args[0] == "devices":
            return self.devices_output.encode()
        if args[:2] == ["-s", self.serial]:
            rest = args[2:]
        else:
            rest = args
        assert rest, f"空 adb 调用: {argv}"
        if rest[0] == "devices":
            return self.devices_output.encode()
        if rest[0] == "exec-out":
            if rest[1] == "screencap":
                # `screencap -p` 是设备侧 PNG 兜底通道；否则是 raw RGBA
                if "-p" in rest:
                    return self.png_frame
                return self.raw_frame
        if rest[0] == "shell":
            script = " ".join(rest[1:])
            self.shell_scripts.append(script)
            return self._shell(script).encode()
        if rest[0] == "root":
            return b"adbd cannot run as root in production builds"
        return b""

    def _shell(self, script: str) -> str:
        if script.startswith("getevent -pl"):
            return GETEVENT_PL
        if script.startswith("pm list packages"):
            return self.packages
        if script.startswith("wm size"):
            return WM_SIZE
        if script.startswith("dumpsys window displays"):
            return WINDOW_DISPLAYS
        if "dumpsys window" in script:
            return WINDOW_GAME if self.focus == "game" else WINDOW_OTHER
        if script.startswith("dumpsys power"):
            return POWER_AWAKE
        if script.startswith("settings get secure default_input_method"):
            return self.default_ime + "\n"
        if script.startswith("ime list -s"):
            return self.imes
        if script.startswith("ime enable") or script.startswith("ime set"):
            marker = "com.android.adbkeyboard/.AdbIME"
            if marker in script:
                self.default_ime = marker
            elif "ime set" in script:
                self.default_ime = script.split("ime set ", 1)[1].strip()
            return ""
        if script.startswith("am broadcast"):
            self.broadcasts.append(script)
            return "Broadcast completed: result=0\n"
        if script.startswith("sendevent") or "sendevent" in script:
            return ""
        if script.startswith("monkey") or script.startswith("am start"):
            return "Events injected: 1\n"
        if script.startswith("getprop"):
            prop = script.split("getprop ", 1)[1].strip()
            return {"ro.product.model": "SM-G9900\n",
                    "ro.build.version.release": "12\n"}.get(prop, "\n")
        return ""
