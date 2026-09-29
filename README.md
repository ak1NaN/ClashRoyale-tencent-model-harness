# ClashRoyale-tencent-model-harness
[实况视频](https://www.bilibili.com/video/BV19Ean6YEfb/ "必有一锤") 
面向 macOS 的腾讯版《皇室战争》FirstLight 模型部署端。它从正在运行的 Android 客户端读取本局可见状态，转换为 FirstLight V4 模型输入，并把模型选择的卡牌、落点或英雄技能提交给客户端。界面提供单次对战、连续对战、事件日志、状态报错和实时调试战场。**本仓库是运行源码，不包含游戏客户端、模拟器、模型权重或 Android 游戏资源。**

当前原生偏移配置针对腾讯客户端 **38.4.2**，运行环境为 macOS 与具备 Root ADB 的 Android ARM64 模拟器。客户端更新后可能需要重新测定偏移；其他版本、模拟器和桌面系统未作兼容性保证。模型策略与真实联网客户端之间存在状态同步延迟，本项目无法改变游戏本身的联网时序。

## 运行流程

```text
Android 游戏客户端
  → Frida 探针读取本局状态
  → 状态映射与 FirstLight V4 张量化
  → 用户选择的 .pt 模型推理
  → 原生命令提交与客户端应用回执
  → GUI、对局 JSONL 与本地终局判断
```

`third_party/FirstLight_CR/` 随仓库提供推理所需的 FirstLight 源码和卡牌语义数据；不需要另外克隆作者完整仓库。作者的 `.pt` 文件由用户自行准备，在 GUI 中选择。这个目录保留了原作者的许可证与 NOTICE，来源和固定版本见 [SOURCE.md](third_party/FirstLight_CR/SOURCE.md)。

## 运行条件

- macOS、Python **3.12 或更新版本**（包含 Tkinter）、Xcode Command Line Tools 的 `swiftc`。
- 已安装并打开腾讯版客户端的 Android ARM64 模拟器，ADB 可连接且 `adb shell id` 返回 Root。默认地址为 `127.0.0.1:5555`。
- 与 [config/native_offsets.json](config/native_offsets.json) 一致的腾讯客户端版本。目前默认版本为 **38.4.2**。
- 用户自行准备的 FirstLight V4 `.pt` 模型。模型与卡组应自行匹配；本仓库不提供权重。
- 首次安装依赖需要联网。Python 包按 [requirements.txt](requirements.txt) 固定版本安装。

## 安装与启动

1. 下载或克隆本仓库。保持整个文件夹结构完整。
2. 双击 `Install.command`，或在终端运行 `./Install.command`。脚本会创建 `.venv/`，从 Google 下载 macOS ADB Platform-Tools，从 Frida 官方发布页下载并核验 **17.17.0 Android ARM64** Server，安装 Python 包并编译连续对战的窗口定位辅助程序。下载的二进制放在 `.deps/`，不会进入 Git 提交。
3. 启动模拟器和腾讯客户端，并确认模拟器的 Root ADB 已启用。把自己的模型文件放入 `models/`，也可以存放在其他位置。
4. 双击 `Start.command`，或运行 `.venv/bin/python run_gui.py`。若本地依赖尚未安装，`Start.command` 会先运行安装脚本。
5. 在 GUI 中点击 **连接设备**，再点击 **选择 .pt** 与 **加载模型**。设备或模型错误会显示在“状态与报错”页；路径和模拟器地址可在齿轮设置中修改。

安装脚本不会安装模拟器、游戏或模型，也不会修改游戏 APK。如果 Python 或 `swiftc` 缺失，脚本会停止并提示补齐。macOS 若阻止打开 `.command`，可从终端运行同一文件查看错误。

## 对战操作

**单次对战：**在游戏主页点击 GUI 的“单次对战”，再由用户进入一场新的经典 1v1。程序从新局开始接入，模型自行选择下牌、落点和可用的英雄技能；“停止”会停止托管并取消待提交命令。

**连续对战：**先在设置页打开“Android 按钮映射”，把绿色“开始对局”和橙色“结束对局”浮标拖到模拟器相应按钮中心。回到首页，在“连续对战”旁选择 1～325 场并启动。程序按已保存的位置点击开始，等待本局终局、本地结果确认和结算画面稳定后再点击结束。单次与连续模式互斥；结果不明确、连接中断或超时会停止连战。

**结算等待：**游戏出现终局后请**等几秒再手动结束战斗或关闭结果画面**，让本地终局状态和记录完成写入。连续对战在确认本地结果后额外等待 **3 秒** 才自动点击“结束对局”。公开版只根据本地原生终局标志与双方塔状态判断胜负，不使用昵称、截图 OCR 或画面文字推断；证据不足的对局保留为“未确认”，不会猜测结果。

## GUI 与数据

- 首页：连接设备、选择并加载模型、单次/连续对战、停止、下牌最小间隔、虚拟实体开关和设置入口。
- 对局记录：当前模型动作、落点、原生命令、应用回执，以及双方公开下牌、塔血量、圣水与其他模型输入指标。
- 调试战场：以蓝色显示我方、红色显示对手、黄色显示仅存在于模型输入的待应用预测实体。它展示的是当前映射观测，不是游戏渲染帧。
- 状态与报错、事件记录：设备、探针、模型和命令管线的状态，以及异常信息。

每场的 JSONL 与摘要写入 `records/matches/`。终局可确定时写入 `terminal-evidence.json`，并为 JSONL 生成含日期、模式和模型名的文件名。`records/`、模型、个人设置与安装产物均被 Git 忽略。卡牌游戏 ID 与中文名见 [config/card_names_zh.json](config/card_names_zh.json)，附带的 [config/card_catalog.json](config/card_catalog.json) 可供查阅。

设置保存在本机 `config/runtime.json`。若 ADB 地址、游戏包名或模型路径与默认值不同，请在设置页修改。Frida Server 使用与 Python `frida==17.17.0` 对应的版本；程序连接设备时会按需将 Server 推送到模拟器并启动。

## 验证与范围

安装依赖后，在仓库目录运行：

```bash
.venv/bin/python -m pytest -q
```

模型可在 GUI 的设置页运行“离线推理”来验证加载，不会对游戏下牌。运行测试或离线推理时不要同时启动另一份模型对战进程。本项目的映射覆盖当前验证过的状态字段；未完整覆盖的游戏运行语义不会伪装成已知数据。只有真实本地终局证据才用于自动记录胜负。

## 目录与许可

| 路径 | 用途 |
| --- | --- |
| `ai_client/` | GUI、模型/设备控制、连续对战、观测与日志 |
| `probes/` | 腾讯客户端运行时读取和原生命令脚本 |
| `tools/` | 模型输入适配、推理 worker、窗口定位源码 |
| `config/` | 版本偏移与卡牌 ID/中文名称数据 |
| `third_party/FirstLight_CR/` | FirstLight 推理所需的原版源码和语义数据 |
| `tests/` | 本地单元与集成测试 |

本仓库自有代码采用 [Apache License 2.0](LICENSE)。随附的 FirstLight 部分保持原作者版权和 Apache-2.0 声明，详见 [NOTICE](NOTICE) 与 [上游许可证](third_party/FirstLight_CR/LICENSE)。项目与游戏发行方及 FirstLight 作者无隶属关系。
