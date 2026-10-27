"""tests/test_mcp_server.py — sky-mcp-server 执行层单元测试（假 adb，无需模拟器）。

用 tests/fake_adb.FakeAdb 注入假设备：importlib 按路径加载 "sky-mcp-server.py"
（文件名带连字符，不能直接 import），AdbSkyController(device=AdbDevice(..., runner=fake))
之后所有 MCP 工具都走假 adb 记录 / 仿真输出。

运行:
    python tests/run_all.py
"""

from __future__ import annotations

import base64
import importlib.util
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for path in (ROOT, HERE):
    if path not in sys.path:
        sys.path.insert(0, path)

import sky_adb
from sky_adb import LINUX_KEYCODES, AdbConfig, AdbDevice
from fake_adb import SERIAL, FakeAdb

SERVER_PATH = os.path.join(ROOT, "sky-mcp-server.py")


def load_server_module():
    """按路径加载执行层（文件名是 sky-mcp-server.py，不是合法模块名）。"""
    spec = importlib.util.spec_from_file_location(
        "sky_mcp_server_under_test", SERVER_PATH)
    module = importlib.util.module_from_spec(spec)
    # dataclass 装饰器要用 sys.modules[cls.__module__] 解析注解，先注册再执行
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


server_mod = load_server_module()

#: numpy/cv2 是可选依赖；没有时 screenshot 会走 adb PNG 兜底（断言相应放宽）
try:
    import cv2  # noqa: F401
    import numpy  # noqa: F401
    HAS_CV = True
except Exception:
    HAS_CV = False


# ===================== 测试脚手架 =====================

def make_server(**fake_kwargs):
    """构造一套「假 adb → AdbDevice → AdbSkyController → McpServer」。"""
    fake = FakeAdb(**fake_kwargs)
    adb_config = AdbConfig(adb_path="fake-adb", serial=SERIAL,
                           startup_release=False, foreground_ttl=0.0)
    device = AdbDevice(adb_config, runner=fake)
    config = server_mod.ServerConfig(input_backend="adb", adb_config=adb_config)
    controller = server_mod.AdbSkyController(config, device=device)
    return server_mod.McpServer(controller), controller, fake


def call_tool(mcp, tool: str, **arguments):
    """走完整 JSON-RPC tools/call 路径，返回 result（含 content / isError）。"""
    response = mcp.handle({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    })
    return response["result"]


def result_text(result) -> str:
    """把 result.content 里的 text 块拼起来。"""
    return "\n".join(
        item.get("text", "")
        for item in result.get("content", [])
        if isinstance(item, dict) and item.get("type") == "text"
    )


def image_blocks(result) -> list[dict]:
    return [item for item in result.get("content", [])
            if isinstance(item, dict) and item.get("type") == "image"]


class ServerTestBase(unittest.TestCase):
    """每个用例一套干净 server，结束时 close()（松键 + 还原输入法）。"""

    def make(self, **fake_kwargs):
        mcp, controller, fake = make_server(**fake_kwargs)
        self.addCleanup(controller.close)
        return mcp, controller, fake


# ===================== status =====================

class TestStatus(ServerTestBase):
    def test_status_reports_adb_execution_layer(self):
        mcp, _, _ = self.make()
        result = call_tool(mcp, "status")
        self.assertFalse(result.get("isError"))
        status = json.loads(result_text(result))
        self.assertEqual(status["mode"], "android-emulator")
        self.assertEqual(status["input_backend"], "adb")
        self.assertEqual(status["serial"], SERIAL)
        self.assertEqual(status["package"], "com.netease.sky")
        self.assertEqual(status["keyboard_device"], "/dev/input/event4")
        # 横屏：wm size 报 1800x3200，screen_size() 按 rotation 交换
        self.assertEqual(status["screen"], {"width": 3200, "height": 1800})
        self.assertTrue(status["foreground"]["is_game"])
        self.assertTrue(status["ime"]["adbkeyboard_installed"])
        self.assertIn("current", status["ime"])
        self.assertTrue(status["device_ready"])
        self.assertEqual(status["screenshot_max"]["width"], 1920)
        self.assertEqual(status["screenshot_max"]["height"], 1080)
        self.assertIn("ocr", status)

    def test_status_foreground_other_is_reported_not_raised(self):
        mcp, _, _ = self.make(focus="other")
        result = call_tool(mcp, "status")
        self.assertFalse(result.get("isError"))
        status = json.loads(result_text(result))
        self.assertTrue(status["device_ready"])
        self.assertFalse(status["foreground"]["is_game"])
        self.assertIn("sogou", status["foreground"]["package"])

    def test_status_device_missing_returns_error_field(self):
        mcp, _, _ = self.make(adb_present=False)
        result = call_tool(mcp, "status")
        self.assertFalse(result.get("isError"))  # status 本身不抛，交给自检打印
        status = json.loads(result_text(result))
        self.assertFalse(status["device_ready"])
        self.assertIn("adb", status["error"])

    def test_status_rejects_removed_config_backend(self):
        fake = FakeAdb()
        adb_config = AdbConfig(adb_path="fake-adb", serial=SERIAL,
                               startup_release=False)
        device = AdbDevice(adb_config, runner=fake)
        controller = server_mod.AdbSkyController(
            server_mod.ServerConfig(input_backend="arduino",
                                    adb_config=adb_config),
            device=device)
        self.addCleanup(controller.close)
        with self.assertRaises(server_mod.SkyError) as ctx:
            controller.status()
        message = str(ctx.exception)
        self.assertIn("已移除", message)
        self.assertIn("adb", message)


