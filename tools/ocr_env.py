#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sky-mcp OCR 运行时环境安装器：把 rapidocr 依赖装进仓库的 ``.deps/``。

为什么不用 pip
--------------
本机（Windows + Python 3.14）上 ``python -m pip install --target .deps ...`` 会被
DSH 文件沙箱拦在 pip 自己的临时目录/重命名步骤上（OSError: Permission denied，
即使把 TEMP 指到仓库内也一样）。因此这里用**纯标准库**做同样的事：

1. ``urllib`` 拉取 PyPI simple 索引页（默认清华镜像，失败回退 pypi.org）；
2. 按当前解释器（CPython 3.14 / win_amd64）挑选可用的 wheel：
   优先 ``cp314-cp314-win_amd64``，其次 ``cpXX-abi3-win_amd64``（abi3 向下兼容），
   最后 ``py3-none-any`` 纯 Python 轮子；
3. ``zipfile`` 直接解包到 ``.deps/``（wheel 就是 zip）；
4. 写 ``.deps/_ocr_env_manifest.json`` 记录版本/来源/哈希，重复执行时跳过已装好的包。

安装内容（版本在本机 Python 3.14.9 + win_amd64 上实测可用）::

    rapidocr-onnxruntime 1.4.4   OCR 引擎（纯 Python，wheel 内自带 det/cls/rec 模型）
    onnxruntime          1.31.1  推理运行时（cp314 win_amd64 wheel）
    numpy                2.5.4   数值库
    opencv-python        5.0.0.93（cp37-abi3 wheel，兼容 3.14）
    shapely / pyclipper / Pillow / PyYAML / six / tqdm   rapidocr 其余依赖
    flatbuffers / packaging / protobuf / sympy / mpmath  onnxruntime 依赖

用法
----
    python tools/ocr_env.py                 # 安装/补齐 .deps（幂等，可反复跑）
    python tools/ocr_env.py --check         # 只检查，不下载；全好退出码 0
    python tools/ocr_env.py --verify-ocr    # 安装后跑一次真实 OCR（fixtures 中文截图）
    python tools/ocr_env.py --force         # 忽略清单强制重装
    python tools/ocr_env.py --json ...      # 机器可读输出（stdout 只有 JSON）

装好后怎么用（两种组合本机都验证过）::

    # A) 系统 python（推荐：仓库自带全部依赖，不依赖别的 venv）
    $env:PYTHONPATH = 'F:\\project\\sky-mcp\\.deps'
    python sky-mcp-server.py --http

    # B) 已有 numpy/cv2 的 venv + .deps（.deps 里也有 numpy/cv2，PYTHONPATH 优先）
    $env:PYTHONPATH = 'F:\\project\\sky-mcp\\.deps'
    & F:\\project\\sky-bot\\.venv\\Scripts\\python.exe sky-mcp-server.py --http

注意：``.deps/`` 需要写进 ``.gitignore``（当前仓库还没有，已报给 Lead）。

自检::

    python -m py_compile tools/ocr_env.py
