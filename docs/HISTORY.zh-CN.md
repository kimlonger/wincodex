# ChatCode MCP

> 历史开发记录。项目现名 WinCodex，当前功能与下载入口请阅读 [中文首页](README.zh-CN.md)。本文件中的旧版本路径、界面及验收限制仅对应当时阶段。

让 ChatGPT / claude.ai 这类**网页对话**，通过 MCP 协议直接**查看并改造你本地的项目**。

一个命令启动，自带内网穿透。核心使用 Python 标准库；可选桌面功能使用独立运行环境。

当前版本：**v0.11.1**，共 **50 个工具**。支持 Java/Go 语言服务检测与声明导航、持久增量索引、上下文预算、任务归档/导出/清理，以及无需外部沙箱软件的 Windows AppContainer 执行。本版放开原生沙箱公网出站，优化任务面板刷新；桌面设置入口隐藏，界面标明“桌面自动化：暂不使用”。

本次打包位于 `dist/v0.11.1/ChatCode启动器.exe`；历史版本保留在原位置。双击新版，停止旧服务后重新连接；旧 exe 不会自动包含源码更新。

v0.5.1 修复系统代理识别：优先读取环境变量和 Windows 当前用户的手动代理设置，再回退常用端口扫描（包含 7892）。GUI 和启动日志会显示配置来源；只有 PAC 时提示填写实际代理地址，不执行 PAC 脚本。显式 `--proxy` 仍优先，`--no-proxy` 禁用自动读取。

```
你在网页里说："把 src/api.py 里的超时改成 30 秒，然后跑测试"
        ↓
ChatGPT 云端模型（走你的订阅额度）
        ↓ MCP over HTTPS
Cloudflare 隧道（公网地址）
        ↓
ChatCode MCP（本地服务，路由到已登记项目与所属任务）
        ↓
真实读写文件 / 跑命令 / 看 git
```

---

## 快速开始

```bash
# 1. 起服务 + 穿透（默认 cloudflared）
python -m chatcode --workspace D:/your/project

# 想用图形界面
python -m chatcode --gui
```

启动后终端会打印：

- **公网 MCP 地址**（形如 `https://xxx.trycloudflare.com/mcp`）
- **授权口令**

拿这两个去 ChatGPT 建连接器即可。

### 两种连接方式，二选一

| 方式 | 客户端要做什么 | 安全性 |
| --- | --- | --- |
| **直连**（`--auth-mode path`，默认推荐） | 选「无认证」，填地址就用，**不弹授权页、不输口令** | 凭据是地址末尾那串随机字符，别人猜不到就进不来 |
| **授权**（`--auth-mode oauth`） | 选 OAuth，授权页输一次终端打印的口令 | 授权码 + PKCE S256，access token 24h，支持刷新令牌轮换 |

直连模式的地址长这样，末尾那段就是凭据：

```
https://xxx.trycloudflare.com/mcp/kR9f2...（随机串）
```

不带那串字符访问 `/mcp` 会直接 404。别把完整地址发到公开场合即可。

#### 直连模式接入步骤

1. ChatGPT → 设置 → 开启 **开发者模式（Developer Mode）**
2. 新建**自定义连接器 / MCP 应用**
3. 服务器 URL 填终端打印的**完整地址**（含末尾随机串）
4. 认证方式选 **无认证（No Auth）**，直接保存
5. 新建对话 → 工具菜单里启用它 → 直接提需求

#### 授权模式接入步骤

同上，只是第 4 步改成选 **OAuth**，并在授权页输入终端打印的口令。
之后令牌会自动续期，不会再反复要求授权。

> ChatGPT 的自定义连接器只提供 OAuth 和"无认证"两个选项，**没有填静态 Bearer 头的地方**。
> 所以直连模式把凭据放进 URL 路径，既不用授权动作，也保留了访问控制。

### 接入 Claude Desktop / Cursor / Cline

用 bearer 模式，它更适合本地客户端：

```bash
python -m chatcode --workspace D:/your/project --auth-mode bearer
```

```json
{
  "mcpServers": {
    "chatcode": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "https://xxx.trycloudflare.com/mcp",
               "--header", "Authorization: Bearer <令牌>"]
    }
  }
}
```

---

## v0.7 项目热切换与请求绑定

本机服务和隧道是持续运行的连接入口，项目目录可以在运行中切换：选择目录或最近项目，或输入目录后点「切换项目」。**不重启本地端口、不重启隧道、不更换本次 MCP 地址和认证凭据**。GUI 改动与进程列表显示当前项目；切回原项目可继续查看原记录。关闭整个服务仍会清理所有已登记项目的后台命令和语言服务。

- 本次连接最多登记 20 个项目；由本机用户选择目录登记。远程没有添加任意目录的工具。
- 已登记项目共享本次连接的认证授权。`workspace_token` 是防止项目混用的上下文身份，不是每个网页客户端的独立权限系统；不要登记不希望当前连接访问的目录。
- `list_workspaces` 列出已登记项目。`server_info` 返回项目状态；两者返回 `workspace_id` 和配套的 `workspace_token`。后续工具调用携带这两个参数。
- 单项目启动保留旧调用兼容。登记过第二个项目后，本次连接永久要求显式项目上下文；缺少上下文返回 `WORKSPACE_REQUIRED`，仅 `server_info/list_workspaces` 可不带上下文查询。
- 已有对话携带 A 的上下文时，切换到 B 后仍操作 A。在途请求也固定绑定最初的项目。不要把旧任务自动改成当前 GUI 项目。
- 批量读取的子查询不能覆盖外层项目。进程、改动历史、撤销、去重记录和语言服务均按工作区分开。
- 切换失败保留原项目和连接。已登记项目复用原运行实例及其策略；修改文件规则或配置后需重启服务重新加载。
- 切换项目不会改变认证、代理、端口或连接级权限；新项目读取其保存的语言服务配置。连接级只读和执行限制不能被项目设置放宽。

CLI 可在启动时登记其他项目：

```powershell
python -m chatcode --workspace D:/projects/A --project D:/projects/B --project D:/projects/C
```

网页助手示例调用：

```json
{"path":"src/main.go","workspace_id":"从 list_workspaces 取得的 ID","workspace_token":"同一个项目的上下文令牌"}
```

一次服务运行中换项目不再更换地址。程序退出或免费隧道异常重建后，随机公网域名仍可能变化；跨重启的固定地址需要固定隧道或域名配置。

