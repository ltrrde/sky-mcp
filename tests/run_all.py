"""tests/run_all.py — 一条命令跑全部单元测试（不需要模拟器）。

用法:
    python tests/run_all.py              # 全部
    python tests/run_all.py -v           # 详细输出
    python tests/run_all.py test_sky_adb # 只跑名字包含该子串的模块

测试全部用假 adb（tests/fake_adb.py）；numpy/opencv 是可选依赖，
没装时截图相关断言自动走 PNG 兜底分支。
"""

from __future__ import annotations

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in (ROOT, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)


def main(argv: list[str]) -> int:
    verbosity = 2 if "-v" in argv else 1
    selectors = [a for a in argv if not a.startswith("-")]
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    pattern = "test_*.py"
    for selector in selectors:
        pattern = f"*{selector}*.py"
        break
    discovered = loader.discover(HERE, pattern=pattern, top_level_dir=HERE)
    suite.addTests(discovered)
    runner = unittest.TextTestRunner(verbosity=verbosity)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
