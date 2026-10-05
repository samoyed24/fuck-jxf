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
        result = signin.send_portcloud(self.cfg, "subject", "text", "<p>html</p>")
        self.post.assert_called_once()
        self.get.assert_not_called()
        return result

    def test_accepted_without_polling(self):
        self.response(201, {"log_id": 123, "status": "queued"})
        self.assertTrue(self.send())
        self.post.assert_called_once_with(
            "https://notify.example.com/api/v1/send",
            headers={"Authorization": "Bearer test-key", "Content-Type": "application/json"},
            json={"to": "test@example.com", "subject": "subject", "text": "text", "html": "<p>html</p>"},
            timeout=15,
        )
        self.assertIn("log_id=123", self.log.call_args.args[0])
        self.assertIn("已受理", self.log.call_args.args[0])

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


if __name__ == "__main__":
    unittest.main()
