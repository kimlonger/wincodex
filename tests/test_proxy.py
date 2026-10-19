"""代理配置优先级、Windows 注册表、PAC 提示与隧道环境传递。"""

import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from chatcode.config import Settings
from chatcode.proxy import ProxyDetection, _windows_proxy_settings, detect_proxy, display_proxy
from chatcode.tunnel import start_tunnel


class ProxyCase(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def test_environment_precedes_system_and_port_scan(self):
        with patch.dict(os.environ, {"HTTPS_PROXY": "http://proxy.example:8080"}), \
                patch("chatcode.proxy._windows_proxy_settings") as registry, \
                patch("chatcode.proxy.port_is_open") as ports:
            found = detect_proxy()
        self.assertEqual(found.proxy, "http://proxy.example:8080")
        self.assertIn("HTTPS_PROXY", found.source)
        registry.assert_not_called()
        ports.assert_not_called()

    def test_system_proxy_on_custom_port_precedes_scan(self):
        with patch("chatcode.proxy._windows_proxy_settings", return_value=({"https": "127.0.0.1:7892"}, False)), \
                patch("chatcode.proxy.port_is_open") as ports:
            found = detect_proxy()
        self.assertEqual(found.proxy, "http://127.0.0.1:7892")
        self.assertEqual(found.source, "Windows 系统代理")
        ports.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows 系统代理注册表")
    def test_windows_generic_and_per_protocol_settings_and_disabled_proxy(self):
        import winreg
        configurations = [
            ({"ProxyEnable": 1, "ProxyServer": "127.0.0.1:7892"}, "http://127.0.0.1:7892"),
            ({"ProxyEnable": 1, "ProxyServer": "http=127.0.0.1:8000;https=127.0.0.1:8001"}, "http://127.0.0.1:8001"),
            ({"ProxyEnable": 1, "ProxyServer": "socks=127.0.0.1:1080"}, "socks5://127.0.0.1:1080"),
            ({"ProxyEnable": 0, "ProxyServer": "127.0.0.1:7892"}, None),
        ]
        for values, expected in configurations:
            def query(key, name):
                if name not in values:
                    raise FileNotFoundError(name)
                return values[name], 0
            with self.subTest(values=values), patch.object(winreg, "OpenKey", return_value=MagicMock()), \
                    patch.object(winreg, "QueryValueEx", side_effect=query), patch("chatcode.proxy.port_is_open", return_value=False):
                self.assertEqual(detect_proxy().proxy, expected)

    def test_pac_is_identified_without_fetching_or_executing_it(self):
        with patch("chatcode.proxy._windows_proxy_settings", return_value=({}, True)), patch("chatcode.proxy.port_is_open", return_value=False):
            found = detect_proxy()
        self.assertIsNone(found.proxy)
        self.assertEqual(found.source, "Windows PAC")
        self.assertIn("PAC", found.message)

    def test_port_fallback_includes_7892_and_reports_heuristic(self):
        with patch("chatcode.proxy._windows_proxy_settings", return_value=({}, False)), \
                patch("chatcode.proxy.port_is_open", side_effect=lambda host, port: port == 7892):
            found = detect_proxy()
        self.assertEqual(found.proxy, "http://127.0.0.1:7892")
        self.assertIn("尚未验证", found.message)

    def test_bad_environment_value_does_not_hide_system_setting(self):
        with patch.dict(os.environ, {"HTTPS_PROXY": "http://127.0.0.1:bad"}), \
                patch("chatcode.proxy._windows_proxy_settings", return_value=({"http": "[::1]:7892"}, False)):
            self.assertEqual(detect_proxy().proxy, "http://[::1]:7892")
        self.assertEqual(display_proxy("http://user:secret@[::1]:7892"), "http://***@[::1]:7892")

    def test_tunnel_receives_system_proxy_and_no_proxy_disables_detection(self):
        found = ProxyDetection("http://127.0.0.1:7892", "Windows 系统代理", "")
        with patch("chatcode.tunnel.detect_proxy", return_value=found), patch("chatcode.tunnel._start_cloudflared") as launch:
            start_tunnel(Settings(workspace=Path.cwd(), tunnel="cloudflared"), log=lambda message: None)
        self.assertEqual(launch.call_args.args[1]["HTTPS_PROXY"], found.proxy)
        with patch.dict(os.environ, {"HTTPS_PROXY": "http://127.0.0.1:7892"}), \
                patch("chatcode.tunnel.detect_proxy") as detect, patch("chatcode.tunnel._start_cloudflared") as launch:
            start_tunnel(Settings(workspace=Path.cwd(), tunnel="cloudflared", auto_proxy=False), log=lambda message: None)
        detect.assert_not_called()
        self.assertNotIn("HTTPS_PROXY", launch.call_args.args[1])

    def test_gui_uses_detected_value_and_reports_source(self):
        from chatcode.gui import App
        app = SimpleNamespace(proxy_var=MagicMock(), log=MagicMock())
        App._proxy_result(app, ProxyDetection("http://127.0.0.1:7892", "Windows 系统代理", "已读取"))
        app.proxy_var.set.assert_called_once_with("http://127.0.0.1:7892")
        self.assertIn("Windows 系统代理", app.log.call_args.args[0])
