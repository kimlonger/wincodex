import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from chatcode.config import Settings, build_arg_parser
from chatcode.gui_profiles import load_profile, save_profile


class ProfileCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="chatcode-profile-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        env = patch.dict(os.environ, {"CHATCODE_DATA_DIR": str(self.root / "state")})
        env.start()
        self.addCleanup(env.stop)

    def test_last_workspace_and_settings_are_restored(self):
        workspace = self.root / "project"
        workspace.mkdir()
        save_profile(Settings(workspace=workspace, port=9999, tunnel="none", auth_mode="oauth", allow_exec=False))
        args, recents = load_profile(build_arg_parser().parse_args([]))
        self.assertEqual(args.workspace, str(workspace))
        self.assertEqual(args.port, 9999)
        self.assertTrue(args.no_exec)
        self.assertIn(str(workspace), recents)

    def test_equivalent_paths_share_profiles_and_legacy_aliases_still_load(self):
        workspace = self.root / "project"
        workspace.mkdir()
        alias = workspace / ".." / "project"
        save_profile(Settings(workspace=alias, port=9876))
        path = self.root / "state" / "gui_profiles.json"
        saved = json.loads(path.read_text(encoding="utf-8"))
        canonical = str(workspace.resolve())
        self.assertIn(canonical, saved["profiles"])
        # Simulate the non-canonical profile key written by older releases.
        saved["profiles"][str(alias)] = saved["profiles"].pop(canonical)
        path.write_text(json.dumps(saved), encoding="utf-8")
        args, _ = load_profile(build_arg_parser().parse_args(["--workspace", canonical]))
        self.assertEqual(args.port, 9876)

    def test_explicit_cli_default_value_also_overrides_saved_profile(self):
        workspace = self.root / "project"
        workspace.mkdir()
        save_profile(Settings(workspace=workspace, port=9999, auth_mode="oauth"))
        for tokens in (["--port", "8765", "--auth-mode", "path"], ["-p8765", "--auth-mode=path"]):
            args, _ = load_profile(build_arg_parser().parse_args(tokens))
            self.assertEqual(args.port, 8765)
            self.assertEqual(args.auth_mode, "path")

    def test_tokens_and_credentialed_proxy_not_written(self):
        settings = Settings(workspace=self.root, proxy="http://user:secret@127.0.0.1:7890", frp_token="frpsecret", cpolar_authtoken="cpolarsecret")
        save_profile(settings)
        content = (self.root / "state/gui_profiles.json").read_text(encoding="utf-8")
        for secret in ("frpsecret", "cpolarsecret", "user:secret", settings.path_token, settings.bearer_token, settings.oauth_password):
            self.assertNotIn(secret, content)

    def test_bad_saved_values_are_ignored(self):
        directory = self.root / "state"
        directory.mkdir()
        data = {"last_workspace": str(self.root), "profiles": {str(self.root): {"port": -10, "auth_mode": "bad", "tunnel": 4}}}
        (directory / "gui_profiles.json").write_text(json.dumps(data))
        args, _ = load_profile(build_arg_parser().parse_args([]))
        self.assertEqual(args.port, 8765)
        self.assertEqual(args.auth_mode, "path")


if __name__ == "__main__":
    unittest.main()
