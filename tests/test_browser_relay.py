import importlib.util
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills" / "chatgpt-review" / "scripts" / "browser_relay.py"
SPEC = importlib.util.spec_from_file_location("browser_relay", SCRIPT)
browser_relay = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = browser_relay
SPEC.loader.exec_module(browser_relay)

ApprovalRequiredError = browser_relay.ApprovalRequiredError
BrowserRelay = browser_relay.BrowserRelay
BrowserRelayError = browser_relay.BrowserRelayError
DeliveryError = browser_relay.DeliveryError
TabInfo = browser_relay.TabInfo


class BrowserRelayTests(unittest.TestCase):
    def setUp(self):
        self.relay = BrowserRelay(bsk_executable="mock-bsk")

    def test_check_status_parses_running_daemon(self):
        status_json = json.dumps({
            "daemon_version": "0.3.2",
            "browsers": [
                {
                    "instance_id": "97ed2d33",
                    "browser_name": "chrome",
                }
            ],
        })
        with mock.patch.object(self.relay, "_run_bsk") as mock_bsk:
            mock_bsk.return_value = subprocess.CompletedProcess(
                args=["mock-bsk", "status", "--json"],
                returncode=0,
                stdout=status_json,
                stderr="",
            )
            st = self.relay.check_status()
            self.assertTrue(st.daemon_running)
            self.assertEqual(st.daemon_version, "0.3.2")
            self.assertEqual(st.connected_browser_id, "97ed2d33")
            self.assertEqual(st.browser_name, "chrome")

    def test_check_status_handles_failure(self):
        with mock.patch.object(self.relay, "_run_bsk") as mock_bsk:
            mock_bsk.return_value = subprocess.CompletedProcess(
                args=["mock-bsk", "status", "--json"],
                returncode=1,
                stdout="",
                stderr="connection refused",
            )
            st = self.relay.check_status()
            self.assertFalse(st.daemon_running)
            self.assertIn("connection refused", st.error)

    def test_start_session_returns_session_id(self):
        with mock.patch.object(self.relay, "_run_bsk") as mock_bsk:
            mock_bsk.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=json.dumps({"session_id": "test-sess-123"}),
                stderr="",
            )
            sess = self.relay.start_session("browser-1", "task-1")
            self.assertEqual(sess, "test-sess-123")

    def test_list_and_find_chatgpt_tab(self):
        output = """TAB         SCOPE  WIN         TITLE                       URL
1587806085  user   1587805881  ChatGPT Review Session      https://chatgpt.com/g/g-test
1587806116  user   1587805881  GitHub Repository           https://github.com/test
"""
        with mock.patch.object(self.relay, "_run_bsk") as mock_bsk:
            mock_bsk.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout=output, stderr="")
            tabs = self.relay.list_user_tabs("sess-1")
            self.assertEqual(len(tabs), 2)
            self.assertEqual(tabs[0].tab_id, "1587806085")
            self.assertEqual(tabs[0].url, "https://chatgpt.com/g/g-test")

            target = self.relay.find_chatgpt_tab("sess-1")
            self.assertIsNotNone(target)
            self.assertEqual(target.tab_id, "1587806085")

    def test_borrow_tab_raises_approval_required_on_timeout(self):
        with mock.patch.object(self.relay, "_run_bsk") as mock_bsk:
            mock_bsk.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=1,
                stdout="",
                stderr="error: timed out waiting for human confirmation",
            )
            with self.assertRaises(ApprovalRequiredError):
                self.relay.borrow_tab("sess-1", "tab-1", timeout_secs=1)

    def test_idempotency_check_skips_duplicate_send(self):
        with mock.patch.object(self.relay, "evaluate") as mock_eval:
            mock_eval.return_value = "true"
            self.assertTrue(self.relay.check_request_exists("sess-1", "REQ-2026-001"))
            res = self.relay.send_prompt("sess-1", "hello", "REQ-2026-001")
            self.assertTrue(res)
            # Evaluated once for explicit check, once inside send_prompt idempotency guard
            self.assertEqual(mock_eval.call_count, 2)

    def test_send_prompt_verifies_delivery(self):
        eval_responses = [
            "false",
            "OK",
            "CLICKED",
            "true",
        ]
        with mock.patch.object(self.relay, "evaluate", side_effect=eval_responses):
            res = self.relay.send_prompt("sess-1", "test message", "REQ-002")
            self.assertTrue(res)

    def test_run_e2e_review_always_cleans_up_on_approval_timeout(self):
        with mock.patch.object(self.relay, "check_status") as mock_status, \
             mock.patch.object(self.relay, "start_session") as mock_start, \
             mock.patch.object(self.relay, "find_chatgpt_tab") as mock_find, \
             mock.patch.object(self.relay, "borrow_tab") as mock_borrow, \
             mock.patch.object(self.relay, "return_tab") as mock_return, \
             mock.patch.object(self.relay, "stop_session") as mock_stop:

            mock_status.return_value = browser_relay.BrowserStatus(
                daemon_running=True,
                connected_browser_id="b-1",
            )
            mock_start.return_value = "sess-cleanup"
            mock_find.return_value = TabInfo("tab-123", "user", "w-1", "ChatGPT", "https://chatgpt.com")
            mock_borrow.side_effect = ApprovalRequiredError("timed out waiting for human confirmation")

            result = self.relay.run_e2e_review("task-1", "msg", "req-1")

            self.assertEqual(result["status"], "BLOCKED_WAITING_HUMAN_APPROVAL")
            self.assertTrue(result["manual_fallback_required"])
            mock_stop.assert_called_once_with("sess-cleanup")


if __name__ == "__main__":
    unittest.main()
