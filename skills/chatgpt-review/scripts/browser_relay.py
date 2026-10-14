"""Minimal browser-relay prototype for ChatGPT review communication via bsk CLI.

Reuses existing browser-skill (bsk) daemon and user-authorized ChatGPT tab.
Enforces borrow approval, idempotency with request_id, delivery verification,
and automatic tab return.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("browser_relay")


class BrowserRelayError(Exception):
    """Base exception for browser relay operations."""


class ApprovalRequiredError(BrowserRelayError):
    """Raised when tab borrow confirmation was denied or timed out."""


class TabNotFoundError(BrowserRelayError):
    """Raised when no matching ChatGPT tab is found."""


class DeliveryError(BrowserRelayError):
    """Raised when message delivery could not be confirmed."""


@dataclass
class BrowserStatus:
    daemon_running: bool
    daemon_version: Optional[str] = None
    browser_count: int = 0
    connected_browser_id: Optional[str] = None
    browser_name: Optional[str] = None
    error: Optional[str] = None


@dataclass
class TabInfo:
    tab_id: str
    scope: str
    window_id: str
    title: str
    url: str


class BrowserRelay:
    """Manages browser interaction for ChatGPT review exchange using bsk CLI."""

    def __init__(self, bsk_executable: Optional[str] = None):
        self.bsk = bsk_executable or shutil.which("bsk") or "bsk"

    def _run_bsk(self, *args: str, timeout: Optional[int] = None) -> subprocess.CompletedProcess[str]:
        cmd = [self.bsk, *args]
        try:
            return subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise BrowserRelayError(f"Command timed out after {timeout}s: {' '.join(cmd)}") from exc
        except FileNotFoundError as exc:
            raise BrowserRelayError(f"bsk executable not found at '{self.bsk}'") from exc

    def check_status(self) -> BrowserStatus:
        proc = self._run_bsk("status", "--json", timeout=10)
        if proc.returncode != 0:
            return BrowserStatus(
                daemon_running=False,
                error=proc.stderr.strip() or proc.stdout.strip() or "bsk status failed",
            )
        try:
            data = json.loads(proc.stdout)
            browsers = data.get("browsers", [])
            browser_id = browsers[0].get("instance_id") if browsers else None
            browser_name = browsers[0].get("browser_name") if browsers else None
            return BrowserStatus(
                daemon_running=True,
                daemon_version=data.get("daemon_version"),
                browser_count=len(browsers),
                connected_browser_id=browser_id,
                browser_name=browser_name,
            )
        except Exception as exc:
            return BrowserStatus(daemon_running=False, error=str(exc))

    def start_session(self, browser_id: str, task_id: str) -> str:
        proc = self._run_bsk(
            "session",
            "start",
            "--browser",
            browser_id,
            "--name",
            task_id,
            "--no-focus",
            "--json",
            timeout=15,
        )
        if proc.returncode != 0:
            raise BrowserRelayError(f"Failed to start bsk session: {proc.stderr.strip() or proc.stdout.strip()}")
        data = json.loads(proc.stdout)
        session_id = data.get("session_id")
        if not session_id:
            raise BrowserRelayError("Session start did not return a session_id")
        return session_id

    def stop_session(self, session_id: str) -> bool:
        proc = self._run_bsk("session", "stop", session_id, timeout=10)
        return proc.returncode == 0

    def list_user_tabs(self, session_id: str) -> List[TabInfo]:
        proc = self._run_bsk("tab", "list", "--scope", "user", "--session", session_id, timeout=15)
        if proc.returncode != 0:
            raise BrowserRelayError(f"Failed to list user tabs: {proc.stderr.strip() or proc.stdout.strip()}")
        tabs = []
        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line or line.startswith("TAB") or line.startswith("---"):
                continue
            parts = re.split(r"\s{2,}", line)
            if len(parts) >= 5:
                tabs.append(
                    TabInfo(
                        tab_id=parts[0].strip(),
                        scope=parts[1].strip(),
                        window_id=parts[2].strip(),
                        title=parts[3].strip(),
                        url=parts[4].strip(),
                    )
                )
        return tabs

    def find_chatgpt_tab(self, session_id: str) -> Optional[TabInfo]:
        tabs = self.list_user_tabs(session_id)
        for tab in tabs:
            if "chatgpt.com" in tab.url.lower() or "chatgpt" in tab.title.lower():
                return tab
        return None

    def borrow_tab(self, session_id: str, tab_id: str, timeout_secs: int = 30) -> bool:
        """Borrow tab with human confirmation.

        Raises ApprovalRequiredError if human confirmation times out or is rejected.
        """
        proc = self._run_bsk(
            "tab",
            "borrow",
            "--session",
            session_id,
            str(tab_id),
            "--timeout",
            f"{timeout_secs}s",
            timeout=timeout_secs + 5,
        )
        if proc.returncode != 0:
            combined = (proc.stderr + " " + proc.stdout).lower()
            if "timed out waiting for human confirmation" in combined or "approval" in combined or "borrow" in combined:
                raise ApprovalRequiredError(
                    "Tab borrow requires human confirmation in Chrome, which timed out or was rejected. "
                    "Manual review transfer fallback remains available."
                )
            raise BrowserRelayError(f"Failed to borrow tab: {proc.stderr.strip() or proc.stdout.strip()}")
        return True

    def return_tab(self, session_id: str, tab_id: str) -> bool:
        proc = self._run_bsk("tab", "return", "--session", session_id, str(tab_id), timeout=15)
        return proc.returncode == 0

    def evaluate(self, session_id: str, expression: str, timeout: int = 15) -> str:
        proc = self._run_bsk("evaluate", "--session", session_id, expression, timeout=timeout)
        if proc.returncode != 0:
            raise BrowserRelayError(f"Evaluate failed: {proc.stderr.strip() or proc.stdout.strip()}")
        return proc.stdout.strip()

    def check_request_exists(self, session_id: str, request_id: str) -> bool:
        """Idempotency check: returns True if request_id is already present on the page."""
        escaped = json.dumps(request_id)
        js = f"document.body ? document.body.innerText.includes({escaped}) : false"
        result = self.evaluate(session_id, js)
        return result.strip().lower() == "true"

    def send_prompt(
        self, session_id: str, message: str, request_id: str
    ) -> bool:
        """Injects prompt and triggers send. Verifies arrival on page."""
        if self.check_request_exists(session_id, request_id):
            logger.info("Request %s is already present on the page, skipping re-send.", request_id)
            return True

        # JS injection into ChatGPT prompt field
        # Supports contenteditable div, textarea, and #prompt-textarea
        encoded_msg = json.dumps(message)
        inject_js = f"""
