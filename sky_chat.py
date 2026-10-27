#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sky_chat.py — 光遇（安卓模拟器）聊天区域 ROI + 聊天文本结构化解析（R2）。

本模块只做两件事，不碰 OCR、不碰截图、不 import 任何第三方库（纯标准库）：

1. **聊天区域 ROI**：把「聊天面板消息区」「世界内聊天气泡区」表示成相对坐标
   （0~1，不随分辨率变化），再换算成原图绝对像素，供 ``sky_ocr.OcrEngine.run(region=...)``
   裁剪 + 放大 + 增强。
2. **结构化解析**：把 OCR 出来的 items（``text/confidence/x/y[/width/height]``，坐标已
   还原到原图）按光遇的聊天格式解析成
   ``{"speaker", "text", "confidence", "y", "is_self", "kind"}`` 列表，
   稳定排序、去重、过滤 UI 噪音。

设计意图 / 踩坑记录
--------------------
* 光遇聊天面板每行格式是 **「内容 - 名字」**（名字在末尾），分隔符可能被 OCR 识别成
  ``- ‒ – — ~ 〜`` 等不同字符；长消息换行后，名字只出现在最后一行，
  所以「无分隔符的行」既可能是自己发的消息（游戏不显示自己的名字），
  也可能是上一条长消息的续行 —— 用「行距」区分（见 ``CONTINUATION_FACTOR``）。
* 聊天面板最底部是输入框（1920x1080 下 y≈0.90~0.99），里面是「聊天......」占位文字，
  属于 UI 噪音，必须过滤（见 ``SKIP_TEXTS``）。
* OCR 会漏字/串行，同一帧重复调用必须给出**完全一致**的结果，
  因此所有排序都带确定的次级键（y → x → 原始下标），任何集合都不参与顺序。
* 纯标准库是硬约束：模块在没装 numpy/cv2/OCR 的机器上也要能被 import
  （server 的 status 路径要求它永不因可选依赖缺失而崩）。

ROI 标定依据（1920x1080 截图，真机 3200x1800 为同比例放大）
------------------------------------------------------------
用 ``tests/fixtures/sky_chat_panel.jpg``（聊天面板打开态）做像素分析 +
rapidocr 实测（坐标已还原到原图）：

* 面板消息行（含半透明深色胶囊背景）出现在 y≈663~856（4 行，行距约 50~66 px），
  文字高约 26 px（rapidocr 框高约 33 px）；短消息胶囊宽度约 200 px，
  输入框右边缘 x=629；面板行文字起点 x≈62~110（相对 0.03~0.06 W）。
* 底部输入框：面板态 y≈1007~1068、x≈3~628；输入框聚焦态（sky_chat_input.jpg）
  整体上移，y≈976~1034；占位文字「聊天......」在 1080p 下 y≈1000~1030。
  真机验收时 ROI 若伸到 y≈0.945，就会把占位文字 OCR 成一条聊天消息，
  所以 ``CHAT_ROI_REL`` 底边收到 y≈0.90（1920x1080 下 y≈972）。
* 横向保留到 x≈0.365 W（输入框右缘 x=0.3276 W 外再留一点）：
  长消息/长名字会向右延伸，横向切太紧会把内容截断。
* ``tests/fixtures/sky_chat_region.jpg`` 是聊天区域放大裁剪图，用模板匹配反推：
  它正好是 ``sky_chat_panel.jpg`` 的 ``(0, 49, 700, 971)`` 区域放大 2.78 倍
  （匹配得分 0.99）—— 即联调时用来核对「聊天区域上面这块」的参考裁剪，
  其底边含输入框上沿，因此本模块 ROI 只沿用它的顶部/宽度，底部改收。

世界气泡（bubble）位置随说话人在画面里的位置变化，只能 best-effort：

* fixture 里两条气泡在 x≈0.11~0.33、y≈0.78~0.91（rapidocr 实测：
  第一条 y≈897 x≈269、第二条 y≈946 x≈322）；需求文档记录的另一次采样在
  x≈0.12~0.38、y≈0.50~0.62。取两者的并集再留余量，同时避开底部系统横幅
  （「珍珠季的新任务现已开启!」在 y≈0.94 以下、x≈0.40 以右）。
* bubble 模式还有两个坑：① 气泡 ROI 与面板列表区域重叠，面板打开时会把面板行
  OCR 进来 → 带落款的行直接丢弃；② 面板行被 ROI 左缘裁断后只剩「陌生人」这种
  残片（没有落款，bubble 规则本来会收）→ 起点贴着 ROI 左缘（< 2% W）的丢弃。

真实消息的两种形态：面板里 vs 世界气泡（真机验收结论）
--------------------------------------------------------
3200x1800 真机、输入框聊天界面打开时实测：

* 面板左列全是「......... - 陌生人」这类**正文被省略的行**，rapidocr 只读到名字
  「陌生人」（有时带「- 」前缀），4~20 行不等 —— 这些是名字碎片，不是消息，
  必须过滤（见 ``NAME_NOISE_TEXTS`` / ``_is_name_noise``），否则会输出大量
  「陌生人」垃圾并把 bubble 回退挤掉。
* 自己刚发送的真实消息显示为**世界内气泡**，叠在输入框上方（1080p 帧实测
  y≈730~806、x≈120~630，聊天界面开着时不消失）。因此 panel 模式里无落款裸行
  只有「与 own_texts 相似」或「贴左边 x<0.10W 且 y>0.72H（底部气泡区）且
  文本≥2 字符且不是名字碎片」才当自己消息，其余一律丢弃。
