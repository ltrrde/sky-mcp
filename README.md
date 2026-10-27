# Sky ADB MCP 工具库

一个给《Sky: Children of the Light / 光遇》**安卓模拟器**用的本地 MCP 工具库。
它把「模拟器截图、OCR 读屏、**结构化读取游戏内聊天**、键盘输入、中文聊天」
包装成 MCP / JSON-RPC tool，让任意支持 MCP 或 HTTP tool 的 AI 客户端来控制游戏里的角色。

**本项目不包含决策逻辑**：没有 LLM 循环、没有人设、不替 AI 做任何决定。
AI 客户端调用工具、自己决定该看哪里、按什么键、说什么话。

## 它做什么

- 识别并拉起模拟器里的光遇（国服 `com.netease.sky` / 国际服 `com.tgc.sky.android`）
- 截取模拟器画面；返回 `image_size` / `captured_at` / `frame_id`，可另存 PNG
- OCR 读屏（自动探测 PaddleOCR / rapidocr-onnxruntime / Tesseract），
  失败时把「缺哪个包、该装什么」写进 `status.ocr_error`
- **`read_chat` 读取游戏内聊天**：返回 `{speaker, text, confidence, y, is_self, kind}`，
  支持聊天面板与画面内气泡两种形态
- 发送键盘按键（`w`/`a`/`s`/`d`/`space`/`f`/`q`/`enter`/`escape`/方向键/数字键…），支持长按与组合键
- 打开聊天框、输入中文/英文消息并发送（ADBKeyboard 输入法，无需剪贴板），返回 JSON 回执
- 前台守卫：游戏不在前台时拒绝按键，避免按键落进模拟器里的其他 App

它不读游戏内存，不修改客户端，不破解协议，也不提供刷资源、代肝或自动跑图功能。
请只把它当作本地可访问性/陪聊实验使用。

## 需要什么

