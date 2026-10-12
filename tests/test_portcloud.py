import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch


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
            "pc_key": "test-key",
            "pc_to": "test@example.com",
            "pc_use_preset": False,
            "pc_type": "email",
            "request_timeout": 15,
        }
        self.post = self.start_patch(signin.requests, "post")
        self.get = self.start_patch(signin.requests, "get")
        self.log = self.start_patch(signin, "log")

    def start_patch(self, target, name):
        patcher = patch.object(target, name)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def response(self, status, data):
        self.post.return_value = Mock(status_code=status, text="response")
        self.post.return_value.json.return_value = data

    def send(self):
        result = signin.send_portcloud(self.cfg, "subject", "text")
        self.post.assert_called_once()
        self.get.assert_not_called()
        return result

    def test_accepted_without_polling(self):
        self.response(201, {"log_id": 123, "status": "queued"})
        self.assertTrue(self.send())

    def test_acceptance_does_not_depend_on_delivery_status(self):
        for status in (None, "queued", "sending", "failed"):
            with self.subTest(status=status):
                self.post.reset_mock()
                self.response(201, {"log_id": 123, "status": status})
                self.assertTrue(self.send())

    def test_invalid_log_ids(self):
        for value in (None, True, False, 0, -1, "123", 1.5):
            with self.subTest(log_id=value):
                self.post.reset_mock()
                self.response(201, {"log_id": value})
                self.assertFalse(self.send())

    def test_malformed_json_objects(self):
        for data in (None, [], "queued", {"error": "invalid"}):
            with self.subTest(data=data):
                self.post.reset_mock()
                self.response(400, data)
                self.assertFalse(self.send())

    def test_http_errors_and_obsolete_success_are_not_accepted(self):
        for status in (200, 400, 401, 403, 429, 503):
            with self.subTest(status=status):
                self.post.reset_mock()
                self.response(status, {"success": True, "log_id": 123,
                                       "error": {"code": "DENIED", "message": "denied"}})
                self.assertFalse(self.send())

    def test_non_json_response(self):
        self.response(201, {})
        self.post.return_value.json.side_effect = ValueError("not JSON")
        self.assertFalse(self.send())

    def test_request_exception_not_retried(self):
        self.post.side_effect = signin.requests.Timeout("timeout")
        self.assertFalse(self.send())

    def test_main_preset_failure_exit_without_fallback(self):
        self.cfg.update(username="student", pc_enabled=True, pc_use_preset=True,
                        notify_success=True, notify_failure=True)
        with patch.object(signin, "load_config", return_value=self.cfg), \
                patch.object(signin, "login", return_value="token"), \
                patch.object(signin, "fetch_records", return_value=[]), \
                patch.object(sys, "argv", ["signin.py", "--check"]):
            for status, data, expected in (
                (400, {"error": {"code": "NO_PRESET", "message": "no preset"}}, 1),
                (201, {"log_id": 123, "preset_id": 42}, 0),
            ):
                with self.subTest(status=status):
                    self.post.reset_mock()
                    self.response(status, data)
                    self.assertEqual(signin.main(), expected)
                    self.post.assert_called_once()
            self.post.reset_mock()
            self.post.side_effect = signin.requests.Timeout("timeout")
            self.assertEqual(signin.main(), 1)
            self.post.assert_called_once()
            self.get.assert_not_called()

    def test_direct_failure_returns_one_and_disabled_notification_skips_send(self):
        self.cfg.update(username="student", pc_enabled=True,
                        notify_success=True, notify_failure=True)
        self.response(400, {"error": {"code": "DENIED", "message": "denied"}})
        self.assertEqual(signin.notify(self.cfg, signin.ST_SUCCESS, "done"), 1)
        self.post.reset_mock()
        self.cfg.update(pc_use_preset=True, notify_success=False)
        self.assertEqual(signin.notify(self.cfg, signin.ST_SUCCESS, "done"), 0)
        self.post.assert_not_called()

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
            for smtp_ok, http_status, expected in (
                (True, 201, 0), (False, 201, 1), (True, 400, 1), (False, 400, 1),
            ):
                with self.subTest(smtp_ok=smtp_ok, http_status=http_status):
                    mail.reset_mock()
                    self.post.reset_mock()
                    mail.return_value = smtp_ok
                    self.response(http_status, {"log_id": 123})
                    self.assertEqual(signin.main(), expected)
                    mail.assert_called_once()
                    self.post.assert_called_once()

    def test_no_notification_channels_is_not_a_failure(self):
        self.cfg.update(username="student", notify_success=True, notify_failure=True)
        self.assertEqual(signin.notify(self.cfg, signin.ST_SUCCESS, "done"), 0)
        self.post.assert_not_called()


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
