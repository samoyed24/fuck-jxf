import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

from portcloud_notify import (
    APIError,
    AcceptanceUnknownError,
    ConfigurationError,
    PresetSendAccepted,
    SendAccepted,
    TransportError,
)


# Isolate unrelated OCR and .env loading from notification tests.
with patch.dict(sys.modules, {"ddddocr": Mock(), "dotenv": Mock()}):
    spec = importlib.util.spec_from_file_location(
        "signin_under_test", Path(__file__).resolve().parents[1] / "signin.py"
    )
    signin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(signin)


class PortcloudTests(unittest.TestCase):
    def setUp(self):
        self.cfg = {
            "pc_url": "https://notify.example.com",
            "pc_key": "pck_test",
            "pc_to": "test@example.com",
            "pc_use_preset": False,
            "pc_type": "email",
            "request_timeout": 15,
        }
        self.client = Mock()
        self.client_factory = self.start_patch(signin, "Client")
        self.client_factory.return_value = self.client
        self.log = self.start_patch(signin, "log")

    def start_patch(self, target, name):
        patcher = patch.object(target, name)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def send(self):
        result = signin.send_portcloud(self.cfg, "subject", "text")
        self.client_factory.assert_called_once_with(
            api_key="pck_test",
            base_url="https://notify.example.com",
            timeout=15,
        )
        return result

    def test_preset_send_uses_the_preset_chain(self):
        self.cfg["pc_use_preset"] = True
        self.client.send_preset.return_value = PresetSendAccepted(
            log_id=123, preset_id=42)

        self.assertTrue(self.send())
        self.client.send_preset.assert_called_once_with("subject", "text")
        self.client.send.assert_not_called()

    def test_direct_send_uses_type_and_target(self):
        self.client.send.return_value = SendAccepted(log_id=7)

        self.assertTrue(self.send())
        self.client.send.assert_called_once_with(
            "email", "subject", body="text", to="test@example.com")
        self.client.send_preset.assert_not_called()

    def test_ntfy_direct_send_passes_the_topic(self):
        self.cfg.update(pc_type="ntfy", pc_to="a1b2c3-alerts")
        self.client.send.return_value = SendAccepted(log_id=7)

        self.assertTrue(self.send())
        self.client.send.assert_called_once_with(
            "ntfy", "subject", body="text", to="a1b2c3-alerts")

    def test_rejection_is_reported_with_its_code(self):
        self.client.send.side_effect = APIError(400, "NO_PRESET", "no preset")

        self.assertFalse(self.send())
        self.assertIn("NO_PRESET", str(self.log.call_args))

    def test_unknown_outcome_is_not_retried(self):
        self.cfg["pc_use_preset"] = True
        self.client.send_preset.side_effect = AcceptanceUnknownError(
            "unknown", http_status=503, code="DEPENDENCY_UNAVAILABLE")

        self.assertFalse(self.send())
        self.client.send_preset.assert_called_once()
        self.client.wait.assert_not_called()
        self.client.get_status.assert_not_called()

    def test_transport_error_is_not_retried(self):
        self.client.send.side_effect = TransportError("connection reset")

        self.assertFalse(self.send())
        self.client.send.assert_called_once()

    def test_configuration_error_is_reported_without_a_request(self):
        self.client_factory.side_effect = ConfigurationError("bad base url")

        self.assertFalse(self.send())
        self.client.send.assert_not_called()
        self.client.send_preset.assert_not_called()

    def test_main_preset_failure_exit_without_fallback(self):
        self.cfg.update(username="student", pc_enabled=True, pc_use_preset=True,
                        notify_success=True, notify_failure=True)
        self.client.send_preset.side_effect = [
            APIError(400, "NO_PRESET", "no preset"),
            PresetSendAccepted(log_id=123, preset_id=42),
            AcceptanceUnknownError("unknown", http_status=503),
        ]
        with patch.object(signin, "load_config", return_value=self.cfg), \
                patch.object(signin, "login", return_value="token"), \
                patch.object(signin, "fetch_records", return_value=[]), \
                patch.object(sys, "argv", ["signin.py", "--check"]):
            for expected in (1, 0, 1):
                self.assertEqual(signin.main(), expected)
            self.assertEqual(self.client.send_preset.call_count, 3)
            self.client.wait.assert_not_called()
            self.client.get_status.assert_not_called()

    def test_direct_failure_returns_one_and_disabled_notification_skips_send(self):
        self.cfg.update(username="student", pc_enabled=True,
                        notify_success=True, notify_failure=True)
        self.client.send.side_effect = APIError(400, "DENIED", "denied")
        self.assertEqual(signin.notify(self.cfg, signin.ST_SUCCESS, "done"), 1)
        self.client.send.reset_mock()
        self.cfg.update(pc_use_preset=True, notify_success=False)
        self.assertEqual(signin.notify(self.cfg, signin.ST_SUCCESS, "done"), 0)
        self.client.send.assert_not_called()

    def test_successful_signin_exits_one_on_any_notification_failure(self):
        self.cfg.update(username="student", pc_enabled=True, mail_enabled=True,
                        notify_success=True, notify_failure=True)
        with patch.object(signin, "load_config", return_value=self.cfg), \
                patch.object(signin, "login", return_value="token"), \
                patch.object(signin, "fetch_records", return_value=[]), \
                patch.object(signin, "resolve_coords", return_value=(1.0, 2.0)), \
                patch.object(signin, "do_signin", return_value=True), \
                patch.object(signin, "send_mail") as mail, \
                patch.object(sys, "argv", ["signin.py", "--force"]):
            for smtp_ok, notify_ok, expected in (
                (True, True, 0), (False, True, 1), (True, False, 1), (False, False, 1),
            ):
                with self.subTest(smtp_ok=smtp_ok, notify_ok=notify_ok):
                    mail.reset_mock()
                    self.client.send.reset_mock()
                    mail.return_value = smtp_ok
                    self.client.send.return_value = SendAccepted(log_id=123)
                    self.client.send.side_effect = (
                        None if notify_ok else APIError(400, "DENIED", "denied")
                    )
                    self.assertEqual(signin.main(), expected)
                    mail.assert_called_once()
                    self.client.send.assert_called_once()

    def test_no_notification_channels_is_not_a_failure(self):
        self.cfg.update(username="student", notify_success=True, notify_failure=True)
        self.assertEqual(signin.notify(self.cfg, signin.ST_SUCCESS, "done"), 0)
        self.client.send.assert_not_called()
        self.client.send_preset.assert_not_called()


class PortcloudConfigTests(unittest.TestCase):
    def load(self, **extra):
        environment = {"JXF_USERNAME": "student", "JXF_PASSWORD": "password",
                       "JXF_PC_KEY": "test-key"}
        environment.update(extra)
        with patch.dict(signin.os.environ, environment, clear=True):
            return signin.load_config()

    def test_preset_ignores_invalid_type_and_missing_target(self):
        cfg = self.load(JXF_PC_USE_PRESET="true", JXF_PC_TYPE="invalid")
        self.assertTrue(cfg["pc_enabled"])
        self.assertTrue(cfg["pc_use_preset"])

    def test_direct_configuration_requires_target_and_valid_type(self):
        for extra in ({}, {"JXF_PC_TO": "topic", "JXF_PC_TYPE": "invalid"},
                      {"JXF_PC_USE_PRESET": "invalid"}):
            with self.subTest(extra=extra), self.assertRaises(SystemExit):
                self.load(**extra)

    def test_preset_requires_key(self):
        with self.assertRaises(SystemExit):
            self.load(JXF_PC_USE_PRESET="true", JXF_PC_KEY="")


if __name__ == "__main__":
    unittest.main()
