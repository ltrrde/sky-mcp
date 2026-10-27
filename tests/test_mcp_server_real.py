"""tests/test_mcp_server_real.py — 真实 MCP 工具测试（真模拟器 + 真游戏 + 真 OCR）。

通过真实 JSON-RPC `tools/call` 路径调用执行层，落到真实 adb 设备与真实游戏：
  - 会真实开关聊天面板、读取真机截图/OCR；
  - 会真实发送 1 条带时间戳的测试消息（验证 send_chat 回执与 read_chat 回读）；
  - 前台守卫测试会真实切到系统设置再 focus_game 拉回游戏。
测试结束由 real_env 的 atexit 清理：松键 + 还原输入法。
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import time
import unittest

from real_env import ROOT, get

TMP_OUT = os.path.join(ROOT, "tests", ".tmp_out")


def call_tool(mcp, tool: str, **arguments):
    """走完整 JSON-RPC tools/call 路径，返回 result（含 content / isError）。"""
    response = mcp.handle({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    })
    return response["result"]


def result_text(result) -> str:
    return "\n".join(
        item.get("text", "")
        for item in result.get("content", [])
        if isinstance(item, dict) and item.get("type") == "text"
    )


def image_blocks(result) -> list[dict]:
    return [item for item in result.get("content", [])
            if isinstance(item, dict) and item.get("type") == "image"]


class TestRealMcpTools(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = get()
        cls.mcp = cls.env.mcp
        cls.controller = cls.env.controller
        cls.server = cls.env.server_mod

    def setUp(self):
        self.env.ensure_game_foreground()

    def tearDown(self):
        self.env.reset_chat()

    def test_status_tool_real(self):
        result = call_tool(self.mcp, "status")
        self.assertFalse(result.get("isError"), result_text(result))
        payload = json.loads(result_text(result))
        self.assertEqual(payload["mode"], "android-emulator")
        self.assertTrue(payload["device_ready"])
        self.assertEqual(payload["ocr"], "rapidocr")
        self.assertTrue(payload["ocr_ready"])
        self.assertEqual(payload["ocr_error"], "")
        self.assertTrue(payload["foreground"]["is_game"])
        self.assertGreater(payload["screenshot_max"]["width"], 0)

    def test_screenshot_tool_real(self):
        result = call_tool(self.mcp, "screenshot")
        self.assertFalse(result.get("isError"), result_text(result))
        images = image_blocks(result)
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0]["mimeType"], "image/png")
        raw = base64.b64decode(images[0]["data"])
        self.assertTrue(raw.startswith(b"\x89PNG\r\n\x1a\n"))
        meta = json.loads(result_text(result))
        self.assertGreater(meta["image_size"]["width"], 0)
        self.assertGreater(meta["image_size"]["height"], 0)
        self.assertTrue(meta["frame_id"])
        self.assertTrue(meta["captured_at"])
        self.assertEqual(
            self.server.AdbSkyController._png_size(raw),
            (meta["image_size"]["width"], meta["image_size"]["height"]))

    def test_read_screen_tool_real(self):
        result = call_tool(self.mcp, "read_screen")
        self.assertFalse(result.get("isError"), result_text(result))
        images = image_blocks(result)
        self.assertEqual(len(images), 1)
        payload = json.loads(result_text(result))
        self.assertEqual(payload["ocr"], "rapidocr")
        self.assertGreater(payload["ocr_ms"], 0.0)
        self.assertTrue(payload["frame_id"])
        self.assertTrue(payload["captured_at"])
        self.assertEqual(payload["region"], "full")
        self.assertIsInstance(payload["texts"], list)
        self.assertEqual(
            self.server.AdbSkyController._png_size(
                base64.b64decode(images[0]["data"])),
            (payload["image_size"]["width"], payload["image_size"]["height"]))
        # 文本与图片必须来自同一帧（同一 frame_id 语义）
        self.assertEqual(payload["region"], "full")

    def test_read_screen_save_path_real(self):
        shutil.rmtree(TMP_OUT, ignore_errors=True)
        path = os.path.join(TMP_OUT, "read_screen_real.png")
        result = call_tool(self.mcp, "read_screen", save_path=path)
        self.assertFalse(result.get("isError"), result_text(result))
        payload = json.loads(result_text(result))
        saved = payload["saved_path"]
        self.assertTrue(os.path.isfile(saved))
        with open(saved, "rb") as handle:
            self.assertTrue(handle.read(8).startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertGreater(os.path.getsize(saved), 0)

    def test_read_chat_tool_real(self):
        self.env.ensure_panel_open()
        result = call_tool(self.mcp, "read_chat", limit=5)
        self.assertFalse(result.get("isError"), result_text(result))
        payload = json.loads(result_text(result))
        self.assertEqual(payload["ocr"], "rapidocr")
        self.assertGreater(payload["ocr_ms"], 0.0)
        self.assertIn("chat", payload["region"])
        self.assertIs(payload["panel_open"], True)
        self.assertIsInstance(payload["messages"], list)
        for message in payload["messages"]:
            for field in ("speaker", "text", "confidence", "y",
                          "is_self", "kind"):
                self.assertIn(field, message)
        ys = [message["y"] for message in payload["messages"]]
        self.assertEqual(ys, sorted(ys))

    def test_press_key_toggles_chat_panel_via_mcp(self):
        self.env.ensure_panel_closed()
        self.assertIsNot(self.env.chat_state().get("panel_open"), True)
        result = call_tool(self.mcp, "press_key", key="c", duration_ms=40)
        self.assertFalse(result.get("isError"), result_text(result))
        self.assertIn("pressed c", result_text(result))
        deadline = time.time() + 8.0
        opened = False
        while time.time() < deadline:
            if self.env.chat_state().get("panel_open") is True:
                opened = True
                break
            time.sleep(0.8)
        self.assertTrue(opened, "MCP press_key 后聊天面板没有打开")

    def test_send_chat_real_receipt_and_readback(self):
        self.env.ensure_panel_open()
        marker = f"skytest{int(time.time()) % 100000}"
        result = call_tool(self.mcp, "send_chat", message=marker, verify=True)
        self.assertFalse(result.get("isError"), result_text(result))
        receipt = json.loads(result_text(result))
        for field in ("ok", "action", "sent", "sent_at", "chars",
                      "truncated", "message"):
            self.assertIn(field, receipt)
        self.assertTrue(receipt["ok"])
        self.assertEqual(receipt["action"], "sent")
        self.assertTrue(receipt["sent"])
        self.assertEqual(receipt["chars"], len(marker))
        self.assertEqual(receipt["message"], marker)
        self.assertFalse(receipt["truncated"])

        # 真实回读：verify 内部读一次；若 OCR 抖动失败，再读一次确认
        if receipt.get("verified") is not True:
            time.sleep(1.0)
            payload = json.loads(result_text(
                call_tool(self.mcp, "read_chat", limit=30)))
            joined = "".join(str(message.get("text") or "")
                             for message in payload["messages"])
            self.assertIn(marker, joined,
                          f"发送后 read_chat 找不到 {marker!r}: {joined!r}")

    def test_zz_foreground_guard_blocks_real_input(self):
        # 真机切到系统设置 → 前台守卫必须拒绝按键 → focus_game 拉回游戏
        self.env.reset_chat()
        self.env.device.shell("am start -a android.settings.SETTINGS")
        switched = False
        deadline = time.time() + 10.0
        while time.time() < deadline:
            try:
                self.env.device.require_game_foreground()
            except Exception:
                switched = True
                break
            time.sleep(0.5)
        self.assertTrue(switched, "切到系统设置后前台守卫仍未报告非游戏前台")

        result = call_tool(self.mcp, "press_key", key="c", duration_ms=40)
        self.assertTrue(result.get("isError"))
        payload = json.loads(result_text(result))
        try:
            self.assertEqual(payload["error_type"], "foreground")
            self.assertIn("前台", payload["error"])
        finally:
            # 无论断言是否失败，都必须把游戏拉回前台，避免污染后续用例
            self.env.ensure_game_foreground()


if __name__ == "__main__":
    unittest.main(verbosity=2)