# ===================== 按键 =====================

class TestKeys(ServerTestBase):
    def test_press_key_generates_sendevent(self):
        mcp, _, fake = self.make()
        result = call_tool(mcp, "press_key", key="c", duration_ms=150)
        self.assertFalse(result.get("isError"))
        self.assertIn("pressed c", result_text(result))
        scripts = fake.sendevent_scripts()
        self.assertEqual(len(scripts), 1)
        script = scripts[0]
        code = LINUX_KEYCODES["c"]
        self.assertIn("/dev/input/event4", script)
        self.assertIn(f"1 {code} 1", script)          # KEY_C down
        self.assertIn(f"1 {code} 0", script)          # KEY_C up
        self.assertIn("sleep 0.150", script)
        self.assertLess(script.index(f"1 {code} 1"), script.index(f"1 {code} 0"))

    def test_press_key_clamps_duration(self):
        mcp, _, fake = self.make()
        result = call_tool(mcp, "press_key", key="space", duration_ms=99999)
        self.assertIn(f"for {sky_adb.MAX_PRESS_MS}ms", result_text(result))
        script = fake.sendevent_scripts()[-1]
        self.assertIn(f"sleep {sky_adb.MAX_PRESS_MS / 1000:.3f}", script)
        call_tool(mcp, "press_key", key="space", duration_ms=0)
        script = fake.sendevent_scripts()[-1]
        self.assertIn("sleep 0.001", script)

    def test_press_key_combo_still_supported(self):
        # 旧 PC 版支持 "shift-space" 这种写法，模拟器版保留（内部走 combo）
        mcp, _, fake = self.make()
        result = call_tool(mcp, "press_key", key="shift-space", duration_ms=50)
        self.assertIn("pressed leftshift+space", result_text(result))
        script = fake.sendevent_scripts()[-1]
        shift, space = LINUX_KEYCODES["leftshift"], LINUX_KEYCODES["space"]
        self.assertLess(script.index(f"1 {shift} 1"), script.index(f"1 {space} 1"))
        self.assertIn(f"1 {shift} 0", script)
        self.assertIn(f"1 {space} 0", script)

    def test_open_chat_taps_enter(self):
        mcp, _, fake = self.make()
        result = call_tool(mcp, "open_chat")
        self.assertIn("tapped enter", result_text(result))
        script = fake.sendevent_scripts()[-1]
        enter = LINUX_KEYCODES["enter"]
        self.assertIn(f"1 {enter} 1", script)
        self.assertIn(f"1 {enter} 0", script)

    def test_foreground_guard_blocks_press_key(self):
        mcp, _, fake = self.make(focus="other")
        result = call_tool(mcp, "press_key", key="c", duration_ms=150)
        self.assertTrue(result.get("isError"))
        # 错误文本里带「前台」，AI 客户端可据此稍后重试
        self.assertIn("前台", result_text(result))
        self.assertEqual(fake.sendevent_scripts(), [])

    def test_unknown_key_reports_available_keys(self):
        mcp, _, fake = self.make()
        result = call_tool(mcp, "press_key", key="yolo")
        self.assertTrue(result.get("isError"))
        self.assertIn("未知按键", result_text(result))
        self.assertEqual(fake.sendevent_scripts(), [])


# ===================== 中文输入 / 聊天 =====================