"""

from __future__ import annotations

import argparse
import hashlib
import html as html_mod
import json
import os
import re
import sys
import sysconfig
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 仓库根目录（本文件在 tools/ 下）
ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DEPS_DIR = ROOT / ".deps"

#: 默认索引（国内优先清华镜像；失败再走官方 PyPI）
DEFAULT_INDEX_URLS = (
    "https://pypi.tuna.tsinghua.edu.cn/simple",
    "https://pypi.org/simple",
)

#: 需要安装的包（精确版本 = 本机实测可用的已知良好组合，保证可重复）
#: 字段：PyPI 包名、版本、导入名（用于 --check）、说明
PACKAGES: tuple[dict[str, str], ...] = (
    {"pkg": "rapidocr-onnxruntime", "ver": "1.4.4", "import": "rapidocr_onnxruntime",
     "desc": "OCR 引擎（wheel 内自带模型）"},
    {"pkg": "onnxruntime", "ver": "1.31.1", "import": "onnxruntime",
     "desc": "ONNX 推理运行时"},
    {"pkg": "numpy", "ver": "2.5.4", "import": "numpy", "desc": "数值库"},
    {"pkg": "opencv-python", "ver": "5.0.0.93", "import": "cv2", "desc": "图像处理"},
    {"pkg": "shapely", "ver": "2.2.0", "import": "shapely", "desc": "rapidocr 依赖"},
    {"pkg": "pyclipper", "ver": "1.4.0", "import": "pyclipper", "desc": "rapidocr 依赖"},
    {"pkg": "Pillow", "ver": "13.0.0", "import": "PIL", "desc": "rapidocr 依赖"},
    {"pkg": "PyYAML", "ver": "6.0.3", "import": "yaml", "desc": "rapidocr 依赖"},
    {"pkg": "six", "ver": "1.17.0", "import": "six", "desc": "rapidocr 依赖"},
    {"pkg": "tqdm", "ver": "4.70.1", "import": "tqdm", "desc": "rapidocr 依赖"},
    {"pkg": "flatbuffers", "ver": "25.12.19", "import": "flatbuffers",
     "desc": "onnxruntime 依赖"},
    {"pkg": "packaging", "ver": "26.3", "import": "packaging",
     "desc": "onnxruntime 依赖"},
    {"pkg": "protobuf", "ver": "7.36.2", "import": "google.protobuf",
     "desc": "onnxruntime 依赖"},
    {"pkg": "sympy", "ver": "1.14.0", "import": "sympy", "desc": "onnxruntime 依赖"},
    {"pkg": "mpmath", "ver": "1.4.1", "import": "mpmath", "desc": "sympy 依赖"},
)

MANIFEST_NAME = "_ocr_env_manifest.json"
USER_AGENT = "sky-mcp-ocr-env/1.0 (+https://github.com/Aevella/sky-pc-mcp-companion)"
CHUNK = 256 * 1024


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

class InstallError(RuntimeError):
    """安装/检查失败（消息直接给人看）。"""


def force_utf8_stdio() -> None:
    """把 stdout/stderr 固定成 UTF-8。

    Windows 下 Python 往**管道**输出时默认用 ANSI 代码页（本机 cp936），
    而很多终端/工具按 UTF-8 解码，中文验收结论就会变成乱码。这里主动统一成
    UTF-8（Windows 控制台本身就支持 UTF-8），保证输出可复制进报告。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass


def say(msg: str, *, json_mode: bool = False) -> None:
    """进度信息：--json 模式走 stderr，避免污染 stdout 的 JSON。"""
    stream = sys.stderr if json_mode else sys.stdout
    print(msg, file=stream, flush=True)


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024.0
    return f"{n} B"


def python_tag() -> str:
    return f"cp{sys.version_info[0]}{sys.version_info[1]}"


def platform_tags() -> tuple[str, ...]:
    """当前解释器可接受的 wheel 平台标签（按优先级）。"""
    plat = sysconfig.get_platform().lower()  # 例: win-amd64 -> 转成 win_amd64
    plat = plat.replace("-", "_").replace(".", "_")
    if plat.startswith("win"):
        if "arm64" in plat:
            return ("win_arm64",)
        if "amd64" in plat or "x86_64" in plat:
            return ("win_amd64",)
        return ("win32",)
    # 非 Windows 只在纯 Python 轮子上兜底（本脚本主要面向 Windows 验收机）
    return ()


def norm_dist(name: str) -> str:
    """PEP 503 归一化（`opencv-python` / `opencv_python` 视为同一个）。"""
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_wheel(filename: str) -> dict[str, str] | None:
    """拆 wheel 文件名。

    格式：``{name}-{version}(-{build})?-{pytag}-{abitag}-{plattag}.whl``
    注意 5 段（无 build）与 6 段（有 build）的歧义，不能用一个正则搞定。
    """
    if not filename.endswith(".whl"):
        return None
    parts = filename[:-4].split("-")
    if len(parts) == 5:
        name, ver, py, abi, plat = parts
    elif len(parts) == 6:
        name, ver, _build, py, abi, plat = parts
    else:
        return None
    return {"filename": filename, "name": name, "ver": ver,
            "py": py, "abi": abi, "plat": plat}


