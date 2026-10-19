# WinCodex

**Take web AI beyond the chat box. Put it to work on your Windows projects.**

A local coding engine for ChatGPT and MCP-capable web AI — from reading your codebase to editing files, running tests, and delivering changes you can review and apply.

**Your web AI supplies the intelligence. WinCodex gives it the tools to act.**

Built for **Windows**. Connected through **Model Context Protocol (MCP)**. **No model API key. No Docker.** Keep working with your chosen web chat while WinCodex connects it to your local development environment.

[![Tests](https://github.com/kimlonger/wincodex/actions/workflows/ci.yml/badge.svg)](https://github.com/kimlonger/wincodex/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache--2.0-blue.svg)](LICENSE)
[![Windows release](https://img.shields.io/github/v/release/kimlonger/wincodex)](https://github.com/kimlonger/wincodex/releases/latest)

[中文说明](docs/README.zh-CN.md) · [Windows downloads](https://github.com/kimlonger/wincodex/releases/latest) · [Tool reference](docs/TOOLS.md) · [Architecture](docs/ARCHITECTURE.md)

Independent, community-developed software. Not affiliated with OpenAI or Anthropic. Formerly **ChatCode MCP**.

Previously published as **ChatGPT Code Bridge MCP**. The repository and launcher are now named **WinCodex**; legacy Python module/data identities stay compatible.

For a source-based comparison with WebCodex, see [the comparison notes](docs/COMPARISON-webcodex.zh-CN.md).

## Why use it?

You already use a web chat for coding, but the model cannot directly inspect your project, edit multiple files, or run your test suite. This bridge exposes those capabilities over authenticated HTTPS.

**No model API key is configured in this bridge.** Your chosen client still controls model access, MCP availability, tool permissions, and usage limits. The bridge does not bypass those limits or scrape chat websites.

A typical request:

> Inspect this Java or Go project, fix the failing behavior in an isolated task copy, run the tests, and show me the diff before applying it.

## Features

| Capability | What is included |
| --- | --- |
| Local code tools | File discovery, batched reads, text search, version-aware edits and multi-file patches |
| Project switching | Register and switch projects while keeping the same running MCP endpoint and tunnel |
| Code navigation | Python AST, Java/Go declaration candidates, optional LSP references and cross-file rename |
| Context control | Persistent incremental symbol index, bounded snippets, pagination and context packs |
| Command execution | Background jobs, output cursors, timeouts, process-tree termination and named checks |
| Review and recovery | Git status/diff/log, edit history, undo, task-copy diffs, guarded apply and revert |
| Durable tasks | Goals, acceptance criteria, recovery context, required checks, archive, export and cleanup |
| Optional OS isolation | Windows AppContainer task execution with public outbound networking and resource limits |
| Remote access | Cloudflare tunnel support, path credentials, OAuth with PKCE, or bearer authentication |

**50 MCP tools** are available in the standard mode. Read-only mode exposes a reduced set.

The desktop automation backend is optional and its GUI controls are currently hidden. The launcher displays **“桌面自动化：暂不使用”** (“Desktop automation: not in use for now”). Coding does not require binding a desktop window.

## Quick start: Windows executable

1. Download `WinCodex.exe` from the [latest release](https://github.com/kimlonger/wincodex/releases/latest).
2. Open it and select a project directory.
3. Click **启动服务 + 穿透** (“Start service + tunnel”).
4. Configure your web client's remote MCP app/connector using the complete URL shown by the launcher.
5. Enable the connector in a conversation and ask the model to inspect the project.

The Windows x64 executable includes its Python execution runtime and the Cloudflare tunnel client. Basic use requires **neither a separate Python installation nor Docker**. Java/Go compilers, language servers and project dependencies are separate.

The launcher UI is currently Chinese. The repository documentation is available in English and Chinese.

### Authentication

| Mode | How to connect |
| --- | --- |
| `path` — default | Paste the complete URL, including the random credential suffix; select no additional authentication |
| `oauth` | Choose OAuth and enter the locally displayed authorization password |
| `bearer` | Use a client that supports an Authorization bearer header |
| `none` | Local loopback testing only |

The full path-mode URL is a credential. Keep it private. Client plan/account requirements and remote MCP support vary; this project does not grant access to a client feature.

## Run from source

Use **Python 3.12 or newer**. The core server uses the standard library.

```powershell
git clone https://github.com/kimlonger/wincodex.git
cd wincodex

# GUI
python -m chatcode --gui

# CLI with a public HTTPS tunnel
python -m chatcode --workspace D:/your/project

# Local read-only access without executing project commands
python -m chatcode --workspace D:/your/project --tunnel none --read-only
```

The default tunnel helper is discovered locally or downloaded on first use. A proxy can be supplied with `--proxy`; `--no-proxy` disables automatic proxy discovery. View all options with `python -m chatcode --help`.

The `chatcode` Python module, `chatcode-mcp` data directory and protocol name remain unchanged for compatibility with existing installations.

### Java and Go

Plain-text code reading works without a language server. Built-in Java/Go declaration candidates are explicitly marked as **non-semantic**.

For semantic references and cross-file rename, install the project toolchain and optional language services:

```powershell
python languages/setup_languages.py --language both
```

The helper pins gopls and JDT LS versions and verifies the JDT LS download hash. Use the launcher's advanced settings to detect and fill the resulting commands. JDT LS is a language service; using Eclipse as an editor is unnecessary. Language caches stay outside the project, and Eclipse metadata generation at the project root is disabled by default.

## Recommended task workflow

1. Call `server_info` / `list_workspaces` and check the target project.
2. Create a task with `task_create(isolation="copy")`.
3. Pass the original `workspace_id`, `workspace_token` and `task_id` to subsequent calls.
4. Read code, edit it, and run the required checks inside the copy.
5. Inspect `task_changes` and complete the task with evidence.
6. Preview `task_apply`; explicitly apply when ready. Use `task_revert` to undo an applied task if the current files still match.

A task copy keeps edits separate from the original project. It is **not itself an OS sandbox**. The bridge checks versions before editing/applying/reverting and rejects conflicting changes.

Persisted task context helps a new conversation continue the work. New model reasoning and tool calls still depend on the web client; closing the web chat does not create an autonomous reasoning loop.

## Execution permissions

The default `host` mode runs commands under the current user's account. The file-tool boundary does not restrict arbitrary commands executed in that mode.

The optional `native` mode uses **Windows AppContainer**:

- Requires an isolated task copy and fails instead of falling back to host execution.
- Grants public outbound networking through `internetClient`.
- Grants modification rights to the task copy and read/execute rights to the bundled runtime.
- Restricts process trees, memory, CPU and total execution time.
- Does not inherit the host environment or grant access to private user files.
- Follows Windows loopback/network policy; it does not add system exemptions.
- Is not a VM, does not hide Windows-public resources, and does not enforce a hard runtime disk quota.

Select `native` in the advanced settings, or use `--execution-mode native`. Source builds must prepare the embedded runtime first. Host language servers, host Git tools and desktop input are disabled in native mode.

See [SECURITY.md](SECURITY.md) for the permission and data-flow boundaries.

## Build the Windows executable

Run on Windows with Python 3.12:

```powershell
python -m pip install -r requirements-dev.txt
python scripts/prepare_build.py
python -m PyInstaller --noconfirm --distpath dist/v0.11.3 --workpath build/v0.11.3/pyinstaller WinCodex.spec
python -B tests/verify_frozen.py dist/v0.11.3/WinCodex.exe
```

The preparation helper downloads pinned official third-party binaries and checks their hashes. Generated binaries and runtime directories are excluded from Git. See [third_party/README.md](third_party/README.md) for sources and licenses.

## Tests and verification

```powershell
# Unit tests plus HTTP smoke checks; no public tunnel
python -B tests/run.py

# Explicit native-isolation integration test; requires runtime and Internet
python -B tests/verify_native_sandbox.py

# Optional real toolchain/integration checks
python -B tests/verify_java_go.py
python -B tests/verify_large_index.py
```

GitHub Actions runs the core suite on Windows. Release notes distinguish tests run on that release from evidence retained from earlier versions. Local verification covered 169 unit tests (168 passed, one platform-specific skip), 44 HTTP smoke checks, an actual connected-MCP coding workflow, and native sandbox HTTPS/file/process/resource checks before the public release preparation.

## Compatibility and current limits

- The packaged release targets **Windows x64**. Windows is the only supported operating system; Linux and macOS are outside the product scope.
- ChatGPT connectivity has been exercised through a connected MCP app. Other remote-MCP clients need their own compatibility verification.
- Transport is HTTP POST with JSON responses; this implementation does not offer SSE.
- Switching projects preserves a running endpoint. Restarting a free Quick Tunnel can change its hostname; use a configured fixed-domain tunnel for a stable address.
- Once multiple projects are registered, explicit project context is required to prevent accidental cross-project operations.
- OAuth registrations persist, but access/refresh tokens are held in memory; a service restart can require authorization again.
- Optional desktop functionality and the client's actual visual-model handling are separate from code-tool acceptance.
- Task copies, language caches and export files consume local disk. Cleanup is previewed and explicit; the storage budget is checked before copy creation/command start.

## Contributing and license

Bug reports, reproducible examples and focused pull requests are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) and the [issue templates](https://github.com/kimlonger/wincodex/issues/new/choose).

Licensed under **Apache-2.0**. Attribution and third-party information are in [LICENSE](LICENSE), [NOTICE](NOTICE) and [third_party](third_party/README.md).
