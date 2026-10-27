"""tests/test_sky_ocr_real.py — 真实 OCR 引擎测试（真 rapidocr，无 fake engine）。

用真实 rapidocr-onnxruntime 识别仓库里真实真机截图（tests/fixtures/*.jpg），
并在真实设备截图（controller.capture_frame）上跑完整 OCR 链路。
"""

from __future__ import annotations

import unittest

from real_env import get

import sky_chat
import sky_ocr

EXPECTED_BANNER = "珍珠季的新任务现已开启"
EXPECTED_BUBBLE = "沐子在呢，一起去云野看云吗"


def char_hit_rate(expected: str, actual_texts: list[str]) -> float:
    """期望文本的字符命中率（OCR 允许少量错字，按字符集合算）。"""
    got = "".join(actual_texts)
    if not expected:
        return 0.0
    return sum(1 for char in set(expected) if char in got) / len(set(expected))


class TestRealOcrEngine(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = get()
        cls.engine = sky_ocr.OcrEngine("rapidocr")
        cls.status_before = cls.engine.status()
        cls.status_ready = cls.engine.prewarm()
        cls.fixtures = cls.env.fixtures
        cls.chat_image_size = (1920, 1080)

    def test_status_before_and_after_real_prewarm(self):
        before = self.status_before
        self.assertEqual(before["ocr"], "rapidocr")
        self.assertFalse(before["ocr_ready"])
        self.assertIn("rapidocr", before["ocr_available"])
        self.assertIn("rapidocr", before["ocr_chain"])

        ready = self.status_ready
        self.assertEqual(ready["ocr"], "rapidocr")
        self.assertTrue(ready["ocr_ready"])
        self.assertGreater(ready["ocr_load_ms"], 0.0)
        self.assertEqual(ready["ocr_error"], "")

    def test_banner_fixture_reads_real_chinese(self):
        result = self.engine.run(self.fixtures["world_banner"])
        self.assertEqual(result.image_size, self.chat_image_size)
        self.assertEqual(result.preprocess, "default")
        self.assertGreater(result.ocr_ms, 0.0)
        self.assertGreater(len(result.items), 0)
        texts = [item.text for item in result.items]
        self.assertGreaterEqual(char_hit_rate(EXPECTED_BANNER, texts), 0.8,
                                f"真实 OCR 未读出系统横幅：{texts}")
        for item in result.items:
            payload = item.as_dict()
            for key in ("text", "confidence", "x", "y", "width", "height"):
                self.assertIn(key, payload)

    def test_chat_region_coordinates_on_real_fixture(self):
        region = sky_chat.chat_region(self.chat_image_size)
        self.assertEqual(region, (0, 49, 701, 923))
        result = self.engine.run(self.fixtures["chat_panel"],
                                 region=region, preprocess="chat")
        self.assertEqual(result.image_size, self.chat_image_size)
        self.assertEqual(result.region, region)
        self.assertEqual(result.preprocess, "chat")
        self.assertGreater(result.ocr_ms, 0.0)
        for item in result.items:
            self.assertGreaterEqual(item.x, region[0])
            self.assertGreaterEqual(item.y, region[1])
            self.assertLessEqual(item.x, region[0] + region[2])
            self.assertLessEqual(item.y, region[1] + region[3])

    def test_chat_bubble_text_on_real_fixture(self):
        region = sky_chat.bubble_region(self.chat_image_size)
        result = self.engine.run(self.fixtures["chat_panel"],
                                 region=region, preprocess="chat")
        self.assertGreater(result.ocr_ms, 0.0)
        texts = [item.text for item in result.items]
        self.assertGreaterEqual(char_hit_rate(EXPECTED_BUBBLE, texts), 0.8,
                                f"真实 OCR 未读出世界气泡：{texts}")

    def test_live_screen_ocr_has_real_frame_metadata(self):
        frame = self.env.controller.capture_frame()
        result = self.engine.run(frame.png)
        self.assertEqual(result.image_size, (frame.width, frame.height))
        self.assertGreater(result.ocr_ms, 0.0)
        self.assertIsInstance(result.texts(), list)
        for item in result.texts():
            self.assertIsInstance(item["text"], str)
            self.assertIsInstance(item["confidence"], float)

    def test_unavailable_engine_reports_real_error(self):
        engine = sky_ocr.OcrEngine("none")
        status = engine.status()
        self.assertEqual(status["ocr"], "none")
        self.assertTrue(status["ocr_error"])
        with self.assertRaises(sky_ocr.OcrError) as ctx:
            engine.run(self.fixtures["world_banner"])
        self.assertIn("禁用", str(ctx.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