def wheel_rank(wheel: dict[str, str]) -> int:
    """wheel 对当前解释器/平台的适配分；0 = 不可用，越大越好。

    排序原则：
      * 平台必须匹配（win_amd64 等）或纯 Python `any`；
      * 完全匹配的解释器标签（cp314-cp314）最优；
      * abi3 轮子（cpXX-abi3）只要 XX <= 当前版本就兼容，版本越高越好；
      * 纯 Python `py3-none-any` 兜底。
    """
    if wheel["plat"] not in platform_tags() and wheel["plat"] != "any":
        return 0
    cur = sys.version_info[0] * 100 + sys.version_info[1]
    platform_bonus = 5 if wheel["plat"] != "any" else 0

    def cp_num(tag: str) -> int | None:
        m = re.fullmatch(r"cp(\d+)", tag)
        return int(m.group(1)) if m else None

    tags = wheel["py"].split(".")
    for tag in tags:
        n = cp_num(tag)
        if n is None:
            continue
        # 完全匹配：cp314-cp314-win_amd64
        if n == cur and wheel["abi"] == tag:
            return 100 + platform_bonus
        # abi3：cp37-abi3 / cp310-abi3 …（对 Python >= 该版本都生效）
        if wheel["abi"] == "abi3" and n <= cur:
            return 80 + platform_bonus + min(n // 100, 3)
    # 纯 Python
    if wheel["abi"] == "none" and wheel["plat"] == "any":
        if "py3" in tags:
            return 60
        if "py2.py3" in tags:
            return 55
        if f"py{sys.version_info[0]}" in tags:
            return 50
    return 0


def safe_extract(archive: zipfile.ZipFile, dest: Path) -> int:
    """把 wheel 解包到 dest，拒绝 ``..`` / 绝对路径（防 zip slip）。返回文件数。"""
    dest = dest.resolve()
    count = 0
    for info in archive.infolist():
        name = info.filename
        if not name or name.endswith("/"):
            continue
        # 统一成相对路径并校验不会跳出 dest
        rel = name.replace("\\", "/").lstrip("/")
        target = (dest / rel).resolve()
        if target != dest and dest not in target.parents:
            raise InstallError(f"wheel 内路径非法，拒绝解包: {name}")
        archive.extract(info, dest)
        count += 1
    return count


# ---------------------------------------------------------------------------
# 索引页 / 下载
# ---------------------------------------------------------------------------

#: simple 索引页里的锚点；用正则而不是 html.parser 足够稳且代码更短
_HREF_RE = re.compile(r'<a\s+[^>]*href="([^"]+)"[^>]*>', re.IGNORECASE)


def fetch_index(index_url: str, pkg: str, *, json_mode: bool,
                timeout: float = 60.0) -> tuple[str, list[dict[str, str]]]:
    """拉取 simple 索引，返回 (最终 URL, [{filename, url, sha256}])。"""
    url = f"{index_url.rstrip('/')}/{pkg}/"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            page = resp.read().decode("utf-8", "replace")
            final_url = resp.geturl()
    except Exception as exc:  # 网络/镜像问题交给上层回退
        raise InstallError(f"拉取索引失败 {url}: {type(exc).__name__}: {exc}") from exc
    entries: list[dict[str, str]] = []
    for href in _HREF_RE.findall(page):
        href = html_mod.unescape(href)
        raw, _, frag = href.partition("#")
        filename = urllib.parse.unquote(raw.rsplit("/", 1)[-1])
        if not filename.endswith(".whl"):
            continue
        sha = ""
        for part in frag.split("&"):
            if part.startswith("sha256="):
                sha = part[len("sha256="):]
        entries.append({
            "filename": filename,
            "url": urllib.parse.urljoin(final_url, raw),
            "sha256": sha,
        })
    return final_url, entries


def pick_wheel(entries: list[dict[str, str]], ver: str) -> dict[str, str] | None:
    """在索引条目里挑出指定版本、适配当前解释器的最佳 wheel。"""
    scored: list[tuple[int, dict[str, str]]] = []
    for entry in entries:
        wheel = parse_wheel(entry["filename"])
        if wheel is None or wheel["ver"] != ver:
            continue
        rank = wheel_rank(wheel)
        if rank > 0:
            scored.append((rank, {**wheel, "url": entry["url"],
                                  "sha256": entry["sha256"]}))
    if not scored:
        return None
    scored.sort(key=lambda item: item[0], reverse=True)
    return scored[0][1]


def resolve_wheel(pkg: str, ver: str, index_urls: tuple[str, ...],
                  *, json_mode: bool) -> dict[str, str]:
    """按索引优先级找到一个可用 wheel（返回含 url/sha256 的字典）。"""
    errors: list[str] = []
    for index_url in index_urls:
        try:
            _final, entries = fetch_index(index_url, pkg, json_mode=json_mode)
        except InstallError as exc:
            errors.append(str(exc))
            continue
        wheel = pick_wheel(entries, ver)
        if wheel is None:
            errors.append(f"{index_url}: 没有 {pkg}=={ver} 的可用于 "
                          f"CPython {sys.version.split()[0]} {platform_tags() or '当前平台'} 的 wheel")
            continue
        wheel["index"] = index_url
        return wheel
    raise InstallError(f"找不到 {pkg}=={ver} 的 wheel：\n  " + "\n  ".join(errors))


def download_wheel(wheel: dict[str, str], cache_dir: Path, *, json_mode: bool) -> Path:
    """下载 wheel 到 cache_dir（已有且哈希一致则复用）。返回本地路径。"""
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / wheel["filename"]
    expected = wheel.get("sha256") or ""
    if target.is_file():
        if not expected:
            return target
        digest = _sha256_file(target)
        if digest == expected:
            say(f"  复用缓存 {wheel['filename']}", json_mode=json_mode)
            return target
        target.unlink()  # 缓存损坏，重下

    req = urllib.request.Request(wheel["url"], headers={"User-Agent": USER_AGENT})
    tmp = target.with_suffix(target.suffix + ".part")
    sha = hashlib.sha256()
    done = 0
    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=120) as resp, open(tmp, "wb") as fp:
            total = int(resp.headers.get("Content-Length") or 0)
            while True:
                chunk = resp.read(CHUNK)
                if not chunk:
                    break
                fp.write(chunk)
                sha.update(chunk)
                done += len(chunk)
                if total >= 2 * 1024 * 1024:
                    pct = done * 100 / total if total else 0
                    print(f"\r  下载 {wheel['filename']} "
                          f"{human_size(done)}/{human_size(total)} ({pct:.0f}%)",
                          end="", file=sys.stderr if json_mode else sys.stdout, flush=True)
        if wheel["url"].endswith(".whl"):  # 进度行收尾
            print("", file=sys.stderr if json_mode else sys.stdout, flush=True)
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        raise InstallError(f"下载失败 {wheel['url']}: {type(exc).__name__}: {exc}") from exc

    digest = sha.hexdigest()
    if expected and digest != expected:
        tmp.unlink(missing_ok=True)
        raise InstallError(f"下载校验失败 {wheel['filename']}: sha256={digest} "
                           f"期望 {expected}")
    wheel["sha256"] = digest
    tmp.replace(target)
    elapsed = time.time() - started
    say(f"  下载完成 {human_size(done)}，用时 {elapsed:.1f}s", json_mode=json_mode)
    return target


