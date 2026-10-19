# Contributing

Thanks for helping improve ChatCodex.

## Development

Use Windows and Python 3.12+ for the tested development path. The core server and tests use the standard library. Build dependencies are in `requirements-dev.txt`; desktop dependencies are separately pinned in `desktop/requirements.lock`.

```powershell
git clone https://github.com/kimlonger/chatcodex-mcp.git
cd chatcodex-mcp
python -B tests/run.py
```

Run real sandbox, public-tunnel, language-toolchain or desktop acceptance scripts explicitly. Some start processes, access the Internet or create their own test windows. Do not run them against unrelated user applications or production data.

## Changes

- Explain the concrete problem, resulting behavior and how it was verified.
- Preserve project identity, version checks, operation deduplication and task ownership boundaries.
- Keep network credentials and real project contents out of fixtures, screenshots and logs committed to Git.
- Do not silently fall back from native isolation to host execution.
- Label lexical code matches separately from semantic language-service results.
- Add focused regression checks for behavior changes. Documentation-only edits do not need artificial tests.
- Keep the legacy `chatcode` module, `chatcode-mcp` protocol identity and data directory compatible unless a migration is explicitly designed.

## Pull requests

Run `python -B tests/run.py` before submitting code changes. Include any necessary integration results and disclose checks that were not run. A passing mock or protocol probe is not evidence that every web client or visual model behaves the same way.

Use the issue templates for bugs and feature requests. For vulnerabilities, follow [SECURITY.md](SECURITY.md) instead of opening a public issue with exploit data or secrets.

Contributions are submitted under the repository's Apache-2.0 license. Preserve existing attribution and note new third-party dependencies and their licenses.