### 本版修复

- 正则搜索放入可终止子进程，默认总时限 5000ms，可传 `timeout_ms`（100–10000）；最多并发两个正则搜索。超时返回 `SEARCH_TIMEOUT`，可简化表达式、缩小目录或使用普通文本搜索。子进程沿用启动时的文件访问策略。
- 项目检查支持 `test/tests` 别名，修复 JavaScript 项目默认检查找不到 `test` 脚本的问题。
- Git 状态使用 NUL 分隔解析，中文、空格与重命名路径可直接复用；嵌套工作区不返回树外状态。
- GUI 后台读取固定捕获所属项目，旧项目结果不会刷新到新项目列表；停止后禁止延迟创建进程或语言服务管理器。

---

## v0.6 正式版优化

正式版始终使用「网页对话 → MCP → 本地项目工具」架构。网页模型负责推理，本机负责工具执行；实验性网页模型后端已迁出本项目，不参与发布。

- **文件访问策略**：默认屏蔽 `.env`、`.env.*`（保留 `.example/.sample/.template` 示例）、私钥名 `id_rsa/id_dsa/id_ecdsa/id_ed25519` 和 `.pem/.key/.p12/.pfx` 文件。直接读取、批量读取、搜索、编辑、Git diff 和历史 diff 都检查路径策略。
- **只读模式**：CLI 加 `--read-only`，或在 GUI 高级设置勾选。远程只公布只读工具；直接调用写工具也会被拒绝，同时关闭命令和语言服务进程。GUI 手动撤销仍属于本机用户的管理操作。
- **命令重试去重**：`exec_command` 和 `run_checks` 支持 `operation_id`；相同请求重试复用任务，更换参数复用 ID 会拒绝。服务重启、任务淘汰或启动中断后，状态不明时返回 `OPERATION_UNAVAILABLE`，不会自动重复执行。
- **Git diff 分页**：文本和结构化结果使用同一份受限内容；支持已删除文件、`staged: true`，并用 `next_offset` / `diff_revision` 安全续读。
- **GUI 状态同步**：就绪地址与状态在同一主线程回调中更新，消除“地址已经出现，状态仍显示正在启动”的时序竞争。

### 文件访问规则

根目录可放 `.chatcodeignore`，UTF-8，最多 64 KB，例如：

```text
# 任意层级同名目录
confidential/
# 任意层级的文件名
*.local.txt
# 相对根目录的路径模式
config/private/*
```

这些是独立的拒绝规则，不自动读取 `.gitignore`；`#` 行是注释，不支持 `!` 反选或 `..`。带 `/` 的规则按工作区相对路径匹配（`*` 可跨目录）；不带 `/` 的规则按各路径段匹配。Windows 匹配忽略大小写。规则在**服务启动时固定**，修改后需重启；无效、超大或越界的规则文件会阻止启动。

确需读写真实环境文件或密钥时，可在本机 GUI 高级设置勾选「允许访问 .env / 私钥文件」，或使用 `--allow-sensitive-files`。该选项只关闭默认敏感名称过滤，不覆盖 `.chatcodeignore`；GUI 不保存此项，重开程序后需再次显式启用。

**文件策略本身不是操作系统沙箱。** 默认 host 模式下，命令和语言服务以当前用户运行，可能读取策略外文件；远程工具指令明确要求不得绕过访问策略。需要限制执行能力时使用只读模式或 `--no-exec`。命令产生的改动不在编辑工具的撤销历史中。

### 命令与 diff 的重试约定

```json
{"argv":["python","-m","pytest","-q"],"operation_id":"login-fix-tests-1","yield_time_ms":1000}
```

网络重试保持相同操作 ID 和参数；仅 `yield_time_ms` 可变化。需要再次真正运行时使用新 ID。检查名称也属于请求身份。提供操作 ID 才启用去重；去重记录保存在应用数据目录，每工作区最多 10000 条，满额时拒绝新预约；新预约会清理超过 30 天的记录，因此不承诺过期后的去重。启动前先写预约记录，遇到状态不明应先核对任务日志，再决定是否用新 ID 手动重跑。记录只保存请求摘要、进程 ID 和状态，不保存 env 原文。

```json
{"path":"src","max_bytes":32000}
```

下一页使用上次返回值：

```json
{"path":"src","max_bytes":32000,"offset":32000,"diff_revision":"上一页返回的 diff_revision"}
```

`offset` 必须使用实际 `next_offset`（可能因 UTF-8 边界小于 32000），并保持相同 `path/staged`。diff 变化返回 `REVISION_MISMATCH`，应从头读取。单页最多 60000 字节；单次最多选择 200 个文件，同时限制路径参数总长度，超出时 `files_truncated: true`，需要缩小 `path`。被访问策略拒绝的路径不返回补丁，数量见 `skipped_files_count`。为使过滤准确，重命名以删除和新建表示。

---

## 工具清单

| 分类 | 工具 | 说明 |
| --- | --- | --- |
| 查看 | `list_dir` | 列目录，recursive=true 递归整个项目（自动跳过 node_modules/.git） |
| | `read_file` | 读文件（带行号），返回 revision 供冲突检测 |
| | `read_files` | **一次读多个文件**（最多 20 个）—— 减少网络往返，明显更快 |
| | `search_text` | 文本或正则搜索，返回路径、行号、分页游标和跳过文件提示 |
| 修改 | `replace_in_file` | **精确替换一段文本**，日常改代码首选 |
| | `write_file` | 整文件写入（新建或覆盖） |
| | `apply_patch` | unified diff，支持修改/新建/删除；预检失败不写入，提交失败尝试回滚 |
| 执行 | `exec_command` | 跑测试/构建/安装依赖，有超时、输出上限和破坏性命令拦截 |
| 版本 | `git_status` `git_diff` `git_log` | 查看改动与历史；diff 支持分页、暂存区与已删除文件 |
| 项目入口 | `list_workspaces` | 查询本机登记项目与显式上下文，复用同一连接 |
| 元信息 | `server_info` | 工作区、认证模式、公网地址、运行时状态 |
| 项目 | `project_overview` `find_files` | 项目类型、检查命令建议、glob 文件定位与分页 |
| 代码 | `code_outline` `read_code` `find_symbol` | 内置 Python AST；可选 LSP 提供其他语言大纲、符号读取与定位 |
| 语义 | `find_references` `rename_symbol` | 配置 LSP 后查语义引用、跨文件重命名；未配置时引用仅返回明确标记的文本候选 |
| 批处理 | `batch_read` `edit_files` | 独立只读查询并行；带版本、语法预检和操作 ID 的多文件编辑 |
| 历史 | `get_changes` `undo_changes` | 本服务改动的精确 diff 与按版本撤销，无需 Git |
| 任务 | `read_process` `stop_process` `run_checks` | 增量日志、后台任务停止、命名项目检查 |

