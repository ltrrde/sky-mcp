"""tests/run_all.py — 一条命令跑全部测试（**真机环境**，无 fake/mock）。

测试直接操作当前正在运行的安卓模拟器 + 光遇 + 真实 rapidocr 引擎：
    python tests/run_all.py                  # 全部
    python tests/run_all.py -v               # 详细输出
    python tests/run_all.py real_mcp         # 只跑名字包含该子串的模块

前置条件（缺一不可）：
  1) 模拟器已启动，adb 能看到设备（默认 127.0.0.1:16384）；
  2) 光遇已登录并能进到可操作场景；
  3) ADBKeyboard 已安装（中文输入）；
  4) rapidocr-onnxruntime + numpy/opencv 可导入（本机开发可放 .deps/，
     见 tools/ocr_env.py；正式环境 python -m pip install -r requirements.txt）。

环境不满足时本脚本以退出码 2 结束并打印修复指引——不会 skip 后假装通过。
测试会真实操作游戏：开关聊天面板、真发 1 条带时间戳的测试消息、
切一次系统设置再 focus_game 拉回游戏；结束时会松键并还原输入法。
"""

from __future__ import annotations

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _path in (ROOT, HERE):
    if _path not in sys.path:
        sys.path.insert(0, _path)


def main(argv: list[str]) -> int:
    verbosity = 2 if "-v" in argv else 1
    selectors = [arg for arg in argv if not arg.startswith("-")]

    # 真机环境预检：设备/游戏/OCR 任何一个不满足都直接返回 2，不跑出假绿
    try:
        from real_env import preflight

        env = preflight()
    except Exception as exc:  # 环境错误统一提示
        print("=" * 72)
        print("真机测试环境未就绪，测试未运行：")
        print(exc)
        print("=" * 72)
        return 2

    status = env.device.status()
    screen = status.get("screen") or {}
    print(
        "真机环境就绪："
        f"设备 {status.get('serial')} | "
        f"屏幕 {screen.get('width')}x{screen.get('height')} | "
        f"OCR {env.ocr_status.get('ocr')}"
        f"(ready={env.ocr_status.get('ocr_ready')}, "
        f"load_ms={round(float(env.ocr_status.get('ocr_load_ms') or 0))})"
    )

    loader = unittest.TestLoader()
    pattern = "test_*.py"
    for selector in selectors:
        pattern = f"*{selector}*.py"
        break
    discovered = loader.discover(HERE, pattern=pattern, top_level_dir=HERE)
    suite = unittest.TestSuite()
    suite.addTests(discovered)
    runner = unittest.TextTestRunner(verbosity=verbosity)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