- Windows 电脑 + 安卓模拟器（本项目在 **MuMu 12 + 光遇国服** 上实测）
- [Android platform-tools](https://developer.android.com/tools/releases/platform-tools)（`adb`，加入 PATH 或设置 `ANDROID_HOME`）
- **ADBKeyboard 输入法**（中文输入必需，见下）
- Python 3.10+
- OCR 依赖
- 模拟器里光遇已登录、能自由行动

## 安装

### 1. Python 部分

```bat
python -m pip install -r requirements.txt
```

`requirements.txt` 默认安装 OCR 依赖：`rapidocr-onnxruntime`（CPU 即可，中文效果好）、
`numpy`、`opencv-python`。也支持 PaddleOCR / Tesseract（见「OCR 与聊天读取」）。

本机验证 OCR 是否生效：

```bat
python -c "from rapidocr_onnxruntime import RapidOCR; print('rapidocr ok')"
```

### 2. 装 ADBKeyboard（中文输入必需）

安卓系统下没有 root 时，命令行很难把中文打进输入框。ADBKeyboard 是一个
"专门接收 adb 广播、把文字直接塞进当前输入框"的输入法，稳定且无副作用。

1. 下载 [`ADBKeyboard.apk`](https://github.com/senzhk/ADBKeyBoard)
2. 装进模拟器：

   ```bat
   adb install -r ADBKeyboard.apk
   adb shell ime enable com.android.adbkeyboard/.AdbIME
   ```

3. 不用手动设为默认输入法：server 第一次发消息时自动切换，Ctrl+C 退出时自动还原。

验证：

```bat
adb shell ime list -s
:: 应该能看到 com.android.adbkeyboard/.AdbIME
```

### 3. 确认 adb 能看到模拟器

```bat
adb devices -l
:: 例: 127.0.0.1:16384  device product:r9q model:SM_G9900 device:r9q
```

MuMu 12 的 adb 端口常见是 `16384`，雷电 `5555`，蓝叠 `5555/5556`；
有的模拟器需要在设置里手动打开「ADB 调试」。设备没出现时先 `adb connect 127.0.0.1:16384`。

也可以先跑一次体检（设备/键盘节点/前台/输入法/截屏耗时都会列出来）：

```bat
python sky_adb.py doctor
```

## 启动 HTTP MCP

```bat
python sky-mcp-server.py --http
```

监听地址/端口/token 默认读 `sky_config.json`（样例见 `sky_config.example.json`），
默认 `127.0.0.1:9900`、token `1234`。启动日志会打印 OCR 预热线程与引擎状态。

局域网里的手机/其他客户端连接时，监听改成 `0.0.0.0`：

```bat
python sky-mcp-server.py --http --host 0.0.0.0
```

客户端填：

```text
URL:   http://运行服务那台电脑的局域网IP:9900
Header: Authorization
Value:  Bearer 1234
```

`Bearer` 后面有一个英文空格。建议把 `sky_config.json` 里的 token 换成自己的随机值。
浏览器打开 `http://IP:9900/health` 能看到 `{"status":"ok",...,"ocr_ready":true}` 就说明正常。

查当前生效配置（不会打印 token 明文）：`python sky_config.py`。

## 本地 stdio MCP

```json
{
  "command": "python",
  "args": ["C:\\path\\to\\sky-mcp\\sky-mcp-server.py"]
}
```

## 常用 tool

| tool | 作用 | 主要参数 |
|---|---|---|
| `status` | 设备/输入法/前台/OCR/截图上限自检。OCR 字段：`ocr`、`ocr_engine`、`ocr_device`、`ocr_error`、`ocr_ready`、`ocr_load_ms`、`ocr_available`、`ocr_chain` | — |
| `focus_game` | 唤醒屏幕并把光遇拉到前台（必要时拉起游戏） | — |
| `press_key` | 按键，支持长按与组合键 | `key`、`duration_ms`、`backend`(adb/auto) |
| `open_chat` | 短按聊天键（默认 `enter`）打开输入框/面板 | `key`、`duration_ms` |
| `send_chat` | 打开聊天框 → 输入消息 → 发送；返回 JSON 回执 | `message`、`send`、`open_delay_ms`… |
| `type_text` | 往**已经打开**的输入框打字，可选回车发送；返回 JSON 回执 | `message`、`send` |
| `read_screen` | 截屏 + OCR。返回 engine/诊断/耗时/帧元数据 + `texts`（含坐标），并返回 PNG | `region`(full/chat/bubble)、`save_path` |
| `read_chat` | **读取游戏内聊天**，返回结构化 `messages` + OCR/帧元数据 | `limit`、`mode`(auto/panel/bubble)、`save_path` |
| `screenshot` | 只返回当前画面 PNG + 帧元数据 | `save_path` |

示例（JSON-RPC `tools/call` 的 params 部分）：

```json
{"name": "status", "arguments": {}}
{"name": "screenshot", "arguments": {"save_path": "D:/sky_shots/now.png"}}
{"name": "read_screen", "arguments": {"region": "chat"}}
{"name": "read_chat", "arguments": {"limit": 20, "mode": "auto"}}
{"name": "press_key", "arguments": {"key": "w", "duration_ms": 800}}
{"name": "open_chat", "arguments": {}}
{"name": "send_chat", "arguments": {"message": "你好，我是本地 AI 陪聊测试"}}
```

`send_chat` / `type_text` 的返回（JSON 文本，R5）：

```json
{"ok": true, "action": "sent", "sent": true, "sent_at": "2026-10-27T17:30:00.123+08:00",
 "chars": 12, "truncated": false, "message": "你好", "backend": "adb"}
```

失败时 `isError=true`，文本是 `{"ok": false, "error_type": "foreground|ime|device|ocr|io|invalid|unknown", "error": "..."}`。

`send_chat` 可选 `verify=true`（额外约 4~5s）：发完自动回读一次聊天，回执里带
`verified`（true/false）；为 false 时带 `verify_error: "send_key"`，用于区分
「发送键没生效/消息没上屏」和「其实发出去了但 OCR 认不出」。默认关闭以保持低延迟。

## OCR 与聊天读取

### 引擎探测（R1）

顺序：`paddleocr → rapidocr-onnxruntime → tesseract`；可用
`--ocr-engine` 或环境变量 `SKY_OCR_ENGINE`（`auto/paddleocr/rapidocr/tesseract/none`）强制指定。

- `status.ocr`：实际/首选引擎名（`rapidocr`、`paddleocr-mobile-cpu`、`tesseract`、`none`）；
- `status.ocr_error`：探测/加载失败的**可操作原因**（缺哪个包/模型/可执行文件），不再静默 `none`；
- `status.ocr_ready`：模型是否已加载（HTTP 启动默认后台预热，`--no-ocr-prewarm` 可关）；
- `read_screen.texts`：引擎可用时是真实文本（含 `x/y/width/height/confidence`），不可用时为空 + `ocr_error`。

### 读聊天（R2）

```json
{"name": "read_chat", "arguments": {"limit": 20}}
```

返回：

```json
{
  "messages": [
    {"speaker": "小光", "text": "一起去云野看云吗", "confidence": 0.92,
     "y": 780, "is_self": false, "kind": "panel"},
    {"speaker": "", "text": "小光来啦，测试一下聊天", "confidence": 0.9,
     "y": 860, "is_self": true, "kind": "bubble"}
  ],
  "ocr": "rapidocr", "ocr_device": "cpu", "ocr_error": "", "ocr_ready": true,
  "ocr_ms": 4200.5, "image_size": {"width": 3200, "height": 1800},
  "captured_at": "2026-10-27T17:31:02.000+08:00", "frame_id": "18f...-ab12",
  "region": "chat+bubble", "mode": "auto", "panel_open": true
}
```

- 读取前请确保聊天面板开着（AI 可以先调 `press_key c` 或 `open_chat`）；
- `mode=auto`：**同时 OCR 聊天面板（左列带名字的消息）和世界气泡（发送后叠在
  输入框上方的消息，聊天界面开着时不会消失），两路按文本去重、按 y 排序后合并**；
  `mode=panel / bubble` 只读对应区域（各一遍 OCR，更快）；
- 光遇不显示自己的名字：无名字行按 x 位置/历史发送记录标 `is_self=true`；
  server 会把最近 10 分钟 `send_chat` 发过的文本作为证据（`own_texts`）提高判断准确率；
- 面板里 OCR 只剩「陌生人/图像」等名字/菜单碎片的行、单字符残片会被过滤掉；
- 同一帧反复调用结果稳定；`limit` 取最近 N 条后仍按 y 从上到下排序；
- `auto` 两遍 OCR 约 4~5s（CPU rapidocr），要更低延迟可用 `mode=panel` 或
  `mode=bubble`。

### 帧元数据与保存（R4）

`read_screen` / `read_chat` / `screenshot` 都返回 `image_size`、`captured_at`、`frame_id`；
`read_screen` 的文本和图片来自**同一帧**（同一 `frame_id`）。
可选 `save_path` 把该帧另存为 PNG（父目录自动创建），返回 `saved_path`。
原有 `ImageContent`（base64 PNG，`mimeType: image/png`）保持不变。

## 配置

### sky_config.json — MCP 监听

复制 `sky_config.example.json` 为 `sky_config.json`（已 gitignore）：

```json
{"mcp": {"host": "127.0.0.1", "port": 9900, "token": "1234", "timeout": 30}}
```

| 字段 | 环境变量 | 说明 |
|---|---|---|
| `mcp.host` | `SKY_MCP_HOST` | 监听地址（局域网客户端用 `0.0.0.0`） |
| `mcp.port` | `SKY_MCP_PORT` | 监听端口 |
| `mcp.token` | `SKY_MCP_TOKEN` | HTTP bearer token |
| `mcp.timeout` | `SKY_MCP_TIMEOUT` | 单次工具调用超时秒数 |

### adb_config.json — 设备/采集（一般不用写）

零配置即可跑（自动探测 adb / 设备 / 包名 / 键盘节点 / 输入法）。要覆盖时复制
`adb_config.example.json` 为 `adb_config.json`，或用环境变量：

| 配置项 | 环境变量 | 说明 |
|---|---|---|
| `adb_path` | `SKY_ADB_PATH` | adb 可执行文件路径 |
| `serial` | `SKY_ADB_SERIAL` | 设备序列号，如 `127.0.0.1:16384`（多设备时必须） |
| `package` | `SKY_GAME_PACKAGE` | 光遇包名（默认自动探测） |
| `keyboard_device` | `SKY_KEYBOARD_DEVICE` | 键盘输入设备，如 `/dev/input/event4` |
| `ime.component` / `ime.restore` | — | ADBKeyboard 组件名 / 退出还原输入法 |
| `verbose` | — | 打印每条 adb 命令 |

OCR 相关：`--ocr-engine` 或 `SKY_OCR_ENGINE`；`--no-ocr-prewarm` 关闭启动预热。

## 一些踩坑换来的设计

- **按键必须走 `sendevent`，不能用 `adb shell input keyevent`**。实测 `input keyevent`
  注入的事件光遇安卓版完全无视；直接往模拟器键盘设备写 Linux input 事件
  （`sendevent /dev/input/eventN 1 <code> 1/0`）游戏会当成真实键盘，且按键语义与 PC 版一致。
  `sky_adb.py` 会自动从 `getevent -pl` 里找到那块"带全键盘"的设备。
- **中文输入**：没有 root 时命令行无法直接输入 Unicode，所以走 ADBKeyboard 的
  `ADB_INPUT_B64` 广播；不经过剪贴板。发送前会先清空输入框草稿（光遇会保留草稿）。
- **ADBKeyboard 崩溃自愈**：真机实测，输入框还没拿到 `InputConnection` 时广播
  `ADB_INPUT_B64/ADB_CLEAR_TEXT` 会让 ADBKeyboard 空指针崩溃（系统弹「屡次停止运行」），
  之后所有广播被静默丢弃——表现就是"回执 ok 但消息没发出去"。现在：
  1) 打字前用 `dumpsys input_method` 等输入框真正聚焦（`mServedInputConnectionWrapper`
     非 null，最多等 3~5s，超时报 `error_type=ime`），避免触发崩溃；
  2) 每次输入前用 `pidof com.android.adbkeyboard` 查进程，崩了就清对话框并自动重绑
     IME；仍失败则明确报 `error_type=ime`。