### v0.5 可选语言服务

基础文件工具和 Python AST 不需要额外依赖。其他语言的大纲、符号读取和修改，以及语义引用、跨文件重命名，使用你显式配置的本地 LSP stdio 语言服务；本程序不自动安装语言服务。

例如，先在自己的 Python 环境安装 `python-lsp-server`，再指定该环境的解释器：

```powershell
python -m pip install python-lsp-server
python -m chatcode --workspace D:/your/project --language-server 'python=["C:/path/to/python.exe", "-m", "pylsp"]'
```

GUI 的「高级连接设置」可填语言服务命令 JSON，例如：

```json
{"python": ["C:/path/to/python.exe", "-m", "pylsp"], "typescript": ["typescript-language-server", "--stdio"]}
```

- TypeScript 等服务需自行安装；命令须在本机存在。配置键支持 python/javascript/typescript/go/rust/java/csharp/c/cpp/vue/svelte，能力取决于所选服务。CLI 可重复传 `--language-server LANG=COMMAND`。
- Python 默认 `code_outline`/`read_code` 使用 AST；传 `backend: "lsp"` 可切换。`find_symbol` 传 `language` 使用对应服务；其他语言默认使用已配置服务。
- `find_references` 接受 `path`、`line`、`column`，均从 1 开始，列按 Unicode 字符计数；内部转换 UTF-16 位置。只有 `semantic: true` 才是语义引用；未配置服务时会注明文本候选可能含字符串、注释和同名符号。
- `rename_symbol` 需要源文件的 `expected_revision`，支持 `dry_run`、`operation_id`。影响文件同步后重新计算，提交前统一复核原始字节版本，沿用事务历史和撤销。拒绝工作区外路径、重叠编辑和文件资源操作；不进行猜测性文本重命名。
- `edit_files` 可以用 LSP 大纲返回的 `symbol_id` 精确替换其他语言的完整符号；默认语法预检仍只覆盖 Python/JSON。
- 语言服务按需启动，停止服务时清理；`--no-exec` 同时禁用语言服务进程。GUI 会保存语言服务命令配置，请使用不含凭据的命令。
- 搜索结果有 60 KB 结构化匹配预算，用 `next_offset` 续读。单次最多遍历 5000 条目录项，单文件搜索上限 400000 字节；`skipped_count`/`skipped_files` 和 `scan_limit_reached` 会明确提示未搜索部分，应缩小目录或按文件读取。

### v0.4 后台执行与本地管理

- `exec_command` 支持 command 或 argv（二选一），以及 default/cmd/powershell/bash。默认先等待 1000ms，未完成返回 process_id；`yield_time_ms` 可设 0–10000。
- 用 `read_process` 返回的 `next_stdout_offset` / `next_stderr_offset` 续读，避免重复日志；`server_info` 可以找回当前服务的任务 ID。
- `timeout` 默认 60 秒，上限 3600，0 表示持续运行直到停止。每个项目最多同时运行 4 个命令任务；停止服务会清理其任务。
- Windows 优先使用 Job Object 管理后代进程，失败时回退 taskkill；POSIX 使用进程组。默认 host 模式仍使用当前用户权限；native 模式使用启动前强制加入 Job 的 AppContainer，不允许回退。
- 每个任务的 stdout/stderr 各保留不超过约 4 MiB 的滚动日志，丢弃内容用 lost_bytes 明确报告。最近已完成任务的磁盘日志保留约 32 条，位于应用数据目录。
- `run_checks` 支持显式 argv/command，也可以执行项目 `.chatcode.toml` 的命名检查。检查诊断为日志的尽力解析，完整输出仍可继续读取；零测试结果会提示核对配置。
- GUI 的后台任务页可查看日志、停止任务；文件改动页可看 diff 和撤销。高级设置支持固定地址、frp 和命令执行开关。
- 最近 20 个项目的非秘密连接设置保存在应用数据目录，显式 CLI 参数优先；授权口令和 token 不保存，带认证的代理地址不写入配置。

```toml
# 项目根目录 .chatcode.toml，工具只会在 run_checks 被调用时运行命令
[checks]
tests = ["python", "-m", "pytest", "-q"]
lint = ["ruff", "check", "."]
```

### v0.3 读写流程

1. 新对话先用 `project_overview`，再用 `find_files` / `find_symbol` 定位。
2. 用 `code_outline` 查看定义，`read_code` 按 qualified_name 或 symbol_id 读取；内置后端目前支持 Python。
3. `read_file` 流式读取指定行，同时计算整个文件的字节版本；大文件可通过 `offset` / `limit` 分页。超长行会明确标记 `truncated_lines`。
4. `read_files` 除兼容 `paths`，还支持 `requests: [{path, offset, limit}]`。`batch_read` 一次并行执行最多 8 个独立只读工具，逐项返回结果或错误。
5. `edit_files` 的 `files` 支持 create/update/delete/move；更新文件可传完整 content 或 edits（唯一字符串、行范围、符号）。同一文件的所有 edits 都基于原始快照，重叠编辑会被拒绝。Python/JSON 默认做语法预检。
6. 每次批量修改提供唯一 `operation_id`，在历史保留期限内，相同请求重试会复用结果；不同内容复用 ID 会被拒绝。
7. 修改返回 change_id 和有预算限制的 diff，可用 `get_changes` 展开，或 `undo_changes` 恢复；遇到后续文件变化时撤销会被拒绝。

### v0.2 文件修改约定

