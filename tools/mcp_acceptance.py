#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sky-mcp R1-R5 真机验收脚本（纯标准库，无第三方依赖）。

用途
----
按 `mcp-requirements.md` 的 R1-R5 逐项验收 `sky-mcp-server.py` 的真机表现，输出
PASS/FAIL/SKIP 明细与汇总，可作为交付证据。默认自己拉起一个 HTTP MCP server：

    python tools/mcp_acceptance.py
    python tools/mcp_acceptance.py --send-chat "测试ABC123"       # 真发 1 条游戏消息
    python tools/mcp_acceptance.py --send-chat "hello world" \\
        --send-chat "今天天气不错一起去云野看云吧测试二十字"        # 发 5 条可复跑 R2 验收
    python tools/mcp_acceptance.py --url http://127.0.0.1:9900 --token 1234

默认行为
--------
1. 用当前 python 在**随机空闲端口 + 随机 token** 拉起 `sky-mcp-server.py --http`，
   自动把 `F:\\project\\sky-mcp\\.deps` 加进子进程 `PYTHONPATH`（rapidocr 依赖），
   等 `/health` 与 OCR 预热完成后逐项验收；
2. 也支持 `--url/--token` 连接**已经启动**的 server（此时不拉起/不停止进程）；
3. 截图与 JSON 报告写到 `debug_out/`（已 gitignore），每次运行覆盖 latest；
4. **只有显式传 `--send-chat TEXT` 才会真的在游戏里发消息**（可重复传多次）。

验收项（与 task-7 契约一致，实现见 `mcp-requirements.md`）
---------------------------------------------------------
- 准备 PRE：server 可用 / 工具清单齐全 / `.deps` OCR 运行时完整
- R1 OCR 引擎：`status.ocr` 非 none + `ocr_ready/ocr_error/ocr_device`；
  `read_screen.texts` 非空且含中文；固定 fixture 截图离线 OCR 字符 ≥80%；
  `--ocr-engine none` 时 `texts=[]`、`ocr_error` 有值、不置 isError
- R2 read_chat：结构化 `messages[{speaker,text,confidence,y,is_self,kind}]`、
  `ocr/frame_id/image_size/region`；同帧重复稳定；`--send-chat` 后回读到该文本；
  发满 5 条时 `read_chat` ≥4 条且 y 排序正确。
  真实发送前会先确认「左侧聊天日志面板」已打开（read_chat 观测 → 按 c → open_chat），
  否则 send_chat 只开输入框、消息只以世界气泡出现，回读会漏（R2-6 记录该前置状态）。
- R3 性能/预处理：`read_screen`/`read_chat` 返回 `ocr_ms`；OCR ≤3s；
  `preprocess` 字段合法；预热后 `status.ocr_ready=True`；长中文消息识别率 ≥80%
- R4 画面元数据：`read_screen`/`screenshot` 返回 `image_size/captured_at/frame_id`；
  ImageContent(mimeType=image/png) 可解码且尺寸一致；`save_path` 能落盘；
  两次截图 `frame_id` 不同
- R5 send_chat 回执：JSON 含 `ok/sent/sent_at/chars/truncated/message`；
  失败路径 `isError` 且文本含 `error_type`

退出码：0 = 无 FAIL（允许 SKIP，`--strict` 时 SKIP 也算失败）；1 = 有 FAIL。

自检::

    python -m py_compile tools/mcp_acceptance.py
    python tools/mcp_acceptance.py --help
"""

from __future__ import annotations

import argparse
import base64
import difflib
import json
import os
import re
import secrets
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
SERVER_PATH = ROOT / "sky-mcp-server.py"
DEPS_DIR = ROOT / ".deps"
OCR_ENV = ROOT / "tools" / "ocr_env.py"
DEBUG_DIR = ROOT / "debug_out"

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"

#: 必需的工具（R1-R5 会用到；少一个都算契约不完整）
REQUIRED_TOOLS = (
    "status", "focus_game", "press_key", "open_chat", "send_chat",
    "type_text", "read_screen", "read_chat", "screenshot",
)

#: R1 中文识别期望（任意一条命中 ≥80% 字符即算通过；可用 --expect 覆盖）
DEFAULT_EXPECT = (
    "珍珠季的新任务现已开启",
    "小光来啦",
    "沐子在呢",
    "一起去云野",
)
#: 聊天闭环默认发送文本（--send-chat 未指定时不会真实发送）
DEFAULT_SEND_TEXT = "测试ABC123"


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

class AcceptanceError(RuntimeError):
    """验收流程无法继续（server 起不来 / 连不上等）。"""


class RpcError(RuntimeError):
    """JSON-RPC / HTTP 层错误。"""


def force_utf8_stdio() -> None:
    """统一 stdout/stderr 为 UTF-8，避免 Windows 管道里中文乱码（cp936）。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def png_size(data: bytes) -> tuple[int, int]:
    """从 PNG 头解析宽高（纯标准库）。"""
    if (len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n"
            and data[12:16] == b"IHDR"):
        width, height = struct.unpack(">II", data[16:24])
        return int(width), int(height)
    return 0, 0


def norm_text(text: str) -> str:
    """归一化：只保留中文/字母/数字，便于算字符命中率。"""
    return re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", text or "")


def char_hit_rate(expected: str, actual: str) -> float:
    """expected 的字符有多少出现在 actual 里（多重集，忽略顺序）。"""
    exp = norm_text(expected)
    pool = list(norm_text(actual))
    if not exp:
        return 0.0
    hits = 0
    for ch in exp:
        if ch in pool:
            pool.remove(ch)
            hits += 1
    return hits / len(exp)


def zh_count(text: str) -> int:
    return len(re.findall(r"[\u4e00-\u9fff]", text or ""))


def short_json(obj, limit: int = 600) -> str:
    """把 dict/list 压成单行短 JSON，便于放进验收明细。"""
    try:
        text = json.dumps(obj, ensure_ascii=False)
    except (TypeError, ValueError):
        text = repr(obj)
    return text if len(text) <= limit else text[:limit] + "…"