- **前台守卫**：判断的是**安卓前台应用**（`mCurrentFocus`），不是 Windows 窗口：
  模拟器窗口最小化/被挡住都照常工作，只有你在模拟器里切到别的 App 才会拦。
  若前台是 `Application Error: com.android.adbkeyboard`，按 `ime` 报错而不是 `foreground`。
- **按键安全**：单次按住封顶 2s；所有注入在同一把锁里；异常/退出时 `panic_release`
  松开所有可能悬着的键。
- **OCR 预热**：模型首次加载可能要几秒，HTTP 启动后后台线程预热（加载 + 用
  1920x1080 空白图各跑一遍 full/chat 区域，让 onnxruntime 完成首帧图优化），
  用 `status.ocr_ready` 暴露进度；首个 `read_screen` 不被拖住。
- **OCR 工作图上限**：截图先按 `screenshot_scale/max` 缩到 ≤1920x1080，OCR 预处理再
  按模式限最长边（default ≤1900、chat ≤2000）；再大只会变慢，识别条数并不会变多。
- **聊天判读**：面板行格式是「内容 - 名字」；自己发的消息没有名字。`read_chat` 同时
  读面板与输入框上方的世界气泡，合并去重并过滤「陌生人/图像」这类名字/菜单碎片。
- **发送回执可验证**：默认不验证（快）；关键消息可传 `verify=true`，发完自动回读，
  回执带 `verified`/`verify_error=send_key`，用于区分"没发出去"和"发了但 OCR 认不出"。

