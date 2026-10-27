"""sky_ocr.py — OCR 引擎抽象层（sky-mcp 感知层的统一 OCR 入口）。

设计目标（对应 mcp-requirements.md 的 R1 / R3）：

- **R1 引擎支持**：探测链 ``paddleocr → rapidocr → tesseract``，auto 逐级回退。
  本机（Windows + Python 3.14）实际可用的是 ``rapidocr-onnxruntime``；旧
  ``sky-mcp-server.py`` 只探测 Paddle/Tesseract，所以 ``status.ocr`` 一直是
  ``"none"``、``read_screen.texts`` 只有提示文案。
- **R1 错误可操作**：引擎加载失败时把「缺什么、怎么装」写进 ``ocr_error`` /
  ``OcrError``（例如 ``rapidocr: 未安装或导入失败（pip install
  rapidocr-onnxruntime）…``），多个候选失败用 ``; `` 连接并截断到 500 字内。
- **R3 诊断/性能**：``status()`` **不加载模型**（只用 ``importlib.util.find_spec``
  级探测），HTTP 启动时调用也很快、不会阻塞；``prewarm()`` 才真正加载
  （server 在后台线程调用，``load_ms``/``ocr_ready`` 反映加载状态）；
  ``run()`` 的 ``ocr_ms`` **只统计推理**（引擎 recognize 本身，不含解码/裁剪/
  预处理/等锁），便于 R3 按「≤3s」验收。工作图按模式限制最长边（default
  ≤2400px / chat ≤2000px），避免 3200x1800 全屏 2x、聊天 ROI 2.5x 后变成
  6000+ 像素大图导致 rapidocr 慢/占内存。
- **区域裁剪 + 坐标还原**：``run(..., region=(x, y, w, h))`` 接收**原图绝对像素**，
  内部裁剪/放大/增强后 OCR，返回的 item ``x``/``y`` 已还原到**原图坐标系**
  （含 region 偏移与缩放反算）。聊天面板解析（``sky_chat.parse_chat_items``）
  依赖这一点按 y 排序。
- **惰性导入**：模块 import 时只用标准库。``paddleocr`` / ``rapidocr_onnxruntime``
  / ``pytesseract`` / ``numpy`` / ``cv2`` 都在真正用到时才 import；没装任何 OCR
  依赖时 ``import sky_ocr`` 也不会失败，``status()`` 能返回清晰的安装提示。
- **线程安全**：HTTP MCP 可能并发调用 ``read_screen``/``read_chat``。引擎加载与
  推理分别用 ``threading.Lock`` 串行——onnxruntime / paddle 的 session 并发收益
  有限，多个推理同时跑更会抢 CPU/内存，实测容易把首帧拖慢。
- **降级策略**：没有 numpy/cv2 时不裁剪、不缩放，直接把原图路径交给引擎：
  ``region`` / ``scale`` 显式传值会抛可操作的 ``OcrError``（缺依赖），
  避免静默返回错误坐标。临时文件统一 ``tempfile`` 创建并在 ``run()`` 结束后清理。

常用用法::

    engine = OcrEngine()                    # 或 OcrEngine(engine="rapidocr")
    engine.prewarm()                        # 后台线程调用；阻塞到模型加载完
    result = engine.run(png_bytes, region=(0, 50, 650, 900), preprocess="chat")
    for item in result.items:               # x/y 为原图坐标（已含 region 偏移）
        ...
    engine.status()                         # 非阻塞：ocr / ocr_device / ...

单测注入（``tests/test_sky_ocr.py`` 用，不依赖真实 OCR）::

    OcrEngine(engine="rapidocr", factories={"rapidocr": lambda: FakeBackend()})

``factories`` 是 ``name → 无参工厂``，工厂返回对象的契约见 ``_FactoryBackend``。
"""

from __future__ import annotations

import importlib.util
import math
import os
import struct
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

__all__ = [
    "DEFAULT_CHAIN",
    "PREPROCESS_MODES",
    "OcrEngine",
    "OcrError",
    "OcrItem",
    "OcrResult",
]

#: auto 模式下的引擎探测顺序（mcp-requirements.md R1 建议顺序）
DEFAULT_CHAIN: tuple[str, ...] = ("paddleocr", "rapidocr", "tesseract")

#: 支持的前处理模式：default=全屏通用，chat=聊天小字专用，none=原图直出
PREPROCESS_MODES: tuple[str, ...] = ("default", "chat", "none")

#: preprocess → 默认放大倍数（R3：default 2x；chat 聊天小字需要 ≥2.5x）
_PREPROCESS_SCALE: dict[str, float] = {"default": 2.0, "chat": 2.5, "none": 1.0}

#: 工作图最长边上限（按 preprocess 模式）：放大后超过就等比压回。
#: Lead 实测建议（R3 性能）：3200x1800 截图做 default(2x) 是 6400x3600、
#: 聊天 ROI 1088x1610 做 chat(2.5x) 是 2720x4025，rapidocr 都会很慢且占内存。
#: 因此 crop 后放大时限制：default ≤1700、chat ≤2000；none 一般 1:1，
#: 显式 scale 时用 5120 防超大图。实际生效缩放记录在坐标反算里。
#: （1700 是 Lead 在 1920x1080 真机帧上实测的平衡点：识别条数不变，
#:   预热后单次 OCR 约 1.0~1.3s；2100/2400 时偶发 >3s 会让 R3 验收抖动，
#:   1900 在机器繁忙时仍偶发 3s+，再降一档留余量。）
_MAX_WORK_SIDE_BY_MODE: dict[str, int] = {
    "default": 1700,
    "chat": 2000,
    "none": 5120,
}
#: 不在上表里的模式 / 兜底上限
_MAX_WORK_SIDE = 5120

#: status.ocr_error / OcrError 摘要长度上限（接口契约：≤500 字）
_ERROR_LIMIT = 500

#: 探测链逻辑名 → 需要 import 的 Python 包名
_ENGINE_MODULES: dict[str, str] = {
    "paddleocr": "paddleocr",
    "rapidocr": "rapidocr_onnxruntime",
    "tesseract": "pytesseract",
}

#: 安装提示：必须让调用方/用户能直接照做（R1：ocr_error 要可操作）
_INSTALL_HINTS: dict[str, str] = {
    "paddleocr": (
        "pip install paddleocr paddlepaddle"
        "（Python 3.14 下可能没有 wheel；可改用 rapidocr: "
        "pip install rapidocr-onnxruntime）"
    ),
    "rapidocr": "pip install rapidocr-onnxruntime",
    "tesseract": (
        "pip install pytesseract，并安装 Tesseract-OCR 且把 tesseract.exe 加入 PATH"
        "（中文识别需要 chi_sim 语言包）"
    ),
}

