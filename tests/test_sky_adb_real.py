"""tests/test_sky_adb_real.py — 真实 adb 设备层测试（真模拟器 + 真光遇，无 fake）。

覆盖：设备/屏幕/截屏、前台守卫、输入法健康、真实按键切换聊天面板、
真实 InputConnection 建立、无焦点打字必须报 ime 错误。
"""

from __future__ import annotations

import time
import unittest

from real_env import get


class TestRealAdbDevice(unittest.TestCase):
    def setUp(self):
        self.env.ensure_game_foreground()

    @classmethod
    def setUpClass(cls):
        cls.env = get()
        cls.device = cls.env.device
        cls.controller = cls.env.controller
        cls.SkyError = cls.env.server_mod.SkyError

    def tearDown(self):
        # 每个用例后把聊天 UI 恢复干净（真实按键 + 真实 OCR 校验）
        self.env.reset_chat()

    def test_status_reports_real_device(self):
        status = self.device.status()
        self.assertEqual(status["mode"], "android-emulator")
        self.assertEqual(status["input_backend"], "adb-sendevent")
        self.assertEqual(status["package"], "com.netease.sky")
        self.assertIn(":", status["serial"])
        self.assertTrue(status["foreground"]["is_game"])
        self.assertTrue(status["ime"]["adbkeyboard_installed"])
        self.assertGreater(status["screen"]["width"], status["screen"]["height"])
        self.assertTrue(status["keyboard_device"].startswith("/dev/input/"))
        self.assertTrue(status["adb"])

    def test_screen_size_matches_real_capture(self):
        screen = self.device.screen_size(refresh=True)
        pixels, width, height, pixel_format = self.device.screencap_raw()
        self.assertEqual((width, height), (screen.width, screen.height))
        self.assertIn(pixel_format, ("rgba", "bgra"))
        self.assertEqual(len(pixels), width * height * 4)
        png = self.device.screencap_png()
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(self.env.server_mod.AdbSkyController._png_size(png),
                         (width, height))

    def test_foreground_guard_real(self):
        foreground = self.device.require_game_foreground()
        self.assertTrue(foreground["is_game"])
        self.assertIn("sky", foreground["package"])

    def test_ime_is_healthy(self):
        self.assertTrue(self.device.adb_ime_installed())
        self.assertEqual(self.device.ensure_ime_healthy(), "ime ok")
        self.assertTrue(self.device.ime_process_alive())

    def test_press_key_toggles_real_chat_panel(self):
        # 真实按 c → 真实 OCR 观察到面板打开 → 再按 c 关闭
        self.env.ensure_panel_closed()
        self.assertIsNot(self.env.chat_state().get("panel_open"), True)

        self.controller.press_key("c", 40)
        deadline = time.time() + 8.0
        opened = False
        while time.time() < deadline:
            if self.env.chat_state().get("panel_open") is True:
                opened = True
                break
            time.sleep(0.8)
        self.assertTrue(opened, "真实按 c 后聊天面板没有打开（read_chat 判定）")

        self.env.ensure_panel_closed()
        self.assertIsNot(self.env.chat_state().get("panel_open"), True)

    def test_enter_opens_real_input_connection(self):
        self.env.ensure_panel_closed()
        self.controller.press_key("enter", 35)
        self.assertTrue(self.device.wait_input_connection(6.0),
                        "真实按 enter 后聊天输入框没有建立 InputConnection")
        self.assertTrue(self.device.adb_ime_active())
        self.controller.press_key("escape", 200)

    def test_type_text_without_focus_is_ime_error(self):
        # 没有聚焦输入框时绝对不能广播（否则 ADBKeyboard 崩溃/丢字）
        self.env.reset_chat()
        with self.assertRaises(self.SkyError) as ctx:
            self.controller.type_text("no-focus-real-test")
        self.assertEqual(ctx.exception.code, "ime")
        self.assertIn("输入焦点", str(ctx.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