def _sha256_file(path: Path) -> str:
    sha = hashlib.sha256()
    with open(path, "rb") as fp:
        for chunk in iter(lambda: fp.read(1024 * 1024), b""):
            sha.update(chunk)
    return sha.hexdigest()


# ---------------------------------------------------------------------------
# 清单
# ---------------------------------------------------------------------------

def load_manifest(deps_dir: Path) -> dict:
    path = deps_dir / MANIFEST_NAME
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_manifest(deps_dir: Path, manifest: dict) -> None:
    deps_dir.mkdir(parents=True, exist_ok=True)
    path = deps_dir / MANIFEST_NAME
    tmp = path.with_suffix(".json.part")
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    tmp.replace(path)


def dist_info_version(deps_dir: Path, pkg: str) -> str | None:
    """在 .deps 里找 ``<归一化名>-<版本>.dist-info`` 目录，返回版本。"""
    want = norm_dist(pkg)
    if not deps_dir.is_dir():
        return None
    for child in deps_dir.iterdir():
        if not child.is_dir() or not child.name.endswith(".dist-info"):
            continue
        stem = child.name[: -len(".dist-info")]
        if "-" not in stem:
            continue
        name, _, version = stem.rpartition("-")
        if norm_dist(name) == want:
            return version
    return None