(() => {{
    const el = document.querySelector('#prompt-textarea') ||
               document.querySelector('div[contenteditable="true"]') ||
               document.querySelector('textarea');
    if (!el) return 'NO_INPUT';
    if (el.tagName.toLowerCase() === 'textarea') {{
        el.value = {encoded_msg};
        el.dispatchEvent(new Event('input', {{ bubbles: true }}));
        el.dispatchEvent(new Event('change', {{ bubbles: true }}));
    }} else {{
        el.focus();
        el.innerText = {encoded_msg};
        el.dispatchEvent(new Event('input', {{ bubbles: true }}));
    }}
    return 'OK';
}})()
"""
        res = self.evaluate(session_id, inject_js)
        if res != "OK":
            raise DeliveryError(f"Could not locate prompt input element on ChatGPT page: {res}")

        # Click send button or press enter
        click_js = """
(() => {
    const btn = document.querySelector('button[data-testid="send-button"]') ||
                document.querySelector('button[aria-label="Send prompt"]') ||
                document.querySelector('button[aria-label="发送提示词"]');
    if (btn && !btn.disabled) {
        btn.click();
        return 'CLICKED';
    }
    // Fallback: dispatch Enter keyboard event
    const el = document.querySelector('#prompt-textarea') || document.activeElement;
    if (el) {
        el.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true }));
        return 'ENTER_SENT';
    }
    return 'NO_SEND_ACTION';
})()
"""
        send_res = self.evaluate(session_id, click_js)
        if send_res not in ("CLICKED", "ENTER_SENT"):
            raise DeliveryError(f"Failed to trigger send action: {send_res}")

        # Verify delivery within 10 seconds
        start = time.time()
        while time.time() - start < 10:
            if self.check_request_exists(session_id, request_id):
                return True
            time.sleep(1)

        raise DeliveryError(f"Message containing request_id={request_id} was submitted but could not be confirmed in transcript.")

    def wait_for_response(
        self, session_id: str, timeout_secs: int = 180, poll_interval: int = 4
    ) -> str:
        """Polls until ChatGPT assistant response finishes streaming and extracts text."""
        start = time.time()
        last_length = -1
        stable_count = 0

        # Check script that determines if generating is in progress and gets last response text
        poll_js = """
