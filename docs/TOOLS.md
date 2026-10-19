# MCP tool reference

The standard mode exposes 50 tools. Read-only mode filters the list and denies modification/execution paths. Use each tool's advertised schema as the argument reference.

| Group | Tools |
| --- | --- |
| Projects | `server_info`, `list_workspaces` |
| Reading and discovery | `list_dir`, `read_file`, `read_files`, `find_files`, `search_text`, `project_overview`, `batch_read` |
| Code and context | `code_outline`, `read_code`, `find_symbol`, `find_references`, `rename_symbol`, `refresh_index`, `index_status`, `context_pack` |
| Editing and history | `write_file`, `replace_in_file`, `apply_patch`, `edit_files`, `get_changes`, `undo_changes` |
| Execution | `exec_command`, `read_process`, `stop_process`, `run_checks` |
| Git inspection | `git_status`, `git_diff`, `git_log` |
| Tasks | `task_create`, `task_list`, `task_get`, `task_update`, `task_context`, `task_finish`, `task_cancel`, `task_changes`, `task_apply`, `task_revert`, `task_archive`, `task_export`, `task_cleanup` |
| Health and capacity | `language_status`, `sandbox_status`, `storage_status` |
| Optional desktop | `desktop_status`, `desktop_observe`, `desktop_action`, `desktop_release` |

## Calling conventions

- Read `server_info` / `list_workspaces` first. Use the returned `workspace_id` and `workspace_token`; after multiple projects are registered, omitted context is rejected.
- Carry the original project context and `task_id` when working in a task copy. Do not register the copy as another project.
- Existing-file writes need the current revision. Read again after `REVISION_MISMATCH` rather than overwriting changes.
- Reuse an operation ID only when retrying the same intended operation. Do not automatically repeat commands or UI actions with an unknown outcome.
- Follow `next_offset` and output cursors; a clipped result is not a complete scan.
- `run_checks` accepts explicit commands/argv or named `[checks]` from `.chatcode.toml`.
- `task_finish` completes work in a copy but does not merge it into the original project.
- Apply, revert and cleanup provide previews. Actual execution uses the current revision/plan plus an explicit non-dry-run request.

Native mode requires copy tasks and disables host Git, host LSP and desktop input. Desktop tools are optional and their setup controls are hidden in the current GUI.