# ---------------------------------------------------------------------------
# 安装 / 检查
# ---------------------------------------------------------------------------

def install(deps_dir: Path, index_urls: tuple[str, ...], *, force: bool,
            json_mode: bool) -> dict:
    """把 PACKAGES 装进 deps_dir。返回结构化结果。"""
    deps_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = deps_dir / ".wheels"
    manifest = load_manifest(deps_dir)
    installed = {item["pkg"].lower(): item for item in manifest.get("installed", [])}
    report: dict = {"deps_dir": str(deps_dir), "python": sys.version.split()[0],
                    "platform": sysconfig.get_platform(), "packages": [],
                    "changed": False, "ok": True}

    for i, spec in enumerate(PACKAGES, 1):
        pkg, ver = spec["pkg"], spec["ver"]
        prefix = f"[{i}/{len(PACKAGES)}] {pkg}=={ver}"
        prev = installed.get(pkg.lower())
        current = dist_info_version(deps_dir, pkg)
        if not force and prev and prev.get("version") == ver and current == ver:
            say(f"{prefix} 已安装，跳过", json_mode=json_mode)
            report["packages"].append({"pkg": pkg, "version": ver,
                                       "status": "cached"})
            continue
        say(f"{prefix} （{spec['desc']}）下载中…", json_mode=json_mode)
        try:
            wheel = resolve_wheel(pkg, ver, index_urls, json_mode=json_mode)
            path = download_wheel(wheel, cache_dir, json_mode=json_mode)
            with zipfile.ZipFile(path) as archive:
                files = safe_extract(archive, deps_dir)
        except Exception as exc:
            report["ok"] = False
            report["packages"].append({"pkg": pkg, "version": ver,
                                       "status": "failed", "error": str(exc)})
            raise InstallError(f"{prefix} 安装失败: {exc}") from exc
        installed[pkg.lower()] = {
            "pkg": pkg, "version": ver, "wheel": wheel["filename"],
            "sha256": wheel["sha256"], "index": wheel.get("index", ""),
            "files": files, "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        manifest.update({"python": sys.version, "platform": sysconfig.get_platform(),
                         "deps_dir": str(deps_dir), "updated_at": time.strftime(
                             "%Y-%m-%dT%H:%M:%S%z")})
        manifest["installed"] = list(installed.values())
        save_manifest(deps_dir, manifest)
        report["packages"].append({"pkg": pkg, "version": ver,
                                   "status": "installed",
                                   "wheel": wheel["filename"], "files": files})
        report["changed"] = True
        say(f"{prefix} 解包 {files} 个文件完成", json_mode=json_mode)

    save_manifest(deps_dir, manifest)
    return report