#: 显式 engine 名称的别名（用户配置/环境变量里可能写别的名字）
_ENGINE_ALIASES: dict[str, str] = {
    "auto": "auto",
    "paddle": "paddleocr",
    "ppocr": "paddleocr",
    "rapid": "rapidocr",
    "rapidocr-onnxruntime": "rapidocr",
    "pytesseract": "tesseract",
    "tess": "tesseract",
    "off": "none",
    "disabled": "none",
}


# ===================== 数据结构 =====================


class OcrError(RuntimeError):
    """OCR 不可用 / 识别失败。消息必须包含可操作的安装或排查提示。"""


@dataclass
class OcrItem:
    """一行 OCR 结果。x/y/width/height 均相对于「原图」像素坐标。"""

    text: str
    confidence: float
    x: int
    y: int
    width: int = 0
    height: int = 0

    def as_dict(self) -> dict[str, Any]:
        """转成 MCP JSON（read_screen.texts / read_chat.messages 的基本条目）。"""
        return {
            "text": self.text,
            "confidence": round(float(self.confidence), 4),
            "x": int(self.x),
            "y": int(self.y),
            "width": int(self.width),
            "height": int(self.height),
        }


@dataclass
class OcrResult:
    """一次 OCR 调用的完整结果（含引擎/耗时/区域等元信息）。"""

    items: list[OcrItem] = field(default_factory=list)
    #: 实际/计划引擎名：paddleocr-mobile-<device> / rapidocr / tesseract / none
    engine: str = "none"
    #: 推理设备：cpu / gpu:0 / unknown
    device: str = "unknown"
    #: 空串=无错误；引擎不可用时为可操作的原因摘要
    error: str = ""
    #: 本次 run() 总耗时（毫秒，含解码/裁剪/预处理/推理）
    ocr_ms: float = 0.0
    #: 引擎加载耗时（毫秒；已加载过则沿用首次加载值）
    load_ms: float = 0.0
    preprocess: str = "default"
    #: 原图尺寸 (width, height)，不是裁剪/放大后的尺寸
    image_size: tuple[int, int] = (0, 0)
    #: 原图绝对裁剪区域 (x, y, w, h)；None 表示全图
    region: tuple[int, int, int, int] | None = None

    def texts(self) -> list[dict[str, Any]]:
        """``[item.as_dict() for item in items]``，直接塞进 MCP 返回 JSON。"""
        return [item.as_dict() for item in self.items]


# ===================== 通用小工具（纯标准库） =====================


def _truncate(text: str, limit: int = _ERROR_LIMIT) -> str:
    """压平空白并截断到 limit 字（契约要求 ocr_error ≤500 字）。"""
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)] + "…"


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(number) or math.isinf(number):
        return default
    return number


def _importable(module_name: str) -> bool:
    """只做 find_spec 级探测（不执行 import、不加载模型，status() 专用）。

    find_spec 对不存在的包很快；某些损坏的安装/命名空间包会抛异常，这里统一
    当作「不可用」处理，避免 status() 因为环境问题直接炸掉。
    """
    try:
        return importlib.util.find_spec(module_name) is not None
    except (ImportError, AttributeError, ValueError, TypeError):
        return False


#: numpy/cv2 惰性加载缓存（True=未探测；测试可把 ``_CV_CACHE[0]`` 改成
#: ``(None, None)`` 模拟「没有图像库」的降级分支，用完记得还原）。
_CV_CACHE: list[Any] = [object()]


def _load_cv() -> tuple[Any, Any]:
    """惰性加载 cv2/numpy；缺任一返回 ``(None, None)``（纯路径模式兜底）。"""
    cached = _CV_CACHE[0]
    if cached is not None and not (isinstance(cached, tuple) and len(cached) == 2):
        try:
            import cv2  # noqa: PLC0415 - 惰性导入是硬性要求
            import numpy as np  # noqa: PLC0415
        except Exception:
            cv2, np = None, None
        _CV_CACHE[0] = (cv2, np)
    return _CV_CACHE[0]


def _png_size_from_bytes(data: bytes) -> tuple[int, int] | None:
    """从 PNG 头解析尺寸（不依赖任何图像库）。"""
    if (len(data) >= 24
            and data[:8] == b"\x89PNG\r\n\x1a\n"
            and data[12:16] == b"IHDR"):
        width, height = struct.unpack(">II", data[16:24])
        if 0 < width <= 20000 and 0 < height <= 20000:
            return int(width), int(height)
    return None


def _jpeg_size_from_bytes(data: bytes) -> tuple[int, int] | None:
    """从 JPEG 的 SOF 段解析尺寸（纯标准库，给无 cv2 环境兜底）。"""
    if not data.startswith(b"\xff\xd8"):
        return None
    index = 2
    length = len(data)
    while index + 9 < length:
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        # 无长度字段的标记：SOI/EOI/RSTn/TEM
        if marker in (0xD8, 0xD9, 0x01) or 0xD0 <= marker <= 0xD7:
            index += 2
            continue
        segment_length = int.from_bytes(data[index + 2:index + 4], "big")
        if segment_length < 2:
            return None
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                      0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            height = int.from_bytes(data[index + 5:index + 7], "big")
            width = int.from_bytes(data[index + 7:index + 9], "big")
            if 0 < width <= 20000 and 0 < height <= 20000:
                return int(width), int(height)
            return None
        index += 2 + segment_length
    return None


def _probe_image_size(path: str) -> tuple[int, int]:
    """无 cv2 时从文件头拿原图尺寸；解析失败返回 (0, 0)（不影响引擎调用）。"""
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError:
        return (0, 0)
    return _png_size_from_bytes(data) or _jpeg_size_from_bytes(data) or (0, 0)


def _guess_suffix(data: bytes) -> str:
    """按文件魔数给临时文件挑后缀（部分引擎/解码器依赖后缀）。"""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8"):
        return ".jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data.startswith(b"BM"):
        return ".bmp"
    return ".png"


def _write_temp_file(data: bytes, temp_paths: list[str], suffix: str = ".png") -> str:
    """写一个临时文件并登记路径（调用方负责在 finally 里清理）。"""
    handle = tempfile.NamedTemporaryFile(
        prefix="sky-ocr-", suffix=suffix, delete=False)
    try:
        with handle:
            handle.write(data)
    except Exception:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise
    temp_paths.append(handle.name)
    return handle.name


def _normalize_engine_name(value: str | None) -> str | None:
    """engine 参数/环境变量归一化：None/auto → None（自动），其余转小写别名。"""
    if value is None:
        return None
    name = str(value).strip().lower()
    if not name:
        return None
    name = _ENGINE_ALIASES.get(name, name)
    return None if name == "auto" else name