## 常见问题

- **`status.ocr` 是 `none`**：看 `status.ocr_error`（会说缺什么）；装
  `rapidocr-onnxruntime`（`python -m pip install rapidocr-onnxruntime`）后重启；
  或 `--ocr-engine paddleocr/tesseract`。
- **`read_screen.texts` 为空**：同上；OCR 不可用时仍返回截图，AI 客户端可回退视觉模型。
- **`read_chat` 没有消息**：先打开聊天面板（`open_chat` / `press_key c`）；
  `mode=auto` 会同时读面板和世界气泡，两遍 OCR 约 4~5s；要更快用
  `mode=panel` / `mode=bubble`（各一遍）。
- **提示 `error_type=ime` / 系统弹「ADBKeyboard 屡次停止运行」**：server 会自动清掉
  对话框并重绑输入法；若反复出现，重启模拟器或重装 ADBKeyboard.apk，并确认
  `adb shell ime enable com.android.adbkeyboard/.AdbIME`。
- **回执 ok 但游戏里没看到消息**：ADBKeyboard 崩溃/输入框未聚焦时可能静默丢字；
  用 `send_chat {"verify": true}` 复现确认（`verified=false` 即发送键没生效）。
- **HTTP 401**：客户端 URL 是局域网 IP（不是 `0.0.0.0`）；请求头
  `Authorization: Bearer <token>`；改过 token 要重启服务。