(() => {
    const isGenerating = Boolean(
        document.querySelector('button[aria-label="Stop generating"]') ||
        document.querySelector('button[aria-label="停止生成"]') ||
        document.querySelector('button[data-testid="stop-button"]')
    );
    // Find assistant responses
    const turns = Array.from(document.querySelectorAll('[data-message-author-role="assistant"], div[class*="agent-turn"]'));
    const lastTurn = turns.length > 0 ? turns[turns.length - 1] : null;
    const text = lastTurn ? lastTurn.innerText : (document.body ? document.body.innerText.slice(-2000) : '');
    return JSON.stringify({
        isGenerating: isGenerating,
        textLength: text.length,
        text: text
    });
})()
"""
        while time.time() - start < timeout_secs:
            try:
                raw = self.evaluate(session_id, poll_js)
                state = json.loads(raw)
            except Exception as exc:
                time.sleep(poll_interval)
                continue

            is_generating = state.get("isGenerating", False)
            current_length = state.get("textLength", 0)

            if not is_generating:
                if current_length == last_length and current_length > 0:
                    stable_count += 1
                    if stable_count >= 2:
                        return state.get("text", "")
                else:
                    stable_count = 1
                    last_length = current_length
            else:
                stable_count = 0
                last_length = current_length

            time.sleep(poll_interval)

        # Return whatever was collected upon timeout
        try:
            raw = self.evaluate(session_id, poll_js)
            return json.loads(raw).get("text", "")
        except Exception:
            return ""

    def run_e2e_review(
        self,
        task_id: str,
        message: str,
        request_id: str,
        timeout_secs: int = 180,
    ) -> Dict[str, Any]:
        """Runs the full E2E review cycle safely.

        Guarantees:
        1. Checks status first.
        2. Respects borrow approval; does not bypass if denied/timed out.
        3. Enforces idempotency (request_id).
        4. Guarantees tab return and session cleanup in finally block.
        """
        status = self.check_status()
        if not status.daemon_running or not status.connected_browser_id:
            return {
                "status": "BLOCKED_NO_BROWSER",
                "error": status.error or "bsk daemon or browser not available",
                "manual_fallback_required": True,
            }

        session_id = self.start_session(status.connected_browser_id, task_id)
        borrowed_tab_id: Optional[str] = None
        try:
            target_tab = self.find_chatgpt_tab(session_id)
            if not target_tab:
                return {
                    "status": "BLOCKED_NO_CHATGPT_TAB",
                    "error": "No open ChatGPT tab found in user browser",
                    "manual_fallback_required": True,
                }

            # Borrow tab with human confirmation
            self.borrow_tab(session_id, target_tab.tab_id, timeout_secs=25)
            borrowed_tab_id = target_tab.tab_id

            # Send prompt (with duplicate check)
            self.send_prompt(session_id, message, request_id)

            # Wait for response completion
            reply_text = self.wait_for_response(session_id, timeout_secs=timeout_secs)

            return {
                "status": "SUCCESS",
                "request_id": request_id,
                "tab_id": borrowed_tab_id,
                "response_text": reply_text,
                "manual_fallback_required": False,
            }
        except ApprovalRequiredError as exc:
            return {
                "status": "BLOCKED_WAITING_HUMAN_APPROVAL",
                "error": str(exc),
                "manual_fallback_required": True,
            }
        except Exception as exc:
            return {
                "status": "ERROR",
                "error": str(exc),
                "manual_fallback_required": True,
            }
        finally:
            if borrowed_tab_id:
                try:
                    self.return_tab(session_id, borrowed_tab_id)
                except Exception as exc:
                    logger.warning("Failed to return tab %s: %s", borrowed_tab_id, exc)
            try:
                self.stop_session(session_id)
            except Exception as exc:
                logger.warning("Failed to stop session %s: %s", session_id, exc)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Minimal browser-relay prototype for chatgpt-review")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # status
    subparsers.add_parser("status", help="Check bsk daemon and browser status")

    # tabs
    tabs_p = subparsers.add_parser("tabs", help="List user tabs")
    tabs_p.add_argument("--task-id", default="check-tabs")

    # relay
    relay_p = subparsers.add_parser("relay", help="Run E2E review relay")
    relay_p.add_argument("--task-id", required=True, help="Task ID")
    relay_p.add_argument("--request-id", required=True, help="Unique request ID for idempotency")
    relay_p.add_argument("--message-file", required=True, help="Path to markdown message file")
    relay_p.add_argument("--output-file", help="Path to write collected assistant response")
    relay_p.add_argument("--timeout", type=int, default=180, help="Response polling timeout in seconds")

    args = parser.parse_args(argv)
    relay = BrowserRelay()

    if args.command == "status":
        st = relay.check_status()
        print(json.dumps(st.__dict__, indent=2, ensure_ascii=False))
        return 0 if st.daemon_running else 1

    if args.command == "tabs":
        st = relay.check_status()
        if not st.daemon_running or not st.connected_browser_id:
            print(f"Browser unavailable: {st.error}")
            return 1
        sess = relay.start_session(st.connected_browser_id, args.task_id)
        try:
            tabs = relay.list_user_tabs(sess)
            for t in tabs:
                print(f"[{t.tab_id}] {t.title} -> {t.url}")
            return 0
        finally:
            relay.stop_session(sess)

    if args.command == "relay":
        msg_path = Path(args.message_file)
        if not msg_path.is_file():
            print(f"Error: message file does not exist: {msg_path}", file=sys.stderr)
            return 1
        message = msg_path.read_text(encoding="utf-8")
        res = relay.run_e2e_review(args.task_id, message, args.request_id, timeout_secs=args.timeout)
        print(json.dumps(res, indent=2, ensure_ascii=False))
        if res.get("status") == "SUCCESS" and args.output_file:
            out_path = Path(args.output_file)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(res.get("response_text", ""), encoding="utf-8")
            print(f"Saved response to {out_path}")
        return 0 if res.get("status") == "SUCCESS" else 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