def _missing_error(name: str) -> str:
    """缺包/缺可执行文件时的可操作错误文案。"""
    hint = _INSTALL_HINTS.get(name)
    if hint:
        return f"{name}: 未安装或不可用（{hint}）"
    return (
        f"{name}: 未知 OCR 引擎（可选：paddleocr / rapidocr / tesseract / "
        f"auto / none；测试可用 factories 注入）"
    )


def _install_error(name: str, exc: BaseException) -> str:
    """import/初始化抛异常时的可操作错误文案。"""
    hint = _INSTALL_HINTS.get(name)
    detail = f"{type(exc).__name__}: {exc}"
    if hint:
        return f"{name}: 未安装或导入失败（{hint}）；{detail}"
    return f"{name}: 未安装或导入失败；{detail}"


def _planned_display_name(name: str) -> str:
    """未加载但可 import 时，status.ocr 显示的「首选候选名」。"""
    return {
        "paddleocr": "paddleocr-mobile-unknown",
        "rapidocr": "rapidocr",
        "tesseract": "tesseract",
    }.get(name, name)


def _planned_device(name: str) -> str:
    """未加载时能确定的设备名：rapidocr/tesseract 一定跑 CPU，其余 unknown。"""
    return {"rapidocr": "cpu", "tesseract": "cpu"}.get(name, "unknown")


def _validate_region_arg(region: Any) -> tuple[int, int, int, int] | None:
    """校验 region 参数形态（结构/正宽高）；真正的越界裁剪在 _prepare 里做。"""
    if region is None:
        return None
    try:
        values = tuple(region)
    except TypeError as exc:
        raise OcrError(
            f"region 需要 4 个整数 (x, y, w, h)，收到 {region!r}") from exc
    if len(values) != 4:
        raise OcrError(
            f"region 需要 4 个整数 (x, y, w, h)，收到 {region!r}")
    try:
        x, y, width, height = (int(round(float(value))) for value in values)
    except (TypeError, ValueError) as exc:
        raise OcrError(
            f"region 需要 4 个整数 (x, y, w, h)，收到 {region!r}") from exc
    if width <= 0 or height <= 0:
        raise OcrError(f"region 的宽高必须 >0，收到 {region!r}")
    return (x, y, width, height)


def _validate_scale_arg(scale: Any) -> float | None:
    if scale is None:
        return None
    try:
        value = float(scale)
    except (TypeError, ValueError) as exc:
        raise OcrError(f"scale 需要正数，收到 {scale!r}") from exc
    if not math.isfinite(value) or value <= 0:
        raise OcrError(f"scale 需要有限正数，收到 {scale!r}")
    return value


def _clip_region(region: tuple[int, int, int, int],
                 image_size: tuple[int, int]) -> tuple[int, int, int, int]:
    """把原图绝对 region 裁到图像范围内（越界部分直接裁掉，不放大）。"""
    x, y, width, height = region
    image_width, image_height = image_size
    left = max(0, x)
    top = max(0, y)
    right = min(image_width, x + width)
    bottom = min(image_height, y + height)
    if right <= left or bottom <= top:
        raise OcrError(
            f"region {region!r} 与图像 {image_size} 没有交集")
    return (left, top, right - left, bottom - top)


# ===================== OCR 结果解析（各引擎原生格式 → OcrItem） =====================


def _points_from_box(box: Any) -> list[tuple[float, float]]:
    """框转四点。兼容四个点 [[x,y],...] 与矩形 [x1,y1,x2,y2] / numpy 数组。"""
    if box is None:
        return []
    if hasattr(box, "tolist"):
        try:
            box = box.tolist()
        except Exception:
            return []
    try:
        if isinstance(box, (list, tuple)) and box:
            first = box[0]
            if isinstance(first, (list, tuple)) and len(first) >= 2:
                return [(float(point[0]), float(point[1]))
                        for point in box
                        if isinstance(point, (list, tuple)) and len(point) >= 2]
            if len(box) >= 4:
                x1, y1, x2, y2 = (float(box[index]) for index in range(4))
                return [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]
    except (TypeError, ValueError):
        return []
    return []


def _looks_like_rapidocr_payload(data: Any) -> bool:
    """判断 rapidocr 返回的 ``(result, elapse)`` 里的 result 形态。"""
    if data is None:
        return True
    if not isinstance(data, (list, tuple)):
        return False
    if not data:
        return True
    first = data[0]
    return (isinstance(first, (list, tuple))
            and len(first) >= 2
            and isinstance(first[1], str))


def _parse_rapidocr_result(result: Any) -> list[OcrItem]:
    """解析 rapidocr 结果，兼容 ``(result, elapse)`` 与直接 list 两种返回值。

    rapidocr (rapidocr-onnxruntime 1.x) 返回 ``(result, elapse)``：
    result 为 ``[[box, text, score], ...]``（也可能为 None）；部分包装/新版本
    直接返回 list。这里两种都吃掉。
    """
    data = result
    if isinstance(data, tuple) and len(data) == 2:
        data = data[0]
    elif (isinstance(data, list) and len(data) == 2
          and isinstance(data[1], (list, tuple))
          and _looks_like_rapidocr_payload(data[0])
          and not isinstance(data[1], str)):
        # 少数版本把 (result, elapse) 包成 list；elapse 是数字列表
        if all(isinstance(value, (int, float)) for value in data[1]):
            data = data[0]
    if data is None:
        return []
    if isinstance(data, tuple):
        data = list(data)
    if not isinstance(data, list):
        return []
    items: list[OcrItem] = []
    for entry in data:
        if not isinstance(entry, (list, tuple)) or len(entry) < 2:
            continue
        box, text = entry[0], entry[1]
        score = entry[2] if len(entry) >= 3 else 0.0
        clean = str(text).strip()
        points = _points_from_box(box)
        if not clean or not points:
            continue
        xs = [point[0] for point in points]
        ys = [point[1] for point in points]
        items.append(OcrItem(
            text=clean,
            confidence=_as_float(score),
            x=int(round(min(xs))),
            y=int(round(min(ys))),
            width=int(round(max(xs) - min(xs))),
            height=int(round(max(ys) - min(ys))),
        ))
    return items