* panel 模式只信任带落款（分隔符 + 正文非空）的行；bubble 模式过滤单字符碎片
  （"İ"/"D"）、名字噪音、带落款行与被 ROI 左缘裁断的残片。

``parse_chat_items`` 的可选 ``image_size`` 决定是否启用上述几何规则：
传了（server 会传）才启用严格规则，不传退回旧行为，兼容老调用方。
"""

from __future__ import annotations

import difflib
import re

# ===========================================================================
# 冻结接口：ROI 常量（相对坐标 x, y, w, h，取值 0~1）
# ===========================================================================

#: 聊天面板消息区相对 ROI（x, y, w, h）。
#: 标定见模块 docstring：1920x1080 下 ≈ (0, 49, 701, 923) 像素。
#: 底部只到 y≈0.90（而不是聊天区域裁剪图的 0.945）：真机验收发现再往下会把
#: 输入框占位「聊天......」（1080p 下 y≈1000~1030）OCR 进来，且输入框聚焦时
#: 整条会上移（顶边 y≈976）—— 因此底部必须停在输入框上沿之上。
CHAT_ROI_REL: tuple[float, float, float, float] = (0.0, 0.045, 0.365, 0.855)

#: 世界内聊天气泡的相对搜索区（best-effort，位置随玩家站位变化）。
#: 覆盖 fixture 实测区间与需求文档记录区间，并避开底部系统横幅。
BUBBLE_ROI_REL: tuple[float, float, float, float] = (0.06, 0.42, 0.36, 0.50)

#: UI 噪音文本（比较前会做「去空白 + 全半角归一 + 小写 + 去标点」归一化）。
#: 聊天面板/输入框上的按钮、占位符、快捷键提示，OCR 会当成正文读出来。
#: 后 6 个是手机 UI 菜单词（真机验收时曾被读成聊天消息）。
SKIP_TEXTS: frozenset[str] = frozenset({
    "聊天", "聊天框", "发送", "语音输入", "语音", "输入", "输入法", "表情",
    "键盘", "清空", "关闭", "返回", "取消", "确认", "换行", "设置", "帮助",
    "enter", "esc", "escape", "return", "back", "send", "chat", "menu", "ok",
    "图像", "音频", "控制", "账号", "好友", "商店", "遇境", "菜单",
})

#: 置信度低于该值的 OCR 行直接丢弃（小字/气泡透过世界画面识别经常很低）。
MIN_CONFIDENCE = 0.35

#: 续行判定系数：Δy < 1.8 × 行高估计 → 认为是同一条消息的换行。
#: 注意「行高估计」不是 OCR 框高：rapidocr 的文字框含上下留白
#: （1920x1080 实测框高 32~35 px，而白字实际字高约 26 px），
#: 直接用框高代入 1.8 会把两条独立消息（行距 48~51 px）误并成一条。
#: 因此先按 ``LINE_HEIGHT_FACTOR`` 把框高折算回文字行高，再乘本系数。
CONTINUATION_FACTOR = 1.8

#: OCR 框高 → 文字行高的折算系数（实测框高 33 px ≈ 1.3 × 字高 26 px）。
#: 折算后 1080p 阈值 ≈ 1.8 × 0.72 × 33 ≈ 42.8 px：
#: - 同一条消息内换行行距（约 34~40 px）< 阈值 → 合并；
#: - 两条独立消息之间行距（实测 48~66 px）> 阈值 → 分开。
LINE_HEIGHT_FACTOR = 0.72

#: 去重容差：同一帧里 y 相差 ≤ 8 px 且文本相同 → 认为是同一个文字框的重复输出。
DEDUP_Y_TOLERANCE = 8

#: 与 own_texts 相似度阈值（difflib.SequenceMatcher，标点归一化后比较）。
OWN_TEXT_SIMILARITY = 0.75

#: 名字允许的最大长度（超过多半是把整句当成名字的误判）。
NAME_MAX_LEN = 24

#: 分隔符集合：连字符/各种破折号/波浪线。OCR 对「-」的识别不稳定，
#: 所以这里比接口约定（- ‒ – — ~ 〜）多收了全角波浪线等常见误识别。
_SEPARATOR_CHARS = "-\u2010\u2011\u2012\u2013\u2014\u2015~\u301c\uff5e"
_SEPARATOR_RE = re.compile(r"\s*[" + re.escape(_SEPARATOR_CHARS) + r"]\s*")

#: 纯数字/纯点号/纯符号（UI 上的计数、省略号、分隔线等）。
#: 除了常见中英文标点，还收了一批 OCR 可能把「. . . .」认成的点号变体。
_PUNCT_CHARS = (
    ".．。｡。,，、:：;；!！?？'\"‘’“”`~～-‐‑‒–—―_/\\|+*=<>^%$#@&()（）[]【】{}"
    "·•・･…⋯‥﹒※○●□■△▲◆◇☆★◦▪▫"
)
_NOISE_RE = re.compile(
    r"^[\s\d" + re.escape(_PUNCT_CHARS) + r"]+$")

#: 输入框占位/按钮特征文本（归一化后比较）。面板打开时它出现在聊天 ROI 底部。
_INPUT_HINT_TEXTS = frozenset({"聊天", "发送", "语音输入", "输入", "说点什么"})

#: panel 模式判定「自己发的消息」的横向上限（相对画面宽度）：
#: 面板列表贴左边缘，自己消息/换行的文字起点 x≈0.03~0.06 W；
#: 更靠右的无落款行属于世界气泡（跟说话人站位走），panel 模式丢弃。
PANEL_SELF_MAX_X_FRAC = 0.10

#: panel 模式判定「底部气泡区」的纵向下限（相对画面高度）：
#: 真实消息发送后显示为世界气泡、叠在输入框上方（真机 1080p 帧实测 y≈730~806），
#: 聊天界面开着时不会消失。只有落在屏幕下半部（y > 0.72 H）的裸行才可能是
#: 自己发的消息，面板中上部的裸行一律按名字碎片丢弃。
PANEL_SELF_MIN_Y_FRAC = 0.72

#: 世界气泡允许的最短文本（归一化后）：过滤 OCR 抖动出的单字符碎片（"İ"、"D"）。
BUBBLE_MIN_TEXT_LEN = 2

#: 名字/占位噪音文本：真机 OCR 会把这些名字碎片当成聊天内容读出来
#: （面板左列「......... - 陌生人」这类行，正文被省略只剩名字）。
NAME_NOISE_TEXTS: frozenset[str] = frozenset({
    "陌生人", "光之子", "玩家", "旅人",
})

#: bubble 模式的 ROI 左缘安全边距（相对画面宽度，至少 2 px）：
#: 气泡 ROI 是从左列切出来的，面板打开时面板行会被 ROI 左缘裁断成「陌生人」这类
#: 残片（fixture 实测残片 x 与 ROI 左缘只差 0~1 px；真机气泡最左约 x≈120、
#: ROI 左缘 115 → 差 5 px）。边距取 0.2% W（1080p 约 3.8 px）：裁断残片被丢弃，
#: 紧贴 ROI 左缘的真实气泡仍保留。
BUBBLE_EDGE_MARGIN_FRAC = 0.002

#: 消息结果的字段顺序（契约字段，不要随意增删）。
_MESSAGE_FIELDS = ("speaker", "text", "confidence", "y", "is_self", "kind")


# ===========================================================================
# ROI：相对坐标 → 原图绝对像素
# ===========================================================================

def _rel_to_abs(relative: tuple[float, float, float, float],
                image_size: tuple[int, int] | dict) -> tuple[int, int, int, int]:
    """把相对 ROI (x, y, w, h) 换算成原图像素 (x, y, w, h)。

    - 支持 ``(width, height)`` 元组，也容忍 server 直接传
      ``{"width": .., "height": ..}`` 形式的 image_size（例如 read_screen 返回的字段）；
    - 四舍五入到整像素，并夹到画面范围内（不越界、宽高至少 1 px）。
    """
    if isinstance(image_size, dict):
        width = image_size.get("width")
        height = image_size.get("height")
    else:
        try:
            width, height = image_size
        except (TypeError, ValueError):
            raise ValueError(
                f"image_size 必须是 (width, height) 或含这两个键的 dict: "
                f"{image_size!r}") from None
    try:
        width_int = int(round(float(width)))
        height_int = int(round(float(height)))
    except (TypeError, ValueError):
        raise ValueError(f"image_size 宽高必须是数字: {image_size!r}") from None
    if width_int <= 0 or height_int <= 0:
        raise ValueError(f"image_size 必须为正数: {image_size!r}")

    x = int(round(relative[0] * width_int))
    y = int(round(relative[1] * height_int))
    w = int(round(relative[2] * width_int))
    h = int(round(relative[3] * height_int))
    # 夹到画面内：x/y 落在 [0, size-1]；w/h 至少 1 px 且不越界。
    x = min(max(x, 0), width_int - 1)
    y = min(max(y, 0), height_int - 1)
    w = max(1, min(w, width_int - x))
    h = max(1, min(h, height_int - y))
    return x, y, w, h


def chat_region(image_size: tuple[int, int] | dict) -> tuple[int, int, int, int]:
    """聊天面板消息区：相对 ROI → 原图绝对像素 (x, y, w, h)。

    参数 ``image_size`` 为截图的 ``(宽, 高)``（也接受 ``{"width","height"}`` dict）。
    """
    return _rel_to_abs(CHAT_ROI_REL, image_size)


def bubble_region(image_size: tuple[int, int] | dict) -> tuple[int, int, int, int]:
    """世界内聊天气泡搜索区：相对 ROI → 原图绝对像素 (x, y, w, h)。"""
    return _rel_to_abs(BUBBLE_ROI_REL, image_size)


# ===========================================================================
# 文本归一化 / 噪音判断（都是纯函数，方便单测）
# ===========================================================================

def _to_float(value, default: float = 0.0) -> float:
    """宽松转 float；None/非法值返回 default（OCR 字段可能缺失）。"""
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_int(value, default: int = 0) -> int:
    """宽松转 int（OCR 的 x/y 可能是 float/字符串）。"""
    if value is None:
        return default
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return default


def _normalize_text(text: str) -> str:
    """归一化文本，用于去重、相似度、UI 噪音比较。

    - 全角 ASCII 转半角（中文输入法/OCR 常见）；
    - 去掉所有空白与标点（「标点归一」），中文标点也在内；
    - 转小写。
    例：``"測試 ABC123！"`` → ``"測試abc123"``。
    """
    if not text:
        return ""
    chars: list[str] = []
    for ch in str(text):
        code = ord(ch)
        if 0xFF01 <= code <= 0xFF5E:      # ！~ 全角 ASCII 区
            ch = chr(code - 0xFEE0)
        elif ch == "\u3000":               # 全角空格
            ch = " "
        chars.append(ch)
    lowered = "".join(chars).lower()
    # 去掉空白、常见中英文标点与符号（保留字母/数字/CJK/emoji 等）。
    return "".join(
        ch for ch in lowered
        if not ch.isspace() and ch not in _PUNCT_CHARS
    )


def _normalize_skip_key(text: str) -> str:
    """SKIP_TEXTS 的比较键：归一化 + 去掉首尾残留的点（占位符「聊天......」）。"""
    return _normalize_text(text)


_SKIP_KEYS = frozenset(_normalize_skip_key(t) for t in SKIP_TEXTS)


def _is_skip_text(text: str) -> bool:
    """是否为 UI 噪音文本（聊天/发送/ENTER/ESC/聊天...... 之类）。

    归一化后命中 ``SKIP_TEXTS`` 即算；另外「聊天」开头的一律算噪音
    —— 聊天面板输入框占位会 OCR 成「聊天......」「聊天……」「聊天 ....」等，
    归一化后残留各种变体，真机验收里确实漏进来过，用前缀兜底最稳。
    代价是玩家真发一条以「聊天」开头的消息会被过滤，真实聊天里极少见。
    """
    key = _normalize_skip_key(text)
    if not key:
        return False
    return key in _SKIP_KEYS or key.startswith("聊天")


def _is_noise_text(text: str) -> bool:
    """空行 / 纯数字 / 纯点号省略号 / 纯符号 → 视为无意义文本。

    注意：这里比 ``_is_skip_text`` 更宽（讨论的是「内容本身没信息量」），
    例如聊天面板里实测的「…………」占位消息、帧率数字、分隔线等。
    """
    raw = str(text or "").strip()
    if not raw:
        return True
    if _NOISE_RE.match(raw):
        return True
    # 归一化后为空 = 纯标点/空白；归一化后全数字 = 纯数字（允许中间夹空格/标点）。
    normalized = _normalize_text(raw)
    if not normalized:
        return True
    return normalized.isdigit()


def _is_pure_cjk(text: str) -> bool:
    """是否整段都是 CJK 汉字（不含空白/标点/字母数字）。"""
    raw = str(text or "").strip()
    if not raw:
        return False
    return all("\u4e00" <= ch <= "\u9fff" for ch in raw)


def _is_name_noise(text: str, speaker: str | None = None, *,
                   allow_short_cjk: bool = True) -> bool:
    """是否「名字/占位碎片」而不是真正的聊天正文。

    真机证据（3200x1800 聊天界面打开）：面板左列全是
    「......... - 陌生人」这类正文被省略的行，rapidocr 只读到名字
    「陌生人」（有时带「- 」前缀），parser 之前把它们当成自己发的消息输出垃圾。

    判定（归一化后比较）：
    - 文本等于本行 speaker（例如「陌生人 - 陌生人」的误识别组合）；
    - 文本 ∈ ``NAME_NOISE_TEXTS``（陌生人/光之子/玩家/旅人）；
    - ``allow_short_cjk=True`` 时：纯 CJK 且长度 ≤3 → 名字碎片
      （「陌生人」「图像」等；仅用于无落款裸行的过滤，收到真实消息的正文不套用，
      否则「晚安」「你好呀」这类短消息会被误杀）。

    注意：纯标点/纯数字由 ``_is_noise_text`` 负责，这里不重复判断。
    """
    raw = str(text or "").strip()
    if not raw:
        return False
    normalized = _normalize_text(raw)
    if not normalized:
        return False
    if speaker is not None:
        normalized_speaker = _normalize_text(str(speaker))
        if normalized_speaker and normalized == normalized_speaker:
            return True
    if normalized in _NAME_NOISE_KEYS:
        return True
    if allow_short_cjk and len(normalized) <= 3 and _is_pure_cjk(raw):
        return True
    return False


_NAME_NOISE_KEYS = frozenset(_normalize_text(name) for name in NAME_NOISE_TEXTS)


def _is_separator_only(text: str) -> bool:
    """是否只由分隔符构成（用于排除把一串横线当名字的误判）。"""
    stripped = str(text or "").strip()
    return bool(stripped) and all(ch in _SEPARATOR_CHARS or ch.isspace()
                                  for ch in stripped)


def _join_texts(parts) -> str:
    """拼接消息片段：中文直接相连；两侧都是 ASCII 字母数字时补一个空格。"""
    result = ""
    for part in parts:
        piece = str(part or "").strip()
        if not piece:
            continue
        if not result:
            result = piece
        elif (result[-1].isascii() and result[-1].isalnum()
              and piece[0].isascii() and piece[0].isalnum()):
            result += " " + piece
        else:
            result += piece
    return result


def _split_speaker(text: str) -> tuple[str | None, str | None]:
    """把一行聊天文本拆成 (内容, 说话人)。

    光遇面板格式是「内容 - 名字」，名字在**末尾**；内容里也可能出现横线
    （英文词、日期），因此从右往左找最后一个满足条件的合法分隔符。

    返回：
    - ``(内容, 名字)``：找到合法落款；
    - ``("", 名字)``：**孤儿名字行**（只剩落款，正文在上一行，例如长消息换行）；
    - ``(None, None)``：没有落款（自己发的消息 / 世界气泡 / 无名字的残片）。
    """
    if not text:
        return None, None
    for match in reversed(list(_SEPARATOR_RE.finditer(text))):
        name = text[match.end():].strip()
        if not name or len(name) > NAME_MAX_LEN:
            continue
        if _is_separator_only(name):
            continue
        content = text[:match.start()].strip()
        return content, name
    return None, None


def _similar_to_own(text: str, own_texts) -> bool:
    """消息文本是否与「最近自己发出去的消息」高度相似（标点归一 + difflib）。

    server 的 ``send_chat`` 会记录最近发送的文本并通过 ``own_texts`` 传进来，
    用于把 OCR 认不出名字、或漏了名字尾缀的行标成 ``is_self=True``。
    """
    normalized = _normalize_text(text)
    if not normalized:
        return False
    for own in own_texts or ():
        target = _normalize_text(str(own))
        if not target:
            continue
        if normalized == target:
            return True
        if len(normalized) < 2 or len(target) < 2:
            continue
        if difflib.SequenceMatcher(None, normalized, target).ratio() > OWN_TEXT_SIMILARITY:
            return True
    return False


# ===========================================================================
# OCR items → 内部行结构（过滤 / 排序 / 去重）
# ===========================================================================

def _prepare_rows(items) -> list[dict]:
    """把 OCR items 清洗成内部行列表。

    - 丢弃非 dict、空文本、UI 噪音（SKIP_TEXTS）、置信度 < MIN_CONFIDENCE 的项；
    - 按 ``(y, x, 原始下标)`` 排序：y 主序，x 次序，原始下标兜底 —— 保证同一帧
      重复调用、甚至输入顺序被打乱时结果完全一致（禁止随机/集合顺序影响结果）；
    - 去重：同 y ± 8 px 且归一化文本相同的文字框只留一条（保留排序后的第一条）。
    """
    if not isinstance(items, (list, tuple)):
        return []
    rows: list[dict] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        raw_text = item.get("text")
        if raw_text is None:
            continue
        text = str(raw_text).strip()
        if not text or _is_skip_text(text):
            continue
        # 契约保证 items 带 confidence；字段缺失时按「可信」处理，避免静默丢光结果。
        confidence = _to_float(item.get("confidence"), 1.0)
        if confidence < MIN_CONFIDENCE:
            continue
        rows.append({
            "text": text,
            "confidence": confidence,
            "x": _to_int(item.get("x"), 0),
            "y": _to_int(item.get("y"), 0),
            "width": max(0, _to_int(item.get("width"), 0)),
            "height": max(0, _to_int(item.get("height"), 0)),
            "index": index,
        })
    rows.sort(key=lambda row: (row["y"], row["x"], row["index"]))

    deduped: list[dict] = []
    seen: list[tuple[str, int]] = []
    for row in rows:
        key = _normalize_text(row["text"])
        duplicated = any(
            key == seen_key and abs(row["y"] - seen_y) <= DEDUP_Y_TOLERANCE
            for seen_key, seen_y in seen
        )
        if duplicated:
            continue
        seen.append((key, row["y"]))
        deduped.append(row)
    return deduped


def _estimate_line_height(rows: list[dict]) -> float:
    """估计聊天**文字行高**（像素），续行判定用。

    ``sky_ocr`` 给的 ``height`` 是 OCR 文字框高度（rapidocr 会带上下留白，
    1920x1080 实测 32~35 px，而白字实际字高约 26 px），所以先取框高中位数，
    再按 ``LINE_HEIGHT_FACTOR``（0.72）折算回文字行高，最后乘
    ``CONTINUATION_FACTOR`` 得到续行阈值（1080p 下约 43 px）。

    没有高度信息时（例如 tesseract 兜底只给递增 y）返回 0 —— 此时不做续行合并，
    每行按独立消息处理，宁可少合并也不把两条消息粘成一条。
    """
    heights = sorted(row["height"] for row in rows if row["height"] > 0)
    if not heights:
        return 0.0
    count = len(heights)
    if count % 2:                     # 奇数个：取正中间
        median = float(heights[count // 2])
    else:                             # 偶数个：取中间两个的平均
        median = (heights[count // 2 - 1] + heights[count // 2]) / 2.0
    return median * LINE_HEIGHT_FACTOR


def _is_close(delta_y: float, line_height: float) -> bool:
    """Δy 是否属于「同一消息内的换行」（Δy < 1.8 × 行高估计）。"""
    return line_height > 0 and 0 <= delta_y < CONTINUATION_FACTOR * line_height


def _merge_confidence(rows: list[dict]) -> float:
    """合并多行时的置信度：取最小值（换行识别质量不应被高估）。"""
    return round(min(row["confidence"] for row in rows), 4)


def _make_message(speaker: str, text: str, confidence: float, y: int,
                  is_self: bool, kind: str) -> dict:
    """按契约字段构造一条消息。"""
    return {
        "speaker": str(speaker or ""),
        "text": str(text or "").strip(),
        "confidence": round(float(confidence), 4),
        "y": int(y),
        "is_self": bool(is_self),
        "kind": kind,
    }


# ===========================================================================
# panel / bubble 两种形态的解析
# ===========================================================================

def _image_dimensions(image_size) -> tuple[float, float] | None:
    """从 ``(width, height)`` / ``{"width","height"}`` 取 (宽, 高)；无效返回 None。"""
    if not image_size:
        return None
    if isinstance(image_size, dict):
        width = image_size.get("width")
        height = image_size.get("height")
    else:
        try:
            width = image_size[0]
            height = image_size[1]
        except (TypeError, IndexError, KeyError):
            return None
    try:
        width_value = float(width)
        height_value = float(height)
    except (TypeError, ValueError):
        return None
    if width_value <= 0 or height_value <= 0:
        return None
    return width_value, height_value


def _image_width(image_size) -> float | None:
    """从 image_size 里取画面宽度；无效返回 None。"""
    dimensions = _image_dimensions(image_size)
    return dimensions[0] if dimensions else None


def _panel_messages(rows: list[dict], own_texts,
                    image_size=None) -> list[dict]:
    """panel 模式：只信任带落款的行，无落款裸行按真机规则严格判定。

    真机证据（3200x1800，输入框聊天界面打开）：面板左列全是
    「......... - 陌生人」这类正文被省略的行，rapidocr 只能读到名字
    「陌生人」（有时带「- 」前缀），4~20 行不等。旧实现把所有无落款行当自己
    消息，输出大量「陌生人」垃圾，还挤掉了 bubble 回退（R2-4 找不到真实消息）。
    因此：

    - **只信任带落款（分隔符 + 正文非空）的行**；紧贴其上的 pending 裸行作为
      长消息换行的正文前缀合并；
    - **孤儿落款行**（只有「- 名字」）：把 pending 里有意义的正文归给该名字，
      pending 全是名字碎片 / 噪音时直接丢弃；
    - **无落款裸行**（自己发的消息没有名字）：按行距攒成 pending，落盘时只有
      以下情况才当自己消息：
        * 与 ``own_texts`` 相似（server 记录最近 send_chat 文本）→ 位置不限
          （真机里自己发的消息显示为世界气泡，位置跟着镜头走）；
        * 传了 ``image_size`` 时：起点 x < 0.10 W 且 y > 0.72 H（输入框上方的
          世界气泡区）、归一化文本 ≥2 字符且不是名字碎片；
        * 没传 ``image_size``（兼容旧调用方）→ 退回旧行为，全部当自己消息；
    - 其它裸行（面板中上部的名字碎片等）一律丢弃；
    - 名字碎片过滤（``_is_name_noise``）：文本等于 speaker、∈ NAME_NOISE_TEXTS、
      裸行纯 CJK 且长度 ≤3。带落款消息的正文不套用「纯 CJK ≤3」规则，
      避免误杀「晚安」「你好呀」这类真实短消息。
    - 未提供 ``image_size`` 时保留旧版「裸行紧贴上一行命名消息 → 续行」行为；
      提供后不再这么做（真机名字在末尾，命名行后的裸行只会是碎片，追加会污染正文）。
    """
    line_height = _estimate_line_height(rows)
    dimensions = _image_dimensions(image_size)
    strict = dimensions is not None
    self_max_x = dimensions[0] * PANEL_SELF_MAX_X_FRAC if strict else None
    bottom_band_top = dimensions[1] * PANEL_SELF_MIN_Y_FRAC if strict else None
    messages: list[dict] = []
    pending: list[dict] = []      # 已读入、还没确定归属的裸行（按 y 升序）
    previous_y: int | None = None  # 上一行的 y（用于 Δy 判定）

    def meaningful_pending() -> list[dict]:
        """取出 pending 里除纯噪音/名字碎片以外的行（按 y 升序）。"""
        return [
            row for row in pending
            if not _is_noise_text(row["text"])
            and not _is_name_noise(row["text"], allow_short_cjk=strict)
        ]

    def emit_self(parts: list[dict]) -> None:
        """按真机规则决定一批无落款裸行是否作为「自己发的消息」落盘。"""
        if not parts:
            return
        confidence = _merge_confidence(parts)
        joined = _join_texts(row["text"] for row in parts)
        if joined and _similar_to_own(joined, own_texts):
            # 换行合并后的整段命中 server 记录的最近发送文本
            messages.append(_make_message(
                "", joined, confidence, parts[0]["y"],
                is_self=True, kind="panel"))
            return
        if not strict:
            # 未提供画面尺寸：退回旧契约行为（裸行都按自己消息处理）
            if joined:
                messages.append(_make_message(
                    "", joined, confidence, parts[0]["y"],
                    is_self=True, kind="panel"))
            return
        # 严格模式：逐行判断（真机里每条世界气泡是独立消息，不能互相拼接）
        for row in parts:
            text = row["text"]
            normalized = _normalize_text(text)
            if len(normalized) < 2:
                continue
            is_own = _similar_to_own(text, own_texts)
            in_bubble_band = (row["x"] < self_max_x
                              and row["y"] > bottom_band_top)
            if is_own or in_bubble_band:
                messages.append(_make_message(
                    "", text, row["confidence"], row["y"],
                    is_self=True, kind="panel"))
        # 其余裸行（面板中上部的名字碎片等）丢弃

    def flush_pending() -> None:
        parts = meaningful_pending()
        pending.clear()
        emit_self(parts)

    def emit_named(speaker: str, text: str, confidence: float, y: int) -> None:
        """落盘一条带落款的消息；名字碎片/无意义正文直接丢弃。"""
        if not text or _is_noise_text(text):
            return
        if _is_name_noise(text, speaker, allow_short_cjk=False):
            return
        messages.append(_make_message(
            speaker, text, confidence, y,
            is_self=_similar_to_own(text, own_texts), kind="panel",
        ))

    for row in rows:
        content, speaker = _split_speaker(row["text"])
        if speaker is not None:
            # ---------- 带落款的行 ----------
            if not content:
                # 孤儿落款行：正文在 pending 里（长消息换行落款）
                parts = meaningful_pending()
                pending.clear()
                if parts:
                    emit_named(speaker,
                               _join_texts(item["text"] for item in parts),
                               _merge_confidence(parts), parts[0]["y"])
                previous_y = row["y"]
                continue

            # 带落款且正文非空：只信任这一行（可能带 pending 正文前缀）
            prefix = meaningful_pending()
            pending.clear()
            message_y = row["y"]
            if prefix and _is_close(row["y"] - previous_y, line_height):
                text = _join_texts(
                    [item["text"] for item in prefix] + [content])
                confidence = _merge_confidence(prefix + [row])
                message_y = prefix[0]["y"]  # y 取整条消息第一行（顶部）
            else:
                emit_self(prefix)
                text = content
                confidence = row["confidence"]
            emit_named(speaker, text, confidence, message_y)
        else:
            # ---------- 无落款的裸行 ----------
            if (not strict and messages and messages[-1]["speaker"]
                    and _is_close(row["y"] - previous_y, line_height)):
                # 旧版行为（未提供 image_size）：紧贴命名消息的裸行视为续行
                messages[-1]["text"] = _join_texts(
                    [messages[-1]["text"], row["text"]])
                messages[-1]["confidence"] = round(
                    min(messages[-1]["confidence"], row["confidence"]), 4)
            elif pending and _is_close(row["y"] - previous_y, line_height):
                # 紧贴上一行裸行 → 同一条自己消息的换行
                pending.append(row)
            else:
                # 间隔较大：之前攒的裸行先按规则处理
                flush_pending()
                pending.append(row)
        previous_y = row["y"]

    flush_pending()
    return messages


def _bubble_messages(rows: list[dict], image_size=None) -> list[dict]:
    """bubble 模式：世界内聊天气泡没有名字尾缀，每个合理文本行即一条消息。

    - 带落款的行（「内容 - 名字」）是聊天面板行：面板开着时气泡 ROI 会把面板
      列表扫进来，这里必须丢弃；
    - 过滤 OCR 抖动出的单字符碎片（真机出现过 "İ"、"D"）和名字噪音
      （陌生人/光之子/玩家/旅人）；
    - 传了 ``image_size`` 时再校验文字框是否落在 ``BUBBLE_ROI_REL`` 内；
      贴着气泡 ROI 左缘的行是被裁断的面板行残片（只剩名字），也丢弃。
    """
    bubble_box = None
    edge_x = None
    if image_size is not None:
        try:
            bubble_box = bubble_region(image_size)
            width = _image_width(image_size)
            if width is not None:
                margin = max(2.0, width * BUBBLE_EDGE_MARGIN_FRAC)
                edge_x = bubble_box[0] + margin
        except (TypeError, ValueError):
            bubble_box = None
    messages: list[dict] = []
    for row in rows:
        text = row["text"]
        _content, speaker = _split_speaker(text)
        if speaker is not None:
            continue  # 面板消息，不是世界气泡
        raw = str(text).strip()
        normalized = _normalize_text(raw)
        # 单字符/纯标点/名字碎片都不是聊天气泡（"İ"、"D"、"陌生人"…）
        if (len(raw) < BUBBLE_MIN_TEXT_LEN
                or len(normalized) < BUBBLE_MIN_TEXT_LEN
                or _is_noise_text(text)
                or _is_name_noise(text, allow_short_cjk=False)):
            continue
        if bubble_box is not None:
            box_x, box_y, box_w, box_h = bubble_box
            if not (box_x <= row["x"] < box_x + box_w
                    and box_y <= row["y"] < box_y + box_h):
                continue
            # ROI 左缘的裁断残片（面板行被切掉正文，只剩名字）不是气泡
            if edge_x is not None and row["x"] < edge_x:
                continue
        messages.append(_make_message(
            "", text, row["confidence"], row["y"],
            is_self=False, kind="bubble",
        ))
    return messages


def _finalize(messages: list[dict], limit) -> list[dict]:
    """公共收尾：噪音过滤 → 消息级去重 → 按 y 升序 → 取最近 N 条。

    ``limit`` 取最近 N 条（按 y 最大的 N 条）后仍按 y 升序返回；
    ``limit <= 0`` 表示 0 条；``limit is None`` 表示不限制。
    """
    deduped: list[dict] = []
    seen: list[tuple[str, int]] = []
    for message in messages:
        if _is_noise_text(message["text"]):
            continue
        key = _normalize_text(message["text"])
        if any(key == seen_key and abs(message["y"] - seen_y) <= DEDUP_Y_TOLERANCE
               for seen_key, seen_y in seen):
            continue
        seen.append((key, message["y"]))
        deduped.append(message)

    ordered = sorted(deduped, key=lambda message: message["y"])

    if limit is not None:
        try:
            limit_value = int(limit)
        except (TypeError, ValueError):
            raise ValueError(f"limit 必须是整数: {limit!r}") from None
        if limit_value <= 0:
            return []
        if len(ordered) > limit_value:
            # 取最近 N 条（y 最大），再按 y 升序返回
            ordered = ordered[-limit_value:]

    return [{field: message[field] for field in _MESSAGE_FIELDS}
            for message in ordered]


def parse_chat_items(items: list[dict], *,
                     own_texts: tuple[str, ...] | list[str] = (),
                     mode: str = "panel",
                     limit: int = 20,
                     image_size: tuple[int, int] | dict | None = None) -> list[dict]:
    """把 OCR items 解析成结构化的聊天消息列表。

    参数
    ----
    items:
        OCR 结果，字段 ``text/confidence/x/y``（``width/height`` 可选，坐标已还原到
        原图）。允许乱序、允许重复，本函数内部会排序去重。
    own_texts:
        最近自己发出去的聊天文本（server 的 ``send_chat`` 记录）。
        与其中任一条高度相似（标点归一 + SequenceMatcher > 0.75）的消息标
        ``is_self=True``。
    mode:
        ``"panel"``（默认，聊天面板「内容 - 名字」格式）或 ``"bubble"``
        （世界内气泡，每行一条消息、无名字）。其他值抛 ``ValueError``。
    limit:
        最多返回最近多少条（按 y 最大的 N 条，再按 y 升序）。
    image_size:
        可选，截图 ``(宽, 高)`` 或 ``{"width","height"}``。``server`` 的
        ``read_chat`` 会传。传了以后 panel 模式才启用真机严格规则：
        - 无落款裸行只有与 ``own_texts`` 相似、或「贴左边（x<0.10W）+ 屏幕下半部
          （y>0.72H）+ 文本≥2 字符 + 不是名字碎片」时才当自己消息，其余丢弃；
        - 带落款消息的正文里如果只是名字碎片（等于 speaker / 陌生人/光之子/
          玩家/旅人）也丢弃。
        不传时退回旧行为（所有无落款行都按自己消息处理），保持向后兼容。

    返回
    ----
    list[dict]：每项字段固定为
    ``{"speaker", "text", "confidence", "y", "is_self", "kind"}``。

    规则（与需求 R2 对应）
    ----------------------
    - 过滤：空行、纯数字、纯点号/省略号、``SKIP_TEXTS``（聊天/发送/ENTER/菜单词
      等）、置信度 < 0.35、名字碎片（陌生人 等）；
    - 去重：同 y ±8 px 且文本相同只留一条；
    - panel 模式：只信任「- 名字」尾缀 + 非空正文的行；紧贴的裸行作为长消息
      换行的正文前缀合并；无名裸行只有 own_texts 相似 / 底部气泡区（见
      ``image_size``）才当自己的消息（``is_self=True``）；落款单独成行时把
      攒着的正文归给该名字；面板中上部的名字碎片一律丢弃；
    - bubble 模式：每行一条合理气泡，``speaker=""``、``kind="bubble"``、
      ``is_self=False``；带落款的面板行、单字符碎片、名字噪音丢弃；
    - 排序：按 y 升序；同一帧重复调用结果稳定。
    """
    normalized_mode = str(mode or "panel").strip().lower()
    if normalized_mode not in ("panel", "bubble"):
        raise ValueError(f"mode 必须是 'panel' 或 'bubble'，收到: {mode!r}")

    own = own_texts or ()
    if isinstance(own, str):  # 容错：误传单条字符串时当成一条
        own = (own,)

    rows = _prepare_rows(items)
    if not rows:
        return []
    if normalized_mode == "bubble":
        messages = _bubble_messages(rows, image_size)
    else:
        messages = _panel_messages(rows, own, image_size)
    return _finalize(messages, limit)


# ===========================================================================
# 面板是否打开的启发式
# ===========================================================================

def panel_looks_open(items: list[dict],
                     image_size: tuple[int, int] | dict) -> bool | None:
    """根据聊天 ROI 内的 OCR 结果启发式判断聊天面板是否打开。

    - 聊天 ROI 内有 **≥2 行像消息的文字** → ``True``（面板打开且列表里有内容）；
      「像消息」= 带名字落款的面板行，或 x < 0.10 W 的裸行（面板里自己发的消息）；
      世界气泡（裸行但明显靠右）不算；
    - 聊天 ROI 底部出现输入框占位/按钮文字（「聊天」「发送」…）→ ``True``
      （面板打开但可能还没有消息）；
    - 只有 1 行像消息的文字 → ``None``（可能是面板里唯一一条，也可能是世界画面
      误识别，不猜）；
    - 有文字但全是 UI 噪音/低置信 → ``False``（OCR 正常但 ROI 里没有聊天特征）；
    - 没有任何可用文字 → ``None``（OCR 可能失败，无从判断）。

    说明：读取聊天时 server 只 OCR 聊天 ROI，所以世界画面文字很少；但世界里的
    聊天气泡也可能落在这个 ROI 里，启发式无法区分「面板消息」和「世界气泡」，
    因此宁可返回 ``None`` 也不乱下结论。
    """
    if not isinstance(items, (list, tuple)) or not items:
        return None
    try:
        roi_x, roi_y, roi_w, roi_h = chat_region(image_size)
    except (TypeError, ValueError):
        return None
    if roi_w <= 0 or roi_h <= 0:
        return None

    width = _image_width(image_size) or float(roi_w) / CHAT_ROI_REL[2]
    self_max_x = width * PANEL_SELF_MAX_X_FRAC
    input_band_top = roi_y + roi_h * 0.90  # 输入框在 ROI 最底部（约最后 10%）
    usable_count = 0
    message_like = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        confidence = _to_float(item.get("confidence"), 1.0)
        if confidence < MIN_CONFIDENCE:
            continue
        x = _to_int(item.get("x"), 0)
        y = _to_int(item.get("y"), 0)
        # 只统计落在聊天 ROI 内的行（item 坐标是文字框左上角）
        if not (roi_x <= x < roi_x + roi_w and roi_y <= y < roi_y + roi_h):
            continue
        normalized = _normalize_text(text)
        if not normalized:
            continue  # 纯标点
        usable_count += 1
        if y >= input_band_top and normalized in _INPUT_HINT_TEXTS:
            return True  # 输入框特征：面板打开（哪怕还没有消息）
        if _is_skip_text(text) or _is_noise_text(text):
            continue
        has_speaker = _split_speaker(text)[1] is not None
        if has_speaker or x < self_max_x:
            message_like += 1
        # 靠右的裸行是世界气泡，不算面板消息行

    if message_like >= 2:
        return True
    if message_like == 1:
        return None
    if usable_count > 0:
        # ROI 里读到了文字，但既不是消息行也不是输入框特征 → 多半面板没开
        return False
    return None