class TestChat(ServerTestBase):
    def test_send_chat_chinese_uses_ime_and_enter(self):
        mcp, _, fake = self.make()
        message = "你好呀，一起看星星吧"
        result = call_tool(mcp, "send_chat", message=message)
        self.assertFalse(result.get("isError"))
        self.assertIn(f"sent chat: {message}", result_text(result))
        # 1) 输入法切到 ADBKeyboard；先清空旧草稿（光遇会保留未发送草稿）
        self.assertEqual(fake.default_ime, "com.android.adbkeyboard/.AdbIME")
        self.assertTrue(any("ADB_CLEAR_TEXT" in b for b in fake.broadcasts))
        b64 = base64.b64encode(message.encode("utf-8")).decode("ascii")
        input_broadcasts = [b for b in fake.broadcasts if "ADB_INPUT_B64" in b]
        self.assertEqual(len(input_broadcasts), 1)
        self.assertIn(b64, input_broadcasts[0])
        # 2) 开输入框一下 enter + 发送一下 enter = 两段 sendevent
        scripts = fake.sendevent_scripts()
        self.assertEqual(len(scripts), 2)
        enter = LINUX_KEYCODES["enter"]
        for script in scripts:
            self.assertIn(f"1 {enter} 1", script)
            self.assertIn(f"1 {enter} 0", script)

    def test_send_chat_assume_open_skips_open_key(self):
        mcp, _, fake = self.make()
        result = call_tool(mcp, "send_chat", message="hi", assume_open=True,
                           send=False)
        self.assertFalse(result.get("isError"))
        self.assertIn("typed chat: hi", result_text(result))
        self.assertEqual(fake.sendevent_scripts(), [])
        self.assertTrue(any("ADB_CLEAR_TEXT" in b for b in fake.broadcasts))
        self.assertTrue(any("ADB_INPUT_B64" in b for b in fake.broadcasts))

    def test_type_text_without_send(self):
        mcp, _, fake = self.make()
        message = "测试abc123"
        result = call_tool(mcp, "type_text", message=message, send=False)
        self.assertFalse(result.get("isError"))
        self.assertIn(f"typed text: {message}", result_text(result))
        b64 = base64.b64encode(message.encode("utf-8")).decode("ascii")
        self.assertTrue(any("ADB_INPUT_B64" in b and b64 in b
                            for b in fake.broadcasts))
        self.assertEqual(fake.sendevent_scripts(), [])

    def test_type_text_requires_foreground(self):
        mcp, _, fake = self.make(focus="other")
        result = call_tool(mcp, "type_text", message="你好")
        self.assertTrue(result.get("isError"))
        self.assertIn("前台", result_text(result))
        self.assertEqual(fake.broadcasts, [])

    def test_message_validation(self):
        mcp, _, _ = self.make()
        empty = call_tool(mcp, "send_chat", message="   ")
        self.assertTrue(empty.get("isError"))
        self.assertIn("non-empty", result_text(empty))
        too_long = call_tool(mcp, "send_chat", message="x" * 241)
        self.assertTrue(too_long.get("isError"))
        self.assertIn("too long", result_text(too_long))


# ===================== 前台 / 焦点 =====================

class TestFocus(ServerTestBase):
    def test_ensure_game_foreground_error_contains_front(self):
        mcp, controller, fake = self.make(focus="other")
        with self.assertRaises(server_mod.SkyError) as ctx:
            controller.ensure_game_foreground()
        self.assertIn("前台", str(ctx.exception))
        self.assertEqual(fake.sendevent_scripts(), [])

    def test_focus_game_when_already_foreground(self):
        mcp, _, _ = self.make()
        result = call_tool(mcp, "focus_game")
        self.assertFalse(result.get("isError"))
        payload = json.loads(result_text(result))
        self.assertTrue(payload["focused"])
        self.assertIn("already foreground", payload["action"])
        self.assertTrue(payload["foreground"]["is_game"])

    def test_focus_game_launches_when_not_foreground(self):
        class LaunchingFakeAdb(FakeAdb):
            """模拟 monkey 启动后游戏真的回到前台。"""

            def _shell(self, script: str) -> str:
                if script.startswith("monkey"):
                    self.focus = "game"
                return super()._shell(script)

        fake = LaunchingFakeAdb(focus="other")
        adb_config = AdbConfig(adb_path="fake-adb", serial=SERIAL,
                               startup_release=False, foreground_ttl=0.0)
        device = AdbDevice(adb_config, runner=fake)
        controller = server_mod.AdbSkyController(
            server_mod.ServerConfig(adb_config=adb_config), device=device)
        self.addCleanup(controller.close)
        mcp = server_mod.McpServer(controller)
        result = call_tool(mcp, "focus_game")
        self.assertFalse(result.get("isError"), result_text(result))
        payload = json.loads(result_text(result))
        self.assertTrue(payload["focused"])
        self.assertIn("launched", payload["action"])
        self.assertTrue(payload["foreground"]["is_game"])
        self.assertTrue(any("monkey" in command for command in fake.commands()))


