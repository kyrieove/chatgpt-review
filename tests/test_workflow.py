"""Tests for Antigravity single-entry workflow CLI and state machine.

Covers:
1. Process restart recovery
2. Duplicate request deduplication & inverse validation (content collision)
3. Delivery unknown / unconfirmed fails closed without retry ID bypass
4. Wrong round & Wrong SHA rejection (inverse validation)
5. Missing structure / plain-text "passed" rejection
6. Stale review invalidated by newer commits
7. Unknown model / thinking mode fails closed (including substring downgrade)
8. Login invalid / session redirect fails closed
9. Cross-chat continuation & history preservation
10. Two consecutive failures after introduction transition to diagnosis
11. Repair round limit exceeded pause and explicit resolve unpause
12. Missing test evidence cannot reach DONE / ACCEPTED
13. Start cannot record-work directly skipping ChatGPT plan
14. Plain string 'PASS' rejection and structured evidence enforcement
15. Working tree uncommitted modifications fail closed
16. Plan phase rejects ACCEPTED decision
17. Strict feedback JSON schema parsing (multiple blocks, bad round, empty findings)
18. Cross-process TaskLock mutual exclusion
19. Production mock forbidden: simulated response cannot reach ACCEPTED_DONE
20. Materials reviewability: task brief, git diff/log, and no default ACCEPTED
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_SCRIPT = ROOT / "skills" / "chatgpt-review" / "scripts" / "workflow.py"
SPEC = importlib.util.spec_from_file_location("workflow", WORKFLOW_SCRIPT)
workflow = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = workflow
SPEC.loader.exec_module(workflow)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.project_dir = Path(self.temp_dir.name)
        # Initialize a real git repo
        subprocess.run(["git", "init"], cwd=self.project_dir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "TestUser"], cwd=self.project_dir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=self.project_dir, check=True, capture_output=True)
        # Commit an initial file
        (self.project_dir / "README.md").write_text("# Project\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=self.project_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "initial commit"], cwd=self.project_dir, check=True, capture_output=True)
        proc = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.project_dir, check=True, capture_output=True, text=True)
        self.initial_sha = proc.stdout.strip()

    def tearDown(self):
        self.temp_dir.cleanup()

    def _start_task(self, task_id="test-task-1", title="Test Task", model=None):
        return workflow.start_workflow(
            project=self.project_dir,
            title=title,
            task_id=task_id,
            model=model,
        )

    def _make_evidence(self, sha=None, cmd="python -m unittest discover", exit_code=0):
        ev_file = self.project_dir / "handoffs" / "test_evidence.json"
        ev_file.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps({
            "command": cmd,
            "exit_code": exit_code,
            "tested_sha": sha or self.initial_sha,
            "executed_at": "2026-10-15T12:00:00Z",
        })
        ev_file.write_text(content, encoding="utf-8")
        return str(ev_file)

    def _advance_to_execution(self, task_id="test-task-1"):
        task_dir = self.project_dir / "handoffs" / task_id
        state = workflow.load_state(task_dir)
        workflow.apply_review_decision(
            task_dir=task_dir,
            decision="PLAN_READY",
            findings=[],
            reviewed_sha=state["head_sha"],
            next_action="Proceed to execution",
        )

    def test_process_restart_recovery(self):
        """1. State is persisted atomically; simulated restart recovers state cleanly."""
        state = self._start_task(task_id="restart-task-1")
        task_dir = self.project_dir / "handoffs" / "restart-task-1"
        self.assertTrue((task_dir / "workflow.json").is_file())

        loaded = workflow.load_state(task_dir)
        self.assertEqual(loaded["task_id"], "restart-task-1")
        self.assertEqual(loaded["stage"], workflow.STAGE_WAITING_PLAN)
        self.assertEqual(loaded["head_sha"], self.initial_sha)
        self.assertEqual(loaded["round"], 0)

    def test_duplicate_request_deduplication_and_inverse_collision(self):
        """2. Duplicate request deduplication: same request_id + matching hash skips resend.

        Inverse validation: same request_id with differing content hash raises conflict.
        """
        state = workflow.start_workflow(
            project=self.project_dir,
            title="Dedup Task",
            task_id="dedup-task",
            repo_url="https://github.com/example/repo",
        )
        task_dir = self.project_dir / "handoffs" / "dedup-task"

        req_id = "req-dedup-task-r1-plan"
        msg = workflow.build_plan_request_message(state, task_dir, req_id, self.project_dir)
        req_hash = workflow.compute_hash(msg)

        # First recording in active_transaction (complete valid transaction)
        state["active_transaction"] = {
            "request_id": req_id,
            "kind": "plan",
            "round": 1,
            "head_sha": self.initial_sha,
            "message": msg,
            "message_hash": req_hash,
            "chat_url": "https://chatgpt.com/c/dedup-chat",
            "delivery_status": "CONFIRMED_USER_TURN",
            "created_at": workflow.iso_now(),
            "applied": False,
        }
        workflow.save_state(task_dir, state)

        # Same request with matching hash: should recover and skip resend
        loaded = workflow.load_state(task_dir)
        self.assertEqual(loaded["active_transaction"]["request_id"], req_id)
        self.assertEqual(loaded["active_transaction"]["message_hash"], req_hash)

        plan_resp = f"""```json:chatgpt-review
{{
  "task_id": "dedup-task",
  "round": 1,
  "reviewed_head_sha": "{self.initial_sha}",
  "decision": "PLAN_READY",
  "findings": [],
  "next_action": "Proceed"
}}
```"""
        with mock.patch("workflow.BrowserRelay") as MockRelay:
            instance = MockRelay.return_value
            instance.check_status.return_value = mock.Mock(daemon_running=True, connected_browser_id="b-1")
            instance.run_e2e_review.return_value = {
                "status": "SUCCESS",
                "response_text": plan_resp,
                "chat_url": "https://chatgpt.com/c/dedup-chat",
                "tab_id": "tab-1",
            }
            res = workflow.execute_exchange(
                task_dir=task_dir,
                chat_url="https://chatgpt.com/c/dedup-chat",
            )
            self.assertEqual(res["status"], "SUCCESS")
            call_kwargs = instance.run_e2e_review.call_args.kwargs
            self.assertTrue(call_kwargs["skip_send"])
            self.assertTrue(call_kwargs["is_recovery"])

        # Inverse validation: reset unapplied transaction with altered content hash -> raises DeliveryError
        state = workflow.load_state(task_dir)
        state["stage"] = workflow.STAGE_WAITING_PLAN
        state["business_stage"] = workflow.STAGE_WAITING_PLAN
        diff_hash = workflow.compute_hash(msg + " altered!")
        state["active_transaction"] = {
            "request_id": req_id,
            "kind": "plan",
            "round": 1,
            "head_sha": self.initial_sha,
            "message": msg,
            "message_hash": diff_hash,
            "chat_url": "https://chatgpt.com/c/dedup-chat",
            "delivery_status": "CONFIRMED_USER_TURN",
            "created_at": workflow.iso_now(),
            "applied": False,
        }
        workflow.save_state(task_dir, state)

        with self.assertRaises(workflow.DeliveryError):
            workflow.execute_exchange(
                task_dir=task_dir,
                chat_url="https://chatgpt.com/c/dedup-chat",
                _test_transport_response="{}",
            )

    def test_delivery_unknown_fails_closed(self):
        """3. Delivery unconfirmed fails closed and transitions to BLOCKED without renaming request ID."""
        state = workflow.start_workflow(
            project=self.project_dir,
            title="Delivery Unknown Task",
            task_id="delivery-task",
            repo_url="https://github.com/example/repo",
        )
        task_dir = self.project_dir / "handoffs" / "delivery-task"

        with mock.patch("workflow.BrowserRelay") as MockRelay:
            instance = MockRelay.return_value
            instance.check_status.return_value = mock.Mock(daemon_running=True, connected_browser_id="b-1")
            instance.run_e2e_review.return_value = {
                "status": "BLOCKED_DELIVERY_UNCONFIRMED",
                "error": "Message not confirmed in DOM",
            }
            res = workflow.execute_exchange(
                task_dir=task_dir,
                chat_url="https://chatgpt.com/c/delivery-test",
            )
            self.assertEqual(res["status"], "BLOCKED")
            self.assertIn("Message not confirmed in DOM", res["error"])
            instance.run_e2e_review.assert_called_once()
            first_kwargs = instance.run_e2e_review.call_args.kwargs
            self.assertFalse(first_kwargs["skip_send"])
            self.assertFalse(first_kwargs["is_recovery"])
            self.assertEqual(first_kwargs["request_id"], "req-delivery-task-r1-plan")

            loaded = workflow.load_state(task_dir)
            self.assertEqual(loaded["stage"], workflow.STAGE_BLOCKED)
            self.assertEqual(loaded["blocked_from_stage"], workflow.STAGE_WAITING_PLAN)
            self.assertEqual(loaded["active_transaction"]["delivery_status"], "DELIVERY_UNKNOWN")
            # Must preserve original request ID without appending -retry
            self.assertEqual(loaded["active_transaction"]["request_id"], "req-delivery-task-r1-plan")
            self.assertEqual(loaded["active_transaction"]["round"], 1)
            self.assertFalse(loaded["active_transaction"]["applied"])

        # Subsequent recovery exchange must keep original request_id and set skip_send=True (forbid duplicate send)
        plan_resp = f"""```json:chatgpt-review
{{
  "task_id": "delivery-task",
  "round": 1,
  "reviewed_head_sha": "{self.initial_sha}",
  "decision": "PLAN_READY",
  "findings": [],
  "next_action": "Proceed after DOM recovery"
}}
```"""
        with mock.patch("workflow.BrowserRelay") as MockRelayRecover:
            rec_instance = MockRelayRecover.return_value
            rec_instance.check_status.return_value = mock.Mock(daemon_running=True, connected_browser_id="b-1")
            rec_instance.run_e2e_review.return_value = {
                "status": "SUCCESS",
                "response_text": plan_resp,
                "chat_url": "https://chatgpt.com/c/delivery-test",
                "tab_id": "tab-1",
            }
            rec_res = workflow.execute_exchange(
                task_dir=task_dir,
                chat_url="https://chatgpt.com/c/delivery-test",
            )
            self.assertEqual(rec_res["status"], "SUCCESS")
            self.assertEqual(rec_res["decision"], "PLAN_READY")
            rec_instance.run_e2e_review.assert_called_once()
            rec_kwargs = rec_instance.run_e2e_review.call_args.kwargs
            self.assertTrue(rec_kwargs["skip_send"])
            self.assertTrue(rec_kwargs["is_recovery"])
            self.assertEqual(rec_kwargs["request_id"], "req-delivery-task-r1-plan")

            after_rec = workflow.load_state(task_dir)
            self.assertEqual(after_rec["stage"], workflow.STAGE_WAITING_EXECUTION)
            self.assertEqual(after_rec["active_transaction"]["request_id"], "req-delivery-task-r1-plan")
            self.assertEqual(after_rec["active_transaction"]["delivery_status"], "CONFIRMED_USER_TURN")
            self.assertTrue(after_rec["active_transaction"]["applied"])

    def test_wrong_sha_rejected_inverse_validation(self):
        """4. Wrong reviewed_head_sha is rejected with BLOCKED and logs preserved.

        Inverse validation: correct SHA successfully transitions state.
        """
        state = self._start_task(task_id="sha-task")
        task_dir = self.project_dir / "handoffs" / "sha-task"
        self._advance_to_execution("sha-task")

        # Advance to review stage with structured test evidence
        workflow.record_work(
            project=self.project_dir,
            task_id="sha-task",
            head_sha=self.initial_sha,
            test_evidence=self._make_evidence(sha=self.initial_sha),
        )
        loaded = workflow.load_state(task_dir)
        self.assertEqual(loaded["stage"], workflow.STAGE_WAITING_REVIEW)

        # Case A: Inverse validation - Wrong SHA returned by ChatGPT
        wrong_sha = "0000000000000000000000000000000000000000"
        response_wrong = f"""Here is my review:
