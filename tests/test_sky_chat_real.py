"""tests/test_sky_chat_real.py — 真实聊天解析测试（真 OCR + 真截图 + 真机面板）。

不构造假 OCR items：所有输入都来自真实 rapidocr 对真机截图/固定真实截图的识别，
外加真机 read_chat 实时读取。
"""

from __future__ import annotations

import unittest

from real_env import get

import sky_chat
import sky_ocr

MESSAGE_FIELDS = {"speaker": str, "text": str, "confidence": (int, float),
                  "y": int, "is_self": bool, "kind": str}


class TestRealChatParsing(unittest.TestCase):
    def setUp(self):
        self.env.ensure_game_foreground()

    @classmethod
    def setUpClass(cls):
        cls.env = get()
        cls.engine = sky_ocr.OcrEngine("rapidocr")
        cls.engine.prewarm()
        cls.fixtures = cls.env.fixtures
        cls.image_size = (1920, 1080)

    def _fixture_items(self, region):
        result = self.engine.run(self.fixtures["chat_panel"], region=region,
                                 preprocess="chat")
        return result.texts()

    def assert_message_shape(self, messages):
        for message in messages:
            self.assertIsInstance(message, dict)
            for field, kind in MESSAGE_FIELDS.items():
                self.assertIn(field, message)
                self.assertIsInstance(message[field], kind)
            self.assertIn(message["kind"], ("panel", "bubble"))
            self.assertGreaterEqual(message["y"], 0)
        ys = [message["y"] for message in messages]
        self.assertEqual(ys, sorted(ys), "消息必须按 y 升序")

    def test_region_math_matches_real_screen(self):
        self.assertEqual(sky_chat.chat_region((3200, 1800)),
                         (0, 81, 1168, 1539))
        self.assertEqual(sky_chat.chat_region((1920, 1080)),
                         (0, 49, 701, 923))
        x, y, width, height = sky_chat.bubble_region((3200, 1800))
        self.assertGreater(width, 0)
        self.assertGreater(height, 0)
        self.assertLessEqual(x + width, 3200)
        self.assertLessEqual(y + height, 1800)
        self.assertEqual(len(sky_chat.CHAT_ROI_REL), 4)
        self.assertEqual(len(sky_chat.BUBBLE_ROI_REL), 4)

    def test_parse_real_panel_ocr_messages(self):
        items = self._fixture_items(sky_chat.chat_region(self.image_size))
        self.assertTrue(items, "真实 OCR 没有从聊天面板 ROI 识别到任何文字")
        messages = sky_chat.parse_chat_items(
            items, mode="panel", limit=20, image_size=self.image_size)
        self.assertIsInstance(messages, list)
        self.assert_message_shape(messages)
        self.assertIs(sky_chat.panel_looks_open(items, self.image_size), True)

    def test_parse_real_bubble_ocr_messages(self):
        items = self._fixture_items(sky_chat.bubble_region(self.image_size))
        self.assertTrue(items, "真实 OCR 没有从气泡 ROI 识别到任何文字")
        messages = sky_chat.parse_chat_items(
            items, mode="bubble", limit=20, image_size=self.image_size)
        self.assertTrue(messages,
                        f"真实气泡 OCR 没解析出消息: {[i['text'] for i in items]}")
        self.assert_message_shape(messages)
        self.assertTrue(all(message["kind"] == "bubble" for message in messages))
        self.assertTrue(all(message["is_self"] is False for message in messages))
        joined = "".join(message["text"] for message in messages)
        self.assertTrue("沐子" in joined or "小光" in joined,
                        f"真实气泡文本不符合预期: {joined}")

    def test_parse_floating_bubbles_from_real_fullscreen_ocr(self):
        # R2-1：全屏真实 OCR 里的头顶/漂浮气泡要被识别出来（不只左下 ROI）
        result = self.engine.run(self.fixtures["chat_panel"])  # 真实全屏 OCR
        items = result.texts()
        self.assertGreater(len(items), 2, "全屏真实 OCR 文字太少，无法验证")
        floating = sky_chat.parse_floating_bubbles(
            items, image_size=self.image_size)
        joined = "".join(message["text"] for message in floating)
        self.assertIn("沐子", joined, f"全屏气泡未识别出「沐子…」: {joined!r}")
        self.assertIn("小光", joined, f"全屏气泡未识别出「小光…」: {joined!r}")
        self.assertTrue(all(message["kind"] == "bubble"
                            for message in floating))
        self.assert_message_shape(floating)

        # 自己刚发的文本在气泡里 → is_self=True
        own = sky_chat.parse_floating_bubbles(
            items, own_texts=("小光来啦，测试一下聊天",),
            image_size=self.image_size)
        own_messages = [message for message in own if "小光" in message["text"]]
        self.assertTrue(own_messages)
        self.assertTrue(all(message["is_self"] for message in own_messages))

    def test_invalid_mode_rejected_by_real_parser(self):
        with self.assertRaises(ValueError):
            sky_chat.parse_chat_items([], mode="bogus")

    def test_live_panel_read_is_structured(self):
        self.env.ensure_panel_open()
        try:
            payload = self.env.controller.read_chat(limit=10)
            self.assertEqual(payload["ocr"], "rapidocr")
            self.assertGreater(payload["ocr_ms"], 0.0)
            self.assertIn("chat", payload["region"])
            self.assertIs(payload["panel_open"], True)
            self.assertIsInstance(payload["messages"], list)
            self.assert_message_shape(payload["messages"])
            self.assertGreater(payload["image_size"]["width"], 0)
            self.assertGreater(payload["image_size"]["height"], 0)
        finally:
            self.env.reset_chat()


if __name__ == "__main__":
    unittest.main(verbosity=2)