# ===================== 截图 / OCR =====================

class TestScreenshot(ServerTestBase):
    def test_screenshot_returns_png_image_content(self):
        mcp, _, fake = self.make()
        result = call_tool(mcp, "screenshot")
        self.assertFalse(result.get("isError"))
        images = image_blocks(result)
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0]["mimeType"], "image/png")
        raw = base64.b64decode(images[0]["data"])
        self.assertTrue(raw)
        self.assertTrue(any("exec-out screencap" in command
                            for command in fake.commands()))
        if HAS_CV:
            # raw RGBA 帧 8x4 → cv2 转 BGR 再编码 PNG
            self.assertEqual(raw[:8], b"\x89PNG\r\n\x1a\n")
            self.assertEqual(server_mod.AdbSkyController._png_size(raw), (8, 4))

    def test_read_screen_returns_ocr_text_and_image(self):
        mcp, _, _ = self.make()
        result = call_tool(mcp, "read_screen")
        self.assertFalse(result.get("isError"))
        self.assertTrue(any(isinstance(item, dict) and item.get("type") == "text"
                            for item in result["content"]))
        self.assertEqual(len(image_blocks(result)), 1)
        payload = json.loads(result_text(result))
        self.assertEqual(payload["image_size"], {"width": 8, "height": 4})
        self.assertIsInstance(payload["texts"], list)
        self.assertIn("ocr", payload)


# ===================== 错误 / 后端迁移 =====================

class TestErrors(ServerTestBase):
    def test_unknown_tool(self):
        mcp, _, _ = self.make()
        result = call_tool(mcp, "definitely_not_a_tool")
        self.assertTrue(result.get("isError"))
        self.assertIn("Unknown tool", result_text(result))

    def test_removed_pc_backends_rejected_with_clear_message(self):
        mcp, _, fake = self.make()
        for backend in ("arduino", "pyautogui", "pydirectinput", "gamepad"):
            result = call_tool(mcp, "press_key", key="c", backend=backend)
            self.assertTrue(result.get("isError"), backend)
            message = result_text(result)
            self.assertIn("已移除", message)
            self.assertIn("adb", message)
        # 全部在真正下发按键前就被拒了
        self.assertEqual(fake.sendevent_scripts(), [])

    def test_unknown_backend_rejected(self):
        mcp, _, _ = self.make()
        result = call_tool(mcp, "press_key", key="c", backend="xbox")
        self.assertTrue(result.get("isError"))
        self.assertIn("不支持", result_text(result))

    def test_auto_backend_resolves_to_adb(self):
        mcp, controller, _ = self.make()
        self.assertEqual(controller.resolve_backend("auto"), "adb")
        self.assertEqual(controller.resolve_backend(None), "adb")

    def test_initialize_and_tools_list(self):
        mcp, _, _ = self.make()
        init = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        self.assertEqual(init["result"]["protocolVersion"],
                         server_mod.PROTOCOL_VERSION)
        listing = mcp.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = [tool["name"] for tool in listing["result"]["tools"]]
        self.assertEqual(names, [
            "status", "focus_game", "press_key", "open_chat", "send_chat",
            "type_text", "read_screen", "screenshot",
        ])
        # 工具参数名是对 AI 客户端的公开契约，不能随便改
        by_name = {tool["name"]: tool for tool in listing["result"]["tools"]}
        self.assertIn("key", by_name["press_key"]["inputSchema"]["properties"])
        self.assertIn("duration_ms",
                      by_name["press_key"]["inputSchema"]["properties"])
        self.assertIn("backend",
                      by_name["press_key"]["inputSchema"]["properties"])
        self.assertIn("message",
                      by_name["send_chat"]["inputSchema"]["properties"])
        self.assertIn("send",
                      by_name["type_text"]["inputSchema"]["properties"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