def _parse_paddle_result(result: Any) -> list[OcrItem]:
    """解析 PaddleOCR 结果，迁移旧 server ``_run_paddle_ocr`` 的两种格式兼容。

    - PaddleOCR 3.x：``[{"rec_texts": [...], "rec_scores": [...],
      "rec_polys"/"dt_polys": [...]}]``（dict 页）；
    - PaddleOCR 2.x：``[[[box, (text, score)], ...]]``（外层 list，取 [0] 行列表）。
    """
    if not result:
        return []
    if isinstance(result, dict):
        pages: list[Any] | None = [result]
    elif isinstance(result, (list, tuple)) and result and isinstance(result[0], dict):
        pages = list(result)
    else:
        pages = None

    items: list[OcrItem] = []
    if pages is not None:
        for page in pages:
            if not isinstance(page, dict):
                continue
            rec_texts = page.get("rec_texts") or []
            rec_scores = page.get("rec_scores") or []
            rec_boxes = page.get("rec_polys") or page.get("dt_polys") or []
            for index, text in enumerate(rec_texts):
                clean = str(text).strip()
                if not clean:
                    continue
                box = rec_boxes[index] if index < len(rec_boxes) else None
                points = _points_from_box(box)
                if points:
                    xs = [point[0] for point in points]
                    ys = [point[1] for point in points]
                    x = int(round(min(xs)))
                    y = int(round(min(ys)))
                    width = int(round(max(xs) - min(xs)))
                    height = int(round(max(ys) - min(ys)))
                else:
                    x = y = width = height = 0
                score = (_as_float(rec_scores[index])
                         if index < len(rec_scores) else 0.0)
                items.append(OcrItem(
                    text=clean, confidence=score,
                    x=x, y=y, width=width, height=height))
        return items

    # PaddleOCR 2.x：result[0] 是行列表（无文字时可能是 None）
    lines = result[0] if isinstance(result, (list, tuple)) and result else None
    if lines is None:
        return []
    for line in lines:
        if not isinstance(line, (list, tuple)) or len(line) < 2:
            continue
        box, payload = line[0], line[1]
        if isinstance(payload, (list, tuple)) and payload:
            clean = str(payload[0]).strip()
            score = _as_float(payload[1]) if len(payload) > 1 else 0.0
        else:
            clean = str(payload).strip()
            score = 0.0
        points = _points_from_box(box)
        if not clean or not points:
            continue
        xs = [point[0] for point in points]
        ys = [point[1] for point in points]
        items.append(OcrItem(
            text=clean,
            confidence=score,
            x=int(round(min(xs))),
            y=int(round(min(ys))),
            width=int(round(max(xs) - min(xs))),
            height=int(round(max(ys) - min(ys))),
        ))
    return items


def _normalize_injected_items(raw: Any, *, engine: str = "fake") -> list[OcrItem]:
    """把测试注入引擎的返回值归一化成 ``OcrItem`` 列表。

    支持的条目形式：
    - ``OcrItem``；
    - dict（text/confidence/x/y/width/height，confidence 也可写 score）；
    - rapidocr 风格 ``[box, text, score]``；
    - 简化元组 ``(text, confidence, x, y, width, height)``。
    """
    if raw is None:
        return []
    if not isinstance(raw, (list, tuple)):
        raise OcrError(
            f"{engine}: 注入引擎返回值需要 list，收到 {type(raw).__name__}")
    items: list[OcrItem] = []
    for entry in raw:
        if isinstance(entry, OcrItem):
            items.append(entry)
            continue
        if isinstance(entry, dict):
            items.append(OcrItem(
                text=str(entry.get("text", "")),
                confidence=_as_float(entry.get("confidence",
                                                entry.get("score", 0.0))),
                x=int(entry.get("x", 0)),
                y=int(entry.get("y", 0)),
                width=int(entry.get("width", 0)),
                height=int(entry.get("height", 0)),
            ))
            continue
        if isinstance(entry, (list, tuple)):
            if len(entry) >= 3 and isinstance(entry[1], str):
                items.extend(_parse_rapidocr_result([entry]))
                continue
            if entry and isinstance(entry[0], str):
                values = list(entry) + [0] * max(0, 6 - len(entry))
                text, confidence, x, y, width, height = values[:6]
                items.append(OcrItem(
                    text=str(text), confidence=_as_float(confidence),
                    x=int(x), y=int(y),
                    width=int(width), height=int(height)))
                continue
        raise OcrError(f"{engine}: 无法识别的注入 item：{entry!r}")
    return items


# ===================== 具体引擎后端 =====================


class _Backend:
    """引擎后端基类：加载成功后的对象都要能 ``recognize(image) -> list[OcrItem]``。"""

    name = "none"
    #: status.ocr / OcrResult.engine 显示名（paddleocr 会带设备名）
    display_name = "none"
    device = "unknown"
    #: True 表示引擎只接受文件路径（目前只有 tesseract）
    requires_path = False

    def recognize(self, image: Any, *, image_size: tuple[int, int] | None = None
                  ) -> list[OcrItem]:
        raise NotImplementedError


class _RapidOcrBackend(_Backend):
    """rapidocr-onnxruntime（本机唯一可用的 OCR，R1 的核心新增引擎）。"""

    name = "rapidocr"
    display_name = "rapidocr"
    device = "cpu"
    requires_path = False

    def __init__(self) -> None:
        try:
            from rapidocr_onnxruntime import RapidOCR  # noqa: PLC0415 - 惰性导入
        except Exception as exc:
            raise OcrError(_install_error("rapidocr", exc)) from exc
        self._rapid_ocr_cls = RapidOCR
        try:
            self._engine = RapidOCR()
        except Exception as exc:
            raise OcrError(
                f"rapidocr: RapidOCR() 初始化失败（{type(exc).__name__}: {exc}）；"
                f"通常是模型文件缺失，重装：{_INSTALL_HINTS['rapidocr']}"
            ) from exc

    def recognize(self, image: Any, *,
                  image_size: tuple[int, int] | None = None) -> list[OcrItem]:
        try:
            raw = self._engine(image)
        except Exception as exc:
            raise OcrError(
                f"rapidocr: 识别失败（{type(exc).__name__}: {exc}）") from exc
        return _parse_rapidocr_result(raw)


