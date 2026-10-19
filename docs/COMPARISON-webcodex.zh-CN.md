# WinCodex 与 WebCodex 的差异

核对日期：2026-10-19。依据 WebCodex 当前 main 的代码和文档，以及 WinCodex 本地源码；没有运行同机性能或稳定性对照测试。WebCodex 当前公开稳定版为 v0.4.6，main 正在向 v0.5 过渡，不能把开发分支的所有能力都当作稳定版已验收。

## 结论

两者属于同一个方向：通过 MCP，让网页/云端模型操作用户机器上的开发环境。WebCodex 的整体平台能力更完整；WinCodex 聚焦单机 Windows 的代码读写、检查、副本交付和可选原生隔离。

| 维度 | WebCodex 当前代码/文档 | WinCodex 当前实现 |
| --- | --- | --- |
| 架构 | Server 认证与路由，Runner 在项目所在机器执行，支持多机器 | 一个本地服务管理多个本机项目，共享现有隧道 |
| 连接恢复 | Runner 有 QUIC/WebSocket/轮询连接和重连、部分 Job 对账 | 有后台进程与任务恢复，尚无分离的远程 Runner 层 |
| 操作界面 | Desktop、浏览器 Runtime Console、CLI | 中文 Windows GUI 与 CLI |
| 工作区 | 有受管 Git worktree 实现 | 独立文件副本，不要求项目先使用 Git；显式预览、合入、撤回 |
| 工具发现 | Adaptive Runtime：常用工具直接提供，长尾通过清单及网关调用 | 普通模式暴露 50 个工具，只有只读模式做整体过滤 |
| 浏览器 | 专门的 CDP/浏览器域，包含页面、控件、表单和截图操作 | 没有同等级浏览器域；可选桌面控件能力保留，GUI 设置隐藏 |
| 持续工作 | Workflow Session、交接记录、Job，以及更丰富的持久状态领域 | 任务、验收条件、恢复包、检查记录、归档和导出 |
| 命令隔离 | 安全说明明确：命令使用 Runner 账号权限，项目边界不是 OS 沙箱 | 默认 host 也使用当前账号权限；可选 native 用 Windows AppContainer |
| 平台 | 已发布多平台运行时与部分桌面安装包；新统一安装体系仍有验收待完成 | 仅支持 Windows，目前发布 Windows x64 |
| 开发栈 | Rust 核心及桌面/前端工程 | 核心 Python 标准库，Windows 用 PyInstaller 打包 |

## WebCodex 值得借鉴的地方

1. **多机执行层**：把云端入口和各机器的执行边界分开，适合多台电脑、服务器和长期服务。
2. **工具按需发现**：避免随着功能增长，把全部工具细节一次交给模型。
3. **面向实际开发的观察界面**：集中查看连接、项目、执行、Session 与 Git 差异。
4. **受管 worktree 和浏览器域**：更接近复杂工程协作与端到端页面验证。

## WinCodex 已有的差异价值

- 单机产品范围更集中，核心源码和运行组件较少，适合继续保持容易理解和修改的实现。这是维护复杂度的判断，不是性能测试结论。
- 文件副本可用于没有 Git 的项目，不必先设计分支或 worktree 工作流。
- 已对可选 AppContainer 模式验证私有文件保护、公网 HTTPS、进程树、内存和超时；这不意味着默认 host 模式比对方更安全。

WebCodex 同样有 `share` 快速试用和原生运行方式，不能说它必须安装 Docker，也不能仅凭开发栈就断言谁更快、更稳定。

## 选择建议

需要多机器、集中控制台、浏览器自动化和更广的平台能力，优先评估 WebCodex；需要单台 Windows 电脑上的轻量 MCP 编码、文件副本和可选原生隔离，可评估 WinCodex。发布版本的安装体验和客户端兼容性仍应按实际环境核对。

后续若对标 WebCodex，应把 Server/Runner、工具发现分层、浏览器验证和 Git worktree 当作新增能力，而不是称为已经完成的功能优化。

## 一手依据

- [WebCodex README](https://github.com/yyjeqhc/webcodex)
- [架构与 Server/Runner](https://github.com/yyjeqhc/webcodex/blob/main/docs/ARCHITECTURE.md)
- [MCP 与 Adaptive Runtime](https://github.com/yyjeqhc/webcodex/blob/main/docs/MCP.md)
- [浏览器/CDP 架构](https://github.com/yyjeqhc/webcodex/blob/main/docs/architecture/browser-cdp-runtime.md)
- [受管 worktree 实现](https://github.com/yyjeqhc/webcodex/blob/main/crates/webcodex-runner/src/webcodex_runner/projects/managed_worktree.rs)
- [会话延续](https://github.com/yyjeqhc/webcodex/blob/main/docs/SESSION_CONTINUITY.md)
- [安全边界](https://github.com/yyjeqhc/webcodex/blob/main/SECURITY.md)
- [发布记录](https://github.com/yyjeqhc/webcodex/releases/latest)