- **按键没反应**：`python sky_adb.py doctor` 看设备/键盘节点/前台；
  `python sky_adb.py key c --ms 150` 测聊天面板是否弹出。
- **发中文没反应**：ADBKeyboard 没装 / 没 `ime enable`；`sky_adb.py doctor` 可查。
- **多台 adb 设备**：给 server 加 `--adb-serial 127.0.0.1:16384`。

## 开发与测试

本仓库**只有真机测试**（没有 fake adb / 假 OCR / mock）：测试直接操作当前运行的
模拟器 + 光遇 + 真实 rapidocr。跑测试前必须满足：

- 模拟器已启动，`adb devices` 能看到设备（默认 `127.0.0.1:16384`）；
- 光遇已登录并进到可操作场景；
- ADBKeyboard 已安装（中文输入）；
- `rapidocr-onnxruntime` + `numpy` + `opencv` 可导入（本机开发可 `python tools\ocr_env.py`）。

```bat
python tests\run_all.py            # 全部真机测试（约 2.5 分钟）
python tests\run_all.py -v         # 详细输出
python tests\run_all.py real_mcp   # 只跑名字包含该子串的模块
```

- 环境不满足时脚本以**退出码 2**结束并打印可操作的修复提示，不会 skip 后假通过。
- 测试会真实操作游戏：开关聊天面板、真发 **1 条带时间戳的测试消息**（验证回执与
  回读）、切一次系统设置再 `focus_game` 拉回游戏；结束时会松键并还原输入法。
- `tests/fixtures/*.jpg` 是之前从真机截下来的真实截图，用于真实 OCR 的确定性断言
  （例如系统横幅「珍珠季的新任务现已开启」、世界气泡「沐子在呢…」「小光来啦…」）。

真机验收脚本（自起 server，逐项检查 R1-R5；默认只读，`--send-chat` 才真发消息）：

```bat
python tools\mcp_acceptance.py
python tools\mcp_acceptance.py --send-chat "测试ABC123" --send-chat "OCR验收2"
```

- 只读模式约 40~60s；带 5 条真实发送约 110s（R2-4 回读、R3-3 识别率、R5-1 回执）。
- 验收报告和截图落在 `debug_out/`（已 gitignore，含聊天内容不要外传）。
- OCR 运行时依赖可用 `python tools\ocr_env.py` 手工检测/安装（开发机兜底用）。

目录：

```
sky_adb.py            adb 驱动（设备/截屏/按键/中文输入/前台，纯标准库）
sky_ocr.py            OCR 引擎层（引擎探测/错误诊断/预处理/计时）
sky_chat.py           聊天区域 ROI + 结构化解析
sky-mcp-server.py     MCP 工具库（stdio / HTTP JSON-RPC）
sky_config.py         运行配置加载（端口/token）
tools/mcp_acceptance.py  R1-R5 真机验收脚本
tools/ocr_env.py      OCR 运行时环境检测/安装（开发机兜底）
tests/                真机测试（真模拟器+真游戏+真 OCR，无 mock）+ 真实截图样本
```

## 安全边界

- 不要把 MCP 端口暴露到公网；token 不要发给不信任的人。
- 截图/OCR 可能包含聊天内容，不要公开上传调试日志。
- 不建议用于刷资源、自动跑图、代肝、骚扰等用途；游戏规则不允许自动化时请停止使用。

## 致谢

- 执行层最初基于
  [Aevella/sky-pc-mcp-companion](https://github.com/Aevella/sky-pc-mcp-companion)
  改造而来，感谢 Aevella 老师的原始工作；本仓库已重写为安卓模拟器 + adb 的工具库。

## 免责声明

本项目是屏幕识别 + 按键注入的自动化工具，仅供学习与研究。
自动化操作可能违反游戏的用户协议，使用风险自负。
它不修改游戏、不读写游戏内存、不影响其他玩家。

## License

MIT