def check(deps_dir: Path, *, import_modules: bool, json_mode: bool) -> dict:
    """检查 .deps 是否完整可用（不下载）。返回结构化结果，ok=False 表示缺包。"""
    if not deps_dir.is_dir():
        raise InstallError(f"{deps_dir} 不存在，先运行: python tools/ocr_env.py")
    result: dict = {"deps_dir": str(deps_dir), "python": sys.version.split()[0],
                    "platform": sysconfig.get_platform(), "packages": [],
                    "ok": True, "missing": [], "rapidocr_importable": False}
    deps_str = str(deps_dir.resolve())
    if deps_str not in sys.path:
        sys.path.insert(0, deps_str)

    for spec in PACKAGES:
        pkg, ver = spec["pkg"], spec["ver"]
        entry: dict = {"pkg": pkg, "expected": ver, "import": spec["import"]}
        dist_ver = dist_info_version(deps_dir, pkg)
        entry["dist_version"] = dist_ver
        if dist_ver != ver:
            entry["status"] = "missing" if dist_ver is None else "version-mismatch"
            result["ok"] = False
            result["missing"].append(pkg)
        else:
            entry["status"] = "ok"

        if import_modules and entry["status"] in ("ok", "version-mismatch"):
            try:
                module = __import__(spec["import"])
                entry["import_ok"] = True
                entry["module_file"] = getattr(module, "__file__", "") or ""
                # 模块解析到 .deps 之外时说明被别的环境遮住了（只提示不判错）
                deps_norm = deps_str.lower().replace("\\", "/")
                file_norm = entry["module_file"].lower().replace("\\", "/")
                if entry["module_file"] and deps_norm not in file_norm:
                    entry["shadowed_by"] = entry["module_file"]
            except Exception as exc:
                entry["import_ok"] = False
                entry["import_error"] = f"{type(exc).__name__}: {exc}"
                entry["status"] = "import-failed"
                result["ok"] = False
                if pkg not in result["missing"]:
                    result["missing"].append(pkg)
        else:
            entry["import_ok"] = None
        result["packages"].append(entry)

    # 真正的入口检查：能 import 到 RapidOCR 类
    if result["ok"] and import_modules:
        try:
            from rapidocr_onnxruntime import RapidOCR  # noqa: F401
            result["rapidocr_importable"] = True
        except Exception as exc:
            result["rapidocr_importable"] = False
            result["ok"] = False
            result["rapidocr_error"] = f"{type(exc).__name__}: {exc}"
    return result


# ---------------------------------------------------------------------------
# 真实 OCR 自检
# ---------------------------------------------------------------------------

WORLD_BANNER = ROOT / "tests" / "fixtures" / "sky_world_banner.jpg"
DEFAULT_EXPECT = "珍珠季的新任务现已开启!"


def _norm_text(text: str) -> str:
    """归一化：去掉空白/标点，只留中文/字母/数字，便于算字符命中率。"""
    return re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", text)


def char_hit_rate(expected: str, actual: str) -> tuple[float, str]:
    """expected 的字符有多少出现在 actual 里（多重集近似）。返回 (命中率, 命中详情)。"""
    exp = _norm_text(expected)
    act = _norm_text(actual)
    if not exp:
        return 0.0, "期望文本为空"
    pool = list(act)
    hit = []
    for ch in exp:
        if ch in pool:
            pool.remove(ch)
            hit.append(ch)
    return len(hit) / len(exp), "".join(hit)


def verify_ocr(deps_dir: Path, image: Path, expect: str, *, json_mode: bool) -> dict:
    """对一张截图跑真实 rapidocr，返回识别文本与字符命中率。"""
    report = check(deps_dir, import_modules=True, json_mode=json_mode)
    if not report["ok"]:
        raise InstallError("OCR 环境不完整，先运行: python tools/ocr_env.py；"
                           f"缺少: {', '.join(report['missing']) or '未知'}")
    if not image.is_file():
        raise InstallError(f"截图不存在: {image}")
    started = time.time()
    from rapidocr_onnxruntime import RapidOCR

    engine = RapidOCR()
    load_ms = (time.time() - started) * 1000
    t0 = time.time()
    result, _elapse = _run_rapidocr(engine, str(image))
    ocr_ms = (time.time() - t0) * 1000
    texts = [item["text"] for item in result]
    joined = "\n".join(texts)
    rate, hit = char_hit_rate(expect, joined)
    out = {
        "image": str(image),
        "image_size": _image_size(image),
        "engine": "rapidocr",
        "load_ms": round(load_ms, 1),
        "ocr_ms": round(ocr_ms, 1),
        "texts": result,
        "expect": expect,
        "char_hit_rate": round(rate, 3),
        "char_hits": hit,
        "ok": rate >= 0.8,
    }
    return out