class _PaddleOcrBackend(_Backend):
    """PaddleOCR（本机未安装；保留能力，供有 GPU/已装 paddle 的环境使用）。"""

    name = "paddleocr"
    display_name = "paddleocr-mobile-unknown"
    device = "unknown"
    requires_path = False

    def __init__(self) -> None:
        # 与旧 server 行为一致：关掉 paddle 启动时的联网模型源检查 / mkldnn 噪音
        os.environ.setdefault("PADDLE_PDX_ENABLE_MKLDNN_BYDEFAULT", "0")
        os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
        try:
            from paddleocr import PaddleOCR  # noqa: PLC0415 - 惰性导入
        except Exception as exc:
            raise OcrError(_install_error("paddleocr", exc)) from exc

        self.device = "unknown"
        try:
            import paddle  # noqa: PLC0415 - 惰性导入
            try:
                if paddle.device.is_compiled_with_cuda():
                    paddle.set_device("gpu:0")
            except Exception:
                pass
            self.device = str(paddle.get_device())
        except Exception:
            pass
        self.display_name = f"paddleocr-mobile-{self.device}"

        init_errors: list[str] = []
        # 3.x 移除了 use_angle_cls 等参数，按「新→旧」顺序尝试，最大化兼容
        for kwargs in (
            {"use_angle_cls": False, "lang": "ch", "det_limit_side_len": 1920},
            {"use_angle_cls": False, "lang": "ch"},
            {"lang": "ch"},
        ):
            try:
                self._engine = PaddleOCR(**kwargs)
                break
            except Exception as exc:
                init_errors.append(f"{type(exc).__name__}: {exc}")
        else:
            raise OcrError(
                f"paddleocr: PaddleOCR 初始化失败（{init_errors[-1] if init_errors else '未知原因'}）；"
                f"检查 paddlepaddle 与模型文件：{_INSTALL_HINTS['paddleocr']}")

    def recognize(self, image: Any, *,
                  image_size: tuple[int, int] | None = None) -> list[OcrItem]:
        try:
            raw = self._engine.ocr(image, cls=True)
        except Exception as first_exc:
            try:
                raw = self._engine.ocr(image)
            except Exception as exc:
                raise OcrError(
                    f"paddleocr: 识别失败（{type(exc).__name__}: {exc}；"
                    f"cls=True 也失败：{type(first_exc).__name__}: {first_exc}）"
                ) from exc
        return _parse_paddle_result(raw)


class _TesseractBackend(_Backend):
    """pytesseract + 本机 Tesseract-OCR（无坐标，按行序生成递增 y）。"""

    name = "tesseract"
    display_name = "tesseract"
    device = "cpu"
    requires_path = True
    #: 中英文混合（光遇聊天常见中英数混排）
    lang = "chi_sim+eng"

    def __init__(self) -> None:
        try:
            import pytesseract  # noqa: PLC0415 - 惰性导入
        except Exception as exc:
            raise OcrError(_install_error("tesseract", exc)) from exc
        try:
            version = pytesseract.get_tesseract_version()
        except Exception as exc:
            raise OcrError(
                f"tesseract: 找不到 Tesseract-OCR 可执行文件（{type(exc).__name__}: {exc}）；"
                f"{_INSTALL_HINTS['tesseract']}") from exc
        self._pytesseract = pytesseract
        self.version = str(version)

    def recognize(self, image: Any, *,
                  image_size: tuple[int, int] | None = None) -> list[OcrItem]:
        try:
            text = self._pytesseract.image_to_string(
                str(image), lang=self.lang)
        except Exception as exc:
            raise OcrError(
                f"tesseract: 识别失败（{type(exc).__name__}: {exc}）；"
                f"检查 chi_sim 语言包与图片路径：{_INSTALL_HINTS['tesseract']}"
            ) from exc
        lines = [line.strip() for line in str(text).splitlines() if line.strip()]
        if not lines:
            return []
        # tesseract 默认只给纯文本；按行序 * 行高生成递增 y，保证聊天解析
        # 按 y 排序时顺序正确（真实坐标不可得，置信度固定 0.5）。
        height = int(image_size[1]) if image_size else 0
        line_height = max(1, int(round(height / len(lines)))) if height else 20
        return [
            OcrItem(text=line, confidence=0.5, x=0,
                    y=index * line_height, width=0, height=line_height)
            for index, line in enumerate(lines)
        ]


class _FactoryBackend(_Backend):
    """测试/调试注入的假引擎包装（真正加载时调用一次工厂）。

    契约（tests/test_sky_ocr.py 依赖，外部调试也可用）：

    - ``factories[name]`` 是**无参工厂**，只在 prewarm/run 首次加载时调用一次；
    - 工厂返回对象 ``impl``，可以是：
      有 ``recognize(image)`` 的对象 / 有 ``run(image)`` 的对象 / 可调用对象；
    - ``recognize`` 返回 ``list``，每项支持 ``OcrItem``、dict、rapidocr 风格
      ``[box, text, score]`` 或 ``(text, confidence, x, y, width, height)``；
    - impl 可选属性：``display_name``（status 显示名）、``device``、
      ``requires_path``（True 时走「预处理 → 临时 PNG → 引擎」路径）。
    """

    def __init__(self, name: str, factory: Callable[[], Any]) -> None:
        self.name = name
        try:
            impl = factory()
        except OcrError:
            raise
        except Exception as exc:
            raise OcrError(
                f"{name}: 注入引擎初始化失败（{type(exc).__name__}: {exc}）"
            ) from exc
        if impl is None:
            raise OcrError(f"{name}: 注入引擎工厂返回了 None")
        self._impl = impl
        self.display_name = str(getattr(impl, "display_name", name) or name)
        self.device = str(getattr(impl, "device", "cpu") or "cpu")
        self.requires_path = bool(getattr(impl, "requires_path", False))

    def recognize(self, image: Any, *,
                  image_size: tuple[int, int] | None = None) -> list[OcrItem]:
        impl = self._impl
        if hasattr(impl, "recognize"):
            raw = impl.recognize(image)
        elif hasattr(impl, "run"):
            raw = impl.run(image)
        elif callable(impl):
            raw = impl(image)
        else:
            raise OcrError(
                f"{self.name}: 注入引擎对象不支持 recognize/run/__call__")
        return _normalize_injected_items(raw, engine=self.name)


# ===================== 工作图准备（解码 / 裁剪 / 缩放 / 增强） =====================


@dataclass
class _Prepared:
    """一次 run() 的工作图上下文（用完必须 cleanup 临时文件）。"""

    image_size: tuple[int, int]
    region: tuple[int, int, int, int] | None
    work_size: tuple[int, int]
    scale_x: float
    scale_y: float
    offset: tuple[int, int]
    engine_input: Any
    temp_paths: list[str] = field(default_factory=list)

    def cleanup(self) -> None:
        for path in self.temp_paths:
            try:
                os.unlink(path)
            except OSError:
                pass
        self.temp_paths.clear()


def _map_items(items: Iterable[OcrItem], prepared: _Prepared) -> list[OcrItem]:
    """把工作图坐标还原到原图坐标，并按 y/x 升序（聊天面板从上到下）。"""
    offset_x, offset_y = prepared.offset
    scale_x = prepared.scale_x or 1.0
    scale_y = prepared.scale_y or 1.0
    mapped: list[OcrItem] = []
    for item in items:
        mapped.append(OcrItem(
            text=item.text,
            confidence=item.confidence,
            x=int(round(offset_x + item.x / scale_x)),
            y=int(round(offset_y + item.y / scale_y)),
            width=int(round(item.width / scale_x)),
            height=int(round(item.height / scale_y)),
        ))
    mapped.sort(key=lambda entry: (entry.y, entry.x))
    return mapped


