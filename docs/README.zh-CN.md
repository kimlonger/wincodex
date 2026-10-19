# WinCodex

**让网页 AI 走出聊天框，在 Windows 上真正写代码。**

给网页 AI 装上本地开发引擎。通过 MCP，让 ChatGPT 等网页 AI 直接读取项目、修改代码、执行命令、运行测试，把对话推进到可审查、可合入的代码改动。

**网页 AI 负责思考，WinCodex 提供动手的能力。无需模型 API Key，无需 Docker。**

[English](../README.md) · [Windows 下载](https://github.com/kimlonger/wincodex/releases/latest) · [工具列表](TOOLS.md) · [架构说明](ARCHITECTURE.md)

项目原名 ChatCode MCP，当前版本 **v0.11.3**，采用 **Apache-2.0** 开源许可。独立社区项目，与 OpenAI、Anthropic 无隶属关系。

## 项目定位

**仅支持 Windows，面向单机本地开发；不规划 Linux 或 macOS 版本。**

网页客户端提供模型推理，本地程序提供文件、命令、项目上下文和任务执行能力。你可以在网页里提出需求，让模型通过 MCP 读取真实代码、修改文件、运行检查，并给出可核对的结果。

本项目不配置模型 API Key，不抓取聊天网站，也不绕过客户端的账号、工具权限和使用限额。实际可用模型及远程 MCP 功能由所选客户端决定。

## 已有功能

| 功能 | 说明 |
| --- | --- |
| 本地代码读写 | 文件发现、批量读取、搜索、版本检测、多文件补丁和编辑撤销 |
| 多项目切换 | 运行中切换项目，保持当前 MCP 地址和隧道；任务继续归属原项目 |
| 多语言导航 | Python AST；Java/Go 声明候选；可选语言服务支持语义引用及跨文件重命名 |
| 大项目上下文 | 持久增量索引、符号查询、有预算的代码片段与分页 |
| 后台执行 | 命令进程、日志续读、总超时、进程树停止、命名检查 |
| 任务与交付 | 目标、验收条件、恢复包、独立副本、差异预览、合入和撤回 |
| 历史管理 | 归档、导出、清理预览与容量预算 |
| 原生隔离 | Windows AppContainer，允许公网出站，保留私有文件和资源限制 |
| 接入与认证 | Cloudflare 穿透、path 凭据、OAuth + PKCE、bearer |

普通模式共 **50 个工具**，只读模式减少可用工具。完整工具名见 [TOOLS.md](TOOLS.md)。

“桌面窗口设置”和“暂停桌面”入口已隐藏，界面标明 **“桌面自动化：暂不使用”**。底层可选能力保留，日常编码无需绑定窗口。

## Windows 用户快速开始

1. 在 [Releases](https://github.com/kimlonger/wincodex/releases/latest) 下载 `WinCodex.exe`。
2. 打开程序，选择要处理的项目目录。
3. 点击“启动服务 + 穿透”，等待显示公网 MCP 地址。
4. 在支持远程 MCP 的网页客户端中添加应用/连接器，填写程序显示的完整地址。
5. 在对话中启用连接器，先让助手核对项目，再提出修改需求。

Windows x64 发行包内置基础 Python 运行时和隧道程序，**不需要另装 Python，也不依赖 Docker**。Java/Go 编译器、语言服务和项目依赖按项目另行准备。

默认 `path` 模式的凭据就在地址末尾：客户端选择“无额外认证”，填写完整地址即可。**完整地址就是凭据，请勿公开。** OAuth 模式则在授权页输入程序显示的口令。客户端是否提供远程 MCP、账号要求和工具权限，以该客户端当前能力为准。

## 源码运行

需要 Python 3.12+，核心服务使用标准库：

```powershell
git clone https://github.com/kimlonger/wincodex.git
cd wincodex
python -m chatcode --gui

# 命令行启动
python -m chatcode --workspace D:/your/project

# 仅本机只读，不执行项目命令
python -m chatcode --workspace D:/your/project --tunnel none --read-only
```

内部 `chatcode` 包名、`chatcode-mcp` 数据目录和协议标识保留兼容，不会因对外改名迁移或清空原有项目数据。旧 exe 不会自动加载源码变化，需要打开新版程序。

首次源码运行会寻找或下载 Cloudflare 隧道程序。代理可用 `--proxy` 指定，`--no-proxy` 关闭自动探测。其他参数见 `python -m chatcode --help`。

## Java / Go

没有语言服务时仍可读取代码。内置 Java/Go 导航明确标记为非语义声明候选，不冒充真实类型分析。语义引用与跨文件重命名需要配置 gopls / JDT LS，并安装对应 Go 工具链或 JDK：

```powershell
python languages/setup_languages.py --language both
```

脚本固定版本并校验 JDT LS 下载；在高级设置中检测并填入语言服务命令。JDT LS 是后台语言服务，不要求使用 Eclipse 编辑器；缓存放项目外，默认关闭项目根目录的 Eclipse 元数据生成。

## 推荐编码流程

先 `server_info/list_workspaces` 核对项目，再 `task_create(isolation="copy")` 创建独立副本任务。后续工具带原项目身份和 `task_id`，读取、修改、运行检查均在副本中完成。通过必需检查后交付，再预览差异并决定是否合入。

`task_finish` 不自动改动原项目，`task_apply` 和 `task_revert` 都有预览与版本保护。副本本身不隔离系统权限；需要系统隔离时选择 native 模式。

恢复包能帮助新对话接着工作，但新推理仍由网页客户端驱动。网页关闭后，已启动的本地进程可以继续运行，不代表模型会自动继续思考。

## 权限边界

默认 host 模式的命令使用当前账号权限，文件工具的目录策略不等于命令沙箱。高级设置可选择 native，使用 Windows AppContainer 执行副本任务；隔离失败直接拒绝，不回退宿主权限。

native 已允许公网出站，仍限制私有文件访问、进程树、内存、CPU 和超时，不继承宿主环境。它不是虚拟机，Windows 公开资源仍可能可见，回环连接遵循系统策略，容量预算也不是运行时硬磁盘配额。完整说明见 [SECURITY.md](../SECURITY.md)。

## 构建和测试

```powershell
python -B tests/run.py
python -m pip install -r requirements-dev.txt
python scripts/prepare_build.py
python -m PyInstaller --noconfirm --distpath dist/v0.11.3 --workpath build/v0.11.3/pyinstaller WinCodex.spec
python -B tests/verify_frozen.py dist/v0.11.3/WinCodex.exe
```

构建脚本从官方来源准备固定版本二进制并校验哈希；生成文件不进入 Git。真实原生沙箱、语言工具链和桌面集成脚本需要显式运行，其中部分会联网或创建测试窗口，不属于普通源码测试的隐含操作。

## 已知限制

- 发行包面向 Windows x64；其他系统未完成同等级验收。
- 客户端是否支持工具写入、图片和远程 MCP，需要分别验证。
- 当前传输是 HTTP POST + JSON，不提供 SSE。
- 项目切换保持运行中的连接；免费隧道重启可能换域名。固定域名需配置命名隧道或自建服务。
- 多项目登记后必须带明确的项目上下文，防止旧任务误操作当前项目。
- OAuth 客户端注册落盘，令牌保存在内存，服务重启可能需要重新授权。
- 清理会删除归档副本，包括尚未合入的改动，请先核对预览。

## 参与和许可

欢迎提交问题及改进建议。代码贡献见 [CONTRIBUTING.md](../CONTRIBUTING.md)，安全问题使用仓库私密报告入口。

项目采用 [Apache-2.0](../LICENSE)。第三方来源与声明见 [NOTICE](../NOTICE) 和 [third_party](../third_party/README.md)。更名前的阶段开发记录保留在 [HISTORY.zh-CN.md](HISTORY.zh-CN.md)，历史章节不代表当前界面状态。
