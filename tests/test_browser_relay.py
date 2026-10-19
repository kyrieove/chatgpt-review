import hashlib
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
ConversationMismatchError = browser_relay.ConversationMismatchError
DeliveryError = browser_relay.DeliveryError
NotSentError = browser_relay.NotSentError
TabInfo = browser_relay.TabInfo
TabNotFoundError = browser_relay.TabNotFoundError
build_check_user_turn_js = browser_relay.build_check_user_turn_js
build_extract_js = browser_relay.build_extract_js
build_check_model_and_thinking_js = browser_relay.build_check_model_and_thinking_js
validate_chatgpt_origin = browser_relay.validate_chatgpt_origin
validate_conversation_url = browser_relay.validate_conversation_url


class BrowserRelayTests(unittest.TestCase):
    def setUp(self):
        self.relay = BrowserRelay(bsk_executable="mock-bsk")
        self.dom_fixture = ROOT / "tests" / "dom_fixture.js"

    def run_in_dom(self, dom_def, js_code):
        """Helper executing real JavaScript in Node.js against a simulated DOM tree."""
        payload = json.dumps({"dom": dom_def, "js": js_code})
        proc = subprocess.run(
            ["node", str(self.dom_fixture)],
            input=payload,
            text=True,
            capture_output=True,
            check=True,
        )
        out = proc.stdout.strip()
        try:
            return json.loads(out)
        except Exception:
            return out

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

    def test_find_target_tab_with_explicit_url_and_id(self):
        output = """TAB         SCOPE  WIN         TITLE                       URL
1587806085  user   1587805881  ChatGPT Review Session      https://chatgpt.com/c/target-123
1587806116  user   1587805881  Other ChatGPT Session       https://chatgpt.com/c/other-456
"""
        with mock.patch.object(self.relay, "_run_bsk") as mock_bsk:
            mock_bsk.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout=output, stderr="")
            tab = self.relay.find_target_tab("sess-1", target_url_prefix="https://chatgpt.com/c/target-123")
            self.assertEqual(tab.tab_id, "1587806085")

            tab_by_id = self.relay.find_target_tab("sess-1", target_tab_id="1587806116")
            self.assertEqual(tab_by_id.tab_id, "1587806116")

    def test_find_target_tab_rejects_wrong_conversation_or_ambiguous(self):
        output = """TAB         SCOPE  WIN         TITLE                       URL
1587806085  user   1587805881  ChatGPT Review Session      https://chatgpt.com/c/target-123
1587806116  user   1587805881  Other ChatGPT Session       https://chatgpt.com/c/other-456
"""
        with mock.patch.object(self.relay, "_run_bsk") as mock_bsk:
            mock_bsk.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout=output, stderr="")
            with self.assertRaises(ConversationMismatchError):
                self.relay.find_target_tab("sess-1")

            with self.assertRaises(TabNotFoundError):
                self.relay.find_target_tab("sess-1", target_tab_id="9999999999")

            with self.assertRaises(ConversationMismatchError):
                self.relay.find_target_tab(
                    "sess-1", target_tab_id="1587806085", target_url_prefix="https://chatgpt.com/c/mismatched"
                )

    # --- F-010 Origin & Navigation Tests ---

    def test_validate_chatgpt_origin_rules(self):
        self.assertTrue(validate_chatgpt_origin("https://chatgpt.com"))
        self.assertTrue(validate_chatgpt_origin("https://chatgpt.com/c/target-123"))
        self.assertTrue(validate_chatgpt_origin("https://www.chatgpt.com/g/g-model"))
        # Non-HTTPS
        self.assertFalse(validate_chatgpt_origin("http://chatgpt.com/c/target-123"))
        # Malicious subdomains / typosquats
        self.assertFalse(validate_chatgpt_origin("https://chatgpt.com.evil.com/c/target-123"))
        self.assertFalse(validate_chatgpt_origin("https://fakechatgpt.com/c/123"))
        self.assertFalse(validate_chatgpt_origin("https://chatgpt.com.attacker.org"))
        # Non-ChatGPT domains
        self.assertFalse(validate_chatgpt_origin("https://example.com"))
        self.assertFalse(validate_chatgpt_origin("https://github.com"))
        # Invalid port
        self.assertFalse(validate_chatgpt_origin("https://chatgpt.com:8443"))

    def test_find_target_tab_rejects_non_chatgpt_and_typosquat_origins(self):
        output = """TAB         SCOPE  WIN         TITLE                       URL
1001        user   1587805881  Example Site                https://example.com/page
1002        user   1587805881  Evil Phishing Site          https://chatgpt.com.evil.com/login
1003        user   1587805881  Insecure HTTP ChatGPT       http://chatgpt.com/c/test
"""
        with mock.patch.object(self.relay, "_run_bsk") as mock_bsk:
            mock_bsk.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout=output, stderr="")
            # Specifying non-ChatGPT or typosquat tab ID must raise ConversationMismatchError
            with self.assertRaises(ConversationMismatchError):
                self.relay.find_target_tab("sess-1", target_tab_id="1001")
            with self.assertRaises(ConversationMismatchError):
                self.relay.find_target_tab("sess-1", target_tab_id="1002")
            with self.assertRaises(ConversationMismatchError):
                self.relay.find_target_tab("sess-1", target_tab_id="1003")

            # Specifying malicious target URL prefix raises ValueError
            with self.assertRaises(ValueError):
                self.relay.find_target_tab("sess-1", target_url_prefix="https://chatgpt.com.evil.com")

    def test_run_e2e_review_detects_post_borrow_navigation_and_cleans_up(self):
        with mock.patch.object(self.relay, "check_status") as mock_status, \
             mock.patch.object(self.relay, "start_session") as mock_start, \
             mock.patch.object(self.relay, "find_target_tab") as mock_find, \
             mock.patch.object(self.relay, "borrow_tab") as mock_borrow, \
             mock.patch.object(self.relay, "get_current_url") as mock_url, \
             mock.patch.object(self.relay, "return_tab") as mock_return, \
             mock.patch.object(self.relay, "stop_session") as mock_stop:

            mock_status.return_value = browser_relay.BrowserStatus(
                daemon_running=True, connected_browser_id="b-1"
            )
            mock_start.return_value = "sess-nav"
            mock_find.return_value = TabInfo("tab-1", "user", "w-1", "ChatGPT", "https://chatgpt.com/c/123")
            mock_borrow.return_value = True
            # Simulate navigation/redirect to non-ChatGPT malicious origin after borrow
            mock_url.return_value = "https://evil-redirect.com/login"

            result = self.relay.run_e2e_review(
                "task-nav", "test [REQ-NAV]", "REQ-NAV", target_url_prefix="https://chatgpt.com/c/123"
            )

            self.assertEqual(result["status"], "BLOCKED_CONVERSATION_MISMATCH")
            self.assertIn("Tab navigation detected", result["error"])
            # Tab return and session stop must be called in finally
            mock_return.assert_called_once_with("sess-nav", "tab-1")
            mock_stop.assert_called_once_with("sess-nav")

    # --- F-007 / F-008 Real DOM JavaScript Execution Tests via Node.js ---

    def test_dom_execution_scenario_1_assistant_echoes_request_id(self):
        """Scenario 1: Assistant reply echoes request_id.

        Must NOT mistake assistant turn for user turn, and must extract full reply without truncation.
        """
        req_id = "REQ-ECHO-20261015"
        full_reply = f"已收到审查请求 {req_id}。经过细致复查：\n1. F-001 修复有效\n2. 建议 Ready。"
        dom = {
            "tag": "main",
            "children": [
                {
                    "tag": "article",
                    "attrs": {"data-message-author-role": "user"},
                    "text": f"请审查提交 [{req_id}] 并给出反馈。",
                },
                {
                    "tag": "article",
                    "attrs": {"data-message-author-role": "assistant"},
                    "children": [
                        {
                            "tag": "div",
                            "attrs": {"class": "prose markdown"},
                            "text": full_reply,
                        }
                    ],
                },
            ],
        }

        # Check user turn
        js_check = build_check_user_turn_js(req_id)
        res_check = self.run_in_dom(dom, js_check)
        self.assertTrue(res_check["exists"])
        self.assertEqual(res_check["status"], "OK")

        # Extract assistant turn
        js_extract = build_extract_js(req_id)
        res_extract = self.run_in_dom(dom, js_extract)
        self.assertEqual(res_extract["status"], "OK")
        self.assertFalse(res_extract["isGenerating"])
        self.assertEqual(res_extract["text"], full_reply)
        self.assertEqual(res_extract["textLength"], len(full_reply))

    def test_dom_execution_scenario_2_multiple_rounds_targets_correct_turn(self):
        """Scenario 2: Multiple conversation rounds.

        Must locate the exact user turn and extract its immediate adjacent assistant turn only.
        """
        req_target = "REQ-TARGET-ROUND-2"
        dom = {
            "tag": "main",
            "children": [
                # Round 1
                {"tag": "article", "attrs": {"data-message-author-role": "user"}, "text": "第1轮提示词 [REQ-ROUND-1]"},
                {"tag": "article", "attrs": {"data-message-author-role": "assistant"}, "children": [{"tag": "div", "attrs": {"class": "prose"}, "text": "第1轮反馈正文"}]},
                # Round 2 (Target)
                {"tag": "article", "attrs": {"data-message-author-role": "user"}, "text": f"第2轮复查提示词 [{req_target}]"},
                {"tag": "article", "attrs": {"data-message-author-role": "assistant"}, "children": [{"tag": "div", "attrs": {"class": "prose"}, "text": "第2轮专属审查结果：全量通过"}]},
                # Round 3
                {"tag": "article", "attrs": {"data-message-author-role": "user"}, "text": "第3轮新提示词 [REQ-ROUND-3]"},
                {"tag": "article", "attrs": {"data-message-author-role": "assistant"}, "children": [{"tag": "div", "attrs": {"class": "prose"}, "text": "第3轮反馈正文"}]},
            ],
        }

        js_extract = build_extract_js(req_target)
        res = self.run_in_dom(dom, js_extract)
        self.assertEqual(res["status"], "OK")
        self.assertEqual(res["text"], "第2轮专属审查结果：全量通过")

    def test_dom_execution_scenario_3_only_assistant_quotes_request_id(self):
        """Scenario 3: Only assistant turn quotes request_id (user turn does not have it).

        Must return USER_TURN_NOT_FOUND and NOT treat assistant turn as user turn.
        """
        req_ghost = "REQ-GHOST-999"
        dom = {
            "tag": "main",
            "children": [
                {"tag": "article", "attrs": {"data-message-author-role": "user"}, "text": "普通用户消息，没有token"},
                {"tag": "article", "attrs": {"data-message-author-role": "assistant"}, "children": [{"tag": "div", "attrs": {"class": "prose"}, "text": f"我在回复中提到了一个历史ID {req_ghost}"}]},
            ],
        }

        # User turn check must be false
        js_check = build_check_user_turn_js(req_ghost)
        res_check = self.run_in_dom(dom, js_check)
        self.assertFalse(res_check["exists"])
        self.assertEqual(res_check["status"], "USER_TURN_NOT_FOUND")

        # Extract response must report USER_TURN_NOT_FOUND
        js_extract = build_extract_js(req_ghost)
        res_extract = self.run_in_dom(dom, js_extract)
        self.assertEqual(res_extract["status"], "USER_TURN_NOT_FOUND")
        self.assertEqual(res_extract["text"], "")

    def test_dom_execution_scenario_4_no_reliable_node_structure_returns_blocked(self):
        """Scenario 4: No reliable DOM node structure (raw text / unsupported layout).

        Must return BLOCKED_DOM_NOT_SUPPORTED, never guess or slice strings.
        """
        req_test = "REQ-UNSTRUCTURED-001"
        dom = {
            "tag": "main",
            "children": [
                # Unstructured divs without article or role attributes
                {"tag": "div", "text": f"Raw text with {req_test} and some following answer text"},
            ],
        }

        js_check = build_check_user_turn_js(req_test)
        res_check = self.run_in_dom(dom, js_check)
        self.assertFalse(res_check["exists"])
        self.assertEqual(res_check["status"], "BLOCKED_DOM_NOT_SUPPORTED")

        js_extract = build_extract_js(req_test)
        res_extract = self.run_in_dom(dom, js_extract)
        self.assertEqual(res_extract["status"], "BLOCKED_DOM_NOT_SUPPORTED")
        self.assertEqual(res_extract["text"], "")

    def test_dom_execution_strips_button_bars_and_action_controls(self):
        """Verifies that copy buttons, edit buttons, and SVG icons are cleanly removed."""
        req_id = "REQ-CLEAN-TEXT"
        dom = {
            "tag": "main",
            "children": [
                {"tag": "article", "attrs": {"data-message-author-role": "user"}, "text": f"Message [{req_id}]"},
                {
                    "tag": "article",
                    "attrs": {"data-message-author-role": "assistant"},
                    "children": [
                        {
                            "tag": "div",
                            "attrs": {"class": "prose"},
                            "children": [
                                {"tag": "p", "text": "纯净的审查反馈正文。"},
                                {"tag": "button", "attrs": {"aria-label": "Copy"}, "text": "复制"},
                                {"tag": "button", "attrs": {"data-testid": "feedback-button"}, "text": "反馈"},
                            ],
                        }
                    ],
                },
            ],
        }

        js_extract = build_extract_js(req_id)
        res = self.run_in_dom(dom, js_extract)
        self.assertEqual(res["status"], "OK")
        self.assertEqual(res["text"], "纯净的审查反馈正文。")

    # --- General Relay Mechanism Tests ---

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

    def test_send_prompt_rejects_message_without_embedded_request_id(self):
        with self.assertRaises(ValueError):
            self.relay.send_prompt("sess-1", "message without token", "REQ-MISSING-123")

    def test_old_request_id_skips_duplicate_send_on_matching_hash(self):
        msg = "msg with [REQ-EXISTING-001]"
        with mock.patch.object(self.relay, "evaluate") as mock_eval:
            mock_eval.return_value = json.dumps({
                "exists": True,
                "status": "OK",
                "userText": msg,
            })
            res = self.relay.send_prompt("sess-1", msg, "REQ-EXISTING-001")
            self.assertTrue(res)
            self.assertEqual(mock_eval.call_count, 1)

    def test_send_prompt_raises_delivery_error_on_idempotency_content_conflict(self):
        """If user turn with request_id exists but content is different, raise DeliveryError."""
        with mock.patch.object(self.relay, "evaluate") as mock_eval:
            mock_eval.return_value = json.dumps({
                "exists": True,
                "status": "OK",
                "userText": "Original prompt content [REQ-CONFLICT-001]",
            })
            with self.assertRaises(DeliveryError) as ctx:
                self.relay.send_prompt(
                    "sess-1",
                    "Completely different prompt [REQ-CONFLICT-001]",
                    "REQ-CONFLICT-001",
                )
            self.assertIn("Idempotency conflict", str(ctx.exception))

    def test_user_message_delivery_unconfirmed_raises_delivery_error(self):
        eval_responses = [
            json.dumps({"exists": False, "status": "USER_TURN_NOT_FOUND"}),  # check existence before send
            "OK",                                                            # prompt injection
            "CLICKED",                                                       # click send button
        ] + [json.dumps({"exists": False, "status": "USER_TURN_NOT_FOUND"})] * 15  # verification checks fail
        with mock.patch.object(self.relay, "evaluate", side_effect=eval_responses):
            with self.assertRaises(DeliveryError):
                self.relay.send_prompt("sess-1", "msg [REQ-UNCONFIRMED]", "REQ-UNCONFIRMED", delivery_timeout=0.02)

    def test_normal_complete_reply_extracted_accurately(self):
        complete_reply = "完整的第二轮审查反馈正文\n1. F-001 确认修复\n2. 建议 Ready"
        complete_json = json.dumps({"status": "OK", "isGenerating": False, "textLength": len(complete_reply), "text": complete_reply})
        with mock.patch.object(self.relay, "evaluate", return_value=complete_json):
            res = self.relay.wait_for_response("sess-1", "REQ-NORMAL", timeout_secs=10, poll_interval=0.01)
            self.assertEqual(res["status"], "COMPLETE")
            self.assertEqual(res["text"], complete_reply)
            self.assertEqual(res["length"], len(complete_reply))

    def test_streaming_reply_not_finished_reaches_timeout(self):
        streaming_response = json.dumps({
            "status": "GENERATING",
            "isGenerating": True,
            "textLength": 50,
            "text": "正在流式生成中...",
        })
        with mock.patch.object(self.relay, "evaluate", return_value=streaming_response):
            res = self.relay.wait_for_response("sess-1", "REQ-STREAMING", timeout_secs=0.05, poll_interval=0.01)
            self.assertEqual(res["status"], "TIMEOUT")
            self.assertTrue(res["is_generating"])

    def test_assistant_turn_not_found_returns_timeout_with_diagnostic(self):
        not_found_response = json.dumps({
            "status": "USER_TURN_NOT_FOUND",
            "isGenerating": False,
            "text": "",
        })
        with mock.patch.object(self.relay, "evaluate", return_value=not_found_response):
            res = self.relay.wait_for_response("sess-1", "REQ-NOTFOUND", timeout_secs=0.05, poll_interval=0.01)
            self.assertEqual(res["status"], "TIMEOUT")
            self.assertEqual(res["text"], "")

    def test_wait_for_response_immediately_returns_blocked_on_dom_not_supported(self):
        blocked_response = json.dumps({
            "status": "BLOCKED_DOM_NOT_SUPPORTED",
            "isGenerating": False,
            "text": "",
            "reason": "No recognizable turn nodes in DOM",
        })
        with mock.patch.object(self.relay, "evaluate", return_value=blocked_response):
            res = self.relay.wait_for_response("sess-1", "REQ-BLOCKED", timeout_secs=10, poll_interval=0.01)
            self.assertEqual(res["status"], "BLOCKED")
            self.assertEqual(res["reason"], "BLOCKED_DOM_NOT_SUPPORTED")

    def test_run_e2e_review_always_cleans_up_on_approval_timeout(self):
        with mock.patch.object(self.relay, "check_status") as mock_status, \
             mock.patch.object(self.relay, "start_session") as mock_start, \
             mock.patch.object(self.relay, "find_target_tab") as mock_find, \
             mock.patch.object(self.relay, "borrow_tab") as mock_borrow, \
             mock.patch.object(self.relay, "return_tab") as mock_return, \
             mock.patch.object(self.relay, "stop_session") as mock_stop:

            mock_status.return_value = browser_relay.BrowserStatus(
                daemon_running=True,
                connected_browser_id="b-1",
            )
            mock_start.return_value = "sess-cleanup"
            mock_find.return_value = TabInfo("tab-123", "user", "w-1", "ChatGPT", "https://chatgpt.com/c/123")
            mock_borrow.side_effect = ApprovalRequiredError("timed out waiting for human confirmation")

            result = self.relay.run_e2e_review("task-1", "msg [REQ-1]", "REQ-1")

            self.assertEqual(result["status"], "BLOCKED_WAITING_HUMAN_APPROVAL")
            self.assertTrue(result["manual_fallback_required"])
            mock_stop.assert_called_once_with("sess-cleanup")

    # --- r4 Regression Tests (must fail on r3 code) ---

    def test_send_prompt_fails_closed_on_pre_send_parse_error(self):
        """F-008: Pre-send DOM check exception/error must fail-closed."""
        with mock.patch.object(self.relay, "check_user_turn_exists") as mock_check:
            mock_check.return_value = {"exists": False, "status": "EVALUATE_PARSE_ERROR", "raw": "syntax error"}
            with self.assertRaises(DeliveryError) as ctx:
                self.relay.send_prompt("sess-1", "msg [REQ-FAIL-CLOSED]", "REQ-FAIL-CLOSED")
            self.assertIn("failed closed", str(ctx.exception).lower())

    def test_send_prompt_verifies_user_turn_hash_after_send(self):
        """F-008: After send, must verify user turn content hash, not just exists."""
        with mock.patch.object(self.relay, "check_user_turn_exists") as mock_check, \
             mock.patch.object(self.relay, "evaluate") as mock_eval:
            mock_check.side_effect = [
                {"exists": False, "status": "USER_TURN_NOT_FOUND"},
                {"exists": True, "status": "OK", "userText": "Wrong content [REQ-HASH-MISMATCH]", "hash_matches": False},
            ]
            mock_eval.side_effect = ["OK", "CLICKED"]
            with self.assertRaises(DeliveryError) as ctx:
                self.relay.send_prompt("sess-1", "Expected message [REQ-HASH-MISMATCH]", "REQ-HASH-MISMATCH", delivery_timeout=0.01)
            self.assertIn("hash", str(ctx.exception).lower())

    def test_check_user_turn_exists_matches_with_whitespace_normalization(self):
        """F-008: Rendered DOM text with paragraph newlines matches raw message via normalization."""
        raw_msg = "# Title [REQ-NORM-1]\n\n- Item 1: `foo`\n- Item 2: `bar`"
        dom_rendered_text = "# Title [REQ-NORM-1]\n\n\n\n\n- Item 1: `foo`\n\n- Item 2: `bar`\n"
        with mock.patch.object(self.relay, "evaluate") as mock_eval:
            mock_eval.return_value = json.dumps({"exists": True, "status": "OK", "userText": dom_rendered_text})
            res = self.relay.check_user_turn_exists(
                "sess-1", "REQ-NORM-1",
                expected_message_hash=hashlib.sha256(raw_msg.encode("utf-8")).hexdigest(),
                expected_message=raw_msg,
            )
            self.assertTrue(res["exists"])
            self.assertTrue(res["hash_matches"])
            self.assertEqual(res["user_text_hash"], hashlib.sha256(raw_msg.encode("utf-8")).hexdigest())

            # Negative test: content difference fails closed
            diff_msg = "# Title [REQ-NORM-1]\n\n- Item 1: `DIFFERENT`\n- Item 2: `bar`"
            res_diff = self.relay.check_user_turn_exists(
                "sess-1", "REQ-NORM-1",
                expected_message_hash=hashlib.sha256(diff_msg.encode("utf-8")).hexdigest(),
                expected_message=diff_msg,
            )
            self.assertFalse(res_diff["hash_matches"])

    def test_target_conversation_id_must_match_exactly(self):
        """F-010: Target conversation ID must match exactly, not just as a prefix."""
        output = """TAB         SCOPE  WIN         TITLE                       URL
1587806085  user   1587805881  ChatGPT Review Session      https://chatgpt.com/c/conv-123-extra-malicious
"""
        with mock.patch.object(self.relay, "_run_bsk") as mock_bsk:
            mock_bsk.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout=output, stderr="")
            # Specifying target tab id with non-matching conversation ID must raise ConversationMismatchError
            with self.assertRaises(ConversationMismatchError):
                self.relay.find_target_tab("sess-1", target_tab_id="1587806085", target_url_prefix="https://chatgpt.com/c/conv-123")
            # Searching by target_url_prefix alone must NOT match the suffix-extended tab
            with self.assertRaises(TabNotFoundError):
                self.relay.find_target_tab("sess-1", target_url_prefix="https://chatgpt.com/c/conv-123")

    def test_borrowed_page_must_match_initial_url_even_with_target_tab_id_only(self):
        """F-010: Even if only target_tab_id is provided, post-borrow URL must match initial URL."""
        with mock.patch.object(self.relay, "check_status") as mock_status, \
             mock.patch.object(self.relay, "start_session") as mock_start, \
             mock.patch.object(self.relay, "find_target_tab") as mock_find, \
             mock.patch.object(self.relay, "borrow_tab") as mock_borrow, \
             mock.patch.object(self.relay, "get_current_url") as mock_url, \
             mock.patch.object(self.relay, "return_tab") as mock_return, \
             mock.patch.object(self.relay, "stop_session") as mock_stop:

            mock_status.return_value = browser_relay.BrowserStatus(daemon_running=True, connected_browser_id="b-1")
            mock_start.return_value = "sess-1"
            mock_find.return_value = TabInfo("tab-1", "user", "w-1", "ChatGPT", "https://chatgpt.com/c/conv-1")
            mock_borrow.return_value = True
            mock_url.return_value = "https://chatgpt.com/c/conv-2"

            result = self.relay.run_e2e_review("task-1", "msg [REQ-1]", "REQ-1", target_tab_id="tab-1")
            self.assertEqual(result["status"], "BLOCKED_CONVERSATION_MISMATCH")
            self.assertIn("navigated", result["error"].lower())

    def test_send_prompt_verifies_url_before_inject_and_send_preventing_toctou(self):
        """F-010: Re-verify URL in input and click send operations to prevent TOCTOU."""
        with mock.patch.object(self.relay, "check_user_turn_exists") as mock_check, \
             mock.patch.object(self.relay, "get_current_url") as mock_url:
            mock_check.return_value = {"exists": False, "status": "USER_TURN_NOT_FOUND"}
            mock_url.return_value = "https://chatgpt.com/c/hijacked-conv"
            with self.assertRaises(ConversationMismatchError):
                self.relay.send_prompt(
                    "sess-1", "msg [REQ-TOCTOU]", "REQ-TOCTOU",
                    expected_url="https://chatgpt.com/c/expected-conv"
                )

    def test_dom_execution_multi_block_reply_extracts_all_blocks(self):
        """F-007: Must extract ALL text blocks in assistant reply, not just first querySelector match."""
        req_id = "REQ-MULTI-BLOCK"
        dom = {
            "tag": "main",
            "children": [
                {"tag": "article", "attrs": {"data-message-author-role": "user"}, "text": f"Prompt [{req_id}]"},
                {
                    "tag": "article",
                    "attrs": {"data-message-author-role": "assistant"},
                    "children": [
                        {"tag": "div", "attrs": {"class": "prose"}, "text": "第1段正文：概述。"},
                        {"tag": "div", "attrs": {"class": "prose"}, "text": "第2段正文：详细发现。"},
                    ],
                },
            ],
        }
        js_extract = build_extract_js(req_id)
        res = self.run_in_dom(dom, js_extract)
        self.assertEqual(res["status"], "OK")
        self.assertIn("第1段正文：概述。", res["text"])
        self.assertIn("第2段正文：详细发现。", res["text"])

    def test_dom_modern_group_turn_and_markdown_root_extraction(self):
        """Tests turn discovery and MarkdownRoot text extraction for modern ChatGPT layout."""
        req_id = "REQ-MODERN-LAYOUT-001"
        reply = "第一段评审建议。\n\n第二段代码修复分析。"
        dom = {
            "tag": "main",
            "children": [
                {
                    "tag": "div",
                    "attrs": {"class": "flex flex-col gap-3 browser:gap-1"},
                    "children": [
                        {
                            "tag": "div",
                            "attrs": {"class": "block-test"},
                            "children": [
                                {
                                    "tag": "div",
                                    "attrs": {"class": "group/user-message"},
                                    "text": f"开始代码审查 [{req_id}]",
                                },
                                {
                                    "tag": "button",
                                    "attrs": {"aria-label": "编辑消息"},
                                    "text": "编辑",
                                },
                            ],
                        },
                        {
                            "tag": "div",
                            "attrs": {"class": "block-test"},
                            "children": [
                                {
                                    "tag": "div",
                                    "attrs": {"class": "MarkdownRoot-test"},
                                    "text": reply,
                                },
                                {
                                    "tag": "button",
                                    "attrs": {"aria-label": "复制"},
                                    "text": "复制",
                                },
                            ],
                        },
                    ],
                }
            ],
        }

        # Check user turn
        js_check = build_check_user_turn_js(req_id)
        res_check = self.run_in_dom(dom, js_check)
        self.assertTrue(res_check["exists"])
        self.assertEqual(res_check["status"], "OK")

        # Extract assistant turn
        js_extract = build_extract_js(req_id)
        res_extract = self.run_in_dom(dom, js_extract)
        self.assertEqual(res_extract["status"], "OK")
        self.assertEqual(res_extract["text"], reply)

    def test_run_e2e_review_records_diagnostic_when_cleanup_fails(self):
        """Resource cleanup: return_tab or stop_session returning False must be recorded as error."""
        with mock.patch.object(self.relay, "check_status") as mock_status, \
             mock.patch.object(self.relay, "start_session") as mock_start, \
             mock.patch.object(self.relay, "find_target_tab") as mock_find, \
             mock.patch.object(self.relay, "borrow_tab") as mock_borrow, \
             mock.patch.object(self.relay, "get_current_url") as mock_url, \
             mock.patch.object(self.relay, "send_prompt") as mock_send, \
             mock.patch.object(self.relay, "wait_for_response") as mock_wait, \
             mock.patch.object(self.relay, "return_tab") as mock_return, \
             mock.patch.object(self.relay, "stop_session") as mock_stop:

            mock_status.return_value = browser_relay.BrowserStatus(daemon_running=True, connected_browser_id="b-1")
            mock_start.return_value = "sess-1"
            mock_find.return_value = TabInfo("tab-1", "user", "w-1", "ChatGPT", "https://chatgpt.com/c/conv-1")
            mock_borrow.return_value = True
            mock_url.return_value = "https://chatgpt.com/c/conv-1"
            mock_send.return_value = True
            mock_wait.return_value = {"status": "COMPLETE", "text": "All good", "length": 8}
            mock_return.return_value = False
            mock_stop.return_value = True

            result = self.relay.run_e2e_review("task-1", "msg [REQ-1]", "REQ-1")
            self.assertEqual(result["cleanup_status"], "CLEANUP_FAILED")
            self.assertNotEqual(result["status"], "SUCCESS")

    def test_dom_model_and_thinking_detection_in_node(self):
        """Tests model and thinking mode DOM detection running inside Node.js DOM fixture."""
        dom = {
            "tag": "main",
            "children": [
                {
                    "tag": "button",
                    "attrs": {"data-testid": "model-switcher-dropdown-button"},
                    "text": "ChatGPT (o1)",
                },
                {
                    "tag": "button",
                    "attrs": {"data-testid": "thought-indicator"},
                    "text": "Thought for 12 seconds",
                },
            ],
        }
        js = build_check_model_and_thinking_js()
        res = self.run_in_dom(dom, js)
        self.assertTrue(res["detected"])
        self.assertIn("o1", res["model"])
        self.assertIn("Thought", res["thinking_mode"])

    def test_dom_custom_gpt_model_and_thinking_detection_in_node(self):
        """Tests Custom GPT model and composer reasoning effort pill detection in Node.js DOM fixture."""
        dom = {
            "tag": "main",
            "children": [
                {
                    "tag": "header",
                    "children": [
                        {"tag": "div", "text": "OVE PHD\n分享"},
                        {"tag": "button", "attrs": {"aria-label": "切换模式，当前模式：ChatGPT"}, "text": "ChatGPT"},
                    ],
                },
                {
                    "tag": "div",
                    "attrs": {"id": "composer-background"},
                    "children": [
                        {"tag": "button", "attrs": {"aria-label": "选择 ChatGPT 模型"}, "text": "High"},
                    ],
                },
            ],
        }
        js = build_check_model_and_thinking_js()
        res = self.run_in_dom(dom, js)
        self.assertTrue(res["detected"])
        self.assertEqual(res["model"], "ChatGPT")
        self.assertEqual(res["thinking_mode"], "High")


    def test_check_model_and_thinking_validation(self):
        """Tests BrowserRelay.check_model_and_thinking validation logic."""
        with mock.patch.object(self.relay, "evaluate") as mock_eval:
            # Case 1: Match
            mock_eval.return_value = json.dumps({
                "detected": True,
                "model": "GPT-4o",
                "thinking_mode": "standard",
            })
            res = self.relay.check_model_and_thinking("sess-1", expected_model="gpt-4o")
            self.assertTrue(res["verified"])
            self.assertEqual(res["status"], "OK")

            # Case 2: Model Mismatch fails closed
            res_mismatch = self.relay.check_model_and_thinking("sess-1", expected_model="o1-preview")
            self.assertFalse(res_mismatch["verified"])
            self.assertEqual(res_mismatch["status"], "BLOCKED_MODEL_MISMATCH")

            # Case 3: Thinking mode Mismatch fails closed
            res_think_mismatch = self.relay.check_model_and_thinking(
                "sess-1", expected_model="gpt-4o", expected_thinking="deep-thought"
            )
            self.assertFalse(res_think_mismatch["verified"])
            self.assertEqual(res_think_mismatch["status"], "BLOCKED_THINKING_MODE_MISMATCH")

            # Case 4: Substring downgrade rejection (expected GPT-5 Pro, detected GPT-5)
            mock_eval.return_value = json.dumps({
                "detected": True,
                "model": "GPT-5",
                "thinking_mode": "standard",
            })
            res_downgrade = self.relay.check_model_and_thinking("sess-1", expected_model="GPT-5 Pro")
            self.assertFalse(res_downgrade["verified"])
            self.assertEqual(res_downgrade["status"], "BLOCKED_MODEL_MISMATCH")

            # Case 5: Missing thinking mode in DOM fails closed with BLOCKED_THINKING_MODE_UNKNOWN
            mock_eval.return_value = json.dumps({
                "detected": True,
                "model": "o3-mini",
                "thinking_mode": "",
            })
            res_no_thinking = self.relay.check_model_and_thinking("sess-1", expected_model="o3-mini")
            self.assertFalse(res_no_thinking["verified"])
            self.assertEqual(res_no_thinking["status"], "BLOCKED_THINKING_MODE_UNKNOWN")

            # Case 6: Missing model in DOM fails closed with BLOCKED_MODEL_UNKNOWN
            mock_eval.return_value = json.dumps({
                "detected": False,
                "model": "",
                "thinking_mode": "standard",
            })
            res_no_model = self.relay.check_model_and_thinking("sess-1")
            self.assertFalse(res_no_model["verified"])
            self.assertEqual(res_no_model["status"], "BLOCKED_MODEL_UNKNOWN")

            # Case 7: Generic 'enabled' / 'thinking' rejected as wildcard; concrete mode required
            mock_eval.return_value = json.dumps({
                "detected": True,
                "model": "o3-mini",
                "thinking_mode": "enabled",
            })
            res_generic = self.relay.check_model_and_thinking("sess-1", expected_thinking="extended")
            self.assertFalse(res_generic["verified"])
            self.assertEqual(res_generic["status"], "BLOCKED_THINKING_MODE_UNKNOWN")

    def test_create_new_chat_navigates_to_root(self):
        with mock.patch.object(self.relay, "_run_bsk") as mock_bsk:
            mock_bsk.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            res = self.relay.create_new_chat("sess-1")
            self.assertTrue(res["ok"])
            self.assertEqual(res["url"], "https://chatgpt.com/")

    def test_run_e2e_review_blocks_on_model_mismatch(self):
        with mock.patch.object(self.relay, "check_status") as mock_status, \
             mock.patch.object(self.relay, "start_session") as mock_start, \
             mock.patch.object(self.relay, "find_target_tab") as mock_find, \
             mock.patch.object(self.relay, "borrow_tab") as mock_borrow, \
             mock.patch.object(self.relay, "get_current_url") as mock_url, \
             mock.patch.object(self.relay, "check_model_and_thinking") as mock_check, \
             mock.patch.object(self.relay, "return_tab") as mock_return, \
             mock.patch.object(self.relay, "stop_session") as mock_stop:

            mock_status.return_value = browser_relay.BrowserStatus(daemon_running=True, connected_browser_id="b-1")
            mock_start.return_value = "sess-1"
            mock_find.return_value = TabInfo("tab-1", "user", "w-1", "ChatGPT", "https://chatgpt.com/c/conv-1")
            mock_borrow.return_value = True
            mock_url.return_value = "https://chatgpt.com/c/conv-1"
            mock_check.return_value = {
                "verified": False,
                "status": "BLOCKED_MODEL_MISMATCH",
                "reason": "Model mismatch",
            }
            mock_return.return_value = True
            mock_stop.return_value = True

            result = self.relay.run_e2e_review(
                "task-1", "msg [REQ-1]", "REQ-1", expected_model="o1"
            )
            self.assertEqual(result["status"], "BLOCKED_MODEL_MISMATCH")
            self.assertIn("Model mismatch", result["error"])
            mock_return.assert_called_once()
            mock_stop.assert_called_once()

    def test_validate_conversation_url(self):
        """test_validate_conversation_url: 两个合法样例返回 True；占位地址、https://chatgpt.com/、http 协议返回 False。"""
        valid_custom = "https://chatgpt.com/g/g-p-68d1848f372081918ae13ad310a7fff8-ove-phd/c/6ad5e25e-5628-83ed-8cac-6e8e6b447f3c"
        valid_direct = "https://chatgpt.com/c/6ad5e25e-5628-83ed-8cac-6e8e6b447f3c"
        placeholder = "https://chatgpt.com/c/<问候交流的ID>"
        root_url = "https://chatgpt.com/"
        http_url = "http://chatgpt.com/c/6ad5e25e-5628-83ed-8cac-6e8e6b447f3c"

        self.assertTrue(validate_conversation_url(valid_custom))
        self.assertTrue(validate_conversation_url(valid_direct))
        self.assertFalse(validate_conversation_url(placeholder))
        self.assertFalse(validate_conversation_url(root_url))
        self.assertFalse(validate_conversation_url(http_url))

    def test_inject_empty_then_verify_ok_continues_send(self):
        """test_inject_empty_then_verify_ok_continues_send: mock evaluate，注入返回 INJECT_EMPTY:{}，补查返回 OK，之后按原流程成功，断言点击被执行。"""
        with mock.patch.object(self.relay, "check_user_turn_exists") as mock_check, \
             mock.patch.object(self.relay, "evaluate") as mock_eval:
            # First check_user_turn_exists before send: not found
            # Second check_user_turn_exists after send: exists with hash match
            msg = "test message with [REQ-INJECT-OK]"
            msg_hash = hashlib.sha256(msg.strip().encode("utf-8")).hexdigest()
            mock_check.side_effect = [
                {"exists": False, "status": "USER_TURN_NOT_FOUND"},
                {"exists": True, "status": "OK", "hash_matches": True, "user_text_hash": msg_hash},
            ]

            eval_results = [
                'INJECT_EMPTY:{"candidates":[],"chosen":null,"active":null}',  # inject_js result
                "OK",  # verify_read_js result
                "CLICKED",  # click_js result
            ]
            mock_eval.side_effect = eval_results

            res = self.relay.send_prompt(
                "sess-1", msg, "[REQ-INJECT-OK]"
            )
            self.assertTrue(res)
            # Verify click_js was called (at least 3 evaluate calls)
            self.assertGreaterEqual(mock_eval.call_count, 3)
            # Find the evaluate call with click logic
            clicked_calls = [c for c in mock_eval.call_args_list if "send-button" in str(c)]
            self.assertGreaterEqual(len(clicked_calls), 1)

    def test_inject_not_confirmed_raises_not_sent_and_journal_send_clicked_false(self):
        """test_inject_not_confirmed_raises_not_sent_and_journal_send_clicked_false: 注入和补查都失败，send_prompt 抛 NotSentError；经 run_e2e_review 时，journal 的 send_clicked 为 False。"""
        with mock.patch.object(self.relay, "check_user_turn_exists") as mock_check, \
             mock.patch.object(self.relay, "evaluate") as mock_eval:
            mock_check.return_value = {"exists": False, "status": "USER_TURN_NOT_FOUND"}
            mock_eval.side_effect = [
                'INJECT_EMPTY:{"candidates":[],"chosen":null,"active":null}',  # inject_js
                "MISSING",  # verify_read_js
                "",  # clear_js
            ]

            with self.assertRaises(NotSentError):
                self.relay.send_prompt(
                    "sess-1", "msg [REQ-FAIL]", "[REQ-FAIL]"
                )

        with mock.patch.object(self.relay, "check_status") as mock_status, \
             mock.patch.object(self.relay, "start_session") as mock_start, \
             mock.patch.object(self.relay, "find_target_tab") as mock_find, \
             mock.patch.object(self.relay, "borrow_tab") as mock_borrow, \
             mock.patch.object(self.relay, "get_current_url") as mock_url, \
             mock.patch.object(self.relay, "check_model_and_thinking") as mock_model, \
             mock.patch.object(self.relay, "send_prompt") as mock_send, \
             mock.patch.object(self.relay, "return_tab") as mock_return, \
             mock.patch.object(self.relay, "stop_session") as mock_stop:

            mock_status.return_value = browser_relay.BrowserStatus(daemon_running=True, connected_browser_id="b-1")
            mock_start.return_value = "sess-1"
            mock_find.return_value = TabInfo("tab-1", "user", "w-1", "ChatGPT", "https://chatgpt.com/c/6ad5e25e-5628-83ed-8cac-6e8e6b447f3c")
            mock_borrow.return_value = True
            mock_url.return_value = "https://chatgpt.com/c/6ad5e25e-5628-83ed-8cac-6e8e6b447f3c"
            mock_model.return_value = {"verified": True, "status": "OK", "model": "o1", "thinking_mode": "high"}
            mock_send.side_effect = NotSentError("Prompt injection not confirmed: INJECT_EMPTY")
            mock_return.return_value = True
            mock_stop.return_value = True

            result = self.relay.run_e2e_review(
                task_id="task-1",
                message="msg [REQ-FAIL]",
                request_id="[REQ-FAIL]",
                target_url_prefix="https://chatgpt.com/c/6ad5e25e-5628-83ed-8cac-6e8e6b447f3c",
            )
            self.assertEqual(result["status"], "BLOCKED_DELIVERY_UNCONFIRMED")
            self.assertFalse(result.get("send_clicked"))

    def test_dom_inject_prefers_visible_contenteditable(self):
        """node DOM 测试 test_dom_inject_prefers_visible_contenteditable: 捕获生产代码的注入 JS，构造隐藏的 #prompt-textarea 和可见的 contenteditable，执行断言返回 'OK' 且文字写入可见元素。"""
        req_id = "REQ-VISIBLE-TEST"
        msg = f"hello [{req_id}]"

        class _Captured(Exception):
            def __init__(self, js: str):
                self.js = js

        relay = BrowserRelay()
        with mock.patch.object(relay, "check_user_turn_exists") as mock_check, \
             mock.patch.object(relay, "evaluate") as mock_eval:
            mock_check.return_value = {"status": "USER_TURN_NOT_FOUND", "exists": False}

            def _eval_side_effect(session_id, js):
                raise _Captured(js)

            mock_eval.side_effect = _eval_side_effect
            with self.assertRaises(_Captured) as ctx:
                relay.send_prompt("sess-1", msg, req_id)
            captured_js = ctx.exception.js

        dom = {
            "tag": "form",
            "children": [
                {
                    "tag": "textarea",
                    "attrs": {"id": "prompt-textarea", "data-hidden": "true"},
                    "text": "",
                },
                {
                    "tag": "div",
                    "attrs": {"contenteditable": "true", "id": "visible-editor"},
                    "text": "",
                },
            ],
        }
        wrapped_js = f"""(() => {{
            const res = ({captured_js});
            return {{
                "status": res,
                "visibleText": document.querySelector('#visible-editor').innerText,
                "hiddenValue": document.querySelector('#prompt-textarea').value || ''
            }};
        }})()"""
        res = self.run_in_dom(dom, wrapped_js)
        self.assertEqual(res["status"], "OK")
        self.assertIn(req_id, res["visibleText"])
        self.assertNotIn(req_id, res["hiddenValue"])

    def test_no_browser_journal_has_send_clicked_false(self):
        """test_no_browser_journal_has_send_clicked_false: mock check_status 返回 daemon 未运行，run_e2e_review 的 journal 里 send_clicked is False。"""
        with mock.patch.object(self.relay, "check_status") as mock_status:
            mock_status.return_value = browser_relay.BrowserStatus(daemon_running=False, connected_browser_id=None, error="bsk daemon not running")
            res = self.relay.run_e2e_review(
                task_id="task-no-browser",
                message="test [REQ-NO-BROWSER]",
                request_id="REQ-NO-BROWSER",
            )
            self.assertEqual(res["status"], "BLOCKED_NO_BROWSER")
            self.assertIs(res.get("send_clicked"), False)

    def _build_new_layout_dom(self):
        return {
            "tag": "main",
            "children": [
                {
                    "tag": "div",
                    "attrs": {},
                    "children": [
                        {
                            "tag": "div",
                            "attrs": {"data-chatgpt-search-unit-key": "fallback-turn-0:0:user"},
                            "children": [
                                {
                                    "tag": "div",
                                    "attrs": {"data-user-message-bubble": "true"},
                                    "children": [
                                        {
                                            "tag": "div",
                                            "attrs": {"data-markdown-text-tone": "user-message"},
                                            "text": "你好",
                                        }
                                    ],
                                }
                            ],
                        },
                        {
                            "tag": "div",
                            "attrs": {"data-chatgpt-search-unit-key": "fallback-turn-0:2:assistant"},
                            "children": [
                                {
                                    "tag": "div",
                                    "attrs": {"class": "prose"},
                                    "text": "你好！有什么我可以帮你的？",
                                }
                            ],
                        },
                        {
                            "tag": "div",
                            "attrs": {"data-chatgpt-search-unit-key": "fallback-turn-0:3:user"},
                            "children": [
                                {
                                    "tag": "div",
                                    "attrs": {"data-user-message-bubble": "true"},
                                    "children": [
                                        {
                                            "tag": "div",
                                            "attrs": {"data-markdown-text-tone": "user-message"},
                                            "text": "# Plan [req-X]\n详细计划内容",
                                        }
                                    ],
                                }
                            ],
                        },
                        {
                            "tag": "div",
                            "attrs": {"data-chatgpt-search-unit-key": "fallback-turn-0:4:assistant"},
                            "children": [
                                {
                                    "tag": "div",
                                    "attrs": {"class": "prose"},
                                    "text": "answer for X",
                                }
                            ],
                        },
                    ],
                }
            ],
        }

    def test_dom_new_layout_user_turn_found_by_unit_key(self):
        dom = self._build_new_layout_dom()
        js = build_check_user_turn_js("req-X")
        res = self.run_in_dom(dom, js)
        self.assertTrue(res.get("exists"))
        self.assertIn("req-X", res.get("userText", ""))

    def test_dom_new_layout_extract_assistant_after_user(self):
        dom = self._build_new_layout_dom()
        js = build_extract_js("req-X")
        res = self.run_in_dom(dom, js)
        self.assertEqual(res.get("status"), "OK")
        self.assertEqual(res.get("text"), "answer for X")

    def test_dom_not_found_returns_diag(self):
        dom = self._build_new_layout_dom()
        js = build_check_user_turn_js("req-Y")
        res = self.run_in_dom(dom, js)
        self.assertFalse(res.get("exists"))
        self.assertEqual(res.get("status"), "USER_TURN_NOT_FOUND")
        diag = res.get("diag")
        self.assertIsNotNone(diag)
        self.assertEqual(diag.get("units"), 4)
        self.assertEqual(diag.get("bubbles"), 2)
        self.assertIs(diag.get("mainHasReq"), False)


if __name__ == "__main__":
    unittest.main()