# ===================== 引擎管理器 =====================


class OcrEngine:
    """OCR 引擎探测器 + 统一调用入口。

    探测顺序::

        auto:  paddleocr → rapidocr → tesseract
        显式:  engine="rapidocr"（构造参数优先于环境变量 SKY_OCR_ENGINE）
        none:  禁用 OCR（status.ocr="none"，run() 抛 OcrError）

    ``status()`` 永远不加载模型；``prewarm()`` 才真正加载（可重复调用，幂等）。
    """

    def __init__(self, engine: str | None = None, *,
                 factories: dict[str, Callable[[], Any]] | None = None,
                 chain: Iterable[str] | None = None) -> None:
        # 构造参数 > 环境变量 SKY_OCR_ENGINE > auto（None 表示自动）
        env_engine = os.environ.get("SKY_OCR_ENGINE")
        requested = engine if engine is not None else env_engine
        self._requested: str | None = _normalize_engine_name(requested)
        self._forced: str | None = self._requested  # None=auto；"none"=禁用

        base_chain = (tuple(str(name).strip().lower() for name in chain)
                      if chain is not None else DEFAULT_CHAIN)
        self._factories: dict[str, Callable[[], Any]] = {
            str(key).strip().lower(): value
            for key, value in (factories or {}).items()
        }
        # 显式指定的引擎 / 注入的自定义引擎如果不在默认链里，追加到链尾，
        # 保证 status().ocr_chain / ocr_available 能反映真实候选。
        extra: list[str] = []
        if self._forced not in (None, "none") and self._forced not in base_chain:
            extra.append(self._forced)
        for name in self._factories:
            if name not in base_chain and name not in extra:
                extra.append(name)
        self._chain: tuple[str, ...] = base_chain + tuple(extra)

        self._backend: _Backend | None = None
        self._display_name = "none"
        self._device = "unknown"
        self._loaded = False
        self._load_ms = 0.0
        self._last_error = ""
        # 推理锁：并发 HTTP 请求串行推理；加载锁：避免重复加载/加载期间状态撕裂
        self._infer_lock = threading.Lock()
        self._load_lock = threading.Lock()
        self._state_lock = threading.Lock()

    # ---- 探测（不加载模型） ----

    def _candidates(self) -> tuple[str, ...]:
        """本次探测要尝试的候选引擎（显式指定时只试那一个）。"""
        if self._forced == "none":
            return ()
        if self._forced:
            return (self._forced,)
        return tuple(self._chain)

    def _detect_available(self) -> list[str]:
        """find_spec 级探测：哪些候选「理论上可 import」（不加载模型）。"""
        available: list[str] = []
        for name in self._candidates():
            if name in self._factories:
                available.append(name)
                continue
            module = _ENGINE_MODULES.get(name)
            if module and _importable(module):
                available.append(name)
        return available

    def _probe_error(self) -> str:
        """所有候选都不可用时的可操作原因汇总（≤500 字）。"""
        parts: list[str] = []
        for name in self._candidates():
            if name in self._factories:
                continue
            module = _ENGINE_MODULES.get(name)
            if module is None:
                parts.append(
                    f"{name}: 未知 OCR 引擎（可选：paddleocr / rapidocr / "
                    f"tesseract / auto / none）")
            elif not _importable(module):
                parts.append(_missing_error(name))
        if not parts:
            parts = ["未找到可用的 OCR 引擎（探测链：paddleocr → rapidocr → tesseract）"]
        return _truncate("; ".join(parts))

    # ---- status（必须快且非阻塞） ----

    def status(self) -> dict[str, Any]:
        """OCR 状态快照。**不加载模型**，可安全地在 HTTP 启动/每次工具调用时用。

        字段严格对齐冻结接口契约：``ocr`` / ``ocr_engine`` / ``ocr_device`` /
        ``ocr_error`` / ``ocr_ready`` / ``ocr_load_ms`` / ``ocr_chain`` /
        ``ocr_available``。
        """
        with self._state_lock:
            loaded = self._loaded
            display_name = self._display_name
            device = self._device
            load_ms = self._load_ms
            last_error = self._last_error
        available = self._detect_available()

        if loaded:
            name = display_name
            device_name = device or "unknown"
            error = ""  # 契约：加载/探测成功时为空
        elif self._forced == "none":
            name = "none"
            device_name = "unknown"
            error = last_error or "OCR 已被禁用（engine='none'）"
        elif last_error:
            # 已尝试加载但全部失败：不要假装有引擎，老实报 none + 原因
            name = "none"
            device_name = "unknown"
            error = last_error
        elif available:
            # 还没加载：报「首选候选名」，ocr_ready 仍是 False（R3：预热状态）
            name = _planned_display_name(available[0])
            device_name = _planned_device(available[0])
            error = ""
        else:
            name = "none"
            device_name = "unknown"
            error = self._probe_error() or last_error

        return {
            "ocr": name,
            "ocr_engine": name,
            "ocr_device": device_name,
            "ocr_error": error,
            "ocr_ready": loaded,
            "ocr_load_ms": round(float(load_ms), 3),
            "ocr_chain": list(self._chain),
            "ocr_available": available,
        }

    # ---- 加载（阻塞；server 后台线程预热） ----

    def prewarm(self) -> dict[str, Any]:
        """立即加载引擎（阻塞），返回最新 ``status()``。

        失败不抛异常而是把原因放进 ``status().ocr_error``——HTTP 启动预热线程
        里抛异常没人接，调用方应该能从 status 读到「该装什么」。
        """
        if self._loaded:
            return self.status()
        with self._load_lock:
            if self._loaded:  # 等锁期间别的线程可能已经加载好了
                return self.status()
            started = time.perf_counter()
            errors: list[str] = []
            candidates = self._candidates()
            if not candidates:
                with self._state_lock:
                    self._load_ms = (time.perf_counter() - started) * 1000.0
                    self._last_error = "OCR 已被禁用（engine='none'）"
                return self.status()
            for name in candidates:
                try:
                    backend = self._load_backend(name)
                except OcrError as exc:
                    errors.append(str(exc) or _missing_error(name))
                    continue
                except Exception as exc:  # noqa: BLE001 - 探测链要吞掉第三方异常
                    errors.append(
                        f"{name}: 加载失败（{type(exc).__name__}: {exc}）")
                    continue
                with self._state_lock:
                    self._backend = backend
                    self._display_name = str(
                        getattr(backend, "display_name", name) or name)
                    self._device = str(
                        getattr(backend, "device", "unknown") or "unknown")
                    self._loaded = True
                    self._load_ms = (time.perf_counter() - started) * 1000.0
                    self._last_error = ""
                return self.status()
            with self._state_lock:
                self._load_ms = (time.perf_counter() - started) * 1000.0
                self._last_error = _truncate(
                    "; ".join(errors) or "未找到可用的 OCR 引擎")
            return self.status()

    def _load_backend(self, name: str) -> _Backend:
        """按候选名加载后端；失败抛带安装提示的 OcrError。"""
        factory = self._factories.get(name)
        if factory is not None:
            return _FactoryBackend(name, factory)
        if name == "paddleocr":
            return _PaddleOcrBackend()
        if name == "rapidocr":
            return _RapidOcrBackend()
        if name == "tesseract":
            return _TesseractBackend()
        raise OcrError(_missing_error(name))

    def _ensure_backend(self) -> _Backend:
        """run() 用：懒加载 + 不可用时抛可操作 OcrError。"""
        if self._loaded and self._backend is not None:
            return self._backend
        self.prewarm()
        if self._loaded and self._backend is not None:
            return self._backend
        with self._state_lock:
            message = self._last_error
        raise OcrError(
            message
            or "未找到可用的 OCR 引擎（paddleocr / rapidocr / tesseract 都不可用）")

    # ---- 识别 ----

    def run(self, image: Any, *, region: tuple[int, int, int, int] | None = None,
            preprocess: str = "default", scale: float | None = None) -> OcrResult:
        """对图片做 OCR，返回原图坐标系的 ``OcrResult``。

        参数：
        - ``image``：PNG bytes（bytes/bytearray/memoryview）或图片文件路径；
        - ``region``：原图绝对像素 ``(x, y, w, h)``，内部裁剪后 OCR，返回的
          item x/y 会加上该偏移；越界会自动夹到图像内（记录在 result.region）。
          ``None`` = 全图；
        - ``preprocess``：``default``（2x + 对比度/USM）| ``chat``（≥2.5x + CLAHE
          + 气泡背景增强）| ``none``（原图直出）；
        - ``scale``：覆盖默认放大倍数（``preprocess="none"`` 时也可单独放大）。

        引擎不可用 / 识别失败抛 ``OcrError``（消息含安装提示）。
        多线程安全：推理用 ``threading.Lock`` 串行。
        """
        preprocess = str(preprocess or "default").strip().lower()
        if preprocess not in PREPROCESS_MODES:
            raise OcrError(
                f"preprocess 只支持 {list(PREPROCESS_MODES)}，收到 {preprocess!r}")
        region_normalized = _validate_region_arg(region)
        scale_validated = _validate_scale_arg(scale)
        if not isinstance(image, (bytes, bytearray, memoryview, str, os.PathLike)):
            raise OcrError(
                f"image 需要 PNG bytes 或文件路径，收到 {type(image).__name__}")

        backend = self._ensure_backend()
        prepared = self._prepare(
            image, region_normalized, preprocess, scale_validated, backend)
        try:
            try:
                # ocr_ms 只统计推理本身（不含解码/裁剪/预处理/等锁）；
                # 模型加载耗时单独放 load_ms，R3 性能验收按 ocr_ms 判断。
                with self._infer_lock:
                    infer_started = time.perf_counter()
                    raw_items = backend.recognize(
                        prepared.engine_input, image_size=prepared.work_size)
                    ocr_ms = (time.perf_counter() - infer_started) * 1000.0
            except OcrError:
                raise
            except Exception as exc:
                raise OcrError(
                    f"{self._display_name}: 识别失败"
                    f"（{type(exc).__name__}: {exc}）") from exc
            items = _map_items(raw_items, prepared)
        finally:
            prepared.cleanup()
        with self._state_lock:
            load_ms = self._load_ms
            engine_name = self._display_name
            device = self._device
        return OcrResult(
            items=items,
            engine=engine_name,
            device=device,
            error="",
            ocr_ms=ocr_ms,
            load_ms=load_ms,
            preprocess=preprocess,
            image_size=prepared.image_size,
            region=prepared.region,
        )

    # ---- 工作图准备 ----

    def _prepare(self, image: Any, region: tuple[int, int, int, int] | None,
                 preprocess: str, scale: float | None,
                 backend: _Backend) -> _Prepared:
        """解码 → 裁剪 → 缩放 → 增强 → 生成引擎输入（临时文件登记在返回值里）。"""
        temp_paths: list[str] = []
        try:
            cv2, np = _load_cv()
            has_cv = cv2 is not None and np is not None
            source_path: str | None = None
            data: bytes | None = None
            if isinstance(image, (bytes, bytearray, memoryview)):
                data = bytes(image)
                if not data:
                    raise OcrError("OCR 输入图片为空（0 字节）")
            else:
                source_path = os.fspath(image)
                if not os.path.isfile(source_path):
                    raise OcrError(f"OCR 图片文件不存在：{source_path}")

            if not has_cv:
                # 降级路径：没有 numpy/cv2 就不裁剪不缩放，直接把原图交给引擎。
                # region/scale 显式传值时不能静默忽略（返回的坐标会错），
                # 抛可操作的 OcrError 让调用方知道要装依赖。
                if region is not None:
                    raise OcrError(
                        "OCR 按 region 裁剪需要 numpy/opencv"
                        "（pip install numpy opencv-python）；"
                        "当前环境缺少图像库，只能整图 OCR（region=None）")
                if scale is not None:
                    raise OcrError(
                        "OCR scale 缩放需要 numpy/opencv"
                        "（pip install numpy opencv-python）；"
                        "当前环境缺少图像库，只能按原图 OCR（scale=None）")
                if data is not None:
                    source_path = _write_temp_file(
                        data, temp_paths, _guess_suffix(data))
                image_size = _probe_image_size(source_path)
                return _Prepared(
                    image_size=image_size,
                    region=None,
                    work_size=image_size,
                    scale_x=1.0,
                    scale_y=1.0,
                    offset=(0, 0),
                    engine_input=source_path,
                    temp_paths=temp_paths,
                )

            # 有 cv2：np.fromfile + imdecode（Windows 下 cv2.imread 对中文路径不友好）
            if data is not None:
                decoded = cv2.imdecode(
                    np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
            else:
                decoded = cv2.imdecode(
                    np.fromfile(source_path, dtype=np.uint8), cv2.IMREAD_COLOR)
            if decoded is None:
                raise OcrError("无法解码 OCR 输入图片（不是有效的 PNG/JPEG 文件）")
            original_height, original_width = decoded.shape[:2]
            image_size = (int(original_width), int(original_height))

            if region is None:
                crop = decoded
                offset = (0, 0)
                prepared_region: tuple[int, int, int, int] | None = None
            else:
                prepared_region = _clip_region(region, image_size)
                region_x, region_y, region_w, region_h = prepared_region
                crop = decoded[region_y:region_y + region_h,
                               region_x:region_x + region_w]
                offset = (region_x, region_y)

            crop_height, crop_width = crop.shape[:2]
            target_scale = (scale if scale is not None
                            else _PREPROCESS_SCALE.get(preprocess, 1.0))
            work_width = max(1, int(round(crop_width * target_scale)))
            work_height = max(1, int(round(crop_height * target_scale)))
            # 依模式限制工作图最长边：限制后再算有效缩放，坐标反算用有效值。
            max_work_side = _MAX_WORK_SIDE_BY_MODE.get(preprocess, _MAX_WORK_SIDE)
            limit = min(1.0, max_work_side / float(max(work_width, work_height)))
            if limit < 1.0:
                work_width = max(1, int(round(work_width * limit)))
                work_height = max(1, int(round(work_height * limit)))
            scale_x = work_width / float(crop_width)
            scale_y = work_height / float(crop_height)
            work = crop
            if (work_width, work_height) != (crop_width, crop_height):
                interpolation = (cv2.INTER_CUBIC if target_scale > 1.0
                                 else cv2.INTER_AREA)
                work = cv2.resize(
                    crop, (work_width, work_height),
                    interpolation=interpolation)
            work = self._enhance(work, preprocess)

            if getattr(backend, "requires_path", False):
                ok, buffer = cv2.imencode(".png", work)
                if not ok:
                    raise OcrError("OCR 预处理图片 PNG 编码失败")
                engine_input: Any = _write_temp_file(
                    buffer.tobytes(), temp_paths, ".png")
            else:
                engine_input = work
            return _Prepared(
                image_size=image_size,
                region=prepared_region,
                work_size=(int(work_width), int(work_height)),
                scale_x=scale_x,
                scale_y=scale_y,
                offset=offset,
                engine_input=engine_input,
                temp_paths=temp_paths,
            )
        except Exception:
            for path in temp_paths:
                try:
                    os.unlink(path)
                except OSError:
                    pass
            raise

    @staticmethod
    def _enhance(image: Any, preprocess: str) -> Any:
        """在「已裁剪 + 已放大」的工作图上做增强；无 cv2/增强出错时回退原图。

        - default：迁移旧 server 的 ``_enhance_image`` 思路（对比度 + USM 锐化，
          放大已在上一步统一完成，所以这里不再 resize）；
        - chat：CLAHE（局部对比）+ 气泡背景增强。光遇聊天面板是半透明深色
          气泡叠在世界画面上，白字小、背景明暗差大：先用大核高斯估计局部背景
          做归一化（压掉大块明暗），再用顶帽变换提亮小笔画，最后与 CLAHE 图
          混合并 USM。参数（clipLimit=3.0 / σ=12 / 顶帽 9x9 / 混合 0.65）是在
          1920x1080 聊天截图放大 2.5x 的场景下取的经验值，后续可按实测微调。
        """
        if preprocess == "none" or image is None:
            return image
        cv2, np = _load_cv()
        if cv2 is None or np is None:
            return image
        try:
            if preprocess == "default":
                image = cv2.convertScaleAbs(image, alpha=1.5, beta=0)
                blur = cv2.GaussianBlur(image, (0, 0), 1.0)
                return cv2.addWeighted(image, 1.5, blur, -0.5, 0)
            # chat：局部对比 + 气泡背景增强
            lab = cv2.cvtColor(image, cv2.COLOR_BGR2LAB)
            l_channel, a_channel, b_channel = cv2.split(lab)
            clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
            l_channel = clahe.apply(l_channel)
            clahe_bgr = cv2.cvtColor(
                cv2.merge((l_channel, a_channel, b_channel)),
                cv2.COLOR_LAB2BGR)
            gray = cv2.cvtColor(clahe_bgr, cv2.COLOR_BGR2GRAY)
            background = cv2.GaussianBlur(gray, (0, 0), 12)
            background = np.maximum(background, 1)  # 防除零（纯黑区域）
            normalized = cv2.divide(gray, background, scale=128)
            kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (9, 9))
            top_hat = cv2.morphologyEx(gray, cv2.MORPH_TOPHAT, kernel)
            boosted = cv2.addWeighted(normalized, 1.0, top_hat, 0.8, 0)
            boosted = cv2.convertScaleAbs(boosted, alpha=1.2, beta=0)
            enhanced = cv2.cvtColor(boosted, cv2.COLOR_GRAY2BGR)
            # 与 CLAHE 彩图混合：保留少量原图边缘/颜色信息，避免过度二值化
            blended = cv2.addWeighted(clahe_bgr, 0.35, enhanced, 0.65, 0)
            blur = cv2.GaussianBlur(blended, (0, 0), 1.0)
            return cv2.addWeighted(blended, 1.5, blur, -0.5, 0)
        except Exception:
            # 增强只是锦上添花，失败宁可回退原图，也不能让 OCR 整体失败
            return image