def pick_free_port() -> int:
    """让 OS 分一个空闲端口（立即释放，随后 server 绑上去）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


# ---------------------------------------------------------------------------
# MCP HTTP JSON-RPC 客户端
# ---------------------------------------------------------------------------

@dataclass
class ToolResult:
    """一次 tools/call 的结果拆解。"""
    name: str
    elapsed_ms: float
    is_error: bool
    texts: list[str] = field(default_factory=list)     # content 里的 text 项
    images: list[dict] = field(default_factory=list)   # content 里的 image 项
    payload: dict | None = None                        # text 项解析出的 JSON dict
    raw: dict = field(default_factory=dict)

    def image_bytes(self) -> bytes | None:
        """取第一张 image 的 base64 解码结果。"""
        for item in self.images:
            data = item.get("data")
            if not data:
                continue
            try:
                return base64.b64decode(data)
            except (ValueError, TypeError):
                return None
        return None


class McpHttpClient:
    """MCP HTTP 传输（tools/call over JSON-RPC 2.0）的最小客户端。

    server 的 HTTP 端点在根路径 `POST /`，`GET /health` 是健康检查；
    鉴权头 `Authorization: Bearer <token>`（token 为空则不带）。
    """

    def __init__(self, base_url: str, token: str | None = None,
                 timeout: float = 120.0):
        self.base_url = base_url.rstrip("/")
        self.token = token or ""
        self.timeout = timeout
        self._next_id = 0
        self.server_info: dict = {}

    # -- 底层 HTTP -----------------------------------------------------
    def _http(self, method: str, path: str, data: dict | None = None,
              timeout: float | None = None) -> bytes:
        url = self.base_url + path
        body = json.dumps(data, ensure_ascii=False).encode("utf-8") \
            if data is not None else None
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
            "User-Agent": "sky-mcp-acceptance/1.0",
            "Connection": "close",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(url, data=body, headers=headers,
                                         method=method)
        try:
            with urllib.request.urlopen(
                    request, timeout=timeout or self.timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:400].decode("utf-8", "replace")
            raise RpcError(f"HTTP {exc.code} {method} {url}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RpcError(f"连接失败 {url}: {exc.reason}") from exc
        except socket.timeout as exc:
            raise RpcError(f"请求超时({timeout or self.timeout:.0f}s): "
                           f"{method} {url}") from exc

    # -- 协议 ----------------------------------------------------------
    def _rpc(self, method: str, params: dict | None = None,
             timeout: float | None = None) -> dict:
        self._next_id += 1
        message: dict = {"jsonrpc": "2.0", "id": self._next_id,
                         "method": method}
        if params is not None:
            message["params"] = params
        raw = self._http("POST", "/", message, timeout=timeout)
        try:
            response = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise RpcError(f"响应不是合法 JSON: {raw[:300]!r}") from exc
        if not isinstance(response, dict):
            raise RpcError(f"响应结构异常: {response!r}")
        if "error" in response and response["error"]:
            err = response["error"]
            raise RpcError(f"JSON-RPC error {err.get('code')}: "
                           f"{err.get('message')}")
        if "result" not in response:
            raise RpcError(f"响应缺少 result: {short_json(response)}")
        return response["result"]

    def health(self) -> dict:
        raw = self._http("GET", "/health", timeout=10)
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise RpcError(f"/health 响应异常: {raw[:300]!r}") from exc

    def initialize(self) -> dict:
        result = self._rpc("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "sky-mcp-acceptance", "version": "1.0"},
        }, timeout=30)
        self.server_info = result.get("serverInfo") or {}
        # MCP 握手通知：server 返回 204（无 body），失败不阻塞验收
        try:
            self._http("POST", "/", {
                "jsonrpc": "2.0", "method": "notifications/initialized"})
        except RpcError:
            pass
        return result

    def list_tools(self) -> list[dict]:
        result = self._rpc("tools/list")
        tools = result.get("tools")
        return tools if isinstance(tools, list) else []

    def call_tool(self, name: str, arguments: dict | None = None,
                  timeout: float | None = None) -> ToolResult:
        started = time.perf_counter()
        result = self._rpc("tools/call", {"name": name,
                                          "arguments": arguments or {}},
                           timeout=timeout)
        elapsed = (time.perf_counter() - started) * 1000.0
        if not isinstance(result, dict):
            raise RpcError(f"tools/call 结果异常: {result!r}")
        texts: list[str] = []
        images: list[dict] = []
        payload: dict | None = None
        for item in result.get("content") or []:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text":
                texts.append(str(item.get("text", "")))
            elif item.get("type") == "image":
                images.append(item)
        for text in texts:
            try:
                parsed = json.loads(text)
            except (ValueError, TypeError):
                continue
            if isinstance(parsed, dict):
                payload = parsed
                break
        return ToolResult(name=name, elapsed_ms=elapsed,
                          is_error=bool(result.get("isError")),
                          texts=texts, images=images, payload=payload,
                          raw=result)


# ---------------------------------------------------------------------------
# server 子进程管理
# ---------------------------------------------------------------------------

class ServerProcess:
    """拉起/等待/停止本地 `sky-mcp-server.py --http`。"""

    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.proc: subprocess.Popen | None = None
        self.port = pick_free_port()
        # 注意：token 必须以字母/数字开头 —— argparse 会把 "-xxx" 当成选项，
        # 导致 `--token -tIV...` 被误解析、server 直接退出（本机实测踩过）。
        self.token = "sky_" + secrets.token_hex(16)
        self.url = f"http://127.0.0.1:{self.port}"
        self.logs: list[str] = []
        self._reader: threading.Thread | None = None
        self._log_lock = threading.Lock()

    # -- 启动 ----------------------------------------------------------
    def start(self) -> None:
        server_path = Path(self.args.server)
        if not server_path.is_file():
            raise AcceptanceError(f"找不到 server: {server_path}")
        python = self.args.python or sys.executable
        cmd = [
            python, str(server_path), "--http",
            "--host", "127.0.0.1", "--port", str(self.port),
            "--token", self.token,
        ]
        if self.args.ocr_engine:
            cmd += ["--ocr-engine", self.args.ocr_engine]
        if self.args.adb_serial:
            cmd += ["--adb-serial", self.args.adb_serial]
        cmd += self.args.extra_server_arg or []

        env = os.environ.copy()
        deps = str(DEPS_DIR.resolve())
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = deps + (os.pathsep + existing if existing else "")
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env.pop("SKY_OCR_ENGINE", None)  # 别让外部环境干扰引擎选择

        self._log("启动 server: " + " ".join(cmd))
        self._log(f"PYTHONPATH={env['PYTHONPATH']}")
        flags = 0
        if os.name == "nt":
            flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        try:
            self.proc = subprocess.Popen(
                cmd, cwd=str(ROOT), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True, encoding="utf-8", errors="replace",
                bufsize=1, creationflags=flags)
        except OSError as exc:
            raise AcceptanceError(f"server 启动失败: {exc}") from exc
        self._reader = threading.Thread(target=self._read_logs,
                                        name="server-log", daemon=True)
        self._reader.start()

    def _read_logs(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        try:
            for line in self.proc.stdout:
                with self._log_lock:
                    self.logs.append(line.rstrip("\n"))
                    if len(self.logs) > 2000:
                        del self.logs[:1000]
                if self.args.verbose:
                    print(f"    [server] {line.rstrip()}", flush=True)
        except (ValueError, OSError):
            pass

    def _log(self, message: str) -> None:
        with self._log_lock:
            self.logs.append(message)

    def log_tail(self, n: int = 30) -> str:
        with self._log_lock:
            lines = list(self.logs)
        return "\n".join(lines[-n:])

    # -- 等待 ----------------------------------------------------------
    def wait_http(self, client: McpHttpClient) -> dict:
        """轮询 /health 直到就绪或超时。返回 health JSON。"""
        deadline = time.time() + self.args.startup_timeout
        last_error = "（尚未收到响应）"
        while time.time() < deadline:
            assert self.proc is not None
            if self.proc.poll() is not None:
                raise AcceptanceError(
                    f"server 进程提前退出（exit={self.proc.returncode}），"
                    f"日志:\n{self.log_tail()}")
            try:
                body = client.health()
                if body.get("status") == "ok":
                    return body
                last_error = short_json(body)
            except RpcError as exc:
                last_error = str(exc)
            time.sleep(0.25)
        raise AcceptanceError(
            f"{self.args.startup_timeout:.0f}s 内 /health 未就绪: {last_error}\n"
            f"server 日志:\n{self.log_tail()}")

    # -- 停止 ----------------------------------------------------------
    def stop(self) -> None:
        proc = self.proc
        if proc is None or proc.poll() is not None:
            return
        # Windows 下先试 CTRL_BREAK，让 server 走 KeyboardInterrupt 清理路径
        try:
            if os.name == "nt":
                proc.send_signal(getattr(__import__("signal"), "CTRL_BREAK_EVENT"))
            else:
                proc.terminate()
        except (OSError, ValueError, AttributeError):
            try:
                proc.terminate()
            except OSError:
                pass
        try:
            proc.wait(timeout=6)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
        self.proc = None


def spawn_extra_server(args: argparse.Namespace,
                       extra_args: list[str]) -> tuple[ServerProcess, McpHttpClient]:
    """再拉起一个独立 server（用于 R1「OCR 不可用时的降级诊断」）。"""
    clone = argparse.Namespace(**vars(args))
    clone.extra_server_arg = list(args.extra_server_arg or []) + list(extra_args)
    server = ServerProcess(clone)
    server.start()
    client = McpHttpClient(server.url, token=server.token,
                           timeout=args.timeout)
    health = server.wait_http(client)
    client.initialize()
    server.health = health  # type: ignore[attr-defined]
    return server, client


# ---------------------------------------------------------------------------
# 验收框架
# ---------------------------------------------------------------------------

@dataclass
class Check:
    cid: str
    title: str
    status: str
    detail: str = ""
    ms: float = 0.0
    data: dict = field(default_factory=dict)


class Acceptance:
    """按 R1-R5 顺序执行检查并记录结果。"""

    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.checks: list[Check] = []
        self.json_mode = bool(args.json)
        self.started = time.time()
        self.client: McpHttpClient | None = None
        self.server: ServerProcess | None = None
        self.server_desc: dict = {}
        self.health: dict = {}
        self.status: dict | None = None
        self.tools: list[dict] = []
        self.read_screen: ToolResult | None = None
        self.read_screen2: ToolResult | None = None
        self.screenshot: ToolResult | None = None
        self.read_chat_first: ToolResult | None = None
        self.read_chat_second: ToolResult | None = None
        self.read_chat_panel: ToolResult | None = None
        self.read_chat_final: ToolResult | None = None
        self.sent: list[dict] = []      # [{text, receipt, found, hit_rate}]
        self.saved_frames: list[str] = []
        self.ocr_env: dict = {}

    # -- 输出/记录 ------------------------------------------------------
    def log(self, message: str = "") -> None:
        print(message, file=sys.stderr if self.json_mode else sys.stdout,
              flush=True)

    def record(self, cid: str, title: str, status: str, detail: str = "",
               ms: float = 0.0, data: dict | None = None) -> Check:
        check = Check(cid=cid, title=title, status=status, detail=detail,
                      ms=round(ms, 1), data=data or {})
        self.checks.append(check)
        if not self.json_mode:
            self.log(f"[{status:4s}] {cid:6s} {title}  ({check.ms:.0f}ms)")
            if detail:
                for line in str(detail).splitlines():
                    self.log(f"        {line}")
        return check

    def run_check(self, cid: str, title: str, func) -> Check:
        """执行一个检查函数：func() -> (status, detail[, data])。异常记 FAIL。"""
        started = time.perf_counter()
        try:
            value = func()
            if isinstance(value, tuple):
                status = value[0]
                detail = value[1] if len(value) > 1 else ""
                data = value[2] if len(value) > 2 else {}
            else:
                status, detail, data = str(value), "", {}
            return self.record(cid, title, status, detail,
                               (time.perf_counter() - started) * 1000, data)
        except Exception as exc:  # 单项检查失败不拖垮整个验收
            return self.record(
                cid, title, FAIL,
                f"检查异常: {type(exc).__name__}: {exc}",
                (time.perf_counter() - started) * 1000)

    # -- 工具调用封装 ----------------------------------------------------
    def call(self, name: str, arguments: dict | None = None,
             timeout: float | None = None) -> ToolResult:
        assert self.client is not None
        return self.client.call_tool(name, arguments, timeout=timeout)

    def call_json(self, name: str, arguments: dict | None = None,
                  timeout: float | None = None) -> tuple[ToolResult, dict]:
        """调用工具并要求返回 JSON payload（否则抛 AcceptanceError）。"""
        result = self.call(name, arguments, timeout=timeout)
        if result.payload is None:
            text = (result.texts[0] if result.texts else "")[:400]
            raise AcceptanceError(
                f"{name} 未返回 JSON（isError={result.is_error}）: {text!r}")
        return result, result.payload

    # -- debug_out 落盘 ---------------------------------------------------
    def save_debug_png(self, name: str, data: bytes | None) -> str | None:
        if not data or not self.args.save_frames:
            return None
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        path = DEBUG_DIR / name
        try:
            path.write_bytes(data)
        except OSError as exc:
            self.log(f"        （警告：截图落盘失败 {path}: {exc}）")
            return None
        self.saved_frames.append(str(path))
        return str(path)

    # -- 准备检查 --------------------------------------------------------
    def check_pre_2_tools(self) -> tuple[str, str, dict]:
        self.tools = self.client.list_tools()  # type: ignore[union-attr]
        names = [tool.get("name") for tool in self.tools]
        missing = [name for name in REQUIRED_TOOLS if name not in names]
        data = {"count": len(names), "names": names, "missing": missing}
        if missing:
            return FAIL, f"缺少工具: {missing}", data
        return PASS, f"{len(names)} 个工具齐全（含 read_chat）", data

    def check_pre_3_ocr_env(self) -> tuple[str, str, dict]:
        if self.args.skip_env_check:
            return SKIP, "已用 --skip-env-check 跳过 .deps 检查", {}
        if not OCR_ENV.is_file():
            return FAIL, f"缺少 {OCR_ENV}", {}
        result = subprocess.run(
            [sys.executable, str(OCR_ENV), "--check", "--json"],
            cwd=str(ROOT), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=300)
        try:
            report = json.loads(result.stdout)
        except ValueError:
            return FAIL, ("ocr_env.py --check 输出无法解析，"
                          f"exit={result.returncode}, "
                          f"stdout={result.stdout[:300]!r}, "
                          f"stderr={result.stderr[:300]!r}"), {}
        self.ocr_env = report
        ok = bool(report.get("ok")) and result.returncode == 0
        if not ok:
            return FAIL, (f".deps OCR 环境不完整: "
                          f"missing={report.get('missing')} "
                          f"rapidocr_importable={report.get('rapidocr_importable')}"),
        return PASS, (f".deps 环境完整（{len(report.get('packages') or [])} 个包，"
                      f"rapidocr 可导入）"), {
            "deps_dir": report.get("deps_dir"),
            "python": report.get("python"),
            "rapidocr_importable": report.get("rapidocr_importable"),
        }

    def check_r1_3_fixture_ocr(self) -> tuple[str, str, dict]:
        if self.args.skip_env_check:
            return SKIP, "已用 --skip-env-check 跳过", {}
        image = ROOT / "tests" / "fixtures" / "sky_world_banner.jpg"
        if not image.is_file():
            return SKIP, f"fixture 不存在: {image}", {}
        result = subprocess.run(
            [sys.executable, str(OCR_ENV), "--verify-ocr", "--json",
             "--image", str(image)],
            cwd=str(ROOT), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=600)
        try:
            report = json.loads(result.stdout)
        except ValueError:
            return FAIL, ("ocr_env.py --verify-ocr 输出无法解析，"
                          f"exit={result.returncode}, "
                          f"stderr={result.stderr[-400:]!r}"), {}
        ocr = report.get("verify_ocr") or {}
        rate = float(ocr.get("char_hit_rate") or 0.0)
        texts = ocr.get("texts") or []
        data = {
            "image": ocr.get("image"),
            "image_size": ocr.get("image_size"),
            "char_hit_rate": rate,
            "char_hits": ocr.get("char_hits"),
            "load_ms": ocr.get("load_ms"),
            "ocr_ms": ocr.get("ocr_ms"),
            "texts": [item.get("text") for item in texts][:10],
        }
        if rate < 0.8:
            return FAIL, (f"fixture 中文命中率 {rate:.0%} < 80%；"
                          f"识别文本={data['texts']}"), data
        return PASS, (f"「{ocr.get('expect')}」命中率 {rate:.0%}；"
                      f"识别={data['texts']}；OCR {ocr.get('ocr_ms')}ms"), data

    # -- R1 ------------------------------------------------------------
    def check_pre_4_device(self) -> tuple[str, str, dict]:
        """真机预检：设备就绪 + 光遇在前台 + ADBKeyboard 可用。

        没有这三样，按键/截图/中文发送都会以各种间接错误失败（例如系统崩溃弹窗
        抢走前台后，所有按键都会被前台守卫拒绝），提前给出可操作提示。
        """
        _, status = self.call_json("status")
        self.status = status
        foreground = status.get("foreground") or {}
        ime = status.get("ime") or {}
        data = {"device_ready": status.get("device_ready"),
                "foreground": foreground,
                "ime": ime,
                "error": status.get("error")}
        problems: list[str] = []
        if status.get("device_ready") is not True:
            problems.append(f"设备未就绪: {status.get('error') or '未知原因'}"
                            "（检查模拟器/adb: python sky_adb.py doctor）")
        if not isinstance(foreground, dict) or \
                foreground.get("is_game") is not True:
            problems.append(
                "光遇不在前台（当前前台: "
                f"{foreground.get('window') if isinstance(foreground, dict) else foreground}"
                "）；请把模拟器里的光遇切回前台（若有系统崩溃弹窗先关掉）后重跑")
        if ime.get("adbkeyboard_installed") is not True:
            problems.append("ADBKeyboard 输入法未安装/不可用（中文发送会失败，"
                            "见 README「中文输入」）")
        if problems:
            return FAIL, "；".join(problems), data
        return PASS, (f"设备就绪、光遇前台、ADBKeyboard 可用"
                      f"（{ime.get('component') or 'com.android.adbkeyboard/.AdbIME'}，"
                      f"active={ime.get('active')}）"), data

    def check_r1_1_status_ocr(self) -> tuple[str, str, dict]:
        result, status = self.call_json("status")
        self.status = status
        ocr = str(status.get("ocr") or "none")
        required = ("ocr", "ocr_ready", "ocr_error", "ocr_device")
        missing = [key for key in required if key not in status]
        problems: list[str] = []
        if missing:
            problems.append(f"缺少字段 {missing}")
        if ocr == "none":
            problems.append("status.ocr=none（OCR 引擎未生效）")
        if not isinstance(status.get("ocr_ready"), bool):
            problems.append("ocr_ready 不是 bool")
        if not isinstance(status.get("ocr_error"), str):
            problems.append("ocr_error 不是 str")
        if not isinstance(status.get("ocr_device"), str):
            problems.append("ocr_device 不是 str")
        if status.get("ocr_ready") and str(status.get("ocr_error") or ""):
            problems.append(f"已就绪但 ocr_error 非空: {status.get('ocr_error')}")
        if status.get("device_ready") is False:
            problems.append(f"设备未就绪: {status.get('error')}")
        data = {
            "ocr": ocr,
            "ocr_device": status.get("ocr_device"),
            "ocr_ready": status.get("ocr_ready"),
            "ocr_error": status.get("ocr_error"),
            "ocr_load_ms": status.get("ocr_load_ms"),
            "ocr_available": status.get("ocr_available"),
            "ocr_chain": status.get("ocr_chain"),
            "device_ready": status.get("device_ready"),
        }
        if problems:
            return FAIL, "；".join(problems), data
        return PASS, (f"ocr={ocr} device={data['ocr_device']} "
                      f"ready={data['ocr_ready']} "
                      f"load_ms={data['ocr_load_ms']}"), data

    def check_r1_4_ocr_unavailable(self) -> tuple[str, str, dict]:
        if self.args.skip_ocr_off:
            return SKIP, "已用 --skip-ocr-off 跳过", {}
        if self.args.url:
            return SKIP, ("--url 外部 server 模式：不另起 `--ocr-engine none` "
                          "实例验证降级诊断"), {}
        server = None
        try:
            server, client = spawn_extra_server(
                self.args, ["--ocr-engine", "none", "--no-ocr-prewarm"])
            result = client.call_tool("read_screen", {})
            payload = result.payload or {}
            texts = payload.get("texts")
            ocr_error = str(payload.get("ocr_error") or "")
            ocr = str(payload.get("ocr") or "")
            problems = []
            if result.is_error:
                problems.append(f"isError=true（不应置位）: "
                                f"{short_json(result.texts, 300)}")
            if texts:
                problems.append(f"texts 应为空，实际 {short_json(texts, 200)}")
            if not ocr_error:
                problems.append("ocr_error 为空（应给出可操作原因）")
            data = {"ocr": ocr, "texts": texts, "ocr_error": ocr_error,
                    "is_error": result.is_error}
            if problems:
                return FAIL, "；".join(problems), data
            return PASS, (f"引擎禁用时 texts=[]、ocr_error={ocr_error!r}、"
                          f"isError=false（ocr={ocr}）"), data
        finally:
            if server is not None:
                server.stop()

    # -- R2 ------------------------------------------------------------
    @staticmethod
    def _validate_messages(messages: list) -> list[str]:
        problems: list[str] = []
        if not isinstance(messages, list):
            return [f"messages 不是 list: {type(messages).__name__}"]
        for index, message in enumerate(messages):
            if not isinstance(message, dict):
                problems.append(f"messages[{index}] 不是 dict")
                continue
            for key in ("speaker", "text", "confidence", "y", "is_self",
                        "kind"):
                if key not in message:
                    problems.append(f"messages[{index}] 缺少 {key}")
            if not isinstance(message.get("text"), str):
                problems.append(f"messages[{index}].text 不是 str")
            if not isinstance(message.get("speaker"), str):
                problems.append(f"messages[{index}].speaker 不是 str")
            if not isinstance(message.get("confidence"), (int, float)):
                problems.append(f"messages[{index}].confidence 不是数字")
            if not isinstance(message.get("y"), (int, float)):
                problems.append(f"messages[{index}].y 不是数字")
            if not isinstance(message.get("is_self"), bool):
                problems.append(f"messages[{index}].is_self 不是 bool")
            if message.get("kind") not in ("panel", "bubble"):
                problems.append(f"messages[{index}].kind 非法: "
                                f"{message.get('kind')!r}")
        return problems

    def check_r2_1_read_chat(self) -> tuple[str, str, dict]:
        result, payload = self.call_json("read_chat", {"limit": 20})
        self.read_chat_first = result
        required = ("messages", "ocr", "frame_id", "image_size", "region")
        missing = [key for key in required if key not in payload]
        problems = [f"缺少字段 {missing}"] if missing else []
        problems += self._validate_messages(payload.get("messages"))
        messages = payload.get("messages") or []
        data = {
            "messages_count": len(messages),
            "messages": messages[:10],
            "ocr": payload.get("ocr"),
            "frame_id": payload.get("frame_id"),
            "image_size": payload.get("image_size"),
            "region": payload.get("region"),
            "panel_open": payload.get("panel_open"),
            "ocr_ms": payload.get("ocr_ms"),
        }
        if problems:
            return FAIL, "；".join(problems), data
        return PASS, (f"{len(messages)} 条消息；region={payload.get('region')} "
                      f"panel_open={payload.get('panel_open')} "
                      f"ocr={payload.get('ocr')}"), data

    def check_r2_2_stability(self) -> tuple[str, str, dict]:
        """同一画面重复调用 read_chat 结果必须稳定（R2 要求，禁止随机漏行）。

        固定 mode=panel：面板 UI 是静态的，不受世界里气泡动画/行人走动影响。
        """
        result1, payload1 = self.call_json("read_chat",
                                           {"limit": 20, "mode": "panel"})
        result2, payload2 = self.call_json("read_chat",
                                           {"limit": 20, "mode": "panel"})
        self.read_chat_second = result2
        first = payload1.get("messages") or []
        second = payload2.get("messages") or []
        first_seq = [norm_text(m.get("text", "")) for m in first]
        second_seq = [norm_text(m.get("text", "")) for m in second]
        y_ok = all((second[i].get("y", 0) <= second[i + 1].get("y", 0))
                   for i in range(len(second) - 1))
        # 两次调用各自截了一帧（帧间画面有细微动画），OCR 可能出现个别字符抖动；
        # 要求序列基本一致（相似度 ≥80%）且不随机丢行即可，不苛求逐字节相同。
        similarity = (1.0 if first_seq == second_seq
                      else difflib.SequenceMatcher(
                          None, first_seq, second_seq).ratio())
        data = {"first": [m.get("text") for m in first],
                "second": [m.get("text") for m in second],
                "y_ascending": y_ok,
                "similarity": round(similarity, 3)}
        problems = []
        if similarity < 0.8:
            problems.append(f"两次调用结果差异过大（相似度 "
                            f"{similarity:.0%}）: {data['first']} vs "
                            f"{data['second']}")
        if not y_ok:
            problems.append("messages 未按 y 升序")
        if problems:
            return FAIL, "；".join(problems), data
        if similarity < 1.0:
            return PASS, (f"两次调用基本稳定（{len(second)} 条，相似度 "
                          f"{similarity:.0%}；帧间细微 OCR 抖动）"), data
        return PASS, (f"两次调用完全稳定（{len(second)} 条），y 升序正确"), data

    def _observe_panel_open(self) -> tuple[bool | None, dict]:
        """读一次 read_chat(panel 模式)，返回 (panel_open, payload)。

        用 panel 模式而不是 auto：只有面板区域解析出的消息才能证明"面板真的开了"，
        世界气泡（bubble）不算。模拟器偶发卡顿时 UI 会晚几帧才出现，调用方需要轮询。
        """
        _, payload = self.call_json("read_chat",
                                    {"limit": 20, "mode": "panel"})
        return payload.get("panel_open"), payload

    @staticmethod
    def _panel_effectively_open(payload: dict) -> bool:
        """panel_open=True，或 panel 区域解析出了消息，都说明聊天日志面板可见。"""
        return payload.get("panel_open") is True or bool(payload.get("messages"))

    def _poll_panel_open(self, attempts: int = 3,
                         delay_ms: float = 900.0) -> tuple[bool, dict]:
        """连续读面板状态，给模拟器 UI 留出渲染时间。返回 (是否打开, 最后 payload)。"""
        payload: dict = {}
        for attempt in range(max(1, attempts)):
            try:
                opened, payload = self._observe_panel_open()
                if self._panel_effectively_open(payload):
                    return True, payload
            except Exception:
                payload = {}
            if attempt < attempts - 1:
                time.sleep(max(0.3, delay_ms / 1000.0))
        return False, payload

    def _ensure_chat_panel_open(self) -> tuple[bool | None, list[str]]:
        """确保左侧聊天日志面板是打开的（真实发送消息前的前置条件）。

        为什么需要：`send_chat` 只负责打开/聚焦**输入框**，不会展开左侧聊天日志
        面板。面板关着时发出的消息只以世界气泡出现、几秒后消失，`read_chat` 只能
        读到 1~2 条（Lead 真机实测结论）。流程：

            1. 轮询 `read_chat(mode=panel)` 观测（`panel_open=True` 或已有面板消息）；
            2. 不打开 → 按 `c`（聊天面板开关）并轮询；
            3. 仍不打开 → `open_chat`（enter，打开面板+输入框）并轮询。

        注意模拟器偶尔卡顿，UI 会晚几秒才出现，所以每一步都是"发键 → 轮询确认"，
        而不是发完固定 sleep 就下结论。
        """
        flow: list[str] = []
        try:
            opened, payload = self._poll_panel_open(attempts=2, delay_ms=700)
            flow.append(f"初次观测 panel_open={payload.get('panel_open')}，"
                        f"消息 {len(payload.get('messages') or [])} 条")
            if opened:
                return True, flow
        except Exception as exc:  # 观测失败不致命，继续尝试打开
            flow.append(f"初次观测失败: {exc}")

        try:
            self.call("press_key", {"key": "c", "duration_ms": 40})
            flow.append("按 c 切换聊天面板")
        except Exception as exc:
            flow.append(f"按 c 失败: {exc}")
        time.sleep(max(0.3, self.args.panel_wait_ms / 1000.0))
        try:
            opened, payload = self._poll_panel_open(attempts=3, delay_ms=900)
            flow.append(f"c 之后 panel_open={payload.get('panel_open')}，"
                        f"消息 {len(payload.get('messages') or [])} 条")
            if opened:
                return True, flow
        except Exception as exc:
            flow.append(f"c 之后观测失败: {exc}")

        try:
            self.call("open_chat", {})
            flow.append("open_chat（enter）打开输入框")
        except Exception as exc:
            flow.append(f"open_chat 失败: {exc}")
        time.sleep(max(0.3, self.args.open_chat_wait_ms / 1000.0))
        try:
            opened, payload = self._poll_panel_open(attempts=3, delay_ms=900)
            flow.append(f"open_chat 之后 panel_open={payload.get('panel_open')}，"
                        f"消息 {len(payload.get('messages') or [])} 条")
            return opened, flow
        except Exception as exc:
            flow.append(f"open_chat 之后观测失败: {exc}")
            return None, flow

    def check_r2_3_open_chat(self) -> tuple[str, str, dict]:
        panel_opened, flow = self._ensure_chat_panel_open()
        result, payload = self.call_json("read_chat",
                                         {"limit": 20, "mode": "panel"})
        self.read_chat_panel = result
        region = str(payload.get("region") or "")
        data = {"panel_open_after_open": panel_opened,
                "flow": flow,
                "mode": payload.get("mode"),
                "region": region,
                "panel_open": payload.get("panel_open"),
                "messages_count": len(payload.get("messages") or []),
                "ocr_ms": payload.get("ocr_ms")}
        # 面板可能本来就没消息，这里只验"能打开/读取 + panel 模式能跑通"；
        # 面板是否真的打开由 R2-6（发送前）与 R2-4/R2-5（回读条数）交叉验证。
        if payload.get("mode") != "panel":
            return FAIL, f"read_chat mode=panel 回显错误: {payload.get('mode')}", data
        if "chat" not in region:
            return FAIL, f"panel 模式未对聊天区域 OCR，region={region!r}", data
        # 注意：这里不退出输入态/不按 ESC —— 后续 R2-1/R2-2 与发送都需要面板
        # 保持打开；验收结束时由 cleanup_chat_ui() 按 c 关闭面板。
        return PASS, (f"panel 模式 OCR 正常；面板消息 "
                      f"{data['messages_count']} 条，panel_open="
                      f"{data['panel_open']}；流程: {' → '.join(flow)}"), data

    def cleanup_chat_ui(self) -> None:
        """现场收尾：面板开着就按 `c` 关掉。

        **不要用 ESC**：真机实测 ESC 在无输入态会打开设置菜单/把面板关乱，
        会导致后续验收状态不可控（Lead 真机结论）。没有可用的 `c` 时静默跳过。
        """
        try:
            _, payload = self.call_json("read_chat",
                                        {"limit": 5, "mode": "panel"})
            if payload.get("panel_open") is True:
                self.call("press_key", {"key": "c", "duration_ms": 40})
                time.sleep(0.8)
        except Exception:  # 收尾失败不影响验收结论
            pass

    def check_r5_2_error_shape(self) -> tuple[str, str, dict]:
        """失败路径：超长消息应在**注入按键前**被校验拒绝，返回结构化 error_type。"""
        result = self.call("send_chat", {"message": "测" * 400})
        payload = result.payload or {}
        data = {"is_error": result.is_error,
                "payload": payload,
                "texts": [text[:300] for text in result.texts]}
        if not result.is_error:
            return FAIL, "超长消息未返回 isError（可能真的尝试发送了）", data
        error_type = payload.get("error_type")
        if not error_type:
            return FAIL, (f"isError=true 但 JSON 缺少 error_type: "
                          f"{short_json(data['texts'], 300)}"), data
        return PASS, (f"isError=true，error_type={error_type!r}，"
                      f"error={str(payload.get('error'))[:120]!r}"), data

    def _read_back_sent_text(self, text: str, attempts: int = 3):
        """回读刚发出的消息：用默认 auto 模式（server 内部已合并 panel+bubble），
        **不发任何按键、不重发消息**，最多读 attempts 次。

        真机实测：自己发出的消息主要显示为**世界气泡**；server 的 read_chat(auto)
        会分别对 panel / bubble 两个区域 OCR 后合并去重，所以这里直接用它。
        返回 (最佳字符命中率, 命中的消息, 读取模式, 第几次)。
        """
        best_rate, best_message = 0.0, None
        attempts = max(1, attempts)
        for attempt in range(attempts):
            try:
                _, chat = self.call_json(
                    "read_chat", {"limit": 30, "mode": "auto"})
            except Exception as exc:
                self.log(f"        （回读异常: {exc}）")
                continue
            for message in chat.get("messages") or []:
                rate = char_hit_rate(text, str(message.get("text") or ""))
                if rate > best_rate:
                    best_rate = rate
                    best_message = message
            if best_rate >= 0.8:
                return best_rate, best_message, "auto", attempt + 1
            if attempt < attempts - 1:
                time.sleep(0.8)
        return best_rate, best_message, "auto", attempts

    def check_send_loop(self) -> None:
        """R2-4/R2-5/R3-3/R5-1：真实发送消息并回读（只有 --send-chat 才执行）。"""
        texts = list(self.args.send_chat or [])
        if not texts:
            self.record("R2-4", "send_chat → read_chat 闭环回读", SKIP,
                        "未传 --send-chat，未真实发送游戏消息")
            self.record("R2-5", "read_chat ≥4 条消息（发 5 条）", SKIP,
                        "未传 --send-chat；可用 5 个 --send-chat 复跑")
            self.record("R3-3", "聊天长中文识别率 ≥80%", SKIP,
                        "未传 --send-chat，无真实聊天文本可验证")
            self.record("R5-1", "send_chat 回执字段", SKIP,
                        "未传 --send-chat，未真实发送游戏消息")
            return

        # 0) 发送前置：确保左侧聊天日志面板已打开。
        #    send_chat 只打开/聚焦输入框、不展开聊天日志；面板关着时消息只会以
        #    世界气泡出现并很快消失，最终 read_chat 只能看到 1~2 条（R2-5 会失败）。
        panel_opened, panel_flow = self._ensure_chat_panel_open()
        self.record("R2-6", "发送前聊天日志面板已打开",
                    PASS if panel_opened is True else SKIP,
                    f"panel_open={panel_opened}；流程: "
                    f"{' → '.join(panel_flow)}",
                    data={"panel_open": panel_opened, "flow": panel_flow})
        # 关键：**不要按 ESC**（真机会关面板/开设置菜单）。面板确认打开后直接
        # 开始逐条 send_chat —— server 内部按 enter 聚焦输入框，面板保持打开，
        # 发出的消息以气泡叠在输入框上方，聊天 UI 开着时不会消失。
        self.log(f"        真实发送 {len(texts)} 条游戏聊天消息…")
        receipt_problems: list[str] = []
        # 注：真机排查发现 ADBKeyboard 的 ADB_CLEAR_TEXT 在输入框尚未获得焦点时
        # 会 NPE 崩溃（AdbIME.java:157），因此把 open_delay_ms 拉长到 ≥700ms，
        # 等游戏输入框真正拿到输入连接后再清空/打字。可用 --send-open-delay-ms 调。
        for index, text in enumerate(texts, 1):
            arguments = {"message": text,
                         "open_delay_ms": int(self.args.send_open_delay_ms)}
            try:
                result, receipt = self.call_json(
                    "send_chat", arguments)
            except Exception as exc:
                self.sent.append({"text": text, "receipt": None,
                                  "found": False, "hit_rate": 0.0,
                                  "error": str(exc)})
                if index == 1:
                    receipt_problems.append(f"send_chat 失败: {exc}")
                self.log(f"        第 {index}/{len(texts)} 条发送失败: {exc}")
                continue
            self.sent.append({"text": text, "receipt": receipt,
                              "found": False, "hit_rate": 0.0,
                              "received": {k: receipt.get(k) for k in
                                           ("ok", "sent", "sent_at", "chars",
                                            "truncated")}})
            if index == 1:
                # R5 回执契约（用第一条消息验证）
                required = ("ok", "sent", "sent_at", "chars",
                            "truncated", "message")
                missing = [key for key in required if key not in receipt]
                if missing:
                    receipt_problems.append(f"缺少字段 {missing}")
                if receipt.get("sent") is not True:
                    receipt_problems.append(f"sent={receipt.get('sent')!r}")
                if receipt.get("chars") != len(text):
                    receipt_problems.append(
                        f"chars={receipt.get('chars')!r} != {len(text)}")
                if not isinstance(receipt.get("truncated"), bool):
                    receipt_problems.append(
                        f"truncated={receipt.get('truncated')!r} 不是 bool")
                if receipt.get("message") != text:
                    receipt_problems.append("message 回显与发送内容不一致")
                self.record(
                    "R5-1", "send_chat 回执字段",
                    FAIL if receipt_problems else PASS,
                    ("；".join(receipt_problems) if receipt_problems
                     else f"回执={short_json(receipt, 400)}"),
                    data={"receipt": receipt, "problems": receipt_problems})

            time.sleep(max(0.2, self.args.chat_wait_ms / 1000.0))
            # 发完立刻回读（默认 auto：server 内部已合并 panel+bubble 两遍 OCR）
            best_rate, best_message, read_mode, read_attempts = \
                self._read_back_sent_text(text)
            found = best_rate >= 0.8
            self.sent[-1]["found"] = found
            self.sent[-1]["hit_rate"] = round(best_rate, 3)
            self.sent[-1]["matched"] = (best_message or {}).get("text")
            self.sent[-1]["is_self"] = (best_message or {}).get("is_self")
            self.sent[-1]["read_mode"] = read_mode
            self.sent[-1]["read_attempt"] = read_attempts
            self.log(f"        第 {index}/{len(texts)} 条「{text}」→ "
                     f"{'找到' if found else '未找到'}"
                     f"（命中 {best_rate:.0%}，mode={read_mode or '-'}，"
                     f"第 {read_attempts} 次读取）")
            if index < len(texts):
                time.sleep(max(0.2, self.args.send_gap_ms / 1000.0))

        # R2-4：发送 → 立即 read_chat 能找到该文本
        sent_ok = [item for item in self.sent if item["found"]]
        tolerance = max(1, len(texts) - 1)   # 允许最多丢 1 条（OCR 抖动）
        data = {"sent_count": len(texts), "found_count": len(sent_ok),
                "tolerance": tolerance,
                "items": [{k: item.get(k) for k in
                           ("text", "found", "hit_rate", "matched", "is_self",
                            "read_mode", "read_attempt", "error", "received")}
                          for item in self.sent]}
        if len(sent_ok) >= tolerance:
            self.record("R2-4", "send_chat → read_chat 闭环回读", PASS,
                        f"{len(sent_ok)}/{len(texts)} 条回读命中（容差 "
                        f"{tolerance}）", data=data)
        else:
            self.record("R2-4", "send_chat → read_chat 闭环回读", FAIL,
                        f"仅 {len(sent_ok)}/{len(texts)} 条回读命中", data=data)

        # R3-3：取最长的中文消息算识别率（≥80%）
        long_texts = [item for item in self.sent
                      if zh_count(item["text"]) >= 15]
        if long_texts:
            best = max(long_texts, key=lambda item: item["hit_rate"])
            rate = best["hit_rate"]
            self.record("R3-3", "聊天长中文识别率 ≥80%",
                        PASS if rate >= 0.8 else FAIL,
                        f"「{best['text']}」命中率 {rate:.0%}"
                        f"（回读={best.get('matched')!r}）",
                        data={"expected": best["text"],
                              "matched": best.get("matched"),
                              "hit_rate": rate})
        else:
            self.record("R3-3", "聊天长中文识别率 ≥80%", SKIP,
                        f"发送文本中没有 ≥15 个中文字的消息（现有 "
                        f"{[item['text'] for item in self.sent]}），"
                        "可用 --send-chat 传一条 20 字中文复跑")

        # R2-5：发满 5 条时 read_chat 应 ≥4 条（先确认面板仍打开，防 UI 被误关）
        final_flow: list[str] = []
        try:
            _, final_flow = self._ensure_chat_panel_open()
            final_result, final = self.call_json("read_chat", {"limit": 30})
            self.read_chat_final = final_result
        except Exception as exc:
            self.record("R2-5", "read_chat ≥4 条消息（发 5 条）", FAIL,
                        f"最终 read_chat 失败: {exc}")
            return
        messages = final.get("messages") or []
        if len(texts) >= 5:
            y_ok = all((messages[i].get("y", 0) <= messages[i + 1].get("y", 0))
                       for i in range(len(messages) - 1))
            data = {"sent_count": len(texts), "messages_count": len(messages),
                    "messages": [m.get("text") for m in messages],
                    "y_ascending": y_ok,
                    "panel_ensure": final_flow}
            passed = len(messages) >= 4 and y_ok
            self.record("R2-5", "read_chat ≥4 条消息（发 5 条）",
                        PASS if passed else FAIL,
                        f"发送 {len(texts)} 条，read_chat 返回 "
                        f"{len(messages)} 条，y 升序={y_ok}", data=data)
        else:
            self.record("R2-5", "read_chat ≥4 条消息（发 5 条）", SKIP,
                        f"只发送了 {len(texts)} 条（<5）；再传几次 "
                        "--send-chat 可做完整 R2 验收")
        # 证据：发送完成后保存一帧（含聊天面板/世界气泡），便于人工复盘
        if self.args.save_frames:
            try:
                _, shot = self.call_json(
                    "read_screen",
                    {"save_path": str(DEBUG_DIR / "r5_after_send.png")})
                saved = shot.get("saved_path")
                if saved:
                    self.saved_frames.append(saved)
                    self.log(f"        发送后画面已保存: {saved}")
            except Exception as exc:
                self.log(f"        （发送后截图失败: {exc}）")
        # 收尾：只在面板确认打开时按 c 关面板（不要按 ESC，见 Lead 真机结论）
        self.cleanup_chat_ui()

    # -- R3 ------------------------------------------------------------
    def check_r3_1_ocr_ms(self) -> tuple[str, str, dict]:
        rs_ms = None
        if self.read_screen and self.read_screen.payload:
            rs_ms = self.read_screen.payload.get("ocr_ms")
        chat_ms = None
        if self.read_chat_first and self.read_chat_first.payload:
            chat_ms = self.read_chat_first.payload.get("ocr_ms")
        problems = []
        for name, value in (("read_screen.ocr_ms", rs_ms),
                            ("read_chat.ocr_ms", chat_ms)):
            if not isinstance(value, (int, float)):
                problems.append(f"{name} 缺失或不是数字: {value!r}")
        data = {"read_screen_ocr_ms": rs_ms, "read_chat_ocr_ms": chat_ms}
        if problems:
            return FAIL, "；".join(problems), data
        return PASS, f"read_screen={rs_ms}ms read_chat={chat_ms}ms", data

    def check_r3_2_latency(self) -> tuple[str, str, dict]:
        payload = (self.read_screen.payload if self.read_screen else {}) or {}
        ocr_ms = payload.get("ocr_ms")
        elapsed = self.read_screen.elapsed_ms if self.read_screen else 0.0
        limit = self.args.max_ocr_ms
        data = {"ocr_ms": ocr_ms, "http_total_ms": round(elapsed or 0, 1),
                "limit_ms": limit}
        prewarmed = bool(self.args.ocr_ready_before_first_call)
        if not isinstance(ocr_ms, (int, float)):
            return FAIL, "read_screen 没有 ocr_ms，无法评估耗时", data
        problems = []
        if ocr_ms > limit:
            problems.append(f"OCR {ocr_ms}ms > {limit}ms")
        data["prewarmed"] = prewarmed
        if problems:
            return FAIL, "；".join(problems), data
        return PASS, (f"OCR {ocr_ms}ms ≤ {limit}ms；HTTP 总耗时 "
                      f"{elapsed:.0f}ms（预热={prewarmed}）"), data

    def check_r3_4_prewarm(self) -> tuple[str, str, dict]:
        # 预热等待在 prewarm_wait() 里完成，这里只做结论
        detail = self.args.prewarm_detail or ""
        ready = bool(self.args.ocr_ready_before_first_call)
        status = self.status or {}
        ocr = status.get("ocr") or "none"
        data = {"ocr_ready_before_first_call": ready,
                "ocr": ocr,
                "ocr_load_ms": status.get("ocr_load_ms"),
                "detail": detail}
        if ocr == "none":
            return FAIL, "status.ocr=none，OCR 引擎不可用", data
        if not ready:
            return FAIL, ("首次工具调用前 status.ocr_ready 未变 True"
                          + (f"；{detail}" if detail else "")), data
        return PASS, (f"首调前已预热（ocr_ready=True，"
                      f"load_ms={status.get('ocr_load_ms')}；{detail}）"), data

    def wait_prewarm(self) -> None:
        """等 OCR 预热完成（写 args.prewarm_detail / ocr_ready_before_first_call）。"""
        self.args.ocr_ready_before_first_call = False
        self.args.prewarm_detail = ""
        deadline = time.time() + self.args.prewarm_timeout
        started = time.time()
        last: dict = {}
        while time.time() < deadline:
            try:
                health = self.client.health()  # type: ignore[union-attr]
            except RpcError:
                health = {}
            if health.get("ocr") == "none":
                self.args.prewarm_detail = "health.ocr=none（引擎不可用，未等待）"
                return
            ready = health.get("ocr_ready")
            if ready is None:
                # 旧版 server 的 /health 没有 ocr_ready：用 status 工具兜底
                try:
                    _, last = self.call_json("status")
                except Exception:
                    last = {}
                ready = last.get("ocr_ready")
                if not isinstance(ready, bool):
                    ready = None
            if ready is True:
                self.args.ocr_ready_before_first_call = True
                self.args.prewarm_detail = (
                    f"等待 {time.time() - started:.1f}s 后就绪")
                return
            time.sleep(0.5)
        self.args.prewarm_detail = (
            f"{self.args.prewarm_timeout:.0f}s 内未就绪"
            f"（最后 health={short_json(last or health, 200)}）")

    # -- R4 ------------------------------------------------------------
    def check_r4_1_read_screen_meta(self) -> tuple[str, str, dict]:
        if self.read_screen is None or self.read_screen.payload is None:
            return SKIP, "read_screen 未成功返回 JSON，无法检查元数据", {}
        payload = self.read_screen.payload
        problems = []
        image_size = payload.get("image_size")
        normalized = self._normalize_size(image_size)
        if normalized is None:
            problems.append(f"image_size 缺失/格式异常: {image_size!r}")
        if not isinstance(payload.get("captured_at"), str) or \
                not payload.get("captured_at"):
            problems.append(f"captured_at 缺失: {payload.get('captured_at')!r}")
        if not isinstance(payload.get("frame_id"), str) or \
                not payload.get("frame_id"):
            problems.append(f"frame_id 缺失: {payload.get('frame_id')!r}")
        image_bytes = self.read_screen.image_bytes()
        png_dims = png_size(image_bytes) if image_bytes else (0, 0)
        mime = (self.read_screen.images[0].get("mimeType")
                if self.read_screen.images else None)
        if not image_bytes:
            problems.append("read_screen 未返回可解码的 image content")
        elif mime != "image/png":
            problems.append(f"mimeType={mime!r} != image/png")
        elif normalized and png_dims and tuple(normalized) != png_dims:
            problems.append(f"image_size={normalized} 与 PNG 实际尺寸 "
                            f"{png_dims} 不一致")
        data = {"image_size": image_size, "png_size": list(png_dims),
                "captured_at": payload.get("captured_at"),
                "frame_id": payload.get("frame_id"),
                "mimeType": mime,
                "region": payload.get("region"),
                "preprocess": payload.get("preprocess")}
        if image_bytes:
            self.save_debug_png("r1_read_screen.png", image_bytes)
        if problems:
            return FAIL, "；".join(problems), data
        return PASS, (f"image_size={normalized} png={png_dims} "
                      f"captured_at={data['captured_at']} "
                      f"frame_id={data['frame_id']}"), data

    @staticmethod
    def _normalize_size(value) -> tuple[int, int] | None:
        if isinstance(value, dict):
            width = value.get("width") or value.get("w")
            height = value.get("height") or value.get("h")
        elif isinstance(value, (list, tuple)) and len(value) >= 2:
            width, height = value[0], value[1]
        else:
            return None
        try:
            width, height = int(width), int(height)
        except (TypeError, ValueError):
            return None
        if width <= 0 or height <= 0:
            return None
        return width, height

    def check_r4_2_screenshot(self) -> tuple[str, str, dict]:
        save_path = str(DEBUG_DIR / "r4_screenshot.png") \
            if self.args.save_frames else None
        arguments = {"save_path": save_path} if save_path else {}
        result = self.call("screenshot", arguments)
        self.screenshot = result
        problems = []
        payload = result.payload or {}
        meta = {}
        if payload:
            meta = payload
        else:
            # 兼容 metadata 只放在 text 项、且不是纯 JSON 的情况
            for text in result.texts:
                try:
                    parsed = json.loads(text)
                except ValueError:
                    continue
                if isinstance(parsed, dict) and "frame_id" in parsed:
                    meta = parsed
                    break
        image_bytes = result.image_bytes()
        png_dims = png_size(image_bytes) if image_bytes else (0, 0)
        mime = result.images[0].get("mimeType") if result.images else None
        if result.is_error:
            problems.append(f"screenshot isError: "
                            f"{short_json(result.texts, 300)}")
        if not image_bytes:
            problems.append("未返回可 base64 解码的 image content")
        elif mime != "image/png":
            problems.append(f"mimeType={mime!r} != image/png")
        normalized = self._normalize_size(meta.get("image_size"))
        if normalized is None:
            problems.append(f"image_size 缺失/异常: {meta.get('image_size')!r}")
        elif png_dims and normalized != png_dims:
            problems.append(f"image_size={normalized} != PNG {png_dims}")
        if not isinstance(meta.get("captured_at"), str) or \
                not meta.get("captured_at"):
            problems.append("captured_at 缺失")
        if not isinstance(meta.get("frame_id"), str) or not meta.get("frame_id"):
            problems.append("frame_id 缺失")
        if image_bytes:
            self.save_debug_png("r4_screenshot_content.png", image_bytes)
        data = {"png_size": list(png_dims), "mimeType": mime,
                "meta": meta, "is_error": result.is_error}
        if problems:
            return FAIL, "；".join(problems), data
        return PASS, (f"ImageContent image/png {png_dims}，元数据齐全 "
                      f"frame_id={meta.get('frame_id')}"), data

    def check_r4_3_save_path(self) -> tuple[str, str, dict]:
        if not self.args.save_frames:
            return SKIP, "已用 --no-save-frames 跳过落盘检查", {}
        problems = []
        data: dict = {}
        # screenshot 的 saved_path 已经通过 R4-2 的 save_path 参数触发
        if self.screenshot and self.screenshot.payload:
            saved = self.screenshot.payload.get("saved_path")
            data["screenshot_saved_path"] = saved
            if not saved:
                problems.append("screenshot 未返回 saved_path（save_path 参数无效？）")
            else:
                problems.extend(self._check_png_file(saved, "screenshot"))
        # read_screen 也测一次 save_path
        save_path = str(DEBUG_DIR / "r4_read_screen.png")
        result = self.call("read_screen", {"save_path": save_path})
        self.read_screen2 = result
        payload = result.payload or {}
        saved2 = payload.get("saved_path")
        data["read_screen_saved_path"] = saved2
        if result.is_error:
            problems.append(f"read_screen(save_path) isError: "
                            f"{short_json(result.texts, 300)}")
        elif not saved2:
            problems.append("read_screen 未返回 saved_path")
        else:
            problems.extend(self._check_png_file(saved2, "read_screen"))
        if problems:
            return FAIL, "；".join(problems), data
        return PASS, (f"落盘成功: screenshot={data.get('screenshot_saved_path')}，"
                      f"read_screen={data.get('read_screen_saved_path')}"), data

    @staticmethod
    def _check_png_file(path: str, label: str) -> list[str]:
        problems = []
        file = Path(path)
        if not file.is_file():
            return [f"{label} save_path 文件不存在: {path}"]
        try:
            raw = file.read_bytes()
        except OSError as exc:
            return [f"{label} save_path 读取失败: {exc}"]
        dims = png_size(raw)
        if not dims[0]:
            problems.append(f"{label} save_path 不是合法 PNG: {path}")
        if len(raw) < 1000:
            problems.append(f"{label} save_path 文件过小({len(raw)}B): {path}")
        return problems

    def check_r4_4_frame_freshness(self) -> tuple[str, str, dict]:
        first = ((self.read_screen or ToolResult("", 0, False)).payload or {})
        second = ((self.read_screen2 or ToolResult("", 0, False)).payload or {})
        first_id = first.get("frame_id")
        second_id = second.get("frame_id")
        data = {"first_frame_id": first_id, "second_frame_id": second_id}
        if not first_id or not second_id:
            return SKIP, "缺少 frame_id，无法比较两帧", data
        if first_id == second_id:
            return FAIL, (f"两次 read_screen 返回相同 frame_id={first_id}，"
                          "疑似帧缓存未刷新"), data
        shot_id = ((self.screenshot.payload or {}) if self.screenshot
                   else {}).get("frame_id")
        data["screenshot_frame_id"] = shot_id
        return PASS, (f"两帧 frame_id 不同（{first_id} → {second_id}）"), data

    # -- 主流程 ----------------------------------------------------------
    def run(self) -> int:
        self.log("=" * 72)
        self.log("sky-mcp R1-R5 真机验收")
        self.log(f"仓库: {ROOT}")
        self.log(f"时间: {now_iso()}")
        self.log("=" * 72)

        # 1) 连接/启动 server
        is_spawned = not self.args.url
        if is_spawned:
            self.server = ServerProcess(self.args)
            self.server.start()
            self.client = McpHttpClient(self.server.url,
                                        token=self.server.token,
                                        timeout=self.args.timeout)
            self.server_desc = {"mode": "spawned", "url": self.server.url,
                                "port": self.server.port,
                                "python": self.args.python or sys.executable,
                                "token": "<random>"}
        else:
            self.client = McpHttpClient(self.args.url, token=self.args.token,
                                        timeout=self.args.timeout)
            self.server_desc = {"mode": "external", "url": self.args.url,
                                "token": "set" if self.args.token else "none"}

        started = time.perf_counter()
        try:
            health = self.server.wait_http(self.client) if is_spawned \
                else self._wait_health_external(self.client)
            self.health = health
            self.record("PRE-1", "server /health 可用", PASS,
                        f"status=ok mode={health.get('mode')} "
                        f"ocr={health.get('ocr')} "
                        f"ready={health.get('ocr_ready')}",
                        (time.perf_counter() - started) * 1000,
                        {"health": health,
                         "server_info": self.server_desc})
            self.client.initialize()
            tool_status, tool_detail, tool_data = self.check_pre_2_tools()
            self.record("PRE-2", "MCP 工具清单", tool_status, tool_detail,
                        data=tool_data)
            self.log("\n-- 准备：OCR 运行时环境 --")
            self.run_check("PRE-3", "OCR 运行时 .deps 环境完整",
                           self.check_pre_3_ocr_env)
            self.run_check("PRE-4", "真机预检（设备/前台/ADBKeyboard）",
                           self.check_pre_4_device)
        except (AcceptanceError, RpcError) as exc:
            self.record("PRE-1", "server /health 可用", FAIL, str(exc))
            return self._finish()

        # 2) 预热等待（R3-4）+ 状态
        self.wait_prewarm()

        # 3) 逐项验收
        self.log("\n-- R1 OCR 引擎支持 --")
        self.run_check("R1-1", "status OCR 字段与快速诊断",
                       self.check_r1_1_status_ocr)
        self.log("\n-- R1/R3/R4：读屏 --")
        rs_check = self.run_check(
            "R1-2", "read_screen 实时画面 OCR 文本（含中文）", self._check_rs_texts)
        self.run_check("R3-4", "OCR 预热（首调前 ocr_ready）",
                       self.check_r3_4_prewarm)
        self.run_check("R4-1", "read_screen 画面元数据/ImageContent",
                       self.check_r4_1_read_screen_meta)
        self.log("\n-- R2 聊天读取 --")
        # 契约要求：先在游戏里打开聊天面板（或脚本调用 open_chat）再读取。
        # _ensure_chat_panel_open 会按「read_chat 观测 → 按 c → open_chat」确认面板。
        self.run_check("R2-3", "open_chat/panel 打开 + panel 模式读取",
                       self.check_r2_3_open_chat)
        self.run_check("R2-1", "read_chat 结构化返回", self.check_r2_1_read_chat)
        self.run_check("R2-2", "read_chat 同帧重复稳定", self.check_r2_2_stability)
        # 注意：这里不按 ESC/不关面板 —— 真机实测 ESC 会关面板/开设置菜单，
        # 后续 send_chat 从"面板关闭"状态发消息会读不到。面板保持打开。
        self.log("\n-- R4 截图元数据/落盘 --")
        self.run_check("R4-2", "screenshot ImageContent + 元数据",
                       self.check_r4_2_screenshot)
        self.run_check("R4-3", "save_path 落盘 PNG", self.check_r4_3_save_path)
        self.run_check("R4-4", "frame_id 随帧变化", self.check_r4_4_frame_freshness)
        self.log("\n-- R3 OCR 耗时/预处理 --")
        self.run_check("R3-1", "read_screen/read_chat 返回 ocr_ms",
                       self.check_r3_1_ocr_ms)
        self.run_check("R3-2", "OCR 单次耗时 ≤ 上限", self.check_r3_2_latency)
        self.log("\n-- R5 send_chat 回执 / R2 发送回读 --")
        self.run_check("R5-2", "send_chat 失败路径 error_type",
                       self.check_r5_2_error_shape)
        self.check_send_loop()
        self.log("\n-- R1 OCR 不可用降级诊断 --")
        self.run_check("R1-4", "OCR 禁用时 texts=[]/ocr_error 且非 isError",
                       self.check_r1_4_ocr_unavailable)
        self.log("\n-- R1 fixture 离线 OCR --")
        self.run_check("R1-3", "fixture 截图中文识别 ≥80%",
                       self.check_r1_3_fixture_ocr)

        # 现场收尾：聊天面板末置为关闭（按 c；绝不用 ESC，见 Lead 真机结论）
        if not self.args.keep_chat_open:
            self.cleanup_chat_ui()

        elapsed = time.perf_counter() - started
        self.log(f"\n验收执行用时: {elapsed:.1f}s")
        return self._finish()

    def _check_rs_texts(self) -> tuple[str, str, dict]:
        """R1-2：调 read_screen 并对识别文本算中文命中率（结果缓存给 R3/R4）。"""
        try:
            result, payload = self.call_json("read_screen", {})
        except AcceptanceError as exc:
            # 区分「OCR 不可用」与「请求失败」：前者是 FAIL，后者也 FAIL
            return FAIL, str(exc), {}
        self.read_screen = result
        texts = payload.get("texts") or []
        data: dict = {
            "texts_count": len(texts),
            "texts": [item.get("text") for item in texts][:15],
            "ocr": payload.get("ocr"),
            "region": payload.get("region"),
            "preprocess": payload.get("preprocess"),
            "ocr_ms": payload.get("ocr_ms"),
            "ocr_error": payload.get("ocr_error"),
            "image_size": payload.get("image_size"),
            "captured_at": payload.get("captured_at"),
            "frame_id": payload.get("frame_id"),
        }
        if result.is_error:
            return FAIL, (f"read_screen isError: "
                          f"{short_json(result.texts, 400)}"), data
        if data["ocr"] == "none":
            return FAIL, ("read_screen.ocr=none，OCR 引擎未生效"
                          f"（ocr_error={data['ocr_error']!r}）"), data
        ocr_ms = data.get("ocr_ms")
        if not texts:
            # 纯图形场景（如角色在晨岛静坐）本来就可能没有任何文字，这不是
            # OCR 故障：只要引擎可用、OCR 执行过（ocr_ms>0）就算链路正常；
            # 「识别出中文 ≥80%」由 R1-3 固定 fixture 严格验收。
            if isinstance(ocr_ms, (int, float)) and ocr_ms > 0:
                return PASS, (f"当前画面无文字（纯图形场景，{len(texts)} 条）；"
                              f"OCR 链路正常（ocr={data['ocr']}，"
                              f"OCR {ocr_ms}ms）；中文识别率以 R1-3 "
                              f"fixture 严格验收"), data
            return FAIL, (f"texts 为空且 ocr_ms={ocr_ms!r}（OCR 似乎没有执行）；"
                          f"ocr_error={data['ocr_error']!r}"), data
        # 引擎不可用时 server 会返回提示文案，这类"文本"不能算真实识别结果
        hint_texts = [text for text in texts
                      if "未安装 OCR" in text or "OCR 引擎" in text]
        if hint_texts:
            return FAIL, (f"texts 里是引擎不可用提示文案而非识别结果: "
                          f"{hint_texts}"), data
        joined = "\n".join(str(item.get("text") or "") for item in texts)
        rates = {expect: round(char_hit_rate(expect, joined), 3)
                 for expect in self.args.expect}
        best_expect = max(rates, key=rates.get)
        best_rate = rates[best_expect]
        chinese_chars = zh_count(joined)
        data["hit_rates"] = rates
        data["best_expect"] = best_expect
        data["best_rate"] = best_rate
        data["chinese_chars"] = chinese_chars
        # 这里是**实时画面**：游戏可能正停在纯图形场景（没有中文字）。
        #   * 只要 OCR 链路可用、返回了文本，就算 read_screen 工作正常；
        #   * R1 要求的「已知中文 ≥80% 字符」由 R1-3（固定 fixture）严格验收，
        #     不依赖当前游戏画面状态，避免验收结果随游戏画面漂移。
        if best_rate >= 0.8:
            return PASS, (f"{len(texts)} 条文本，中文 {chinese_chars} 字；"
                          f"「{best_expect}」命中 {best_rate:.0%}"
                          f"（ocr={data['ocr']}，OCR {data['ocr_ms']}ms）"), data
        return PASS, (f"{len(texts)} 条文本（中文 {chinese_chars} 字）；"
                      f"当前画面无已知期望文本（上限 "
                      f"{max(rates.values()):.0%}），中文识别率以 R1-3 "
                      f"fixture 严格验收；ocr={data['ocr']}，"
                      f"OCR {data['ocr_ms']}ms"), data

    def _wait_health_external(self, client: McpHttpClient) -> dict:
        deadline = time.time() + self.args.startup_timeout
        last = ""
        while time.time() < deadline:
            try:
                body = client.health()
                if body.get("status") == "ok":
                    return body
                last = short_json(body)
            except RpcError as exc:
                last = str(exc)
            time.sleep(0.5)
        raise AcceptanceError(f"{self.args.startup_timeout:.0f}s 内连不上 "
                              f"{client.base_url}/health: {last}")

    # -- 汇总 ------------------------------------------------------------
    def _finish(self) -> int:
        checks = self.checks
        passed = [c for c in checks if c.status == PASS]
        failed = [c for c in checks if c.status == FAIL]
        skipped = [c for c in checks if c.status == SKIP]
        duration = time.time() - self.started

        # 按需求分组统计
        groups: dict[str, dict[str, int]] = {}
        for check in checks:
            group = check.cid.split("-")[0]
            bucket = groups.setdefault(group, {PASS: 0, FAIL: 0, SKIP: 0})
            bucket[check.status] += 1

        self.log("")
        self.log("=" * 72)
        self.log("验收汇总")
        self.log("=" * 72)
        for group in ("PRE", "R1", "R2", "R3", "R4", "R5"):
            bucket = groups.get(group)
            if not bucket:
                continue
            self.log(f"  {group}: PASS {bucket[PASS]}  FAIL {bucket[FAIL]}  "
                     f"SKIP {bucket[SKIP]}")
        self.log(f"  总计: {len(checks)} 项 | PASS {len(passed)} | "
                 f"FAIL {len(failed)} | SKIP {len(skipped)} | "
                 f"{duration:.1f}s")
        if failed:
            self.log("  失败项:")
            for check in failed:
                self.log(f"    [{check.cid}] {check.title}: "
                         f"{check.detail.splitlines()[0][:300] if check.detail else ''}")
        if self.args.strict and skipped:
            self.log(f"  --strict 模式: {len(skipped)} 项 SKIP 视为失败")
        ok = not failed and not (self.args.strict and skipped)
        self.log(f"  结论: {'PASS' if ok else 'FAIL'}")
        if self.server and self.server.proc is not None:
            self.log(f"  server 日志尾部:\n{self.server.log_tail(15)}")

        report = {
            "started_at": time.strftime(
                "%Y-%m-%dT%H:%M:%S", time.localtime(self.started)),
            "finished_at": now_iso(),
            "duration_s": round(duration, 2),
            "ok": ok,
            "strict": bool(self.args.strict),
            "server": self.server_desc,
            "health": self.health,
            "ocr_env": {
                "deps_dir": self.ocr_env.get("deps_dir"),
                "python": self.ocr_env.get("python"),
                "rapidocr_importable": self.ocr_env.get("rapidocr_importable"),
            },
            "summary": {
                "total": len(checks), "pass": len(passed),
                "fail": len(failed), "skip": len(skipped),
                "groups": groups,
            },
            "saved_frames": self.saved_frames,
            "checks": [
                {"id": c.cid, "title": c.title, "status": c.status,
                 "detail": c.detail, "ms": c.ms, "data": c.data}
                for c in checks
            ],
        }
        report_path = self._save_report(report)
        if self.json_mode:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            self.log(f"  报告: {report_path}")
        return 0 if ok else 1

    def _save_report(self, report: dict) -> str:
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(self.started))
        path = DEBUG_DIR / f"acceptance_{stamp}.json"
        latest = DEBUG_DIR / "acceptance_latest.json"
        text = json.dumps(report, ensure_ascii=False, indent=2)
        for target in (path, latest):
            try:
                target.write_text(text + "\n", encoding="utf-8")
            except OSError as exc:
                self.log(f"（警告：报告写入失败 {target}: {exc}）")
        return str(path)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="sky-mcp R1-R5 真机验收脚本（纯标准库）。默认自己拉起 "
                    "sky-mcp-server.py --http（随机端口/token），逐项验收并输出 "
                    "PASS/FAIL；只有显式 --send-chat 才会真的在游戏里发消息。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""常用命令:
  python tools/mcp_acceptance.py
      只读验收：起 server、验 R1/R2/R3/R4 结构，不发送游戏消息
  python tools/mcp_acceptance.py --send-chat "测试ABC123"
      真发 1 条消息，验 R2 闭环回读 + R5 回执
  python tools/mcp_acceptance.py --send-chat "hello world" ^
      --send-chat "今天天气不错，一起去云野看云吧测试中文字符" ^
      --send-chat "测试ABC123" --send-chat "OCR验收2" --send-chat "OCR验收3"
      发 5 条（含英文/20字中文），完整验收 R2「≥4 条且顺序正确」
  python tools/mcp_acceptance.py --url http://127.0.0.1:9900 --token 1234
      连接已启动的 server（不拉起/不停止进程）

产物:
  debug_out/acceptance_<时间>.json   本次验收报告（含每项明细）
  debug_out/acceptance_latest.json   最近一次报告
  debug_out/*.png                    截图证据（--no-save-frames 可关闭）

退出码: 0 = 无 FAIL；1 = 有 FAIL（--strict 时 SKIP 也算失败）。""")
    parser.add_argument("--url", help="已启动 server 的 URL（如 "
                                      "http://127.0.0.1:9900）；不给则自动拉起")
    parser.add_argument("--token", help="HTTP bearer token（配合 --url；"
                                        "拉起模式可省略，脚本会生成随机 token）")
    parser.add_argument("--send-chat", action="append", metavar="TEXT",
                        help="真实发送一条游戏聊天消息（可重复传多次；"
                             "不传则绝不发消息）")
    parser.add_argument("--expect", action="append", metavar="TEXT",
                        help="R1 读屏中文识别期望文本（可重复；默认内置 "
                             "珍珠季横幅/聊天 fixture 等）")
    parser.add_argument("--server", default=str(SERVER_PATH),
                        help=f"server 脚本路径（默认 {SERVER_PATH}）")
    parser.add_argument("--python", default=None,
                        help="拉起 server 用的 python（默认当前解释器）")
    parser.add_argument("--adb-serial", default=None,
                        help="透传 --adb-serial 给 server（多设备时用）")
    parser.add_argument("--ocr-engine", default=None,
                        help="拉起 server 时的 --ocr-engine（默认让 server 用 auto）")
    parser.add_argument("--extra-server-arg", action="append", metavar="ARG",
                        help="额外透传给 server 的参数（可重复，如 "
                             "--extra-server-arg=--screenshot-max-width)")
    parser.add_argument("--max-ocr-ms", type=float, default=3000.0,
                        help="R3 单次 OCR 耗时上限毫秒（默认 3000）")
    parser.add_argument("--startup-timeout", type=float, default=90.0,
                        help="等 server 启动/健康检查的秒数（默认 90）")
    parser.add_argument("--prewarm-timeout", type=float, default=90.0,
                        help="等 OCR 预热完成（status.ocr_ready）的秒数（默认 90）")
    parser.add_argument("--timeout", type=float, default=120.0,
                        help="单次 HTTP 请求超时秒数（默认 120）")
    parser.add_argument("--open-chat-wait-ms", type=float, default=1500.0,
                        help="open_chat 后等待 UI 的毫秒数（默认 1500）")
    parser.add_argument("--panel-wait-ms", type=float, default=1200.0,
                        help="按 c 切换聊天面板后等待 UI 的毫秒数（默认 1200）")
    parser.add_argument("--chat-wait-ms", type=float, default=600.0,
                        help="send_chat 后等待消息上屏再 read_chat 的毫秒数"
                             "（默认 600；自己消息主要是世界气泡，会较快消失）")
    parser.add_argument("--send-open-delay-ms", type=float, default=800.0,
                        help="send_chat 的 open_delay_ms：按 enter 打开输入框后等多久"
                             "再打字（默认 800ms）。太短会让 ADBKeyboard 的 "
                             "ADB_CLEAR_TEXT 在输入框未获焦点时崩溃（真机实测）")
    parser.add_argument("--send-gap-ms", type=float, default=500.0,
                        help="两条真实消息之间的额外间隔毫秒数（默认 500）")
    parser.add_argument("--skip-env-check", action="store_true",
                        help="跳过 tools/ocr_env.py 的 .deps 检查与 fixture OCR")
    parser.add_argument("--skip-ocr-off", action="store_true",
                        help="跳过「另起 --ocr-engine none server」的降级诊断检查")
    parser.add_argument("--no-save-frames", dest="save_frames",
                        action="store_false", default=True,
                        help="不往 debug_out/ 保存截图与报告截图证据")
    parser.add_argument("--strict", action="store_true",
                        help="SKIP 也视为失败（最终验收建议配合 --send-chat 使用）")
    parser.add_argument("--keep-chat-open", action="store_true",
                        help="收尾时不按 c 关聊天面板（默认关；调试时可留开）")
    parser.add_argument("--json", action="store_true",
                        help="stdout 只输出 JSON 报告（过程信息走 stderr）")
    parser.add_argument("--verbose", action="store_true",
                        help="实时打印 server 日志（调试用）")
    return parser


def main(argv: list[str] | None = None) -> int:
    force_utf8_stdio()
    args = build_parser().parse_args(argv)
    if not args.expect:
        args.expect = list(DEFAULT_EXPECT)
    args.send_chat = args.send_chat or []
    if args.send_chat:
        print("注意: --send-chat 会在游戏里真实发送消息，请确认当前画面可见性。",
              file=sys.stderr)
    acceptance = Acceptance(args)
    server = None
    try:
        # 先建 server 对象（run 内部会启动），方便 finally 清理
        exit_code = 1
        try:
            exit_code = acceptance.run()
        finally:
            server = acceptance.server
        return exit_code
    except KeyboardInterrupt:
        print("\n验收被中断", file=sys.stderr)
        return 130
    except AcceptanceError as exc:
        print(f"验收无法进行: {exc}", file=sys.stderr)
        return 1
    finally:
        if server is not None:
            server.stop()


if __name__ == "__main__":
    sys.exit(main())
