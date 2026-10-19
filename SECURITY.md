# Security and privacy

## Report a vulnerability

Use the repository's **Security → Report a vulnerability** private reporting flow. Do not put working endpoint credentials, access tokens, private source files or sensitive screenshots in public issues. If private reporting is unavailable, open a minimal issue asking for a private contact without including exploit details.

Security fixes target the latest release. Upgrade old launchers explicitly; modifying source files does not update an already-running packaged executable.

## Data flow

Your chosen MCP client sends tool requests over HTTPS through the configured tunnel to the local server. Tool results can include source snippets, command output and, when separately enabled, window images. The client/model provider and tunnel provider handle their respective portions of that traffic. This project does not make cloud processing local or promise that source never leaves the machine.

Local task copies, edit history, execution metadata, language caches and exports remain on the local computer unless you share them or expose them through tools. Task exports omit file bodies and raw command logs, but their metadata can still be private.

## Authentication

- Path mode embeds a credential in the endpoint path. Treat the complete URL as a secret.
- OAuth uses authorization codes and PKCE; bearer mode requires a compatible client header.
- Authentication does not turn the `host` execution mode into a sandbox.
- The default HTTP listener binds to loopback; a tunnel deliberately exposes authenticated access externally.

Revoke or replace exposed credentials and stop the old endpoint. Do not publish connection URLs in screenshots or issue reports.

## Execution boundaries

File tools enforce project boundaries, reject links/junction traversal and block common sensitive files by default. Host commands and host language services run with the current account's permissions and can have effects beyond file-tool boundaries.

Native mode uses Windows AppContainer and requires a service-created task copy. Public Internet access is enabled. Private user files are not granted; the host environment is not inherited. Process trees, memory, CPU and timeout limits remain active. Isolation failures return errors rather than launching on the host.

AppContainer is not a VM. Windows-public resources may be readable. Loopback follows OS policy, project dependencies are not automatically trusted, and disk budgets are not hard runtime quotas.

## Files, tasks and desktop

Version checks and guarded apply/revert reduce overwrite risks but do not lock out external editors or provide an instantaneous multi-file filesystem snapshot. Commands can change files outside the editing tool's undo history in host mode.

Task-copy cleanup permanently removes selected archived copies, including unmerged edits. Review the preview first. Task history and apply/revert receipts are retained.

Desktop controls are hidden in the current GUI. The optional backend requires explicit local configuration and window authorization. UI actions affect real applications and cannot be undone by task-file rollback. Window contents, repository instructions and command output are untrusted input; they do not grant new permissions.