- `read_file` / `read_files` 返回的 `revision` 是实际文件字节的 SHA-256，包含编码、BOM 与换行变化。升级后请重新读取文件，旧版本号不再通用。
- `write_file` 覆盖已有文件、`replace_in_file` 修改文件时，必须传入读取结果中的 `expected_revision`；新建文件可以省略。
- `apply_patch` 可以传 `expected_revisions`（相对路径到版本号的映射）。已有文件的纯插入 hunk 缺少上下文，必须提供对应版本。
- 读取文本统一为 LF，写回沿用原文件编码、BOM 和主要换行风格。内容无变化时保留原始字节；混合换行文件发生实际编辑时统一为主要风格。
- 写入返回 `change_id` 和 `state`。`REVISION_MISMATCH` 表示需要重新读取；`recovery_required` 表示恢复遇到冲突，应检查错误详情和当前文件，避免覆盖后续外部修改。
- 编辑日志位于应用数据目录的 `changes/` 下，正常记录保留最近 100 条、合计约 256 MiB；未完成的恢复材料保留。它们包含被修改文件的副本。

---

## 网速优化（重要）

Cloudflare Quick Tunnel 的链路是两段：

```
ChatGPT 云端 → Cloudflare 边缘        段1：都在海外，很快
你的电脑     → Cloudflare 边缘        段2：跨境，这才是瓶颈
```

**提速办法**：让 cloudflared 走你的本地代理出站。cloudflared 认标准环境变量
`HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY`（见 cloudflared PR #1514 的 proxyAwareDialer），
段 2 就会改走你的优质线路：

```bash
# 自动探测（默认开启，会试 7897/7890/7891/10809 等常见端口）
python -m chatcode --workspace D:/your/project

# 或手动指定
python -m chatcode --workspace D:/your/project --proxy http://127.0.0.1:7897

# 不想走代理
python -m chatcode --workspace D:/your/project --no-proxy
```

其他提速手段：

- **用 `read_files` 批量读**，别一个个 `read_file` —— 每次往返都要跨一次公网
- **换穿透后端**：`--tunnel cpolar`（国内入口）或 `--tunnel frp`（自建服务器，最可控）
- **响应自动 gzip**（超过 1KB 才压，小响应不压以免反而变慢）

### 各后端怎么选

| 后端 | 入口位置 | 适合场景 |
| --- | --- | --- |
| `cloudflared`（默认） | 海外 | ChatGPT 这类云端模型访问（段 1 最快） |
| `cpolar` | 国内 | 国内网络环境的客户端访问 |
| `frp` | 你自己的服务器 | 有服务器时最可控，可配固定域名 |
| `none` | — | 已有公网地址，用 `--public-url` 直接指定 |

---

## 安全边界

| 机制 | 说明 |
| --- | --- |
| 工作区限定 | 所有路径必须落在工作区内，拒绝绝对路径、`..` 穿越、盘符路径 |
| 符号链接检查 | 解析真实路径，拒绝写入指向树外的符号链接 |
| 原子写入 | 同目录临时文件 → fsync → `os.replace`，不会留下半截文件 |
| 乐观锁 | 修改已有文件时校验字节版本，提交前再次复核 |
| 工作区写锁 | 同一应用数据目录下的服务实例通过跨线程/跨进程文件锁串行提交 |
| 补丁恢复 | 全部预检后提交；写入失败尝试恢复原始字节，外部改动冲突保留并报告；启动时恢复被中断的提交 |
| 破坏性命令拦截 | `rm -rf /`、格式化、`git reset --hard` 等直接拒绝 |
| 认证 | 默认 path 密钥路径；也支持 OAuth 授权码 + PKCE S256、Bearer；OAuth 路由只在 OAuth 模式开放 |

**注意**：模型在云端，被读取的文件内容会作为上下文发到对方服务器。
敏感代码请先想清楚合规边界；命令执行和文件写入只发生在你本机。

---

## 已知限制

- **免费隧道域名每次启动都会变**，重启后需要在客户端更新一次地址。想要固定域名请用 `--tunnel frp` 自建，或 Cloudflare 命名隧道配 `--public-url`。
- **当前实现不提供 SSE**：工具请求使用 POST + JSON 响应，具体客户端兼容性应在实际接入时验证。
- **OAuth 令牌**：access token 默认 24 小时；refresh token 默认 30 天并轮换，绑定客户端和资源。客户端注册信息落盘，令牌保存在内存，服务重启后需要重新授权。
- **文件锁范围**：应用数据目录相同的服务实例共用锁；普通编辑器与其他程序不参与此锁。服务会在提交和恢复前检查版本，但不提供操作系统级文件隔离或任意多文件同时切换保证。
- **并发上限 200**：Quick Tunnel 同一时刻最多 200 个在途请求，超出返回 429。正常对话远远用不到。
- 文件副本不隔离系统权限；可选 native 模式使用 Windows AppContainer，具体边界见 v0.11 说明。

---

## 设计参考与许可