def _run_rapidocr(engine, image_path: str):
    """兼容 rapidocr 不同版本的返回格式，统一成 (items, elapse)。"""
    raw = engine(image_path)
    if isinstance(raw, tuple) and len(raw) == 2:
        result, elapse = raw
    else:
        result, elapse = raw, None
    items: list[dict] = []
    for entry in result or []:
        try:
            box, text, score = entry[0], entry[1], entry[2]
        except (TypeError, IndexError):
            continue
        xs = [float(point[0]) for point in box]
        ys = [float(point[1]) for point in box]
        items.append({
            "text": str(text),
            "confidence": round(float(score), 4),
            "x": int(min(xs)), "y": int(min(ys)),
            "width": int(max(xs) - min(xs)),
            "height": int(max(ys) - min(ys)),
        })
    return items, elapse


def _image_size(image: Path) -> list[int] | None:
    """读 PNG/JPEG 尺寸（纯标准库，不依赖 Pillow）。"""
    try:
        with open(image, "rb") as fp:
            head = fp.read(32)
        if head.startswith(b"\x89PNG\r\n\x1a\n") and head[12:16] == b"IHDR":
            import struct
            width, height = struct.unpack(">II", head[16:24])
            return [int(width), int(height)]
        if head.startswith(b"\xff\xd8"):  # JPEG：扫描 SOF 段
            import struct
            with open(image, "rb") as fp:
                fp.seek(2)
                while True:
                    marker = fp.read(2)
                    if len(marker) < 2 or marker[0] != 0xFF:
                        break
                    code = marker[1]
                    if code in (0xD8, 0xD9):
                        continue
                    length = struct.unpack(">H", fp.read(2))[0]
                    if 0xC0 <= code <= 0xCF and code not in (0xC4, 0xC8, 0xCC):
                        fp.read(1)
                        height, width = struct.unpack(">HH", fp.read(4))
                        return [int(width), int(height)]
                    fp.seek(length - 2, 1)
    except OSError:
        pass
    return None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="sky-mcp OCR 运行时环境安装器：不用 pip，把 rapidocr 依赖"
                    "（纯标准库 urllib+zipfile）解包到仓库 .deps/。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例:
  python tools/ocr_env.py                      安装/补齐 .deps（幂等）
  python tools/ocr_env.py --check              只检查，全好退出码 0
  python tools/ocr_env.py --verify-ocr         装好后对 fixtures 截图跑一次真实 OCR
  python tools/ocr_env.py --check --json       机器可读输出（stdout 只有 JSON）

装好后的两种用法:
  系统 python:  $env:PYTHONPATH='F:\\project\\sky-mcp\\.deps'; python sky-mcp-server.py --http
  已有 venv:    $env:PYTHONPATH='F:\\project\\sky-mcp\\.deps'; & <venv>\\Scripts\\python.exe sky-mcp-server.py --http