# ===================== CLI 自检（不是 MCP 工具，方便人工排查） =====================


def _main(argv: list[str]) -> int:
    import argparse
    import json

    parser = argparse.ArgumentParser(
        description="sky_ocr 自检 / 手动识别（排查 OCR 环境用，不是 MCP 服务端）")
    parser.add_argument("image", nargs="?", help="PNG/JPEG 路径；省略时只打印 status")
    parser.add_argument("--engine", default=None,
                        help="auto / paddleocr / rapidocr / tesseract / none")
    parser.add_argument("--preprocess", default="default",
                        choices=list(PREPROCESS_MODES))
    parser.add_argument("--region", default=None,
                        help="原图绝对像素 x,y,w,h（逗号分隔）")
    parser.add_argument("--prewarm", action="store_true",
                        help="先阻塞加载引擎（默认 run 时懒加载）")
    args = parser.parse_args(argv)

    engine = OcrEngine(args.engine)
    if args.prewarm or not args.image:
        engine.prewarm()
    status = engine.status()
    print(json.dumps(status, ensure_ascii=False, indent=2))
    if not args.image:
        return 0 if status["ocr"] != "none" else 1

    region = None
    if args.region:
        try:
            region = tuple(int(value) for value in
                           args.region.replace(" ", "").split(","))
        except ValueError:
            print(f"--region 格式错误：{args.region!r}（示例：0,50,650,900）")
            return 2
    try:
        result = engine.run(args.image, region=region,
                            preprocess=args.preprocess)
    except OcrError as exc:
        print(f"OCR 失败：{exc}")
        return 2
    print(json.dumps({
        "engine": result.engine,
        "device": result.device,
        "ocr_ms": round(result.ocr_ms, 1),
        "load_ms": round(result.load_ms, 1),
        "image_size": list(result.image_size),
        "region": list(result.region) if result.region else None,
        "texts": result.texts(),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