本项目为独立实现（纯 Python 标准库，零第三方依赖），
但在**运行时契约与安全设计**上参考了
[Coding Tools MCP](https://github.com/xyTom/coding-tools-mcp)（Apache-2.0），
借鉴的设计包括：工作区路径边界模型、补丁的原子提交与基线校验、
OAuth 2.1 + PKCE + RFC 7591 动态注册的接入方式、
`content` / `structuredContent` 双层结果结构、以及不提供 SSE 流的取舍。

按其 Apache-2.0 许可要求，此处保留归属声明：

```
Project: Coding Tools MCP
Author: Coding Tools MCP Contributors
Source: https://github.com/xyTom/coding-tools-mcp
License: Apache License 2.0
```

本项目自身也以 Apache License 2.0 发布，见 [LICENSE](../LICENSE)。

---

## 打包成 exe（给同事用）

```bash
pip install pyinstaller
python -m PyInstaller --noconfirm --distpath dist/v0.11.1 --workpath build/v0.11.1/pyinstaller "ChatCode启动器.spec"
```

把 `bin/cloudflared.exe` 一起打进去，同事双击即可，不需要装 Python。

---

## 开发

```bash
python -B tests/run.py  # 可靠性/工作流/语言服务协议回归 + 隐藏 GUI 验证 + 44 项 HTTP 冒烟测试

# 分开运行
python -B -m unittest discover -s tests -p "test_*.py" -v
python -B tests/smoke.py

# Windows 打包后验证 exe 内的任务保存、进程检查、交付与正则子进程
python -B tests/verify_frozen.py "dist/v0.11.1/ChatCode启动器.exe"

# 可选：在另一个 Python 环境安装 pylsp 后，验证真实语义引用、跨文件改名和撤销
python -B tests/verify_language_service.py --python C:/path/to/python-with-pylsp.exe
```

核心纯标准库，无强制运行时依赖，Python ≥ 3.11；LSP 是可选的独立本地进程。

测试在临时工作区和临时应用数据目录中运行，不修改真实 OAuth 注册信息，也不启动公网隧道。`tests/verify_public.py` 是单独的公网验证脚本，按需手动运行。

已分阶段完成：v0.2 可靠性与认证修复 → v0.3 批量读写与事务历史 → v0.4 后台任务与 GUI 管理 → v0.5 可选语义服务。当前没有内置全语言分析器或大型仓库的持久化索引；不依赖语言服务的通用文件工具始终可用。

## v0.8 任务中心与对话恢复

任务用于保存一次工作的目标和执行证据。项目身份决定访问哪个目录，`task_id` 决定记录归入哪项工作；任务范围 `scope` 是说明文字，不构成文件或命令权限隔离。未传 `task_id` 的旧工具调用继续可用，但不会自动归入任务。

| 工具 | 用途 |
| --- | --- |
| `task_create` | 创建目标、验收条件和必需检查，`operation_id` 对创建重试去重 |
| `task_list` / `task_get` | 项目内列出任务、读取进度和分页事件 |
| `task_update` | 用 `expected_revision` 更新摘要、步骤、决策、下一步和状态 |
| `task_context` | 读取恢复包，复核实际文件版本，返回进程、改动及项目说明 |
| `task_finish` | 核对后保存交付说明与逐条验收证据 |
| `task_cancel` | 取消任务，停止本服务可控制的所属进程，保留已完成修改 |

建议对网页助手这样说：

> 先用 server_info / list_workspaces 核对项目，再 task_create 保存目标和验收条件；后续文件、命令与检查都携带项目身份和 task_id。阶段完成后 task_update 保存摘要、决定及下一步。交付前查看改动、执行必需检查，再 task_finish。

换对话后：先 `task_list` 找原任务，`task_context` 读取恢复包，再从 `next_steps` 继续。服务重启后先核对现场，用 `task_update(status="running", expected_revision=...)` 接续。恢复不会自动执行命令，不会唤醒网页模型，也不会恢复网页对话本身。正常停止将进行中的任务标为 `interrupted`；异常退出留下的旧运行任务会显示待恢复。

恢复包包含目标、摘要、步骤、决定、下一步、最近事件、已观察文件的实际版本、任务进程及最近改动，并附项目根 README/AGENTS 与相关目录 AGENTS 的有界摘录。项目材料仍受文件访问策略限制。摘要与验收说明是对话报告，文件版本和进程结果分别核对。

交付限制：

- 存在运行或重启后状态未知的所属进程，拒绝完成；不会根据旧 PID 终止其他服务的进程。
- 存在缺失结束记录的修改调用，拒绝完成；人工核对后取消原任务并创建后续任务，不能假定执行成功或自动重跑。
- 已观察文件发生变化时，要求重新读取；发现版本变化会使旧检查失效。必需检查取同名最新结果，`test/tests` 共用同一结果序列，必须在最近修改或普通命令后通过。已识别的“零测试”结果不能用于完成。
- `criteria_evidence` 按创建时验收条件的顺序填写；它是交付说明，不是业务正确性的自动证明。任务未设置 `required_checks` 时，不强制运行检查。
- 单任务交付复核最多 200 个已观察文件、合计 20 MiB 和 100 个进程；超限或未能核对时拒绝完成，大任务应拆分。恢复包最多约 120 KB，裁剪在 `omitted_for_budget` 中明示；事件可通过 `task_get(after_seq=0)` 从头分页。

暂停阻止该任务后续修改，不停止已经运行的命令；恢复仅修改任务状态。取消不会撤销文件，仍可通过文件改动历史逐项查看和撤销。已完成/取消任务不能继续修改；可创建后续任务。

任务数据库存储于应用数据目录 `tasks/<项目哈希>/tasks.sqlite3`，按项目隔离；单项目最多保存 1000 个任务，单任务摘要限 64 KB、工具开始记录达到 10000 个事件时停止接收新执行调用。进程日志沿用原有滚动和保留策略，可能已裁剪或清理，不承诺永久保留完整输出。任务事件不复制完整命令参数、环境变量、文件正文与命令输出，恢复时附最近进程的有限日志尾部；原进程记录及对话摘要可能包含敏感内容，应保护应用数据目录。

以上为 v0.6 阶段的能力记录。后续已补充任务文件副本、可选桌面后端和 Windows 原生执行隔离，见后续版本章节；桌面设置入口在 v0.11.1 隐藏。

## v0.9 独立任务副本与整批撤回

新建任务可传 `isolation="copy"`，系统在应用数据目录中创建独立文件副本与只读用途的创建基线。后续仍携带**原项目**的 `workspace_id/workspace_token` 和 `task_id`，文件工具、命令、项目检查及语言服务自动使用任务副本；不需要另建隧道或登记新项目。GUI 新建任务默认勾选此选项。API 省略 `isolation` 保持旧版 `direct` 行为；未携带任务 ID 的调用也继续作用于原项目。

建议工作流程：

1. `task_create(isolation="copy", ...)` 创建任务。若依赖现有 Git 历史、虚拟环境或被排除资源，先确认副本是否能完成工作。
2. 文件读取、修改、命令和检查都携带同一个 `task_id`。命令在副本内正常产生的文件变化也会进入后续差异。
3. 运行检查并 `task_finish` 完成副本内工作。此时原项目尚未改变，GUI 显示“待合入”。必需检查保存副本内容指纹，检测到检查后的未观察文件变动也会拒绝完成。
4. `task_changes` 分页查看变动与原目录冲突，用 `next_offset` 续读。`diff` 是全局有界摘录，文件列表分页不改变 `diff_revision`。
5. `task_apply` 默认 `dry_run=true`，仅预览。实际合入须提供 `expected_revision`、预览得到的 `diff_revision` 和 `dry_run=false`。副本完成后发生变化、原文件冲突或原项目仍有本服务命令运行时拒绝合入。原项目无关文件的修改会保留。
6. 若需撤回，用 `task_revert(expected_revision=..., dry_run=true)` 先预览，再设 `false` 执行。新增文件会删除，删除文件会恢复，修改文件恢复到合入前字节；发现后续外部修改则整批拒绝覆盖。合入和撤回均有编辑引擎的恢复记录，网络重试不会重复写入。

GUI「任务中心」提供“副本差异 / 合入原项目 / 预览撤回 / 撤回合入”。“合入原项目”与“撤回合入”按钮会实际修改原目录，建议先使用对应预览按钮。后台进程页包含当前项目各副本的命令，可查看和停止。

边界与容量：

- **这是文件副本隔离，不是操作系统沙箱。** 命令、语言服务仍以当前系统账户运行，能使用绝对路径、网络和系统资源；恶意或写死原路径的命令仍可能影响副本之外。整批撤回只覆盖通过 `task_apply` 合入的文件内容，不能撤回目录外文件、数据库、网络操作或其他进程副作用。
- 沿用当前文件策略与噪音目录排除；不会复制链接/junction、`.git`、`node_modules`、虚拟环境、构建缓存，以及 `.exe/.dll/.pdb/.pyc/.pyo/.obj/.o/.class` 编译产物。默认不复制敏感文件；本机显式允许敏感文件时沿用该设置。排除数量在任务 `isolation.omitted_entries` 中报告；被排除内容不参与合入或删除。
- 复制最多 5000 个文件、64 MiB，总遍历最多 20000 条目录项，单文件最多 8 MiB；超限拒绝创建，不返回半成品任务。单次合入最多 200 个变动文件。diff 只展示有界摘录，实际合入使用完整字节与版本校验。
- 原项目与所有副本合计最多同时运行 4 个命令。服务停止会清理各副本所属进程；重启后任务仍路由到原副本，丢失副本时明确报错，不会回退到原目录运行。
- 任务副本/基线保存在 `isolated/<项目哈希>/<任务ID>/`，当前不会自动删除，需留意磁盘空间。已合入文件的撤回材料沿用编辑历史保留策略（最近 100 条完成记录、约 256 MiB）；不是永久备份。文件权限、空目录和扩展属性不提供完整快照恢复。

后续功能：桌面后端已在 v0.10 接入；Windows 原生执行沙箱见 v0.11。

## v0.10 可选桌面验收链路

桌面能力用于编码后的界面验收：查看程序、操作控件、核对显示结果。它不增强 Java/Go 的语义分析，也不代替语言服务、代码编辑、测试和回滚。

后端仅复用固定提交 Windows-MCP 0.8.8 的 `windows_mcp.uia`，运行在独立 Python 3.14 环境。前端 exe 不导入它的依赖，不启动额外网络服务，不代理其 PowerShell/Registry/FileSystem 工具。核心代码功能可独立使用，桌面默认关闭。

安装（Windows，需要已安装 Python 3.14）：

```powershell
python desktop/setup_backend.py --python "C:/path/to/Python314/python.exe"
```

脚本按 `desktop/requirements.lock` 安装固定版本到 `%LOCALAPPDATA%/chatcode-mcp/desktop-runtime-v0.10`，不自动绑定窗口或启用输入。完整第三方运行环境不包含在启动器 exe 中；许可来源见 NOTICE，各包保留其许可元数据。

本机操作：启动服务 → “桌面窗口设置” → 填后端虚拟环境的 `Scripts/python.exe` → 刷新窗口列表 → 选择窗口 → 按需勾选输入 → “绑定并激活窗口”。默认仅观察。主窗口“暂停桌面”会停止后端，远程不能自行恢复；重新绑定后才恢复。授权窗口、输入开关不写入最近项目配置。

CLI 可显式指定本次授权：

```powershell
python -m chatcode --workspace D:/your/project --desktop-python "C:/path/to/desktop-runtime-v0.10/Scripts/python.exe" --desktop-window 123456
# 确需输入时增加 --desktop-input；--read-only 会禁用输入
```

窗口 HWND 必须来自本机选择，远程不能自行设置。`--no-exec` 禁用代码命令工具，但不否定另行显式配置的桌面后端；桌面输入有独立开关。

| 工具 | 用途 |
| --- | --- |
| `desktop_status` | 查看后端、窗口、输入许可与暂停状态 |
| `desktop_observe` | 观察本机已绑定窗口（支持后台只读观察），返回 UIA 控件、前台状态、版本、30 秒单次票据及可选 PNG |
| `desktop_action` | 按控件 ID 点击，或用 ValuePattern 设置普通编辑框的完整文本 |
| `desktop_release` | 释放当前项目/任务的窗口租约和旧票据 |

网页助手先观察，再提供 `element_id`、观察中的 `revision`、`snapshot_token` 与唯一 `operation_id` 执行动作，随后重新观察验证。每次动作消耗票据；网络重试复用同一操作 ID，已完成动作只返回记录，重启或结果不明时不自动重做。已观察布局/内容变化、窗口失焦、窗口身份变化或检测到本机输入时拒绝操作。任务关联的动作成功或结果不明后，未重新观察会阻止任务直接完成；观察本身不自动证明业务正确。

窗口绑定到本机选定项目；切换 GUI 项目不转移授权。项目内不同任务不能同时持有活动租约。独立后端通过 Windows 命名互斥协调多个 ChatCode 服务；它不协调其他厂商自动化工具。Escape 按住时会阻止后端操作；本机暂停可中断后端请求，但不会撤销已经完成的 UI 动作。

边界：

- 此版仅提供点击与普通非密码编辑框设置全文，不提供任意坐标、全局快捷键、拖拽、应用启动或上游任意工具透传。不能输入的控件会明确报不支持。
- 截图使用窗口自身的 PrintWindow 输出，不回退到整屏截图。已绑定窗口支持后台只读观察；输入和点击仍要求窗口位于前台，切换前台后必须重新观察。部分 GPU/浏览器/特殊窗口可能返回空白或不支持；可用 `image=false` 只取控件树。前台/输入检查是保守措施，不是系统级输入隔离或应用沙箱。
- UIA 树最多返回 200 个控件、约 60 KB，遍历最多 1000 个节点；单张 PNG 最多 2 MB，尺寸缩至 1440×1000 内。窗口内容仍可能包含该窗口展示的敏感信息，权限由本机绑定决定。
- 动作只约束目标窗口，按钮可能触发网络、文件或其他真实业务操作。它们不属于独立文件副本，也不能用 `task_revert` 撤回。
- 去重记录保存在应用数据目录 `desktop_operations/`，不保存输入文本或截图，只保存请求指纹和执行凭证；最多 10000 条，当前不会自动清理。任务事件也不复制截图和输入正文。
- 已验证源码及此前 v0.11 测试包对自建 Win32 窗口的真实截图、UIA 输入、点击后重新观察，以及本地 HTTP 图片/任务验证链路。最终包加入后台只读观察修复，已完成模块一致性与后台执行回归；重建后的输入/点击复验由用户停止，未计为通过。真实浏览器与公网协议探测均已收到 600×330 PNG。具体 ChatGPT 网页会话的工具调用与模型视觉理解仍需独立验收；各层结果见随包 verification.json。

开发验证（普通回归不操作真实桌面）：

```powershell
python -B tests/run.py
python -B tests/verify_desktop.py --python "C:/path/to/desktop-runtime-v0.10/Scripts/python.exe"
```

真实桌面脚本只创建、操作并关闭自己的测试窗口；检测到本机接管时可能拒绝执行，不能通过关闭这些检查把测试变成对其他窗口的控制。

## Windows-MCP 融合方案与后续扩展

已审阅 [CursorTouch/Windows-MCP](https://github.com/CursorTouch/Windows-MCP) 的源码，参考提交 `128f2960c79b74bd2630f4dbc9d4f664f4ce89b3`。

建议将它作为可选 Windows 桌面操作后端，以独立本地进程接入本项目的统一 MCP 入口。本项目继续负责网页接入、认证、项目路由、任务管理、代码编辑和语言服务；后端负责桌面状态、控件树、截图及输入交互。v0.10 已在独立进程内复用 `windows_mcp.uia`；未代理上游完整 MCP 工具集。当前窗口捕获使用本项目 PrintWindow 实现，不捕获整个桌面；原方案中的多显示器观察、DOM、完整桌面控制权仍未接入。

值得复用的设计：

- `Snapshot/Screenshot` 区分完整控件状态与快速视觉观察，支持区域和显示器选择；对网页往返可减少无关上下文。
- UI Automation 控件树与控件标签定位，配合截图；操作后重新观察，不能假定点击成功。
- 桌面控制权与用户接管机制，以及快照与动作之间的状态校验。
- 工具分类和按能力启用；桌面控制需全局互斥，不能随多个项目并发争抢同一个桌面。

集成前提：

- 上游 `pyproject.toml` 当前要求 Python >=3.14，使用 FastMCP、COM/Win32、截图等依赖；本项目当前 Python 3.12 打包环境不能直接导入当前上游包。独立后端可隔离运行环境与原生组件故障。
- v0.10 已支持 MCP PNG 图片内容块，并验证本地真实 HTTP 返回；具体网页客户端的视觉接收能力还需在该客户端验证。
- 初期只映射必要的观察和输入能力；上游 PowerShell、Registry、FileSystem 不直接代理，避免绕开本项目的文件与命令策略。桌面动作本身仍可影响真实系统，需单独授权范围、窗口绑定、活动指示和停止入口。
- 上游不提供系统沙箱；桌面控制权不等于文件/网络/系统权限隔离，不能把接入它当作本项目已有沙箱。
- 固定后端版本，关闭默认匿名遥测（`ANONYMIZED_TELEMETRY=false`），测试依赖与打包行为。上游 MIT 许可要求保留版权与许可声明；实际分发还需核对随包依赖的许可。

一条建议验收链路：网页模型修改项目 → 本地启动预览 → 桌面后端观察并操作浏览器 → 返回截图与验证证据。它补充可见界面的操作与验证，不替代项目和任务运行时。

来源：[依赖](https://github.com/CursorTouch/Windows-MCP/blob/128f2960c79b74bd2630f4dbc9d4f664f4ce89b3/pyproject.toml)、[快照工具](https://github.com/CursorTouch/Windows-MCP/blob/128f2960c79b74bd2630f4dbc9d4f664f4ce89b3/src/windows_mcp/tools/snapshot.py)、[控制分类](https://github.com/CursorTouch/Windows-MCP/blob/128f2960c79b74bd2630f4dbc9d4f664f4ce89b3/src/windows_mcp/tools/control_policy.py)、[安全说明](https://github.com/CursorTouch/Windows-MCP/blob/128f2960c79b74bd2630f4dbc9d4f664f4ce89b3/SECURITY.md)、[许可证](https://github.com/CursorTouch/Windows-MCP/blob/128f2960c79b74bd2630f4dbc9d4f664f4ce89b3/LICENSE.md)。

## v0.11 正式版：语言、大项目、历史回收与原生执行隔离

本版新增 9 个工具，总计 50 个。Windows exe 内置独立 Python 3.14.7 运行时，原生执行隔离**不依赖 Docker、WSL、虚拟机或用户另装 Python**。源码构建者可运行 `python sandbox/prepare_runtime.py` 准备同一固定运行时，再执行打包命令。

### Java / Go

- 无服务时也能读取任意文本代码；Python 使用 AST，Java/Go 的 `code_outline`、`read_code` 提供声明导航，明确返回 `lexical_candidates`、`semantic=false`。这不等于类型推导和真实引用。
- `language_status` 检测 Go/JDK/服务配置与会话状态，不执行或自动启用程序。GUI 高级设置可“检测并填入 Java / Go 语言服务”，保留已有自定义配置。
- 可选安装：`python languages/setup_languages.py --language both`。需要项目对应的 Go 工具链、JDK；安装脚本固定 gopls v0.21.1、JDT LS 1.55.0，并校验 JDT LS 官方下载 SHA256。网络环境需要时可显式设置 `--go-proxy https://goproxy.cn`；仍使用 Go 的校验机制。
- JDT LS 是后台语言服务，不要求安装或使用 Eclipse 编辑器。本机默认关闭根目录 Eclipse 元数据及自动构建，缓存用 `%LOCALAPPDATA%/chatcode-mcp/language-cache/<项目哈希>`，可在本机命令参数中使用 `{workspace_cache}` 绑定各项目独立缓存。
- 已用普通 Maven 目录结构和 Go module 验证：语义引用、跨文件重命名、同名不同作用域保护、dry-run、源文件字节撤回，以及 javac/java、go test。Maven 样例根目录没有生成 `.project`、`.classpath`、`.settings`。语言服务仍可能按工具自身规则产生依赖/构建缓存；宿主模式不把它当作只读文件 API。

### 大项目索引与上下文

- `refresh_index` 增量建立持久 SQLite 定义索引；每批最多处理 5000 个目录项、可配置 1..5000 个需更新文件，并有解析时间预算。未完成时返回 `complete=false`、`pending_is_lower_bound=true`；继续调用即可推进，重启后从现有记录恢复扫描。
- 内置索引存储 Python AST 和 Java/Go 声明候选元数据，不保存整个源文件正文。每项目上限 10 万源文件、20 万扫描项、128 MiB 数据库；单文件 2 MiB、2000 个符号和 200 KB 符号元数据。语法错误和超限会报告，不伪造完整索引。
- `find_symbol` 自动推进索引；支持 `offset`、`limit`、`budget_chars`，返回前重新执行访问策略与内容版本校验。指定 `language` 则使用已经配置的 LSP 工作区符号。
- `context_pack(query, budget_chars, max_files)` 按定义组装有预算的片段，每个文件附实际版本。片段不是整个依赖图，Java/Go 候选也不是语义证明。
- 6000 个 Unicode 路径源文件的实测在 `build/v0.11.0/index-verification.json`；构建分批完成，热查询不重新解析源文件，修改单文件仅重建该文件。

### 归档、导出和清理

- `task_archive(task_id, expected_revision)` 归档已完成/取消任务；`task_list(archived=true)` 查归档，`task_archive(..., archived=false)` 恢复归档任务。归档不删除副本。
- `task_export` 导出本机 JSON（任务、事件、版本和执行元数据），不包含文件正文和原始命令日志；同一任务覆盖自己的导出文件，不向外部服务上传。
- `task_cleanup` 默认 dry-run，返回具体任务、字节数与 `plan_revision`。实际删除必须传 `dry_run=false` 和当前预览版本；仅回收已归档任务副本，**也会删除尚未合入的副本修改**。默认归档 7 天，显式 `older_than_days=0` 可立即回收。运行/状态不明进程、路径链接与变化后的计划会阻止清理。
- 任务历史、执行事件和已合入的撤回凭证保留；释放的副本禁止继续读写/执行，不退回原项目。`task_context` 对这类任务返回 `historical_only=true`，已合入内容仍可按凭证 `task_revert`。
- GUI 任务中心提供归档列表、导出、容量/索引、带预览的清理按钮。`storage_status` 查看副本容量；`--storage-quota-mb` 默认每项目 2048 MiB，在新建副本和启动命令前执行检查。**这是启动预算，不是进程运行期间的硬磁盘配额**；每个命令仍应设置超时。

### Windows 原生沙箱

```powershell
python -m chatcode --workspace D:/your/project --execution-mode native
```

GUI 高级设置把执行模式设为 `native`。默认 `host` 保持兼容；远程工具不能更换执行模式。native 创建任务时默认强制副本，命令须携带其 `task_id`；缺少副本、系统 API/运行时/授权失败时返回错误，绝不退回宿主执行。

- 每条命令使用新的 Windows AppContainer 身份，授予 `internetClient` 以允许公网出站，不继承宿主环境；只为任务副本授予修改权限，为内置运行时授予读取执行权限。不改变原项目、宿主 Python/JDK/Go 的 ACL。网络访问仍遵循 Windows、防火墙和网络环境的策略；未自动添加回环豁免。能力定义参见 [Microsoft AppContainer 文档](https://learn.microsoft.com/en-us/windows/win32/secauthz/implementing-an-appcontainer)。
- 以挂起状态创建进程，成功加入 Windows Job 后才运行；Job 默认整个树 1024 MiB（可设 128..8192）、最多 64 个进程、20% CPU，退出/超时/停止时清理后代。单个副本的原生命令顺序执行，跨任务仍共享总并发上限。
- 基础 Python 标准库随 exe 提供。项目依赖和其他语言编译器仍需按项目准备；不能访问的宿主工具会报错，不会为它们扩大系统权限。native 不启动宿主 LSP、Git 或桌面输入，可使用声明导航、任务 diff 与受隔离的构建命令。
- AppContainer **不是虚拟机或独立操作系统**。Windows 对所有应用公开的只读文件/注册表等资源仍可能可见；应用自己的容器私有存储也可用。不能承诺整个磁盘除副本外完全不可读，也不提供硬磁盘容量限制。
- `sandbox_status` 检查本机 API/内置运行时并报告联网能力；真实隔离验收见 `tests/verify_native_sandbox.py`：实际 token 中的 `internetClient`、公网 DNS/TLS/HTTPS、宿主私有文件读写拒绝、子进程清理、超时、内存和无回退。本次 Windows x64 环境已通过；不是所有 Windows 策略/终端安全环境都已验证。

### 验收范围

浏览器验收使用本机临时测试页，通过真实 MCP 初始化、工具发现和 `desktop_observe` 返回 PNG，再由浏览器解码显示；报告明确记录 `chatgpt_client_tested=false`。它不等同于证明每一种网页聊天产品都把图片交给视觉模型。当前已连接的旧服务也不会自动升级，使用新版 exe 后须重新连接/刷新客户端工具定义。

验收记录在 `build/v0.11.0/`：完整回归、真实 Java/Go、6000 文件索引、原生沙箱、真实桌面、打包模块与浏览器结果。旧版本章节中的能力限制描述对应当时版本，以后续版本说明及对应验收记录为准。

## v0.11.1 优化

- 原生沙箱允许公网出站联网，保留私有文件权限、进程树、内存、CPU、超时隔离及无宿主回退。状态、工具说明与 GUI 文案统一报告 `internetClient`。
- 隐藏 GUI 的“桌面窗口设置”和“暂停桌面”入口，固定显示“桌面自动化：暂不使用”。底层可选桌面工具保留，普通编码无需配置窗口。
- 面板读取期间发生的刷新会合并，并在读取结束后继续刷新，避免刚完成的任务/修改暂时不显示；分页参数取调用时快照。
- 隐藏 GUI 验收直接报告错误，不再等待不可见的错误对话框；副本合入验收等待持久状态落盘后再撤回。
- 已通过当前已连接 v0.11.0 服务进行副本读写、版本冲突、失败检查阻止交付、修复后测试、任务恢复与交付；同一连接执行新源码的联网沙箱验收也通过。此结果不表示正在运行的旧 exe 已升级，也不代表视觉模型验收。
- 本版证据见 `build/v0.11.1/verification.json` 和随包报告。当前运行实例保持不动；使用新版 exe 后才会加载新的界面和联网策略。

实现依据：[Windows AppContainer](https://learn.microsoft.com/en-us/windows/win32/secauthz/appcontainer-isolation)、[Go 官方 gopls](https://go.dev/gopls/)、[Java 语言服务](https://github.com/eclipse-jdtls/eclipse.jdt.ls)。