```json:chatgpt-review
{{
  "task_id": "sha-task",
  "round": 1,
  "reviewed_head_sha": "{wrong_sha}",
  "decision": "ACCEPTED",
  "findings": [],
  "next_action": "Done"
}}
```"""
        parsed = workflow.parse_and_validate_review_response(
            response_text=response_wrong,
            expected_task_id="sha-task",
            expected_round=1,
            expected_head_sha=self.initial_sha,
            expected_stage=workflow.STAGE_WAITING_REVIEW,
        )
        self.assertFalse(parsed["valid"])
        self.assertIn("SHA mismatch", parsed["reason"])

        # Case B: Positive validation - Correct SHA returned
        response_correct = f"""Here is my review:
```json:chatgpt-review
{{
  "task_id": "sha-task",
  "round": 1,
  "reviewed_head_sha": "{self.initial_sha}",
  "decision": "ACCEPTED",
  "findings": [],
  "next_action": "Done"
}}
```"""
        parsed_ok = workflow.parse_and_validate_review_response(
            response_text=response_correct,
            expected_task_id="sha-task",
            expected_round=1,
            expected_head_sha=self.initial_sha,
            expected_stage=workflow.STAGE_WAITING_REVIEW,
        )
        self.assertTrue(parsed_ok["valid"])
        self.assertEqual(parsed_ok["data"]["decision"], "ACCEPTED")

    def test_wrong_round_rejected(self):
        """5. Wrong round in machine-readable JSON is rejected."""
        response = f"""Review:
```json:chatgpt-review
{{
  "task_id": "round-task",
  "round": 99,
  "reviewed_head_sha": "{self.initial_sha}",
  "decision": "ACCEPTED",
  "findings": [],
  "next_action": "Done"
}}
```"""
        parsed = workflow.parse_and_validate_review_response(
            response_text=response,
            expected_task_id="round-task",
            expected_round=1,
            expected_head_sha=self.initial_sha,
            expected_stage=workflow.STAGE_WAITING_REVIEW,
        )
        self.assertFalse(parsed["valid"])
        self.assertIn("Round mismatch", parsed["reason"])

    def test_missing_structure_cannot_accept_on_text_alone(self):
        """6. Response without machine-readable JSON block fails closed; text saying '通过' does not count."""
        response = "本次代码实现得很棒，全部验收项通过，测试完美，建议直接合并通过！"
        parsed = workflow.parse_and_validate_review_response(
            response_text=response,
            expected_task_id="text-task",
            expected_round=1,
            expected_head_sha=self.initial_sha,
            expected_stage=workflow.STAGE_WAITING_REVIEW,
        )
        self.assertFalse(parsed["valid"])
        self.assertIn("Missing machine-readable JSON block", parsed["reason"])

    def test_stale_review_invalidated_by_newer_commits(self):
        """7. When newer commits exist, older reviewed SHA is recognized as stale and recovers via public API."""
        state = self._start_task(task_id="stale-task")
        task_dir = self.project_dir / "handoffs" / "stale-task"

        # Advance from WAITING_PLAN to WAITING_EXECUTION via public API (no private state mutation)
        workflow.apply_review_decision(
            task_dir=task_dir,
            decision="PLAN_READY",
            findings=[],
            reviewed_sha=self.initial_sha,
            next_action="Proceed to execution",
        )

        # Record work on initial SHA -> enters WAITING_REVIEW (round 1)
        workflow.record_work(
            project=self.project_dir,
            task_id="stale-task",
            head_sha=self.initial_sha,
            test_evidence=self._make_evidence(sha=self.initial_sha),
        )

        # New commit happens in project
        (self.project_dir / "file2.txt").write_text("New work", encoding="utf-8")
        subprocess.run(["git", "add", "file2.txt"], cwd=self.project_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "second commit"], cwd=self.project_dir, check=True, capture_output=True)
        proc = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.project_dir, check=True, capture_output=True, text=True)
        new_sha = proc.stdout.strip()

        # Review exchange on stale task_dir fails closed because HEAD moved:
        # Formal contract returns {"status": "BLOCKED", "reason": "...BLOCKED_SOURCE_CHANGED..."} and persists BLOCKED
        with mock.patch("workflow.BrowserRelay") as mock_relay_cls:
            res_stale = workflow.execute_exchange(task_dir=task_dir, _test_transport_response="{}")
            mock_relay_cls.assert_not_called()

        self.assertEqual(res_stale["status"], "BLOCKED")
        self.assertIn("BLOCKED_SOURCE_CHANGED", res_stale["reason"])
        self.assertIn("HEAD SHA drift detected", res_stale["reason"])
        self.assertIn(self.initial_sha, res_stale["reason"])
        self.assertIn(new_sha, res_stale["reason"])

        persisted_blocked = workflow.load_state(task_dir)
        self.assertEqual(persisted_blocked["stage"], workflow.STAGE_BLOCKED)
        self.assertEqual(persisted_blocked["blocked_from_stage"], workflow.STAGE_WAITING_REVIEW)
        self.assertEqual(persisted_blocked["business_stage"], workflow.STAGE_WAITING_REVIEW)
        self.assertIn("BLOCKED_SOURCE_CHANGED", persisted_blocked["next_action"])
        self.assertIsNone(persisted_blocked["active_transaction"])

        # Negative 1: Attempting to record_work with old stale SHA (without private state override) must fail closed
        with self.assertRaises(workflow.WorkflowError) as cm_old_sha:
            workflow.record_work(
                project=self.project_dir,
                task_id="stale-task",
                head_sha=self.initial_sha,
                test_evidence=self._make_evidence(sha=self.initial_sha),
            )
        self.assertIn("does not match current git HEAD", str(cm_old_sha.exception))

        # Negative 2: Attempting to record_work with new SHA but test evidence bound to old SHA must fail closed
        with self.assertRaises(workflow.WorkflowError) as cm_stale_ev:
            workflow.record_work(
                project=self.project_dir,
                task_id="stale-task",
                head_sha=new_sha,
                test_evidence=self._make_evidence(sha=self.initial_sha),
            )
        self.assertIn("tested_sha mismatch", str(cm_stale_ev.exception))

        # Negative 3: Dirty working tree during recovery must fail closed
        (self.project_dir / "README.md").write_text("# Uncommitted drift\n", encoding="utf-8")
        try:
            with self.assertRaises(workflow.WorkflowError) as cm_dirty_rec:
                workflow.record_work(
                    project=self.project_dir,
                    task_id="stale-task",
                    head_sha=new_sha,
                    test_evidence=self._make_evidence(sha=new_sha),
                )
            self.assertIn("BLOCKED_SOURCE_CHANGED", str(cm_dirty_rec.exception))
            self.assertIn("M README.md", str(cm_dirty_rec.exception))
        finally:
            subprocess.run(["git", "checkout", "--", "README.md"], cwd=self.project_dir, check=True, capture_output=True)

        # Still BLOCKED after failed recovery attempts
        self.assertEqual(workflow.load_state(task_dir)["stage"], workflow.STAGE_BLOCKED)

        # Positive recovery: Record work with new SHA and matching test evidence via public API
        workflow.record_work(
            project=self.project_dir,
            task_id="stale-task",
            head_sha=new_sha,
            test_evidence=self._make_evidence(sha=new_sha),
        )
        loaded = workflow.load_state(task_dir)
        self.assertEqual(loaded["head_sha"], new_sha)
        self.assertEqual(loaded["stage"], workflow.STAGE_WAITING_REVIEW)
        self.assertEqual(loaded["business_stage"], workflow.STAGE_WAITING_REVIEW)
        self.assertIsNone(loaded["blocked_from_stage"])
        self.assertEqual(loaded["round"], 2)
        self.assertEqual(len(loaded.get("drift_recovery_history", [])), 1)
        self.assertEqual(loaded["drift_recovery_history"][0]["previous_head_sha"], self.initial_sha)
        self.assertEqual(loaded["drift_recovery_history"][0]["new_head_sha"], new_sha)

        # Negative 4: Duplicate recovery attempt on the same SHA while already in WAITING_REVIEW must fail closed
        with self.assertRaises(workflow.WorkflowError) as cm_dup_rec:
            workflow.record_work(
                project=self.project_dir,
                task_id="stale-task",
                head_sha=new_sha,
                test_evidence=self._make_evidence(sha=new_sha),
            )
        self.assertIn("WAITING_REVIEW", str(cm_dup_rec.exception))
        self.assertEqual(workflow.load_state(task_dir)["round"], 2)

        # Verify subsequent review exchange succeeds only with the new SHA (and rejects old SHA in response)
        stale_resp = f"""```json:chatgpt-review
{{
  "task_id": "stale-task",
  "round": 2,
  "reviewed_head_sha": "{self.initial_sha}",
  "decision": "ACCEPTED",
  "findings": [],
  "next_action": "Stale SHA should be rejected"
}}
```"""
        res_bad_resp_sha = workflow.execute_exchange(task_dir=task_dir, _test_transport_response=stale_resp)
        self.assertEqual(res_bad_resp_sha["status"], "BLOCKED")
        self.assertIn("SHA mismatch", res_bad_resp_sha["reason"])

        valid_new_resp = f"""```json:chatgpt-review
{{
  "task_id": "stale-task",
  "round": 2,
  "reviewed_head_sha": "{new_sha}",
  "decision": "CHANGES_REQUESTED",
  "findings": [{{"id": "F-101", "description": "Minor check on new commit"}}],
  "next_action": "Address F-101 on new SHA"
}}
```"""
        res_recovered = workflow.execute_exchange(task_dir=task_dir, _test_transport_response=valid_new_resp)
        self.assertEqual(res_recovered["status"], "SUCCESS")
        self.assertEqual(res_recovered["decision"], "CHANGES_REQUESTED")
        final_state = workflow.load_state(task_dir)
        self.assertEqual(final_state["stage"], workflow.STAGE_WAITING_FIX_OR_DIAGNOSIS)
        self.assertEqual(final_state["head_sha"], new_sha)
        self.assertTrue(final_state["active_transaction"]["applied"])

    def test_unknown_model_or_thinking_mode_fails_closed(self):
        """8. Unknown model or thinking mode mismatch fails closed to BLOCKED."""
        state = workflow.start_workflow(
            project=self.project_dir,
            title="Model Mismatch Task",
            task_id="model-task",
            repo_url="https://github.com/example/repo",
        )
        task_dir = self.project_dir / "handoffs" / "model-task"

        with mock.patch("workflow.BrowserRelay") as MockRelay:
            instance = MockRelay.return_value
            instance.check_status.return_value = mock.Mock(daemon_running=True, connected_browser_id="b-1")
            instance.run_e2e_review.return_value = {
                "status": "BLOCKED_MODEL_MISMATCH",
                "error": "Expected model 'gpt-5 pro', but detected 'gpt-5'",
            }
            res = workflow.execute_exchange(
                task_dir=task_dir,
                chat_url="https://chatgpt.com/c/model-test",
                expected_model="gpt-5 pro",
                expected_thinking="high",
            )
            self.assertEqual(res["status"], "BLOCKED")
            self.assertIn("gpt-5", res["error"].lower())
            instance.run_e2e_review.assert_called_once()
            call_kwargs = instance.run_e2e_review.call_args.kwargs
            self.assertEqual(call_kwargs["expected_model"], "gpt-5 pro")
            self.assertEqual(call_kwargs["expected_thinking"], "high")
            loaded = workflow.load_state(task_dir)
            self.assertEqual(loaded["stage"], workflow.STAGE_BLOCKED)
            self.assertEqual(loaded["blocked_from_stage"], workflow.STAGE_WAITING_PLAN)
            self.assertIn("gpt-5", loaded["next_action"].lower())
            self.assertEqual(loaded["active_transaction"]["delivery_status"], "DELIVERY_UNKNOWN")
            self.assertEqual(loaded["active_transaction"]["request_id"], "req-model-task-r1-plan")
            self.assertEqual(loaded["active_transaction"]["model"], "gpt-5 pro")
            self.assertEqual(loaded["active_transaction"]["thinking_mode"], "high")

    def test_login_invalid_or_session_redirect_fails_closed(self):
        """9. Redirect away from ChatGPT or invalid origin fails closed."""
        state = self._start_task(task_id="login-task")
        task_dir = self.project_dir / "handoffs" / "login-task"

        with mock.patch("workflow.BrowserRelay") as MockRelay:
            instance = MockRelay.return_value
            instance.check_status.return_value = mock.Mock(daemon_running=True, connected_browser_id="b-1")
            instance.run_e2e_review.return_value = {
                "status": "BLOCKED_CONVERSATION_MISMATCH",
                "error": "Tab navigated to https://auth0.openai.com/u/login",
            }
            res = workflow.execute_exchange(
                task_dir=task_dir,
                chat_url="https://chatgpt.com/c/login-test",
            )
            self.assertEqual(res["status"], "BLOCKED")
            loaded = workflow.load_state(task_dir)
            self.assertEqual(loaded["stage"], workflow.STAGE_BLOCKED)

    def test_cross_chat_continuation(self):
        """10. Cross-chat continuation preserves prior URLs, builds continuation brief, and tracks version."""
        state = self._start_task(task_id="cross-task")
        task_dir = self.project_dir / "handoffs" / "cross-task"

        workflow.update_chat_url(task_dir, "https://chatgpt.com/c/old-chat-1")
        loaded = workflow.load_state(task_dir)
        self.assertEqual(loaded["chat_url"], "https://chatgpt.com/c/old-chat-1")
        self.assertEqual(loaded["previous_chat_urls"], [])

        brief = workflow.resume_workflow(
            project=self.project_dir,
            task_id="cross-task",
            chat_url="https://chatgpt.com/c/new-chat-2",
        )
        loaded = workflow.load_state(task_dir)
        self.assertEqual(loaded["chat_url"], "https://chatgpt.com/c/new-chat-2")
        self.assertEqual(loaded["previous_chat_urls"], ["https://chatgpt.com/c/old-chat-1"])
        self.assertIn("cross-task", brief)
        self.assertIn("old-chat-1", brief)
        # Brief must be stored in state for subsequent prompts
        self.assertEqual(loaded["continuation_brief"], brief)

    def test_two_failures_transition_to_diagnosis(self):
        """11. Two consecutive repair rounds without improvement (after introduction) triggers diagnosis mode."""
        state = self._start_task(task_id="diag-task")
        task_dir = self.project_dir / "handoffs" / "diag-task"

        f1 = [{"id": "F-001", "description": "Crash on empty input"}]

        # Negative check: planning stage must reject CHANGES_REQUESTED
        with self.assertRaises(workflow.WorkflowError) as cm_plan:
            workflow.apply_review_decision(
                task_dir=task_dir,
                decision="CHANGES_REQUESTED",
                findings=f1,
                reviewed_sha=self.initial_sha,
                next_action="Fix F-001",
            )
        self.assertIn("not allowed during planning stage", str(cm_plan.exception))

        # Legitimately advance from WAITING_PLAN -> WAITING_EXECUTION -> WAITING_REVIEW via public APIs
        workflow.apply_review_decision(
            task_dir=task_dir,
            decision="PLAN_READY",
            findings=[],
            reviewed_sha=self.initial_sha,
            next_action="Implement initial plan",
        )
        workflow.record_work(
            project=self.project_dir,
            task_id="diag-task",
            head_sha=self.initial_sha,
            test_evidence=self._make_evidence(sha=self.initial_sha),
        )

        # Round 1: Finding F-001 is introduced (0 failed repairs so far)
        workflow.apply_review_decision(
            task_dir=task_dir,
            decision="CHANGES_REQUESTED",
            findings=f1,
            reviewed_sha=self.initial_sha,
            next_action="Fix F-001",
        )
        loaded1 = workflow.load_state(task_dir)
        self.assertEqual(loaded1["stage"], workflow.STAGE_WAITING_FIX_OR_DIAGNOSIS)
        self.assertFalse(loaded1["is_diagnosis"])
        self.assertEqual(loaded1["consecutive_no_improvement"], 0)

        # Record repair attempt 1 -> WAITING_REVIEW (round 2)
        workflow.record_work(
            project=self.project_dir,
            task_id="diag-task",
            head_sha=self.initial_sha,
            test_evidence=self._make_evidence(sha=self.initial_sha),
        )

        # Round 2: 1st repair failed (F-001 still reported) -> diagnosis still FALSE!
        workflow.apply_review_decision(
            task_dir=task_dir,
            decision="CHANGES_REQUESTED",
            findings=f1,
            reviewed_sha=self.initial_sha,
            next_action="Fix F-001 again",
        )
        loaded2 = workflow.load_state(task_dir)
        self.assertEqual(loaded2["stage"], workflow.STAGE_WAITING_FIX_OR_DIAGNOSIS)
        self.assertFalse(loaded2["is_diagnosis"])
        self.assertEqual(loaded2["consecutive_no_improvement"], 0)

        # Record repair attempt 2 -> WAITING_REVIEW (round 3)
        workflow.record_work(
            project=self.project_dir,
            task_id="diag-task",
            head_sha=self.initial_sha,
            test_evidence=self._make_evidence(sha=self.initial_sha),
        )

        # Round 3: 2nd repair failed (F-001 still reported) -> NOW triggers diagnosis!
        workflow.apply_review_decision(
            task_dir=task_dir,
            decision="CHANGES_REQUESTED",
            findings=f1,
            reviewed_sha=self.initial_sha,
            next_action="Fix F-001 third time",
        )
        loaded3 = workflow.load_state(task_dir)
        self.assertEqual(loaded3["stage"], workflow.STAGE_WAITING_FIX_OR_DIAGNOSIS)
        self.assertTrue(loaded3["is_diagnosis"])
        self.assertEqual(loaded3["consecutive_no_improvement"], 2)
        self.assertIn("diagnosis", loaded3["next_action"].lower())

    def test_limit_exceeded_pause_and_resolve(self):
        """12. Repair rounds exceeding default maximum (5) pauses the workflow; resolve unpauses it."""
        state = self._start_task(task_id="limit-task")
        task_dir = self.project_dir / "handoffs" / "limit-task"

        # Negative check: planning stage must reject CHANGES_REQUESTED
        with self.assertRaises(workflow.WorkflowError) as cm_plan:
            workflow.apply_review_decision(
                task_dir=task_dir,
                decision="CHANGES_REQUESTED",
                findings=[{"id": "F-001", "description": "Issue 1"}],
                reviewed_sha=self.initial_sha,
                next_action="Fix 1",
            )
        self.assertIn("not allowed during planning stage", str(cm_plan.exception))

        # Legitimately advance from WAITING_PLAN -> WAITING_EXECUTION -> WAITING_REVIEW via public APIs
        workflow.apply_review_decision(
            task_dir=task_dir,
            decision="PLAN_READY",
            findings=[],
            reviewed_sha=self.initial_sha,
            next_action="Implement initial plan",
        )
        workflow.record_work(
            project=self.project_dir,
            task_id="limit-task",
            head_sha=self.initial_sha,
            test_evidence=self._make_evidence(sha=self.initial_sha),
        )

        for r in range(1, 6):
            workflow.apply_review_decision(
                task_dir=task_dir,
                decision="CHANGES_REQUESTED",
                findings=[{"id": f"F-00{r}", "description": f"Issue {r}"}],
                reviewed_sha=self.initial_sha,
                next_action=f"Fix {r}",
            )
            loaded = workflow.load_state(task_dir)
            if r < 5:
                self.assertEqual(loaded["stage"], workflow.STAGE_WAITING_FIX_OR_DIAGNOSIS)
                self.assertEqual(loaded["business_stage"], workflow.STAGE_WAITING_FIX_OR_DIAGNOSIS)
                workflow.record_work(
                    project=self.project_dir,
                    task_id="limit-task",
                    head_sha=self.initial_sha,
                    test_evidence=self._make_evidence(sha=self.initial_sha),
                )
            else:
                self.assertEqual(loaded["stage"], workflow.STAGE_PAUSED_LIMIT_EXCEEDED)
                self.assertEqual(loaded["business_stage"], workflow.STAGE_PAUSED_LIMIT_EXCEEDED)
                self.assertIn("exceeded", loaded["next_action"].lower())

        # While paused, record-work is blocked
        with self.assertRaises(workflow.WorkflowError):
            workflow.record_work(
                project=self.project_dir,
                task_id="limit-task",
                test_evidence=self._make_evidence(),
            )

        # While paused, exchange is blocked
        res_ex = workflow.execute_exchange(task_dir=task_dir, _test_transport_response="{}")
        self.assertEqual(res_ex["status"], "BLOCKED")

        # Human user explicit resolve unpauses workflow
        unpaused = workflow.resolve_limit_exceeded(
            project=self.project_dir,
            decision="CONTINUE",
            task_id="limit-task",
            notes="User authorized repair continuation",
        )
        self.assertEqual(unpaused["stage"], workflow.STAGE_WAITING_FIX_OR_DIAGNOSIS)
        self.assertEqual(unpaused["business_stage"], workflow.STAGE_WAITING_FIX_OR_DIAGNOSIS)
        self.assertEqual(unpaused["total_repair_rounds"], 0)

        # Verify that after resolve(CONTINUE), record_work genuinely succeeds via public API
        resumed_work = workflow.record_work(
            project=self.project_dir,
            task_id="limit-task",
            head_sha=self.initial_sha,
            test_evidence=self._make_evidence(sha=self.initial_sha),
        )
        self.assertEqual(resumed_work["stage"], workflow.STAGE_WAITING_REVIEW)
        self.assertEqual(resumed_work["business_stage"], workflow.STAGE_WAITING_REVIEW)

    def test_missing_tests_cannot_be_done(self):
        """13. Attempting to accept/done without recorded test evidence fails closed."""
        state = self._start_task(task_id="notest-task")
        task_dir = self.project_dir / "handoffs" / "notest-task"

        with self.assertRaises(workflow.WorkflowError):
            workflow.apply_review_decision(
                task_dir=task_dir,
                decision="ACCEPTED",
                findings=[],
                reviewed_sha=self.initial_sha,
                next_action="Finish",
            )
        loaded = workflow.load_state(task_dir)
        self.assertNotEqual(loaded["stage"], workflow.STAGE_WAITING_ACCEPTANCE_DONE)

    def test_record_work_rejected_in_waiting_plan_stage(self):
        """14. P1: Calling record-work directly after start (WAITING_PLAN) is strictly forbidden."""
        state = self._start_task(task_id="skip-plan-task")
        with self.assertRaises(workflow.WorkflowError) as cm:
            workflow.record_work(
                project=self.project_dir,
                task_id="skip-plan-task",
                test_evidence=self._make_evidence(),
            )
        self.assertIn("WAITING_PLAN", str(cm.exception))

    def test_record_work_rejects_plain_string_pass_and_requires_structure(self):
        """15. P1: Plain string 'PASS' is rejected; structured test evidence is strictly enforced."""
        state = self._start_task(task_id="evidence-task")
        self._advance_to_execution("evidence-task")

        # Case A1: Plain raw string "PASS" without file
        with self.assertRaises(workflow.WorkflowError):
            workflow.record_work(
                project=self.project_dir,
                task_id="evidence-task",
                test_evidence="PASS",
            )

        # Case A2: File containing plain string "PASS"
        pass_file = self.project_dir / "handoffs" / "pass_evidence.txt"
        pass_file.write_text("PASS\n", encoding="utf-8")
        with self.assertRaises(workflow.WorkflowError) as cm:
            workflow.record_work(
                project=self.project_dir,
                task_id="evidence-task",
                test_evidence=str(pass_file),
            )
        self.assertIn("Plain string 'PASS' is rejected", str(cm.exception))

        # Case B: Exit code non-zero in file
        bad_file = self.project_dir / "handoffs" / "bad_evidence.json"
        bad_file.write_text(json.dumps({
            "command": "python -m unittest",
            "exit_code": 1,
            "tested_sha": self.initial_sha,
        }), encoding="utf-8")
        with self.assertRaises(workflow.WorkflowError) as cm:
            workflow.record_work(
                project=self.project_dir,
                task_id="evidence-task",
                test_evidence=str(bad_file),
            )
        self.assertIn("exit_code must be 0", str(cm.exception))

        # Case C: Valid structured evidence succeeds
        good_evidence = self._make_evidence()
        rec = workflow.record_work(
            project=self.project_dir,
            task_id="evidence-task",
            test_evidence=good_evidence,
        )
        self.assertEqual(rec["stage"], workflow.STAGE_WAITING_REVIEW)
        self.assertTrue(rec["test_evidence_recorded"])

    def test_working_tree_uncommitted_modifications_fail_closed(self):
        """16. P1: Uncommitted modifications in git working tree fail closed."""
        state = self._start_task(task_id="dirty-tree-task")
        task_dir = self.project_dir / "handoffs" / "dirty-tree-task"
        self._advance_to_execution("dirty-tree-task")

        # Modify tracked file without committing
        (self.project_dir / "README.md").write_text("# Modified\n", encoding="utf-8")

        with self.assertRaises(workflow.WorkflowError) as cm:
            workflow.record_work(
                project=self.project_dir,
                task_id="dirty-tree-task",
                test_evidence=self._make_evidence(),
            )
        err_text = str(cm.exception)
        self.assertIn("BLOCKED_SOURCE_CHANGED", err_text)
        self.assertIn("M README.md", err_text)
        self.assertIn(self.initial_sha, err_text)

        # Ensure safety gate did not advance stage or record test evidence
        loaded_dirty = workflow.load_state(task_dir)
        self.assertEqual(loaded_dirty["stage"], workflow.STAGE_WAITING_EXECUTION)
        self.assertFalse(loaded_dirty["test_evidence_recorded"])

        # Clean it up and verify record_work succeeds once working tree matches HEAD
        subprocess.run(["git", "checkout", "--", "README.md"], cwd=self.project_dir, check=True, capture_output=True)
        rec_clean = workflow.record_work(
            project=self.project_dir,
            task_id="dirty-tree-task",
            test_evidence=self._make_evidence(),
        )
        self.assertEqual(rec_clean["stage"], workflow.STAGE_WAITING_REVIEW)
        self.assertTrue(rec_clean["test_evidence_recorded"])

    def test_plan_phase_rejects_accepted_decision(self):
        """17. P1: Plan phase only accepts PLAN_READY, NEEDS_INPUT, BLOCKED; rejects ACCEPTED."""
        resp_accepted = f"""Plan response:
```json:chatgpt-review
{{
  "task_id": "plan-acc-task",
  "round": 1,
  "reviewed_head_sha": "{self.initial_sha}",
  "decision": "ACCEPTED",
  "findings": [],
  "next_action": "Done"
}}
```"""
        parsed = workflow.parse_and_validate_review_response(
            response_text=resp_accepted,
            expected_task_id="plan-acc-task",
            expected_round=1,
            expected_head_sha=self.initial_sha,
            expected_stage=workflow.STAGE_WAITING_PLAN,
        )
        self.assertFalse(parsed["valid"])
        self.assertIn("not allowed during planning stage", parsed["reason"])

    def test_strict_feedback_parsing_rules(self):
        """18. P2: Rejects multiple ambiguous blocks, bad round format, and empty findings on changes."""
        # Ambiguous 2 blocks
        resp_ambiguous = """```json:chatgpt-review
{"task_id": "p2-task", "round": 1, "reviewed_head_sha": "abc", "decision": "ACCEPTED", "findings": [], "next_action": "1"}
```
```json:chatgpt-review
{"task_id": "p2-task", "round": 1, "reviewed_head_sha": "abc", "decision": "BLOCKED", "findings": [], "next_action": "2"}
```"""
        parsed_amb = workflow.parse_and_validate_review_response(
            response_text=resp_ambiguous,
            expected_task_id="p2-task",
            expected_round=1,
            expected_head_sha="abc",
        )
        self.assertFalse(parsed_amb["valid"])
        self.assertEqual(parsed_amb["status"], "BLOCKED_AMBIGUOUS_FEEDBACK")

        # Bad round
        resp_bad_round = f"""```json:chatgpt-review
{{"task_id": "p2-task", "round": "bad", "reviewed_head_sha": "{self.initial_sha}", "decision": "ACCEPTED", "findings": [], "next_action": "Done"}}
```"""
        parsed_rnd = workflow.parse_and_validate_review_response(
            response_text=resp_bad_round,
            expected_task_id="p2-task",
            expected_round=1,
            expected_head_sha=self.initial_sha,
        )
        self.assertFalse(parsed_rnd["valid"])
        self.assertEqual(parsed_rnd["status"], "BLOCKED_MALFORMED_ROUND")

        # CHANGES_REQUESTED with empty findings
        resp_empty_find = f"""```json:chatgpt-review
{{"task_id": "p2-task", "round": 1, "reviewed_head_sha": "{self.initial_sha}", "decision": "CHANGES_REQUESTED", "findings": [], "next_action": "Fix"}}
```"""
        parsed_f = workflow.parse_and_validate_review_response(
            response_text=resp_empty_find,
            expected_task_id="p2-task",
            expected_round=1,
            expected_head_sha=self.initial_sha,
        )
        self.assertFalse(parsed_f["valid"])
        self.assertEqual(parsed_f["status"], "BLOCKED_EMPTY_FINDINGS")

    def test_cross_process_task_lock_mutual_exclusion(self):
        """19. P2: TaskLock guarantees cross-process single-writer mutual exclusion with real independent subprocess."""
        state = self._start_task(task_id="lock-task")
        task_dir = self.project_dir / "handoffs" / "lock-task"

        # Process 1: Acquire lock in an independent subprocess and hold it for 2 seconds
        script_code = (
            "import sys, time\n"
            "from pathlib import Path\n"
            "import importlib.util\n"
            f"spec = importlib.util.spec_from_file_location('workflow', r'{WORKFLOW_SCRIPT}')\n"
            "wf = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(wf)\n"
            f"task_dir = Path(r'{task_dir}')\n"
            "lock = wf.TaskLock(task_dir)\n"
            "lock.acquire()\n"
            "print('LOCKED', flush=True)\n"
            "time.sleep(2)\n"
            "lock.release()\n"
            "print('RELEASED', flush=True)\n"
        )
        p1 = subprocess.Popen([sys.executable, "-c", script_code], stdout=subprocess.PIPE, text=True)
        try:
            line = p1.stdout.readline().strip()
            self.assertEqual(line, "LOCKED")

            # Process 2: Second process attempts CLI command, must get busy (code 5)
            p2 = subprocess.run(
                [sys.executable, str(WORKFLOW_SCRIPT), "status", "--project", str(self.project_dir), "--task-id", "lock-task"],
                capture_output=True,
                text=True,
            )
            self.assertEqual(p2.returncode, 5)
            self.assertIn("Workflow busy", p2.stderr)
        finally:
            p1.wait(timeout=10)
            self.assertEqual(p1.returncode, 0)

        # Process 3: After release, status succeeds
        p3 = subprocess.run(
            [sys.executable, str(WORKFLOW_SCRIPT), "status", "--project", str(self.project_dir), "--task-id", "lock-task"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(p3.returncode, 0)

    def test_production_mock_forbidden_cannot_accept(self):
        """20. P1: Production CLI has no --mock-response, and simulated responses cannot reach ACCEPTED_DONE."""
        state = self._start_task(task_id="mock-task")
        task_dir = self.project_dir / "handoffs" / "mock-task"
        self._advance_to_execution("mock-task")

        workflow.record_work(
            project=self.project_dir,
            task_id="mock-task",
            head_sha=self.initial_sha,
            test_evidence=self._make_evidence(),
        )

        # Internal test simulation returns ACCEPTED
        resp_accepted = f"""```json:chatgpt-review
{{
  "task_id": "mock-task",
  "round": 1,
  "reviewed_head_sha": "{self.initial_sha}",
  "decision": "ACCEPTED",
  "findings": [],
  "next_action": "Simulated accept"
}}
```"""
        res = workflow.execute_exchange(
            task_dir=task_dir,
            _test_transport_response=resp_accepted,
        )
        loaded = workflow.load_state(task_dir)
        self.assertEqual(loaded["stage"], workflow.STAGE_SIMULATED_TEST_PASSED)
        self.assertNotEqual(loaded["stage"], workflow.STAGE_WAITING_ACCEPTANCE_DONE)
        self.assertTrue(loaded.get("is_simulated_test"))

    def test_material_reviewability_includes_brief_history_and_no_default_accepted(self):
        """21. P1: Materials include task brief, git history, and template does not default to ACCEPTED."""
        state = self._start_task(task_id="mat-task")
        task_dir = self.project_dir / "handoffs" / "mat-task"

        plan_msg = workflow.build_plan_request_message(state, task_dir, "req-1", self.project_dir)
        self.assertIn("TASK BRIEF", plan_msg.upper())
        self.assertIn(self.initial_sha, plan_msg)
        self.assertNotIn('"decision": "ACCEPTED"', plan_msg)

        self._advance_to_execution("mat-task")
        workflow.record_work(
            project=self.project_dir,
            task_id="mat-task",
            head_sha=self.initial_sha,
            test_evidence=self._make_evidence(),
        )
        loaded = workflow.load_state(task_dir)
        review_msg = workflow.build_review_request_message(loaded, task_dir, "req-2", self.project_dir)
        self.assertIn("TASK BRIEF", review_msg.upper())
        self.assertIn(self.initial_sha, review_msg)
        self.assertIn("Verified Test Evidence", review_msg)
        self.assertNotIn('"decision": "ACCEPTED"', review_msg)

    def test_plan_blocked_rejects_record_work(self):
        """22. FIX2: When planning is blocked, calling record-work must be strictly rejected."""
        state = self._start_task(task_id="blocked-plan-task")
        task_dir = self.project_dir / "handoffs" / "blocked-plan-task"

        workflow.apply_review_decision(
            task_dir=task_dir,
            decision="BLOCKED",
            findings=[],
            reviewed_sha=self.initial_sha,
            next_action="Clarify architecture",
        )
        loaded = workflow.load_state(task_dir)
        self.assertEqual(loaded["stage"], workflow.STAGE_BLOCKED)
        self.assertEqual(loaded["business_stage"], workflow.STAGE_WAITING_PLAN)

        # Attempt record-work while blocked from planning -> rejected
        with self.assertRaises(workflow.WorkflowError) as cm:
            workflow.record_work(
                project=self.project_dir,
                task_id="blocked-plan-task",
                test_evidence=self._make_evidence(),
            )
        self.assertIn("WAITING_PLAN", str(cm.exception))

    def test_review_blocked_rejects_plan_ready(self):
        """23. FIX2: When in review or review is blocked, applying decision PLAN_READY is rejected."""
        state = self._start_task(task_id="blocked-rev-task")
        task_dir = self.project_dir / "handoffs" / "blocked-rev-task"
        self._advance_to_execution("blocked-rev-task")
        workflow.record_work(
            project=self.project_dir,
            task_id="blocked-rev-task",
            head_sha=self.initial_sha,
            test_evidence=self._make_evidence(),
        )

        workflow.apply_review_decision(
            task_dir=task_dir,
            decision="BLOCKED",
            findings=[],
            reviewed_sha=self.initial_sha,
            next_action="Reviewer blocked",
        )
        loaded = workflow.load_state(task_dir)
        self.assertEqual(loaded["stage"], workflow.STAGE_BLOCKED)
        self.assertEqual(loaded["business_stage"], workflow.STAGE_WAITING_REVIEW)

        # Applying PLAN_READY in review must be rejected
        with self.assertRaises(workflow.WorkflowError) as cm:
            workflow.apply_review_decision(
                task_dir=task_dir,
                decision="PLAN_READY",
                findings=[],
                reviewed_sha=self.initial_sha,
                next_action="Plan ready",
            )
        self.assertIn("not allowed during review stage", str(cm.exception))

    def test_request_sent_head_changed_rejects_accepted(self):
        """24. FIX2: If HEAD commits or moves after review request was sent, ACCEPTED is rejected with BLOCKED_SOURCE_CHANGED."""
        state = self._start_task(task_id="head-drift-task")
        task_dir = self.project_dir / "handoffs" / "head-drift-task"
        self._advance_to_execution("head-drift-task")
        workflow.record_work(
            project=self.project_dir,
            task_id="head-drift-task",
            head_sha=self.initial_sha,
            test_evidence=self._make_evidence(),
        )

        # Simulate git HEAD moves before review decision is applied
        subprocess.run(["git", "commit", "--allow-empty", "-m", "Drift commit"], cwd=self.project_dir, check=True, capture_output=True)

        with self.assertRaises(workflow.WorkflowError) as cm:
            workflow.apply_review_decision(
                task_dir=task_dir,
                decision="ACCEPTED",
                findings=[],
                reviewed_sha=self.initial_sha,
                next_action="Acceptance attempt",
            )
        self.assertIn("BLOCKED_SOURCE_CHANGED", str(cm.exception))

    def test_accepted_task_untracked_source_added_rejects_done(self):
        """25. FIX2: A previously ACCEPTED task with untracked source file added cannot remain completed."""
        state = self._start_task(task_id="untracked-drift-task")
        task_dir = self.project_dir / "handoffs" / "untracked-drift-task"
        self._advance_to_execution("untracked-drift-task")
        workflow.record_work(
            project=self.project_dir,
            task_id="untracked-drift-task",
            head_sha=self.initial_sha,
            test_evidence=self._make_evidence(),
        )
        workflow.apply_review_decision(
            task_dir=task_dir,
            decision="ACCEPTED",
            findings=[],
            reviewed_sha=self.initial_sha,
            next_action="Completed",
        )
        st = workflow.load_state(task_dir)
        self.assertEqual(st["stage"], workflow.STAGE_WAITING_ACCEPTANCE_DONE)

        # Add an untracked source file to working tree
        (self.project_dir / "sneaky_new_source.py").write_text("print('drift')\n", encoding="utf-8")

        # execute_exchange must detect source drift and transition to BLOCKED
        res = workflow.execute_exchange(task_dir=task_dir)
        self.assertEqual(res["status"], "BLOCKED")
        self.assertIn("BLOCKED_SOURCE_CHANGED", res["reason"])

        loaded = workflow.load_state(task_dir)
        self.assertEqual(loaded["stage"], workflow.STAGE_BLOCKED)

    def test_first_plan_unknown_delivery_retains_id_and_round(self):
        """26. FIX2: First plan exchange with unknown delivery preserves r1-plan and round 1, never reverting to r0-plan."""
        state = self._start_task(task_id="tx-plan-task")
        task_dir = self.project_dir / "handoffs" / "tx-plan-task"

        req_id = "req-tx-plan-task-r1-plan"
        msg = workflow.build_plan_request_message(state, task_dir, req_id, self.project_dir)
        req_hash = workflow.compute_hash(msg)
        st = workflow.load_state(task_dir)
        st["active_transaction"] = {
            "request_id": req_id,
            "kind": "plan",
            "round": 1,
            "head_sha": self.initial_sha,
            "message": msg,
            "message_hash": req_hash,
            "chat_url": None,
            "tab_id": None,
            "model": "o3-mini",
            "thinking_mode": "high",
            "delivery_status": "DELIVERY_UNKNOWN",
            "created_at": workflow.iso_now(),
            "applied": False,
        }
        st["stage"] = workflow.STAGE_BLOCKED
        st["blocked_from_stage"] = workflow.STAGE_WAITING_PLAN
        workflow.save_state(task_dir, st)

        res = workflow.execute_exchange(task_dir=task_dir, dry_run=True)
        self.assertEqual(res["status"], "DRY_RUN")
        self.assertEqual(res["request_id"], "req-tx-plan-task-r1-plan")
        self.assertEqual(res["model"], "o3-mini")
        self.assertEqual(res["thinking_mode"], "high")

    def test_first_send_confirmed_timeout_retains_chat_url_and_skips_resend(self):
        """27. FIX2: Sending confirmed via callback persists /c/id immediately; timeout retains URL and skips resend on recovery."""
        state = self._start_task(task_id="timeout-chat-task")
        task_dir = self.project_dir / "handoffs" / "timeout-chat-task"

        st = workflow.load_state(task_dir)
        st["active_transaction"] = {
            "request_id": "req-timeout-chat-task-r1-plan",
            "kind": "plan",
            "round": 1,
            "head_sha": self.initial_sha,
            "message": "test plan",
            "message_hash": workflow.compute_hash("test plan"),
            "chat_url": "https://chatgpt.com/c/chat-67890",
            "delivery_status": "CONFIRMED_USER_TURN",
            "created_at": workflow.iso_now(),
            "applied": False,
        }
        st["chat_url"] = "https://chatgpt.com/c/chat-67890"
        st["stage"] = workflow.STAGE_BLOCKED
        st["blocked_from_stage"] = workflow.STAGE_WAITING_PLAN
        workflow.save_state(task_dir, st)

        resp = f"""```json:chatgpt-review
{{
  "task_id": "timeout-chat-task",
  "round": 1,
  "reviewed_head_sha": "{self.initial_sha}",
  "decision": "PLAN_READY",
  "findings": [],
  "next_action": "Proceed with plan"
}}
```"""
        res = workflow.execute_exchange(task_dir=task_dir, _test_transport_response=resp)
        self.assertEqual(res["status"], "SUCCESS")
        self.assertEqual(res["decision"], "PLAN_READY")

        updated = workflow.load_state(task_dir)
        self.assertEqual(updated["chat_url"], "https://chatgpt.com/c/chat-67890")
        self.assertEqual(updated["stage"], workflow.STAGE_WAITING_EXECUTION)

    def test_blocked_materials_when_no_repo_or_pr_url(self):
        """28. FIX2: Absence of public safe repo URL or PR URL fails closed with BLOCKED_MATERIALS before sending."""
        state = self._start_task(task_id="materials-task")
        task_dir = self.project_dir / "handoffs" / "materials-task"

        with mock.patch("workflow.BrowserRelay") as mock_relay_cls:
            res = workflow.execute_exchange(task_dir=task_dir)
            self.assertEqual(res["status"], "BLOCKED")
            self.assertIn("BLOCKED_MATERIALS", res["reason"])
            mock_relay_cls.assert_not_called()

        st = workflow.load_state(task_dir)
        self.assertEqual(st["stage"], workflow.STAGE_BLOCKED)

    def test_dry_run_does_not_pollute_formal_send_transaction(self):
        """29. F-001 & F-004: dry_run is strictly read-only, does not write SEND_ATTEMPTED, and allows subsequent formal send."""
        workflow.start_workflow(
            project=self.project_dir,
            title="Dry Run Formal Send Task",
            task_id="dry-run-task",
            repo_url="https://github.com/example/repo",
            model="o3-mini",
            thinking_mode="high",
        )
        task_dir = self.project_dir / "handoffs" / "dry-run-task"

        # 1. Read-only dry_run preview on a fresh task
        preview = workflow.execute_exchange(task_dir=task_dir, dry_run=True)
        self.assertEqual(preview["status"], "DRY_RUN")
        self.assertEqual(preview["request_id"], "req-dry-run-task-r1-plan")
        self.assertEqual(preview["model"], "o3-mini")
        self.assertEqual(preview["thinking_mode"], "high")

        # Must not persist active_transaction or SEND_ATTEMPTED
        after_preview = workflow.load_state(task_dir)
        self.assertIsNone(after_preview["active_transaction"])
        self.assertEqual(after_preview["stage"], workflow.STAGE_WAITING_PLAN)
        self.assertEqual(after_preview["round"], 0)
        self.assertFalse((task_dir / "logs" / "req-dry-run-task-r1-plan.request.md").exists())

        # 2. Subsequent formal execution must genuinely send (skip_send=False, is_recovery=False)
        plan_resp = f"""```json:chatgpt-review
{{
  "task_id": "dry-run-task",
  "round": 1,
  "reviewed_head_sha": "{self.initial_sha}",
  "decision": "PLAN_READY",
  "findings": [],
  "next_action": "Implement plan"
}}
```"""
        with mock.patch("workflow.BrowserRelay") as MockRelay:
            instance = MockRelay.return_value
            instance.check_status.return_value = mock.Mock(daemon_running=True, connected_browser_id="b-1")
            instance.run_e2e_review.return_value = {
                "status": "SUCCESS",
                "response_text": plan_resp,
                "chat_url": "https://chatgpt.com/c/dry-run-formal",
                "tab_id": "tab-1",
            }
            formal = workflow.execute_exchange(
                task_dir=task_dir,
                chat_url="https://chatgpt.com/c/dry-run-formal",
            )
            self.assertEqual(formal["status"], "SUCCESS")
            self.assertEqual(formal["decision"], "PLAN_READY")
            instance.run_e2e_review.assert_called_once()
            call_kwargs = instance.run_e2e_review.call_args.kwargs
            self.assertFalse(call_kwargs["skip_send"])
            self.assertFalse(call_kwargs["is_recovery"])

        after_formal = workflow.load_state(task_dir)
        self.assertEqual(after_formal["stage"], workflow.STAGE_WAITING_EXECUTION)
        self.assertTrue(after_formal["active_transaction"]["applied"])

    def test_malformed_transaction_and_identity_conflict_fail_closed(self):
        """30. F-002: Missing transaction fields, non-dict transaction, identity mismatch, or hash collision fail closed with DeliveryError."""
        state = workflow.start_workflow(
            project=self.project_dir,
            title="Malformed Tx Task",
            task_id="malformed-tx-task",
            repo_url="https://github.com/example/repo",
        )
        task_dir = self.project_dir / "handoffs" / "malformed-tx-task"
        req_id = "req-malformed-tx-task-r1-plan"
        msg = workflow.build_plan_request_message(state, task_dir, req_id, self.project_dir)
        req_hash = workflow.compute_hash(msg)

        valid_tx = {
            "request_id": req_id,
            "kind": "plan",
            "round": 1,
            "head_sha": self.initial_sha,
            "message": msg,
            "message_hash": req_hash,
            "chat_url": "https://chatgpt.com/c/tx-test",
            "delivery_status": "DELIVERY_UNKNOWN",
            "created_at": workflow.iso_now(),
            "applied": False,
        }

        malformed_cases = [
            "not-a-dict",
            {k: v for k, v in valid_tx.items() if k != "message"},
            {k: v for k, v in valid_tx.items() if k != "head_sha"},
            {k: v for k, v in valid_tx.items() if k != "message_hash"},
            {k: v for k, v in valid_tx.items() if k != "applied"},
            {**valid_tx, "round": 1.5},
            {**valid_tx, "round": True},
            {**valid_tx, "round": "1"},
            {**valid_tx, "applied": "false"},
            {"request_id": "wrong", "applied": "false"},
            {**valid_tx, "request_id": "req-malformed-tx-task-r2-plan"},
            {**valid_tx, "round": 2},
            {**valid_tx, "kind": "review"},
            {**valid_tx, "head_sha": "0" * 40},
            {**valid_tx, "delivery_status": "INVALID_STATUS"},
            {**valid_tx, "message_hash": workflow.compute_hash(msg + " tampered")},
        ]

        for bad_tx in malformed_cases:
            st = workflow.load_state(task_dir)
            st["stage"] = workflow.STAGE_WAITING_PLAN
            st["business_stage"] = workflow.STAGE_WAITING_PLAN
            st["active_transaction"] = bad_tx
            workflow.save_state(task_dir, st)

            with self.assertRaises(workflow.DeliveryError):
                workflow.execute_exchange(
                    task_dir=task_dir,
                    chat_url="https://chatgpt.com/c/tx-test",
                    _test_transport_response="{}",
                )

    def test_round_strict_int_and_applied_strict_bool_and_historical_tx_integrity(self):
        """31. F-002.1 & F-002.2: round must be strict int, applied must be strict bool, and any existing transaction (including applied=True) is validated fail-closed without transport calls."""
        state = workflow.start_workflow(
            project=self.project_dir,
            title="Strict Round and Applied Task",
            task_id="strict-tx-task",
            repo_url="https://github.com/example/repo",
        )
        task_dir = self.project_dir / "handoffs" / "strict-tx-task"
        req_id = "req-strict-tx-task-r1-plan"
        msg = workflow.build_plan_request_message(state, task_dir, req_id, self.project_dir)
        req_hash = workflow.compute_hash(msg)

        valid_unapplied_tx = {
            "request_id": req_id,
            "kind": "plan",
            "round": 1,
            "head_sha": self.initial_sha,
            "message": msg,
            "message_hash": req_hash,
            "chat_url": "https://chatgpt.com/c/strict-tx",
            "delivery_status": "CONFIRMED_USER_TURN",
            "created_at": workflow.iso_now(),
            "applied": False,
        }

        invalid_round_and_applied_cases = [
            {**valid_unapplied_tx, "round": 1.5},
            {**valid_unapplied_tx, "round": True},
            {**valid_unapplied_tx, "round": False},
            {**valid_unapplied_tx, "round": "1"},
            {**valid_unapplied_tx, "round": 0},
            {**valid_unapplied_tx, "applied": "false"},
            {**valid_unapplied_tx, "applied": "true"},
            {**valid_unapplied_tx, "applied": 0},
            {**valid_unapplied_tx, "applied": 1},
            {**valid_unapplied_tx, "applied": None},
            {k: v for k, v in valid_unapplied_tx.items() if k != "applied"},
            {"request_id": "wrong", "applied": "false"},
            # Same-stage applied=True transaction in WAITING_PLAN must fail-closed to prevent duplicate send
            {**valid_unapplied_tx, "applied": True},
        ]

        for bad_tx in invalid_round_and_applied_cases:
            st = workflow.load_state(task_dir)
            st["stage"] = workflow.STAGE_WAITING_PLAN
            st["business_stage"] = workflow.STAGE_WAITING_PLAN
            st["active_transaction"] = bad_tx
            workflow.save_state(task_dir, st)

            with self.assertRaises(workflow.DeliveryError):
                workflow.execute_exchange(task_dir=task_dir, dry_run=True)

            with mock.patch("workflow.BrowserRelay") as mock_relay_cls:
                with self.assertRaises(workflow.DeliveryError):
                    workflow.execute_exchange(
                        task_dir=task_dir,
                        chat_url="https://chatgpt.com/c/strict-tx",
                    )
                mock_relay_cls.assert_not_called()

        # Advance task to WAITING_REVIEW (Round 1) with a historical applied=True plan transaction
        self._advance_to_execution("strict-tx-task")
        workflow.record_work(
            project=self.project_dir,
            task_id="strict-tx-task",
            head_sha=self.initial_sha,
            test_evidence=self._make_evidence(sha=self.initial_sha),
        )
        valid_applied_plan_tx = {
            **valid_unapplied_tx,
            "delivery_status": "CONFIRMED_USER_TURN",
            "applied": True,
        }

        # Malformed or tampered historical applied=True transactions must also fail-closed
        bad_historical_cases = [
            {**valid_applied_plan_tx, "message_hash": workflow.compute_hash(msg + " corrupted")},
            {k: v for k, v in valid_applied_plan_tx.items() if k != "message"},
            {k: v for k, v in valid_applied_plan_tx.items() if k != "head_sha"},
            {**valid_applied_plan_tx, "round": 1.5},
            {**valid_applied_plan_tx, "delivery_status": "SEND_ATTEMPTED"},
            {**valid_applied_plan_tx, "request_id": "req-strict-tx-task-r99-plan"},
        ]
        for bad_hist_tx in bad_historical_cases:
            st = workflow.load_state(task_dir)
            st["stage"] = workflow.STAGE_WAITING_REVIEW
            st["business_stage"] = workflow.STAGE_WAITING_REVIEW
            st["active_transaction"] = bad_hist_tx
            workflow.save_state(task_dir, st)

            with self.assertRaises(workflow.DeliveryError):
                workflow.execute_exchange(task_dir=task_dir, dry_run=True)

            with mock.patch("workflow.BrowserRelay") as mock_relay_cls:
                with self.assertRaises(workflow.DeliveryError):
                    workflow.execute_exchange(
                        task_dir=task_dir,
                        chat_url="https://chatgpt.com/c/strict-tx",
                    )
                mock_relay_cls.assert_not_called()

        # Valid historical applied=True plan transaction allows dry_run and subsequent formal review send
        st = workflow.load_state(task_dir)
        st["stage"] = workflow.STAGE_WAITING_REVIEW
        st["business_stage"] = workflow.STAGE_WAITING_REVIEW
        st["active_transaction"] = valid_applied_plan_tx
        workflow.save_state(task_dir, st)

        preview_rev = workflow.execute_exchange(task_dir=task_dir, dry_run=True)
        self.assertEqual(preview_rev["status"], "DRY_RUN")
        self.assertEqual(preview_rev["request_id"], "req-strict-tx-task-r1-review")
        self.assertTrue(workflow.load_state(task_dir)["active_transaction"]["applied"])
        self.assertEqual(
            workflow.load_state(task_dir)["active_transaction"]["request_id"],
            "req-strict-tx-task-r1-plan",
        )

        # Formal send is still protected by BLOCKED_MATERIALS when repo_url/pr_url are absent
        st_no_mat = workflow.load_state(task_dir)
        st_no_mat["repo_url"] = None
        st_no_mat["pr_url"] = None
        workflow.save_state(task_dir, st_no_mat)
        with mock.patch("workflow.BrowserRelay") as mock_relay_cls:
            blocked_res = workflow.execute_exchange(
                task_dir=task_dir,
                chat_url="https://chatgpt.com/c/strict-tx",
            )
            self.assertEqual(blocked_res["status"], "BLOCKED")
            self.assertIn("BLOCKED_MATERIALS", blocked_res["reason"])
            mock_relay_cls.assert_not_called()

        # Restore repo_url and verify formal send after dry_run succeeds with skip_send=False
        st_mat = workflow.load_state(task_dir)
        st_mat["stage"] = workflow.STAGE_WAITING_REVIEW
        st_mat["business_stage"] = workflow.STAGE_WAITING_REVIEW
        st_mat["repo_url"] = "https://github.com/example/repo"
        workflow.save_state(task_dir, st_mat)

        rev_resp = f"""```json:chatgpt-review
{{
  "task_id": "strict-tx-task",
  "round": 1,
  "reviewed_head_sha": "{self.initial_sha}",
  "decision": "ACCEPTED",
  "findings": [],
  "next_action": "All checks passed"
}}
```"""
        with mock.patch("workflow.BrowserRelay") as MockRelay:
            instance = MockRelay.return_value
            instance.check_status.return_value = mock.Mock(daemon_running=True, connected_browser_id="b-1")
            instance.run_e2e_review.return_value = {
                "status": "SUCCESS",
                "response_text": rev_resp,
                "chat_url": "https://chatgpt.com/c/strict-tx",
                "tab_id": "tab-1",
            }
            formal_rev = workflow.execute_exchange(
                task_dir=task_dir,
                chat_url="https://chatgpt.com/c/strict-tx",
            )
            self.assertEqual(formal_rev["status"], "SUCCESS")
            self.assertEqual(formal_rev["decision"], "ACCEPTED")
            call_kwargs = instance.run_e2e_review.call_args.kwargs
            self.assertFalse(call_kwargs["skip_send"])
            self.assertFalse(call_kwargs["is_recovery"])

    def test_status_workflow_lock_contention_json_exception_and_accepted_drift(self):
        """32. F-003: status and status --json enforce cross-process TaskLock (exit 5 when busy, 0 when released), release lock on exception, and block ACCEPTED tasks on source drift."""
        self._start_task(task_id="lock-full-task")
        task_dir = self.project_dir / "handoffs" / "lock-full-task"

        # 1. Real cross-process lock contention for both `status` and `status --json`
        holder_code = (
            "import sys\n"
            "from pathlib import Path\n"
            "import importlib.util\n"
            f"spec = importlib.util.spec_from_file_location('workflow', r'{WORKFLOW_SCRIPT}')\n"
            "wf = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(wf)\n"
            f"task_dir = Path(r'{task_dir}')\n"
            "with wf.TaskLock(task_dir):\n"
            "    print('LOCKED', flush=True)\n"
            "    sys.stdin.readline()\n"
            "print('RELEASED', flush=True)\n"
        )
        holder = subprocess.Popen(
            [sys.executable, "-c", holder_code],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            ready_line = holder.stdout.readline().strip()
            self.assertEqual(ready_line, "LOCKED")

            # While held, CLI `status` must return 5 with Workflow busy on stderr and no stdout snapshot
            busy_text = subprocess.run(
                [sys.executable, str(WORKFLOW_SCRIPT), "status", "--project", str(self.project_dir), "--task-id", "lock-full-task"],
                capture_output=True,
                text=True,
            )
            self.assertEqual(busy_text.returncode, 5)
            self.assertIn("Workflow busy", busy_text.stderr)
            self.assertEqual(busy_text.stdout.strip(), "")

            # While held, CLI `status --json` must return 5 with Workflow busy on stderr and no stdout snapshot
            busy_json = subprocess.run(
                [sys.executable, str(WORKFLOW_SCRIPT), "status", "--json", "--project", str(self.project_dir), "--task-id", "lock-full-task"],
                capture_output=True,
                text=True,
            )
            self.assertEqual(busy_json.returncode, 5)
            self.assertIn("Workflow busy", busy_json.stderr)
            self.assertEqual(busy_json.stdout.strip(), "")

            # Signal holder process to release lock
            holder.stdin.write("release\n")
            holder.stdin.flush()
            released_line = holder.stdout.readline().strip()
            self.assertEqual(released_line, "RELEASED")
        finally:
            if holder.stdin:
                holder.stdin.close()
            if holder.stdout:
                holder.stdout.close()
            if holder.stderr:
                holder.stderr.close()
            holder.wait(timeout=10)
            self.assertEqual(holder.returncode, 0)

        # 2. After lock release, both `status` and `status --json` return exit code 0
        ok_text = subprocess.run(
            [sys.executable, str(WORKFLOW_SCRIPT), "status", "--project", str(self.project_dir), "--task-id", "lock-full-task"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(ok_text.returncode, 0)
        self.assertIn("lock-full-task", ok_text.stdout)

        ok_json = subprocess.run(
            [sys.executable, str(WORKFLOW_SCRIPT), "status", "--json", "--project", str(self.project_dir), "--task-id", "lock-full-task"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(ok_json.returncode, 0)
        parsed_json = json.loads(ok_json.stdout)
        self.assertEqual(parsed_json["task_id"], "lock-full-task")
        self.assertEqual(parsed_json["stage"], workflow.STAGE_WAITING_PLAN)

        # 3. Exception inside status_workflow releases TaskLock cleanly (no lock leak)
        with mock.patch("workflow.load_state", side_effect=RuntimeError("Simulated internal failure")):
            with self.assertRaises(RuntimeError):
                workflow.status_workflow(self.project_dir, task_id="lock-full-task", as_json=True)

        # Also test corrupt workflow.json via CLI subprocess returning 1 and releasing the lock
        wf_file = task_dir / "workflow.json"
        original_wf_bytes = wf_file.read_bytes()
        try:
            wf_file.write_text("{corrupt-json", encoding="utf-8")
            err_proc = subprocess.run(
                [sys.executable, str(WORKFLOW_SCRIPT), "status", "--json", "--project", str(self.project_dir), "--task-id", "lock-full-task"],
                capture_output=True,
                text=True,
            )
            self.assertEqual(err_proc.returncode, 1)
        finally:
            wf_file.write_bytes(original_wf_bytes)

        # Independent subprocess can immediately run status --json after exception (lock is not leaked)
        after_exc = subprocess.run(
            [sys.executable, str(WORKFLOW_SCRIPT), "status", "--json", "--project", str(self.project_dir), "--task-id", "lock-full-task"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(after_exc.returncode, 0)

        # 4. ACCEPTED state with real source drift is intercepted and persisted as BLOCKED under lock
        self._advance_to_execution("lock-full-task")
        workflow.record_work(
            project=self.project_dir,
            task_id="lock-full-task",
            head_sha=self.initial_sha,
            test_evidence=self._make_evidence(sha=self.initial_sha),
        )
        workflow.apply_review_decision(
            task_dir=task_dir,
            decision="ACCEPTED",
            findings=[],
            reviewed_sha=self.initial_sha,
            next_action="Accepted clean state",
        )
        clean_acc = subprocess.run(
            [sys.executable, str(WORKFLOW_SCRIPT), "status", "--json", "--project", str(self.project_dir), "--task-id", "lock-full-task"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(clean_acc.returncode, 0)
        self.assertEqual(json.loads(clean_acc.stdout)["stage"], workflow.STAGE_WAITING_ACCEPTANCE_DONE)

        # Introduce real source drift in working tree
        drift_file = self.project_dir / "drifted_source.py"
        drift_file.write_text("x = 1\n", encoding="utf-8")

        drifted_status = subprocess.run(
            [sys.executable, str(WORKFLOW_SCRIPT), "status", "--json", "--project", str(self.project_dir), "--task-id", "lock-full-task"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(drifted_status.returncode, 0)
        drifted_payload = json.loads(drifted_status.stdout)
        self.assertEqual(drifted_payload["stage"], workflow.STAGE_BLOCKED)
        self.assertEqual(drifted_payload["blocked_from_stage"], workflow.STAGE_WAITING_ACCEPTANCE_DONE)
        self.assertIn("BLOCKED_SOURCE_CHANGED", drifted_payload["next_action"])

        # Persisted state on disk is also updated to STAGE_BLOCKED and lock is released
        persisted = workflow.load_state(task_dir)
        self.assertEqual(persisted["stage"], workflow.STAGE_BLOCKED)
        self.assertIn("BLOCKED_SOURCE_CHANGED", persisted["next_action"])

        drifted_text_status = subprocess.run(
            [sys.executable, str(WORKFLOW_SCRIPT), "status", "--project", str(self.project_dir), "--task-id", "lock-full-task"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(drifted_text_status.returncode, 0)
        self.assertIn("Stage:         BLOCKED", drifted_text_status.stdout)
        self.assertIn("BLOCKED_SOURCE_CHANGED", drifted_text_status.stdout)

    def test_dry_run_source_drift_read_only_and_formal_send_persists_blocked(self):
        """33. F-001 Option A: dry_run on source drift (untracked file or HEAD SHA drift) returns BLOCKED without mutating workflow.json, active_transaction, chat_url, or request files; formal send still persists BLOCKED."""
        workflow.start_workflow(
            project=self.project_dir,
            title="F-001 Option A Drift Read-Only Task",
            task_id="f001a-drift-task",
            repo_url="https://github.com/example/repo",
            chat_url="https://chatgpt.com/c/original-chat",
            model="o3-mini",
            thinking_mode="high",
        )
        task_dir = self.project_dir / "handoffs" / "f001a-drift-task"
        wf_file = task_dir / "workflow.json"
        req_file = task_dir / "logs" / "round-1-request.md"
        baseline_bytes = wf_file.read_bytes()

        # 1. dry_run + untracked source file -> returns BLOCKED, workflow.json bytes unchanged
        untracked = self.project_dir / "untracked_drift.py"
        untracked.write_text("drift = True\n", encoding="utf-8")
        try:
            with mock.patch("workflow.BrowserRelay") as mock_relay_cls:
                res_untracked = workflow.execute_exchange(
                    task_dir=task_dir,
                    chat_url="https://chatgpt.com/c/should-not-persist",
                    dry_run=True,
                )
                mock_relay_cls.assert_not_called()

            self.assertEqual(res_untracked["status"], "BLOCKED")
            self.assertIn("BLOCKED_SOURCE_CHANGED", res_untracked["reason"])
            self.assertIn("untracked_drift.py", res_untracked["reason"])
            self.assertEqual(wf_file.read_bytes(), baseline_bytes)
            st_after_untracked = workflow.load_state(task_dir)
            self.assertEqual(st_after_untracked["stage"], workflow.STAGE_WAITING_PLAN)
            self.assertIsNone(st_after_untracked["active_transaction"])
            self.assertEqual(st_after_untracked["chat_url"], "https://chatgpt.com/c/original-chat")
            self.assertFalse(req_file.exists())
        finally:
            if untracked.exists():
                untracked.unlink()

        # 2. dry_run + HEAD SHA drift -> returns BLOCKED, persisted state & bytes unchanged
        subprocess.run(
            ["git", "commit", "--allow-empty", "-m", "head drift commit"],
            cwd=self.project_dir,
            check=True,
            capture_output=True,
        )
        try:
            with mock.patch("workflow.BrowserRelay") as mock_relay_cls:
                res_head_drift = workflow.execute_exchange(
                    task_dir=task_dir,
                    chat_url="https://chatgpt.com/c/should-not-persist",
                    dry_run=True,
                )
                mock_relay_cls.assert_not_called()

            self.assertEqual(res_head_drift["status"], "BLOCKED")
            self.assertIn("BLOCKED_SOURCE_CHANGED", res_head_drift["reason"])
            self.assertIn("HEAD SHA drift detected", res_head_drift["reason"])
            self.assertEqual(wf_file.read_bytes(), baseline_bytes)
            st_after_head = workflow.load_state(task_dir)
            self.assertEqual(st_after_head["stage"], workflow.STAGE_WAITING_PLAN)
            self.assertIsNone(st_after_head["active_transaction"])
            self.assertEqual(st_after_head["chat_url"], "https://chatgpt.com/c/original-chat")
            self.assertFalse(req_file.exists())
        finally:
            subprocess.run(
                ["git", "reset", "--hard", self.initial_sha],
                cwd=self.project_dir,
                check=True,
                capture_output=True,
            )

        # 3. dry_run + clean valid source -> returns DRY_RUN without polluting transaction or files
        with mock.patch("workflow.BrowserRelay") as mock_relay_cls:
            res_clean = workflow.execute_exchange(
                task_dir=task_dir,
                chat_url="https://chatgpt.com/c/should-not-persist",
                dry_run=True,
            )
            mock_relay_cls.assert_not_called()

        self.assertEqual(res_clean["status"], "DRY_RUN")
        self.assertEqual(res_clean["request_id"], "req-f001a-drift-task-r1-plan")
        self.assertEqual(wf_file.read_bytes(), baseline_bytes)
        self.assertIsNone(workflow.load_state(task_dir)["active_transaction"])
        self.assertFalse(req_file.exists())

        # 4. Formal send (dry_run=False) + source drift -> persists BLOCKED fail-closed
        untracked.write_text("formal_drift = True\n", encoding="utf-8")
        try:
            with mock.patch("workflow.BrowserRelay") as mock_relay_cls:
                res_formal_drift = workflow.execute_exchange(
                    task_dir=task_dir,
                    chat_url="https://chatgpt.com/c/should-not-persist",
                    dry_run=False,
                )
                mock_relay_cls.assert_not_called()

            self.assertEqual(res_formal_drift["status"], "BLOCKED")
            self.assertIn("BLOCKED_SOURCE_CHANGED", res_formal_drift["reason"])
            self.assertNotEqual(wf_file.read_bytes(), baseline_bytes)
            st_formal = workflow.load_state(task_dir)
            self.assertEqual(st_formal["stage"], workflow.STAGE_BLOCKED)
            self.assertEqual(st_formal["blocked_from_stage"], workflow.STAGE_WAITING_PLAN)
            self.assertIn("BLOCKED_SOURCE_CHANGED", st_formal["next_action"])
            self.assertIsNone(st_formal["active_transaction"])
            self.assertEqual(st_formal["chat_url"], "https://chatgpt.com/c/original-chat")
            self.assertFalse(req_file.exists())
        finally:
            if untracked.exists():
                untracked.unlink()

    def test_source_drift_recovery_gates_and_transaction_invalidation(self):
        """34. R3P4-001: Controlled source-drift recovery invalidates unapplied old-SHA transactions with audit trail and rejects non-drift BLOCKED gates, stale-SHA re-recording, and duplicate recovery."""
        workflow.start_workflow(
            project=self.project_dir,
            title="R3P4-001 Drift Recovery Task",
            task_id="r3p4-drift-task",
            repo_url="https://github.com/example/repo",
            chat_url="https://chatgpt.com/c/r3p4-chat",
        )
        task_dir = self.project_dir / "handoffs" / "r3p4-drift-task"

        # 1. Advance to WAITING_EXECUTION via public execute_exchange (creates applied plan tx)
        plan_resp = f"""```json:chatgpt-review
{{
  "task_id": "r3p4-drift-task",
  "round": 1,
  "reviewed_head_sha": "{self.initial_sha}",
  "decision": "PLAN_READY",
  "findings": [],
  "next_action": "Implement feature"
}}
```"""
        res_plan = workflow.execute_exchange(task_dir=task_dir, _test_transport_response=plan_resp)
        self.assertEqual(res_plan["status"], "SUCCESS")
        self.assertEqual(workflow.load_state(task_dir)["stage"], workflow.STAGE_WAITING_EXECUTION)

        # 2. Record work on initial_sha -> enters WAITING_REVIEW (round 1)
        workflow.record_work(
            project=self.project_dir,
            task_id="r3p4-drift-task",
            head_sha=self.initial_sha,
            test_evidence=self._make_evidence(sha=self.initial_sha),
        )
        st_rev1 = workflow.load_state(task_dir)
        self.assertEqual(st_rev1["stage"], workflow.STAGE_WAITING_REVIEW)
        self.assertEqual(st_rev1["round"], 1)

        # 3. Simulate Round 1 review exchange failing with DELIVERY_UNKNOWN (leaves unapplied transaction for initial_sha)
        with mock.patch("workflow.BrowserRelay") as MockRelay1:
            inst1 = MockRelay1.return_value
            inst1.check_status.return_value = mock.Mock(daemon_running=True, connected_browser_id="b-1")
            inst1.run_e2e_review.return_value = {
                "status": "BLOCKED_DELIVERY_UNCONFIRMED",
                "error": "DOM confirmation timed out",
            }
            res_deliv = workflow.execute_exchange(task_dir=task_dir)
            self.assertEqual(res_deliv["status"], "BLOCKED")

        st_deliv_blocked = workflow.load_state(task_dir)
        self.assertEqual(st_deliv_blocked["stage"], workflow.STAGE_BLOCKED)
        self.assertEqual(st_deliv_blocked["business_stage"], workflow.STAGE_WAITING_REVIEW)
        self.assertEqual(st_deliv_blocked["active_transaction"]["request_id"], "req-r3p4-drift-task-r1-review")
        self.assertFalse(st_deliv_blocked["active_transaction"]["applied"])

        # 4. Non-source-drift BLOCKED state in WAITING_REVIEW must NOT allow record_work bypass
        with self.assertRaises(workflow.WorkflowError) as cm_non_drift:
            workflow.record_work(
                project=self.project_dir,
                task_id="r3p4-drift-task",
                head_sha=self.initial_sha,
                test_evidence=self._make_evidence(sha=self.initial_sha),
            )
        self.assertIn("WAITING_REVIEW", str(cm_non_drift.exception))

        # 4b. If BLOCKED_SOURCE_CHANGED occurs without a new commit (e.g. untracked file added then removed while HEAD is still initial_sha),
        # attempting to recover WAITING_REVIEW by re-recording the same stale initial_sha must fail closed
        temp_drift = self.project_dir / "temp_drift.py"
        temp_drift.write_text("x = 0\n", encoding="utf-8")
        try:
            res_temp_drift = workflow.execute_exchange(task_dir=task_dir)
            self.assertEqual(res_temp_drift["status"], "BLOCKED")
            self.assertIn("BLOCKED_SOURCE_CHANGED", res_temp_drift["reason"])
        finally:
            if temp_drift.exists():
                temp_drift.unlink()

        with self.assertRaises(workflow.WorkflowError) as cm_same_sha:
            workflow.record_work(
                project=self.project_dir,
                task_id="r3p4-drift-task",
                head_sha=self.initial_sha,
                test_evidence=self._make_evidence(sha=self.initial_sha),
            )
        self.assertIn("previously recorded stale head_sha", str(cm_same_sha.exception))

        # 5. Create new commit (new_sha) while unapplied Round 1 transaction exists
        (self.project_dir / "feature.py").write_text("def run():\n    return 42\n", encoding="utf-8")
        subprocess.run(["git", "add", "feature.py"], cwd=self.project_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "feature commit"], cwd=self.project_dir, check=True, capture_output=True)
        new_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.project_dir, check=True, capture_output=True, text=True
        ).stdout.strip()

        # Formal execute_exchange detects HEAD SHA drift, blocks with BLOCKED_SOURCE_CHANGED, and invalidates unapplied old-SHA tx
        with mock.patch("workflow.BrowserRelay") as mock_relay_cls:
            res_drift = workflow.execute_exchange(task_dir=task_dir)
            mock_relay_cls.assert_not_called()

        self.assertEqual(res_drift["status"], "BLOCKED")
        self.assertIn("BLOCKED_SOURCE_CHANGED", res_drift["reason"])

        st_drift = workflow.load_state(task_dir)
        self.assertEqual(st_drift["stage"], workflow.STAGE_BLOCKED)
        self.assertEqual(st_drift["blocked_from_stage"], workflow.STAGE_WAITING_REVIEW)
        self.assertIsNone(st_drift["active_transaction"])
        self.assertEqual(len(st_drift.get("invalidated_transactions", [])), 1)
        inv_tx = st_drift["invalidated_transactions"][0]
        self.assertEqual(inv_tx["request_id"], "req-r3p4-drift-task-r1-review")
        self.assertEqual(inv_tx["head_sha"], self.initial_sha)
        self.assertEqual(inv_tx["superseded_by_head_sha"], new_sha)
        self.assertIn("BLOCKED_SOURCE_CHANGED", inv_tx["invalidated_reason"])

        # 6. Recover via public record_work with new_sha and matching evidence
        workflow.record_work(
            project=self.project_dir,
            task_id="r3p4-drift-task",
            head_sha=new_sha,
            test_evidence=self._make_evidence(sha=new_sha),
        )
        st_recovered = workflow.load_state(task_dir)
        self.assertEqual(st_recovered["stage"], workflow.STAGE_WAITING_REVIEW)
        self.assertEqual(st_recovered["business_stage"], workflow.STAGE_WAITING_REVIEW)
        self.assertIsNone(st_recovered["blocked_from_stage"])
        self.assertEqual(st_recovered["head_sha"], new_sha)
        self.assertEqual(st_recovered["round"], 2)
        self.assertIsNone(st_recovered["active_transaction"])

        # 7. Next formal review exchange sends fresh Round 2 request (skip_send=False, is_recovery=False) bound to new_sha
        rev2_resp = f"""```json:chatgpt-review
{{
  "task_id": "r3p4-drift-task",
  "round": 2,
  "reviewed_head_sha": "{new_sha}",
  "decision": "ACCEPTED",
  "findings": [],
  "next_action": "All checks passed on new SHA"
}}
```"""
        with mock.patch("workflow.BrowserRelay") as MockRelay2:
            inst2 = MockRelay2.return_value
            inst2.check_status.return_value = mock.Mock(daemon_running=True, connected_browser_id="b-1")
            inst2.run_e2e_review.return_value = {
                "status": "SUCCESS",
                "response_text": rev2_resp,
                "chat_url": "https://chatgpt.com/c/r3p4-chat",
                "tab_id": "tab-1",
            }
            res_rev2 = workflow.execute_exchange(task_dir=task_dir)
            self.assertEqual(res_rev2["status"], "SUCCESS")
            self.assertEqual(res_rev2["decision"], "ACCEPTED")
            inst2.run_e2e_review.assert_called_once()
            rev2_kwargs = inst2.run_e2e_review.call_args.kwargs
            self.assertFalse(rev2_kwargs["skip_send"])
            self.assertFalse(rev2_kwargs["is_recovery"])
            self.assertEqual(rev2_kwargs["request_id"], "req-r3p4-drift-task-r2-review")

        # 8. Once ACCEPTED (or Sim-Accepted), source drift block must NOT allow record_work
        st_after_rev2 = workflow.load_state(task_dir)
        st_after_rev2["stage"] = workflow.STAGE_WAITING_ACCEPTANCE_DONE
        st_after_rev2["business_stage"] = workflow.STAGE_WAITING_ACCEPTANCE_DONE
        workflow.save_state(task_dir, st_after_rev2)

        post_acc_drift = self.project_dir / "post_acc_drift.py"
        post_acc_drift.write_text("y = 1\n", encoding="utf-8")
        try:
            res_acc_drift = workflow.execute_exchange(task_dir=task_dir)
            self.assertEqual(res_acc_drift["status"], "BLOCKED")
            self.assertIn("BLOCKED_SOURCE_CHANGED", res_acc_drift["reason"])
        finally:
            if post_acc_drift.exists():
                post_acc_drift.unlink()

        with self.assertRaises(workflow.WorkflowError) as cm_acc_rec:
            workflow.record_work(
                project=self.project_dir,
                task_id="r3p4-drift-task",
                head_sha=new_sha,
                test_evidence=self._make_evidence(sha=new_sha),
            )
        self.assertIn("already been ACCEPTED", str(cm_acc_rec.exception))

    def test_source_drift_with_corrupt_transaction_fails_closed(self):
        """35. R3P4-002: Stale transaction invalidation under source drift must validate fail-closed against corrupted transactions."""
        workflow.start_workflow(
            project=self.project_dir,
            title="R3P4-002 Corrupt Transaction Task",
            task_id="r3p4-corrupt-tx-task",
            repo_url="https://github.com/example/repo",
            chat_url="https://chatgpt.com/c/r3p4-corrupt",
        )
        task_dir = self.project_dir / "handoffs" / "r3p4-corrupt-tx-task"

        # Advance to WAITING_EXECUTION via public API
        plan_resp = f"""```json:chatgpt-review
{{
  "task_id": "r3p4-corrupt-tx-task",
  "round": 1,
  "reviewed_head_sha": "{self.initial_sha}",
  "decision": "PLAN_READY",
  "findings": [],
  "next_action": "Implement feature"
}}
```"""
        workflow.execute_exchange(task_dir=task_dir, _test_transport_response=plan_resp)

        # Record work on initial_sha -> enters WAITING_REVIEW (round 1)
        workflow.record_work(
            project=self.project_dir,
            task_id="r3p4-corrupt-tx-task",
            head_sha=self.initial_sha,
            test_evidence=self._make_evidence(sha=self.initial_sha),
        )

        st_rev = workflow.load_state(task_dir)
        self.assertEqual(st_rev["stage"], workflow.STAGE_WAITING_REVIEW)
        self.assertEqual(st_rev["round"], 1)

        # Build base valid unapplied review transaction
        req_id = "req-r3p4-corrupt-tx-task-r1-review"
        msg = workflow.build_review_request_message(st_rev, task_dir, req_id, self.project_dir)
        valid_unapplied_tx = {
            "request_id": req_id,
            "kind": "review",
            "round": 1,
            "head_sha": self.initial_sha,
            "message": msg,
            "message_hash": workflow.compute_hash(msg),
            "chat_url": "https://chatgpt.com/c/r3p4-corrupt",
            "delivery_status": "SEND_ATTEMPTED",
            "created_at": workflow.iso_now(),
            "applied": False,
        }

        # Build base valid applied plan transaction (historical)
        plan_req_id = "req-r3p4-corrupt-tx-task-r1-plan"
        plan_msg = workflow.build_plan_request_message(st_rev, task_dir, plan_req_id, self.project_dir)
        valid_applied_hist_tx = {
            "request_id": plan_req_id,
            "kind": "plan",
            "round": 1,
            "head_sha": self.initial_sha,
            "message": plan_msg,
            "message_hash": workflow.compute_hash(plan_msg),
            "chat_url": "https://chatgpt.com/c/r3p4-corrupt",
            "delivery_status": "CONFIRMED_USER_TURN",
            "created_at": workflow.iso_now(),
            "applied": True,
        }

        # Commit a new change to create real Git HEAD drift
        (self.project_dir / "drift_work.py").write_text("x = 100\n", encoding="utf-8")
        subprocess.run(["git", "add", "drift_work.py"], cwd=self.project_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "drift commit"], cwd=self.project_dir, check=True, capture_output=True)
        new_drift_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.project_dir, check=True, capture_output=True, text=True
        ).stdout.strip()
        self.assertNotEqual(new_drift_sha, self.initial_sha)

        # Corrupt test cases to verify fail-closed behavior under source drift
        corrupt_cases = [
            # 1. applied="false" (string instead of boolean - exact reviewer repro)
            {"request_id": "malformed", "head_sha": self.initial_sha, "applied": "false"},
            # 2. applied="true" (string instead of boolean)
            {**valid_unapplied_tx, "applied": "false"},
            {**valid_unapplied_tx, "applied": "true"},
            # 3. Missing required string fields
            {k: v for k, v in valid_unapplied_tx.items() if k != "message"},
            {k: v for k, v in valid_unapplied_tx.items() if k != "message_hash"},
            {k: v for k, v in valid_unapplied_tx.items() if k != "delivery_status"},
            # 4. Message hash mismatch / content collision
            {**valid_unapplied_tx, "message_hash": workflow.compute_hash("tampered content")},
            # 5. Invalid round types or values
            {**valid_unapplied_tx, "round": 1.5},
            {**valid_unapplied_tx, "round": True},
            {**valid_unapplied_tx, "round": "1"},
            {**valid_unapplied_tx, "round": 0},
            # 6. Invalid kind or request_id
            {**valid_unapplied_tx, "kind": "unknown"},
            {**valid_unapplied_tx, "request_id": "req-r3p4-corrupt-tx-task-r99-review"},
            # 7. Corrupted applied=True historical transaction
            {**valid_applied_hist_tx, "message_hash": workflow.compute_hash("tampered plan")},
            {**valid_applied_hist_tx, "delivery_status": "SEND_ATTEMPTED"},
            {**valid_applied_hist_tx, "applied": "true"},
            {k: v for k, v in valid_applied_hist_tx.items() if k != "head_sha"},
        ]

        for idx, bad_tx in enumerate(corrupt_cases):
            st = workflow.load_state(task_dir)
            st["stage"] = workflow.STAGE_WAITING_REVIEW
            st["business_stage"] = workflow.STAGE_WAITING_REVIEW
            st["blocked_from_stage"] = None
            st["active_transaction"] = bad_tx
            st["invalidated_transactions"] = []
            workflow.save_state(task_dir, st)

            # execute_exchange must fail closed with DeliveryError and NOT quietly invalidate to None
            with self.assertRaises(workflow.DeliveryError, msg=f"Case {idx} failed to raise DeliveryError on execute_exchange"):
                workflow.execute_exchange(task_dir=task_dir)

            st_after_ex = workflow.load_state(task_dir)
            self.assertEqual(
                st_after_ex["active_transaction"],
                bad_tx,
                f"Case {idx} modified active_transaction unexpectedly on execute_exchange",
            )
            self.assertEqual(
                st_after_ex.get("invalidated_transactions", []),
                [],
                f"Case {idx} populated invalidated_transactions on execute_exchange",
            )

            # record_work must also fail closed with DeliveryError
            with self.assertRaises(workflow.DeliveryError, msg=f"Case {idx} failed to raise DeliveryError on record_work"):
                workflow.record_work(
                    project=self.project_dir,
                    task_id="r3p4-corrupt-tx-task",
                    head_sha=new_drift_sha,
                    test_evidence=self._make_evidence(sha=new_drift_sha),
                )

            st_after_rec = workflow.load_state(task_dir)
            self.assertEqual(
                st_after_rec["active_transaction"],
                bad_tx,
                f"Case {idx} modified active_transaction unexpectedly on record_work",
            )
            self.assertEqual(
                st_after_rec.get("invalidated_transactions", []),
                [],
                f"Case {idx} populated invalidated_transactions on record_work",
            )


if __name__ == "__main__":
    unittest.main()




