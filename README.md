# Sky ADB MCP 工具库

一个给《Sky: Children of the Light / 光遇》**安卓模拟器**用的本地 MCP 工具库。
它把「模拟器截图、OCR 读屏、键盘输入、中文聊天」包装成 MCP / JSON-RPC tool，
让任意支持 MCP 或 HTTP tool 的 AI 客户端来控制游戏里的角色。

**本项目不包含决策逻辑**：没有 LLM 循环、没有人设、不替 AI 做任何决定。
AI 客户端调用工具、自己决定该看哪里、按什么键、说什么话。

## 它做什么

- 识别并拉起模拟器里的光遇（国服 `com.netease.sky` / 国际服 `com.tgc.sky.android`）
- 截取模拟器画面，可选 OCR 返回文字
- 发送键盘按键（`w` / `a` / `s` / `d` / `space` / `f` / `q` / `enter` / `escape` / 方向键 / 数字键…），支持长按与组合键
- 打开聊天框、输入中文/英文消息并发送（ADBKeyboard 输入法，无需剪贴板）
- 前台守卫：游戏不在前台时拒绝按键，避免按键落进模拟器里的其他 App

它不读游戏内存，不修改客户端，不破解协议，也不提供刷资源、代肝或自动跑图功能。
请只把它当作本地可访问性/陪聊实验使用。

## 需要什么

