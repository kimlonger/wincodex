# Architecture

The model runs in an MCP-capable client. This repository implements the local tool and execution layer; it does not host a model or scrape a chat webpage.

```text
Web chat / MCP client
        │ HTTPS + authenticated MCP POST/JSON
Tunnel provider
        │ loopback HTTP
MCP HTTP server → project registry → tool validation and routing
                                   ├─ file policy, versioned edits and undo
                                   ├─ task state, isolated copies and apply/revert
                                   ├─ process manager and named checks
                                   ├─ persistent index and optional LSP sessions
                                   └─ optional AppContainer / desktop backend
```

## Main modules

| Area | Modules |
| --- | --- |
| Entrypoints and configuration | `__main__.py`, `cli.py`, `gui.py`, `config.py`, `gui_profiles.py` |
| Connection lifecycle | `runtime.py`, `tunnel.py`, `proxy.py`, `mcp_http.py`, `oauth.py` |
| Workspace and file controls | `projects.py`, `workspace.py`, `access.py`, `locking.py` |
| Editing | `editing.py`, `edit_tools.py`, `patches.py`, `files.py` |
| Coding context | `code_index.py`, `persistent_index.py`, `index_tools.py`, `lsp.py`, `language_tools.py` |
| Work and execution | `tasks.py`, `task_tools.py`, `isolation.py`, `processes.py`, `process_tools.py`, `sandbox.py` |
| Maintenance | `maintenance.py`, `language_setup.py` |
| Optional desktop | `desktop.py`, `desktop_worker.py`, `desktop_tools.py` |

The project registry owns a running connection and multiple workspace contexts. Switching the GUI's active project does not recreate the HTTP listener or tunnel. Tools carry a project ID and paired context token; task IDs continue to belong to the original project.

Task copies are stored outside the original project. Editing records file versions and recoverable transactions; task delivery checks execution state and required checks. Applying a copy is a separate operation with a versioned diff preview.

Commands return quickly or receive a process ID for output polling. Operation IDs prevent duplicate launches after uncertain network retries. Native commands are created suspended, attached to a constrained Windows Job, then resumed under a new AppContainer identity with `internetClient` capability.

Python navigation uses AST. Java/Go fallback navigation produces labeled declaration candidates. Configured LSP servers provide semantic operations; host LSP processes are disabled in native mode.

Persistent task/index state helps recovery. It cannot generate another model turn when the client is closed. Optional desktop ownership and local stop controls are separate from task-copy isolation.