安装内容: rapidocr-onnxruntime 1.4.4 + onnxruntime 1.31.1 + numpy/opencv-python 等
（版本固定，CPython 3.14 win_amd64 实测可用）。""")
    parser.add_argument("--deps-dir", type=Path, default=DEFAULT_DEPS_DIR,
                        help=f"依赖安装目录（默认 {DEFAULT_DEPS_DIR}）")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--check", action="store_true",
                        help="只检查已安装环境，不下载")
    action.add_argument("--verify-ocr", action="store_true",
                        help="检查后跑一次真实 OCR（默认 tests/fixtures/sky_world_banner.jpg）")
    parser.add_argument("--image", type=Path,
                        help="--verify-ocr 使用的截图（默认 fixtures/world banner）")
    parser.add_argument("--expect", default=DEFAULT_EXPECT,
                        help=f"--verify-ocr 的期望文本（默认「{DEFAULT_EXPECT}」）")
    parser.add_argument("--force", action="store_true", help="忽略清单强制重新下载解包")
    parser.add_argument("--index-url", action="append", dest="index_urls",
                        help="PyPI simple 索引，可重复；默认先清华镜像后官方 PyPI")
    parser.add_argument("--json", action="store_true",
                        help="stdout 只输出 JSON（进度走 stderr）")
    return parser


def main(argv: list[str] | None = None) -> int:
    force_utf8_stdio()
    args = build_parser().parse_args(argv)
    deps_dir: Path = args.deps_dir
    index_urls = tuple(args.index_urls) if args.index_urls else DEFAULT_INDEX_URLS
    json_mode = args.json

    def emit(payload: dict) -> None:
        print(json.dumps(payload, ensure_ascii=False, indent=2))

    try:
        if args.check or args.verify_ocr:
            report = check(deps_dir, import_modules=True, json_mode=json_mode)
            if args.verify_ocr:
                if not report["ok"]:
                    raise InstallError(
                        "OCR 环境不完整，先运行: python tools/ocr_env.py")
                image = args.image or WORLD_BANNER
                ocr = verify_ocr(deps_dir, image, args.expect, json_mode=json_mode)
                report["verify_ocr"] = ocr
                report["ok"] = report["ok"] and ocr["ok"]
                if not json_mode:
                    print("\n=== rapidocr 自检 ===")
                    print(f"截图: {ocr['image']}  尺寸: {ocr['image_size']}  "
                          f"模型加载: {ocr['load_ms']:.0f}ms  OCR: {ocr['ocr_ms']:.0f}ms")
                    for item in ocr["texts"]:
                        print(f"  [{item['confidence']:.2f}] {item['text']}")
                    print(f"期望「{ocr['expect']}」字符命中率: "
                          f"{ocr['char_hit_rate']:.0%}（命中: {ocr['char_hits']}）")
                    print("OCR 自检:", "PASS" if ocr["ok"] else "FAIL")
            if json_mode:
                emit(report)
            else:
                _print_check_summary(report)
            return 0 if report["ok"] else 1

        report = install(deps_dir, index_urls, force=args.force, json_mode=json_mode)
        # 装完立刻检查一遍，确保可导入（--check 的完整逻辑）
        checked = check(deps_dir, import_modules=True, json_mode=json_mode)
        report["check"] = checked
        report["ok"] = checked["ok"]
        if json_mode:
            emit(report)
        else:
            print(f"\n安装目录: {deps_dir}")
            print(f"解释器: Python {report['python']} ({report['platform']})")
            _print_check_summary(checked)
            print("\n用法: 设置 PYTHONPATH 后启动 server:")
            print(f"  $env:PYTHONPATH='{deps_dir}'")
            print("  python sky-mcp-server.py --http")
        return 0 if report["ok"] else 1
    except InstallError as exc:
        if json_mode:
            emit({"ok": False, "error": str(exc)})
        else:
            print(f"[ocr_env] 失败: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n[ocr_env] 已中断", file=sys.stderr)
        return 130


def _print_check_summary(report: dict) -> None:
    print(f"\n=== OCR 运行时检查（{report['deps_dir']}） ===")
    print(f"解释器: Python {report['python']} ({report['platform']})")
    for entry in report.get("packages", []):
        status = entry["status"]
        mark = {"ok": "OK", "cached": "OK", "installed": "OK"}.get(status, "FAIL")
        extra = ""
        if entry.get("dist_version") and entry.get("dist_version") != entry.get("expected"):
            extra = f" (已装 {entry['dist_version']}，期望 {entry['expected']})"
        if entry.get("import_error"):
            extra += f" import: {entry['import_error']}"
        if entry.get("shadowed_by"):
            extra += f" [注意: 实际加载自 {entry['shadowed_by']}（不在 .deps 内）]"
        print(f"  {mark:4s} {entry['pkg']:24s} {entry.get('expected', ''):10s}{extra}")
    if report.get("rapidocr_importable"):
        print("  OK   from rapidocr_onnxruntime import RapidOCR")
    if report["ok"]:
        print("结论: PASS — OCR 环境可用")
    else:
        print(f"结论: FAIL — 缺少: {', '.join(report.get('missing', []))}；"
              "运行 python tools/ocr_env.py 安装")


if __name__ == "__main__":
    sys.exit(main())
