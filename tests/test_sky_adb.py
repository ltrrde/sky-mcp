"""tests/test_sky_adb.py — AdbDevice 执行层单元测试（假 adb，无需模拟器）。

运行:
    python tests/run_all.py
"""

from __future__ import annotations

import base64
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in (ROOT, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

import sky_adb
from sky_adb import AdbConfig, AdbDevice, AdbError, LINUX_KEYCODES, normalize_key
from fake_adb import (DEVICES_EMPTY, DEVICES_TWO, SERIAL, FakeAdb,
                      make_raw_screencap)


def make_device(**fake_kwargs) -> tuple[AdbDevice, FakeAdb]:
    fake = FakeAdb(**fake_kwargs)
    cfg = AdbConfig(adb_path="fake-adb", serial=SERIAL, startup_release=False,
                    verbose=False)
    dev = AdbDevice(cfg, runner=fake)
    dev.resolve()
    return dev, fake


class TestDiscovery(unittest.TestCase):
    def test_auto_resolve(self):
        dev, fake = make_device()
        self.assertEqual(dev._serial, SERIAL)
        self.assertEqual(dev._package, "com.netease.sky")
        self.assertEqual(dev.keyboard_device, "/dev/input/event4")
        self.assertEqual(dev.screen_size().width, 3200)
        self.assertEqual(dev.screen_size().height, 1800)

    def test_keyboard_device_skips_joystick_and_mouse(self):
        dev, _ = make_device()
        self.assertNotIn("event5", dev.keyboard_device)
        self.assertNotIn("event3", dev.keyboard_device)

    def test_no_device_raises(self):
        fake = FakeAdb(devices_output=DEVICES_EMPTY)
        dev = AdbDevice(AdbConfig(adb_path="fake-adb", startup_release=False),
                        runner=fake)
        with self.assertRaises(AdbError) as ctx:
            dev.resolve()
        self.assertIn("没有在线的 adb 设备", str(ctx.exception))

    def test_multiple_devices_requires_serial(self):
        fake = FakeAdb(devices_output=DEVICES_TWO)
        dev = AdbDevice(AdbConfig(adb_path="fake-adb", startup_release=False),
                        runner=fake)
        with self.assertRaises(AdbError) as ctx:
            dev.resolve()
        self.assertIn("多个 adb 设备", str(ctx.exception))

    def test_explicit_serial_not_online(self):
        fake = FakeAdb()
        dev = AdbDevice(AdbConfig(adb_path="fake-adb", serial="127.0.0.1:5555",
                                  startup_release=False), runner=fake)
        with self.assertRaises(AdbError) as ctx:
            dev.resolve()
        self.assertIn("找不到设备", str(ctx.exception))

    def test_missing_adb_message(self):
        fake = FakeAdb(adb_present=False)
        dev = AdbDevice(AdbConfig(adb_path="fake-adb", startup_release=False),
                        runner=fake)
        with self.assertRaises(AdbError) as ctx:
            dev.resolve()
        self.assertIn("找不到 adb", str(ctx.exception))


class TestKeys(unittest.TestCase):
    def test_normalize_aliases(self):
        self.assertEqual(normalize_key("esc"), "escape")
        self.assertEqual(normalize_key("Return"), "enter")
        self.assertEqual(normalize_key("CTRL"), "leftctrl")
        self.assertEqual(normalize_key("spacebar"), "space")

    def test_unknown_key_lists_available(self):
        with self.assertRaises(AdbError) as ctx:
            normalize_key("yolo")
        self.assertIn("未知按键", str(ctx.exception))

    def test_press_key_sendevent_script(self):
        dev, fake = make_device()
        dev.press_key("c", 150)
        scripts = fake.sendevent_scripts()
        self.assertEqual(len(scripts), 1)
        script = scripts[0]
        self.assertIn("/dev/input/event4", script)
        self.assertIn("1 46 1", script)   # KEY_C down
        self.assertIn("sleep 0.150", script)
        self.assertIn("1 46 0", script)   # KEY_C up
        self.assertLess(script.index("1 46 1"), script.index("1 46 0"))

    def test_press_key_clamps_duration_and_minimum(self):
        dev, fake = make_device()
        dev.press_key("space", 99999)
        script = fake.sendevent_scripts()[-1]
        self.assertIn(f"sleep {sky_adb.MAX_PRESS_MS / 1000:.3f}", script)
        dev.press_key("space", 0)
        script = fake.sendevent_scripts()[-1]
        self.assertIn("sleep 0.001", script)

    def test_combo_shift_space_order(self):
        dev, fake = make_device()
        dev.combo(["shift", "space"], 50)
        script = fake.sendevent_scripts()[-1]
        shift_down = script.index(f"1 {LINUX_KEYCODES['leftshift']} 1")
        space_down = script.index(f"1 {LINUX_KEYCODES['space']} 1")
        self.assertLess(shift_down, space_down)
        self.assertIn(f"1 {LINUX_KEYCODES['leftshift']} 0", script)
        self.assertIn(f"1 {LINUX_KEYCODES['space']} 0", script)

    def test_foreground_guard_blocks_keys(self):
        dev, fake = make_device(focus="other")
        with self.assertRaises(AdbError) as ctx:
            dev.press_key("c", 100)
        self.assertIn("前台", str(ctx.exception))
        self.assertEqual(fake.sendevent_scripts(), [])

    def test_key_down_up(self):
        dev, fake = make_device()
        dev.key_down("w")
        dev.key_up("w")
        self.assertIn(f"1 {LINUX_KEYCODES['w']} 1", fake.sendevent_scripts()[-2])
        self.assertIn(f"1 {LINUX_KEYCODES['w']} 0", fake.sendevent_scripts()[-1])

    def test_panic_release_sends_all_ups(self):
        dev, fake = make_device()
        dev.panic_release()
        script = fake.sendevent_scripts()[-1]
        for key in ("leftctrl", "leftshift", "leftalt", "space", "w", "c"):
            self.assertIn(f"1 {LINUX_KEYCODES[key]} 0", script)
        self.assertNotIn("1 46 1", script)


class TestScreen(unittest.TestCase):
    def test_screencap_raw_parse(self):
        frame = make_raw_screencap(width=16, height=8,
                                   rgba=(1, 2, 3, 255))
        dev, _ = make_device(raw_frame=frame)
        pix, w, h, fmt = dev.screencap_raw()
        self.assertEqual((w, h, fmt), (16, 8, "rgba"))
        self.assertEqual(len(pix), 16 * 8 * 4)
        self.assertEqual(pix[:4], bytes((1, 2, 3, 255)))

    def test_screencap_raw_12_byte_header(self):
        frame = make_raw_screencap(width=4, height=2, header_len=12)
        dev, _ = make_device(raw_frame=frame)
        pix, w, h, fmt = dev.screencap_raw()
        self.assertEqual((w, h), (4, 2))
        self.assertEqual(fmt, "rgba")

    def test_screencap_raw_bad_data(self):
        dev, _ = make_device(raw_frame=b"\x00" * 32)
        with self.assertRaises(AdbError) as ctx:
            dev.screencap_raw()
        self.assertIn("截屏", str(ctx.exception))

    def test_screen_size_parsed(self):
        dev, _ = make_device()
        size = dev.screen_size()
        self.assertEqual(size.as_dict(), {"width": 3200, "height": 1800})


class TestText(unittest.TestCase):
    def test_type_text_switches_ime_and_broadcasts_base64(self):
        dev, fake = make_device()
        msg = "你好呀abc"
        dev.type_text(msg)
        self.assertEqual(fake.default_ime, "com.android.adbkeyboard/.AdbIME")
        # 默认先清空草稿，再输入
        self.assertEqual(len(fake.broadcasts), 2)
        self.assertIn("ADB_CLEAR_TEXT", fake.broadcasts[0])
        script = fake.broadcasts[1]
        self.assertIn("ADB_INPUT_B64", script)
        expected = base64.b64encode(msg.encode()).decode()
        self.assertIn(expected, script)
        # 退出时还原输入法
        dev.close()
        self.assertIn("com.sohu.inputmethod.sogou.chuizi",
                      fake.default_ime)

    def test_type_text_no_clear(self):
        dev, fake = make_device()
        dev.type_text("hi", clear=False)
        self.assertEqual(len(fake.broadcasts), 1)
        self.assertIn("ADB_INPUT_B64", fake.broadcasts[0])

    def test_clear_text_broadcast(self):
        dev, fake = make_device()
        dev.clear_text()
        self.assertEqual(len(fake.broadcasts), 1)
        self.assertIn("ADB_CLEAR_TEXT", fake.broadcasts[0])

    def test_type_text_without_adbkeyboard_raises(self):
        dev, _ = make_device(adb_ime_installed=False)
        with self.assertRaises(AdbError) as ctx:
            dev.type_text("你好")
        self.assertIn("ADBKeyboard", str(ctx.exception))

    def test_type_text_empty(self):
        dev, fake = make_device()
        self.assertEqual(dev.type_text(""), "no text")
        self.assertEqual(fake.broadcasts, [])

    def test_type_text_too_long(self):
        dev, _ = make_device()
        with self.assertRaises(AdbError):
            dev.type_text("x" * 1001)


class TestStatus(unittest.TestCase):
    def test_status_shape(self):
        dev, _ = make_device()
        st = dev.status()
        self.assertEqual(st["mode"], "android-emulator")
        self.assertEqual(st["input_backend"], "adb-sendevent")
        self.assertEqual(st["serial"], SERIAL)
        self.assertEqual(st["package"], "com.netease.sky")
        self.assertEqual(st["keyboard_device"], "/dev/input/event4")
        self.assertTrue(st["foreground"]["is_game"])
        self.assertTrue(st["ime"]["adbkeyboard_installed"])
        self.assertEqual(st["screen"], {"width": 3200, "height": 1800})

    def test_foreground_other_app(self):
        dev, _ = make_device(focus="other")
        fg = dev.foreground(use_cache=False)
        self.assertFalse(fg["is_game"])
        self.assertIn("sogou", fg["package"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