- Windows 电脑 + 安卓模拟器（本项目在 **MuMu 12 + 光遇国服** 上实测，其他模拟器/国际服理论通用）
- [Android platform-tools](https://developer.android.com/tools/releases/platform-tools)（`adb`，加入 PATH 或设置 `ANDROID_HOME`）
- **ADBKeyboard 输入法**（中文输入必需，见下）
- Python 3.10+
- 模拟器里光遇已登录、能自由行动

## 安装

### 1. Python 部分

```bat
python -m pip install -r requirements.txt
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
:: 例: 127.0.0.1:16384  device product:r9q model:SM_G9900
```

MuMu 12 的 adb 端口常见是 `16384`，雷电 `5555`，蓝叠 `5555/5556`；
有的模拟器需要在设置里手动打开「ADB 调试」。

也可以先跑一次体检（设备/键盘节点/前台/输入法/截屏耗时都会列出来）：

```bat
python sky_adb.py doctor
```

## 启动 HTTP MCP

```bat
python sky-mcp-server.py --http
```

监听地址/端口/token 默认读 `sky_config.json`（样例见 `sky_config.example.json`），
默认 `127.0.0.1:9900`、token `1234`。启动后日志会打印：

```text
[sky-mcp] HTTP 传输已启动: http://127.0.0.1:9900
[sky-mcp] HTTP bearer token 已启用
```

局域网里的手机/其他客户端连接时，监听要改成 `0.0.0.0`：

```bat
python sky-mcp-server.py --http --host 0.0.0.0
```

然后在客户端填：

```text
URL:   http://运行服务那台电脑的局域网IP:9900
Header: Authorization
Value:  Bearer 1234
```

`Bearer` 后面有一个英文空格。建议把 `sky_config.json` 里的 token 换成自己的随机值。
用浏览器打开 `http://IP:9900/health` 能看到 `{"status":"ok"...}` 就说明网络通。

查当前生效的配置（不会打印 token 明文）：

```bat
python sky_config.py
```

## 本地 stdio MCP

如果你的 MCP 客户端支持 stdio，直接在客户端配置里拉起：

```json
{
  "command": "python",
  "args": ["C:\\path\\to\\sky-mcp\\sky-mcp-server.py"]
}
```

## 常用 tool

工具清单与参数（stdio 与 HTTP 一致）：

| tool | 作用 | 主要参数 |
|---|---|---|
| `status` | 设备/输入法/前台/OCR/截图上限自检 | — |
| `focus_game` | 唤醒屏幕并把光遇拉到前台（必要时拉起游戏） | — |
| `press_key` | 按键，支持长按与组合键 | `key`、`duration_ms`、`backend`(adb/auto) |
| `open_chat` | 短按聊天键（默认 `enter`）打开输入框/面板 | `key`、`duration_ms` |
| `send_chat` | 打开聊天框 → 输入消息（中文可用）→ 发送 | `message`、`send`、`open_delay_ms`… |
| `type_text` | 往**已经打开**的输入框打字，可选回车发送 | `message`、`send` |
| `read_screen` | 截图 + OCR，返回文字与截图 | — |
| `screenshot` | 只返回当前画面 PNG | — |

示例（JSON-RPC `tools/call` 的 params 部分）：

```json
{"name": "status", "arguments": {}}
{"name": "screenshot", "arguments": {}}
{"name": "read_screen", "arguments": {}}
{"name": "press_key", "arguments": {"key": "w", "duration_ms": 800}}
{"name": "press_key", "arguments": {"key": "shift-space", "duration_ms": 80}}
{"name": "open_chat", "arguments": {}}
{"name": "type_text", "arguments": {"message": "你好", "send": true}}
{"name": "send_chat", "arguments": {"message": "你好，我是本地 AI 陪聊测试"}}
```

按键语义（与光遇 PC 版一致，动作前请结合游戏 UI 状态判断）：

| 键 | 有弹窗 | 面板开（无弹窗） | 都没有 |
|---|---|---|---|
| `escape` | 取消弹窗 | 退出输入态 | 游戏菜单（慎用） |
| `space` | 确认所选 | 打开输入框 | 跳 |
| `enter` | — | 打开输入框 | 打开面板+输入框 |
| `f` | — | 对方伸手时=接受牵手；否则=交互/好友树 | 同左 |

注意：

- `backend` 只支持 `adb` / `auto`（本工具库只有 adb 一条执行路径）。
- `press_key` 的 `duration_ms` 会被夹到 1~2000ms；移动/长按请给足时长。
- 所有按键/输入都受前台守卫保护：游戏不在前台时返回错误（错误文本含「前台」），
  客户端可以等用户切回游戏后重试。
- 单条聊天消息上限 240 字符。
- `send_chat` 会先清空输入框里的草稿再打字（光遇会记住没发出去的内容）。

## 配置

### sky_config.json — MCP 监听

复制 `sky_config.example.json` 为 `sky_config.json`（已 gitignore）后按需改：

```json
{
  "mcp": {"host": "127.0.0.1", "port": 9900, "token": "1234", "timeout": 30}
}
```

| 字段 | 环境变量 | 说明 |
|---|---|---|
| `mcp.host` | `SKY_MCP_HOST` | 监听地址（局域网客户端用 `0.0.0.0`） |
| `mcp.port` | `SKY_MCP_PORT` | 监听端口 |
| `mcp.token` | `SKY_MCP_TOKEN` | HTTP bearer token |
| `mcp.timeout` | `SKY_MCP_TIMEOUT` | 单次工具调用超时秒数 |

优先级：环境变量 > `sky_config.json` > 内置默认值；命令行 `--host/--port/--token`
可临时覆盖。`--config other.json` 可指定别的配置文件。

### adb_config.json — 设备/采集（一般不用写）

零配置即可跑（自动探测 adb / 设备 / 包名 / 键盘节点 / 输入法）。要覆盖时把
`adb_config.example.json` 复制成 `adb_config.json`，或用环境变量：

| 配置项 | 环境变量 | 说明 |
|---|---|---|
| `adb_path` | `SKY_ADB_PATH` | adb 可执行文件路径（默认 PATH / ANDROID_HOME） |
| `serial` | `SKY_ADB_SERIAL` | 设备序列号，如 `127.0.0.1:16384`（多设备时必须） |
| `package` | `SKY_GAME_PACKAGE` | 光遇包名（默认自动探测国服/国际服） |
| `keyboard_device` | `SKY_KEYBOARD_DEVICE` | 键盘输入设备，如 `/dev/input/event4`（默认自动探测） |
| `ime.component` | — | ADBKeyboard 输入法组件名 |
| `ime.restore` | — | 退出时是否还原原输入法（默认 true） |
| `verbose` | — | 打印每条 adb 命令 |

## 一些踩坑换来的设计

- **按键必须走 `sendevent`，不能用 `adb shell input keyevent`**。
  实测：`input keyevent` 注入的事件光遇安卓版完全无视；直接往模拟器的
  键盘设备写 Linux input 事件（`sendevent /dev/input/eventN 1 <code> 1/0`）
  游戏会当成真实键盘，且按键语义与 PC 版完全一致。`sky_adb.py` 会自动从
  `getevent -pl` 里找到那块"带全键盘"的设备（MuMu 通常是
  `Xiaomi Input` → `/dev/input/event4`）。
- **中文输入**：没有 root 时命令行无法直接输入 Unicode，所以走 ADBKeyboard
  的 `ADB_INPUT_B64` 广播；每个字都从输入框提交，不经过剪贴板。
- **前台守卫**：判断的是**安卓前台应用**（`mCurrentFocus`），不是 Windows 窗口：
  模拟器窗口最小化/被挡住都照常工作，只有你在模拟器里切到别的 App 才会拦。
- **按键安全**：单次按住封顶 2s；所有注入在同一把锁里，两个请求的 down/up 不会
  交错；异常/退出时 `panic_release` 松开所有可能悬着的键。
- **截图**：`adb exec-out screencap` 原生 RGBA（3200x1800 约 500ms/帧）；
  装了 numpy/opencv 时转 PNG 很快，否则退回设备侧 PNG 编码（慢 5-8 倍）。

## 常见问题

- **HTTP 401**：客户端 URL 是局域网 IP（不是 `0.0.0.0`）；请求头名
  `Authorization`、值 `Bearer 当前配置里的 token`；改过 token 记得重启服务。
- **按键没反应**：先 `python sky_adb.py doctor` 看设备/键盘节点/前台；
  再用 `python sky_adb.py key c --ms 150` 测一下聊天面板是否弹出
  （不弹说明键盘设备选错了，在 `adb_config.json` 指定 `keyboard_device`）。
- **发中文没反应**：ADBKeyboard 没装或没 `ime enable`；`sky_adb.py doctor`
  里能看到状态。装了还不行就手动 `adb shell ime set com.android.adbkeyboard/.AdbIME`。
- **`没有在线的 adb 设备`**：模拟器没开、没开 ADB 调试，或端口变了。
  `adb devices -l` 先确认，必要时 `adb connect 127.0.0.1:16384`。
- **多台 adb 设备**：给 server 加 `--adb-serial 127.0.0.1:16384`
  （或环境变量 `SKY_ADB_SERIAL`）。
- **输入法被切走**：server 正常退出（Ctrl+C）时会还原；异常退出可手动
  `adb shell ime set <你原来的输入法>`（`ime list -s` 查看）。
- **read_screen 没有 OCR 文本**：装 PaddleOCR 或 Tesseract；不装时仍返回截图。

## 开发与测试

单元测试全部用假 adb（`tests/fake_adb.py`），不需要模拟器：

```bat
python tests\run_all.py
```

目录：

```
sky_adb.py               adb 驱动（设备/截屏/按键/中文输入/前台，纯标准库）
sky-mcp-server.py        MCP 工具库（stdio / HTTP JSON-RPC）
sky_config.py            运行配置加载（端口/token）
sky_config.example.json  配置样例
adb_config.example.json  设备/采集配置样例
tests/                   单元测试（假 adb）
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
