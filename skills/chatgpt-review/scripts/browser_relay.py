"""Minimal browser-relay prototype for ChatGPT review communication via bsk CLI.

Reuses existing browser-skill (bsk) daemon and user-authorized ChatGPT tab.
Enforces:
1. Exact HTTPS ChatGPT origin validation and exact conversation ID matching.
2. Tab ID cannot bypass origin check; post-borrow URL re-verification prevents navigation.
3. Pre-send DOM checks fail-closed; post-send checks verify content SHA-256 (not just exists).
4. TOCTOU prevention: URL re-verified immediately before input injection and before send button click.
5. DOM-node-based conversation turn extraction (locates unique user turn and immediately adjacent assistant turn).
6. All text/prose/markdown blocks in assistant turn are completely extracted (never only the first block).
7. Return BLOCKED when DOM structure is unsupported, ambiguous, or sequence is broken; never guess.
8. Resource cleanup tracking: return_tab/stop_session failures are recorded in diagnostics, not masked.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("browser_relay")


class BrowserRelayError(Exception):
    """Base exception for browser relay operations."""


class ApprovalRequiredError(BrowserRelayError):
    """Raised when tab borrow confirmation was denied or timed out."""


class TabNotFoundError(BrowserRelayError):
    """Raised when no matching ChatGPT tab is found."""


class ConversationMismatchError(BrowserRelayError):
    """Raised when selected tab does not match the specified target conversation."""


class DeliveryError(BrowserRelayError):
    """Raised when message delivery could not be confirmed in a formal user turn."""


class ResponseIncompleteError(BrowserRelayError):
    """Raised when assistant response is incomplete, still streaming, or unparseable."""


def validate_chatgpt_origin(url: Optional[str]) -> bool:
    """Validates that a URL has a legitimate HTTPS ChatGPT origin."""
    if not url or not isinstance(url, str):
        return False
    try:
        parsed = urllib.parse.urlparse(url.strip())
    except Exception:
        return False
    if parsed.scheme.lower() != "https":
        return False
    hostname = (parsed.hostname or "").lower()
    if hostname not in ("chatgpt.com", "www.chatgpt.com"):
        return False
    if parsed.port is not None and parsed.port != 443:
        return False
    return True


def normalize_model_identifier(name: Optional[str]) -> str:
    """Normalizes model identifier: lowercase, trims whitespace, preserving all model variants and suffixes."""
    if not name:
        return ""
    norm = " ".join(name.strip().lower().split())
    aliases = {
        "高": "high",
        "中": "medium",
        "低": "low",
    }
    return aliases.get(norm, norm)



def match_conversation_url(target_url: str, candidate_url: str) -> bool:
    """Matches target and candidate URLs.
    - If target specifies a conversation (/c/<id>), candidate must match exact conversation ID.
    - If target is fresh chat (https://chatgpt.com/ or https://chatgpt.com), candidate can be fresh chat or transition to a valid new conversation (/c/<id>).
    """
    if not target_url or not candidate_url:
        return False
    if not validate_chatgpt_origin(target_url) or not validate_chatgpt_origin(candidate_url):
        return False
    try:
        t_parsed = urllib.parse.urlparse(target_url.strip())
        c_parsed = urllib.parse.urlparse(candidate_url.strip())
    except Exception:
        return False

    if t_parsed.scheme.lower() != c_parsed.scheme.lower():
        return False
    if (t_parsed.hostname or "").lower() != (c_parsed.hostname or "").lower():
        return False

    t_path = t_parsed.path.rstrip("/")
    c_path = c_parsed.path.rstrip("/")

    t_parts = [p for p in t_path.split("/") if p]
    c_parts = [p for p in c_path.split("/") if p]

    # Target specifies /c/<id>
    if "c" in t_parts:
        c_idx = t_parts.index("c")
        if c_idx + 1 < len(t_parts):
            # Target has specific conversation ID: candidate must match exact conversation ID!
            if "c" in c_parts:
                candidate_c_idx = c_parts.index("c")
                if candidate_c_idx + 1 < len(c_parts):
                    return t_parts[c_idx + 1].lower() == c_parts[candidate_c_idx + 1].lower()
            return False

    # Target is fresh chat root (path is "" or "/")
    if not t_parts:
        if not c_parts:
            return True
        if "c" in c_parts:
            # Transition to /c/<id> is legitimate
            return True
        return False

    return c_path.lower().startswith(t_path.lower())


def build_check_user_turn_js(request_id: str) -> str:
    """Constructs DOM JavaScript to verify existence of a genuine user turn containing request_id."""
    escaped_req = json.dumps(request_id)
    return f"""(() => {{
    const main = document.querySelector('main');
    if (!main) return JSON.stringify({{ exists: false, status: 'BLOCKED_NO_MAIN', reason: 'No <main> container in DOM' }});

    function getTurnRole(el) {{
        if (!el || el.nodeType !== 1) return null;
        const role = el.getAttribute('data-message-author-role');
        if (role === 'user') return 'user';
        if (role === 'assistant') return 'assistant';
        if (el.querySelector('[data-message-author-role="user"]')) return 'user';
        if (el.querySelector('[data-message-author-role="assistant"]')) return 'assistant';

        const testId = el.getAttribute('data-testid') || '';
        if (testId.includes('user')) return 'user';
        if (testId.includes('assistant')) return 'assistant';
        if (el.querySelector('[data-testid*="user"]')) return 'user';
        if (el.querySelector('[data-testid*="assistant"]')) return 'assistant';

        if (el.classList && (el.classList.contains('group/user-message') || el.classList.contains('user-message'))) return 'user';
        if (el.querySelector('[class*="group/user-message"], [class*="user-message"]')) return 'user';

        if (el.querySelector('button[aria-label="Edit message"], button[aria-label="编辑消息"], button[aria-label="编辑"], button[aria-label="Edit"], button[data-testid*="edit"]')) {{
            return 'user';
        }}
        if (el.querySelector('button[aria-label="Copy"], button[aria-label="复制"], button[aria-label*="Copy"], button[aria-label*="复制"], button[data-testid*="copy"], .prose, .markdown, [class*="MarkdownRoot"]')) {{
            return 'assistant';
        }}
        return null;
    }}

    function findTurns(main) {{
        let candidates = Array.from(main.querySelectorAll('article'));
        if (candidates.length === 0) {{
            candidates = Array.from(main.querySelectorAll('[data-testid^="conversation-turn-"], [data-testid="conversation-turn"]'));
        }}
        if (candidates.length === 0) {{
            candidates = Array.from(main.querySelectorAll('[data-message-author-role]'));
        }}
        if (candidates.length === 0) {{
            const groups = Array.from(main.querySelectorAll('[class*="browser:gap-1"], [class*="gap-3"]'));
            let groupChildren = [];
            for (const g of groups) {{
                groupChildren.push(...Array.from(g.children));
            }}
            if (groupChildren.length > 0) {{
                candidates = groupChildren;
            }}
        }}
        if (candidates.length === 0) {{
            candidates = Array.from(main.querySelectorAll('[class*="block-"]'));
        }}
        const valid = [];
        for (const c of candidates) {{
            const role = getTurnRole(c);
            if (role) {{
                if (valid.length > 0 && typeof valid[valid.length - 1].node.contains === 'function' && valid[valid.length - 1].node.contains(c)) {{
                    continue;
                }}
                valid.push({{ node: c, role }});
            }}
        }}
        return valid;
    }}

    const turns = findTurns(main);
    if (turns.length === 0) {{
        return JSON.stringify({{ exists: false, status: 'BLOCKED_DOM_NOT_SUPPORTED', reason: 'No recognizable conversation turn nodes in DOM' }});
    }}

    const matchedUserTurns = [];
    for (const t of turns) {{
        if (t.role === 'user') {{
            const txt = (t.node.innerText || t.node.textContent || '');
            if (txt.includes({escaped_req})) {{
                matchedUserTurns.push({{ text: txt }});
            }}
        }}
    }}

    if (matchedUserTurns.length === 1) {{
        return JSON.stringify({{ exists: true, status: 'OK', userText: matchedUserTurns[0].text }});
    }} else if (matchedUserTurns.length > 1) {{
        return JSON.stringify({{ exists: true, status: 'BLOCKED_AMBIGUOUS_USER_TURN', reason: 'Multiple user turns contain request_id', count: matchedUserTurns.length }});
    }} else {{
        return JSON.stringify({{ exists: false, status: 'USER_TURN_NOT_FOUND' }});
    }}
}})()"""


def build_extract_js(request_id: str) -> str:
    """Constructs DOM JavaScript to extract the assistant turn following the unique user turn with request_id.

    Extracts ALL prose/markdown text blocks in the assistant turn, never just the first block.
    """
    escaped_req = json.dumps(request_id)
    return f"""(() => {{
    const main = document.querySelector('main');
    if (!main) return JSON.stringify({{ status: 'BLOCKED_NO_MAIN', text: '', reason: 'No <main> container in DOM' }});

    const isGenerating = Boolean(
        document.querySelector('button[aria-label="Stop generating"]') ||
        document.querySelector('button[aria-label="停止生成"]') ||
        document.querySelector('button[data-testid="stop-button"]') ||
        document.querySelector('.result-streaming') ||
        main.querySelector('.result-streaming')
    );

    function getTurnRole(el) {{
        if (!el || el.nodeType !== 1) return null;
        const role = el.getAttribute('data-message-author-role');
        if (role === 'user') return 'user';
        if (role === 'assistant') return 'assistant';
        if (el.querySelector('[data-message-author-role="user"]')) return 'user';
        if (el.querySelector('[data-message-author-role="assistant"]')) return 'assistant';

        const testId = el.getAttribute('data-testid') || '';
        if (testId.includes('user')) return 'user';
        if (testId.includes('assistant')) return 'assistant';
        if (el.querySelector('[data-testid*="user"]')) return 'user';
        if (el.querySelector('[data-testid*="assistant"]')) return 'assistant';

        if (el.classList && (el.classList.contains('group/user-message') || el.classList.contains('user-message'))) return 'user';
        if (el.querySelector('[class*="group/user-message"], [class*="user-message"]')) return 'user';

        if (el.querySelector('button[aria-label="Edit message"], button[aria-label="编辑消息"], button[aria-label="编辑"], button[aria-label="Edit"], button[data-testid*="edit"]')) {{
            return 'user';
        }}
        if (el.querySelector('button[aria-label="Copy"], button[aria-label="复制"], button[aria-label*="Copy"], button[aria-label*="复制"], button[data-testid*="copy"], .prose, .markdown, [class*="MarkdownRoot"]')) {{
            return 'assistant';
        }}
        return null;
    }}

    function findTurns(main) {{
        let candidates = Array.from(main.querySelectorAll('article'));
        if (candidates.length === 0) {{
            candidates = Array.from(main.querySelectorAll('[data-testid^="conversation-turn-"], [data-testid="conversation-turn"]'));
        }}
        if (candidates.length === 0) {{
            candidates = Array.from(main.querySelectorAll('[data-message-author-role]'));
        }}
        if (candidates.length === 0) {{
            const groups = Array.from(main.querySelectorAll('[class*="browser:gap-1"], [class*="gap-3"]'));
            let groupChildren = [];
            for (const g of groups) {{
                groupChildren.push(...Array.from(g.children));
            }}
            if (groupChildren.length > 0) {{
                candidates = groupChildren;
            }}
        }}
        if (candidates.length === 0) {{
            candidates = Array.from(main.querySelectorAll('[class*="block-"]'));
        }}
        const valid = [];
        for (const c of candidates) {{
            const role = getTurnRole(c);
            if (role) {{
                if (valid.length > 0 && typeof valid[valid.length - 1].node.contains === 'function' && valid[valid.length - 1].node.contains(c)) {{
                    continue;
                }}
                valid.push({{ node: c, role }});
            }}
        }}
        return valid;
    }}

    const turns = findTurns(main);
    if (turns.length === 0) {{
        return JSON.stringify({{
            status: 'BLOCKED_DOM_NOT_SUPPORTED',
            isGenerating: isGenerating,
            text: '',
            reason: 'No recognizable conversation turn nodes in DOM'
        }});
    }}

    const matchedUserTurns = [];
    for (let i = 0; i < turns.length; i++) {{
        if (turns[i].role === 'user') {{
            const txt = (turns[i].node.innerText || turns[i].node.textContent || '');
            if (txt.includes({escaped_req})) {{
                matchedUserTurns.push({{ index: i, turn: turns[i] }});
            }}
        }}
    }}

    if (matchedUserTurns.length === 0) {{
        return JSON.stringify({{
            status: 'USER_TURN_NOT_FOUND',
            isGenerating: isGenerating,
            text: ''
        }});
    }}

    if (matchedUserTurns.length > 1) {{
        return JSON.stringify({{
            status: 'BLOCKED_AMBIGUOUS_USER_TURN',
            isGenerating: isGenerating,
            text: '',
            reason: 'Multiple user turns contain request_id'
        }});
    }}

    const userIndex = matchedUserTurns[0].index;
    const nextIndex = userIndex + 1;

    if (nextIndex >= turns.length) {{
        return JSON.stringify({{
            status: 'WAITING_FOR_ASSISTANT',
            isGenerating: isGenerating,
            text: ''
        }});
    }}

    const nextTurn = turns[nextIndex];
    if (nextTurn.role !== 'assistant') {{
        return JSON.stringify({{
            status: 'BLOCKED_INVALID_SEQUENCE',
            isGenerating: isGenerating,
            text: '',
            reason: 'Turn following user turn is not role assistant'
        }});
    }}

    // Clone assistant turn node and clean button bars, SVGs, controls
    const clone = nextTurn.node.cloneNode(true);
    const toRemove = clone.querySelectorAll('button, svg, [data-testid*="action"], [aria-label*="Copy"], [aria-label*="复制"], [aria-label*="Feedback"]');
    toRemove.forEach(b => b.remove());

    // Extract ALL prose/markdown blocks, joining them with double newline
    const proseBlocks = Array.from(clone.querySelectorAll('.prose, .markdown, [class*="MarkdownRoot"]'));
    let extractedText = '';
    if (proseBlocks.length > 0) {{
        extractedText = proseBlocks.map(b => (b.innerText || b.textContent || '').trim()).filter(Boolean).join('\\n\\n');
    }} else {{
        extractedText = (clone.innerText || clone.textContent || '').trim();
    }}

    return JSON.stringify({{
        status: isGenerating ? 'GENERATING' : 'OK',
        isGenerating: isGenerating,
        textLength: extractedText.length,
        text: extractedText
    }});
}})()"""


def build_check_model_and_thinking_js() -> str:
    """Constructs DOM JavaScript to detect the currently active model and thinking mode."""
    return """(() => {
    let modelName = '';
    const modelBtn = document.querySelector('button[data-testid="model-switcher-dropdown-button"]') ||
                     document.querySelector('button[id^="model-selector"]') ||
                     document.querySelector('[data-testid*="model-selector"]');
    if (modelBtn) {
        modelName = (modelBtn.innerText || modelBtn.textContent || '').trim();
    }
    if (!modelName) {
        const modeBtn = document.querySelector('button[aria-label*="当前模式"], button[aria-label*="Current mode"]');
        if (modeBtn) {
            modelName = (modeBtn.innerText || modeBtn.textContent || '').trim();
        }
    }
    if (!modelName) {
        const headerEl = document.querySelector('header');
        if (headerEl) {
            const m = headerEl.querySelector('button[aria-haspopup="menu"]');
            if (m) modelName = (m.innerText || m.textContent || '').trim();
        }
    }

    let thinkingMode = '';
    const thinkingBtn = document.querySelector('button[data-testid*="reasoning"]') ||
                        document.querySelector('[data-testid*="thought"]') ||
                        document.querySelector('button[aria-label*="Thinking"]') ||
                        document.querySelector('button[aria-label*="思考"]');
    if (thinkingBtn) {
        thinkingMode = (thinkingBtn.innerText || thinkingBtn.textContent || '').trim();
    }
    if (!thinkingMode) {
        const composerPills = Array.from(document.querySelectorAll('button[aria-label*="选择 ChatGPT 模型"], button[aria-label*="Select model"], button[aria-label*="Reasoning effort"]'));
        for (const p of composerPills) {
            const txt = (p.innerText || p.textContent || '').trim();
            if (/^(high|medium|low|高|中|低)$/i.test(txt)) {
                thinkingMode = txt;
                break;
            }
        }
    }

    return JSON.stringify({
        model: modelName,
        thinking_mode: thinkingMode,
        detected: Boolean(modelName)
    });
})()"""



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

    def find_target_tab(
        self,
        session_id: str,
        target_tab_id: Optional[str] = None,
        target_url_prefix: Optional[str] = None,
    ) -> TabInfo:
        """Finds target tab matching tab_id or target_url_prefix.

        Enforces HTTPS ChatGPT origin validation and exact conversation ID matching.
        Specifying Tab ID cannot bypass origin check.
        """
        tabs = self.list_user_tabs(session_id)
        valid_chatgpt_tabs = [t for t in tabs if validate_chatgpt_origin(t.url)]

        if target_tab_id:
            matched = [t for t in tabs if t.tab_id == str(target_tab_id)]
            if not matched:
                raise TabNotFoundError(f"Target tab ID '{target_tab_id}' not found among open user tabs.")
            tab = matched[0]
            # Specifying Tab ID MUST NOT bypass origin check (F-010)
            if not validate_chatgpt_origin(tab.url):
                raise ConversationMismatchError(
                    f"Target tab {target_tab_id} URL '{tab.url}' is not a valid HTTPS ChatGPT origin."
                )
            if target_url_prefix:
                if not validate_chatgpt_origin(target_url_prefix):
                    raise ValueError(f"target_url_prefix '{target_url_prefix}' is not a valid HTTPS ChatGPT origin.")
                if not match_conversation_url(target_url_prefix, tab.url):
                    raise ConversationMismatchError(
                        f"Tab {target_tab_id} URL '{tab.url}' does not match required target '{target_url_prefix}'"
                    )
            return tab

        if target_url_prefix:
            if not validate_chatgpt_origin(target_url_prefix):
                raise ValueError(f"target_url_prefix '{target_url_prefix}' is not a valid HTTPS ChatGPT origin.")
            matched = [t for t in valid_chatgpt_tabs if match_conversation_url(target_url_prefix, t.url)]
            if not matched:
                raise TabNotFoundError(f"No open HTTPS ChatGPT tab found matching target URL: {target_url_prefix}")
            if len(matched) > 1:
                raise ConversationMismatchError(
                    f"Multiple tabs match URL '{target_url_prefix}'. Specify --target-tab-id to disambiguate."
                )
            return matched[0]

        # If neither specified: require exactly one valid ChatGPT tab
        if len(valid_chatgpt_tabs) == 1:
            return valid_chatgpt_tabs[0]
        if not valid_chatgpt_tabs:
            raise TabNotFoundError("No open HTTPS ChatGPT tabs found in connected browser.")
        raise ConversationMismatchError(
            f"Multiple ({len(valid_chatgpt_tabs)}) ChatGPT tabs are open. You must explicitly specify --target-url or --target-tab-id."
        )

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

    def get_current_url(self, session_id: str) -> str:
        """Retrieves the current window.location.href of the active borrowed tab."""
        res = self.evaluate(session_id, "(() => window.location.href)()")
        return res.strip("\"'")

    def check_user_turn_exists(
        self, session_id: str, request_id: str, expected_message_hash: Optional[str] = None
    ) -> Dict[str, Any]:
        """Checks whether a genuine user turn containing request_id exists in the active conversation."""
        js = build_check_user_turn_js(request_id)
        res = self.evaluate(session_id, js)
        try:
            data = json.loads(res)
        except Exception:
            return {"exists": False, "status": "EVALUATE_PARSE_ERROR", "raw": res}

        if data.get("exists") and expected_message_hash:
            user_text = data.get("userText", "")
            user_hash = hashlib.sha256(user_text.strip().encode("utf-8")).hexdigest()
            data["user_text_hash"] = user_hash
            data["hash_matches"] = (user_hash == expected_message_hash)
        return data

    def send_prompt(
        self,
        session_id: str,
        message: str,
        request_id: str,
        delivery_timeout: float = 30.0,
        expected_message_hash: Optional[str] = None,
        expected_url: Optional[str] = None,
        expected_model: Optional[str] = None,
        expected_thinking: Optional[str] = None,
    ) -> bool:
        """Injects prompt and triggers send. Verifies arrival as a formal user turn.

        Enforces:
        1. Pre-send DOM check fail-closed on any error or unexpected status.
        2. TOCTOU prevention: URL and model/thinking checked immediately before inject and before send.
        3. Post-send user turn SHA-256 verification (not just exists).
        """
        if request_id not in message:
            raise ValueError(f"Message content must explicitly contain request_id: {request_id}")

        expected_hash = expected_message_hash or hashlib.sha256(message.strip().encode("utf-8")).hexdigest()
        check = self.check_user_turn_exists(session_id, request_id, expected_message_hash=expected_hash)
        status = check.get("status")

        # Pre-send DOM check must fail-closed on any error/unexpected status
        if status not in ("OK", "USER_TURN_NOT_FOUND"):
            raise DeliveryError(
                f"Pre-send DOM check failed closed: status={status}, reason={check.get('reason', check.get('raw'))}"
            )

        if check.get("exists"):
            # Check for idempotency content collision
            if check.get("user_text_hash") and check.get("user_text_hash") != expected_hash:
                raise DeliveryError(
                    f"Idempotency conflict: user turn with request_id={request_id} exists but content hash does not match current message."
                )
            logger.info("User turn containing request_id %s already exists in conversation. Skipping send.", request_id)
            return True

        # TOCTOU prevention: re-verify URL before injection
        if expected_url:
            current_url = self.get_current_url(session_id)
            if not match_conversation_url(expected_url, current_url):
                raise ConversationMismatchError(
                    f"TOCTOU violation: URL changed before prompt injection from '{expected_url}' to '{current_url}'"
                )

        # TOCTOU prevention: re-verify model and thinking mode before injection
        if expected_model or expected_thinking:
            m_check = self.check_model_and_thinking(
                session_id, expected_model=expected_model, expected_thinking=expected_thinking
            )
            if not m_check.get("verified"):
                raise ConversationMismatchError(
                    f"TOCTOU violation: Model or thinking mode mismatch before injection: {m_check.get('reason')}"
                )

        encoded_msg = json.dumps(message)
        inject_js = f"""(() => {{
    const el = document.querySelector('form #prompt-textarea') ||
               document.querySelector('#prompt-textarea') ||
               document.querySelector('form div[contenteditable="true"]') ||
               document.querySelector('form textarea') ||
               document.querySelector('div[contenteditable="true"]') ||
               document.querySelector('textarea');
    if (!el) return 'NO_INPUT';
    if (el.tagName.toLowerCase() === 'textarea') {{
        el.value = {encoded_msg};
        el.dispatchEvent(new Event('input', {{ bubbles: true }}));
        el.dispatchEvent(new Event('change', {{ bubbles: true }}));
    }} else {{
        el.focus();
        if (typeof document.execCommand === 'function') {{
            document.execCommand('selectAll', false, null);
            document.execCommand('insertText', false, {encoded_msg});
        }} else {{
            el.innerText = {encoded_msg};
        }}
        if (typeof InputEvent !== 'undefined') {{
            el.dispatchEvent(new InputEvent('input', {{ bubbles: true, inputType: 'insertText', data: {encoded_msg} }}));
        }} else {{
            el.dispatchEvent(new Event('input', {{ bubbles: true }}));
        }}
        el.dispatchEvent(new Event('change', {{ bubbles: true }}));
    }}
    return 'OK';
}})()"""
        res = self.evaluate(session_id, inject_js)
        if res != "OK":
            raise DeliveryError(f"Could not locate prompt input element on ChatGPT page: {res}")

        # TOCTOU prevention: re-verify URL before clicking send
        if expected_url:
            current_url = self.get_current_url(session_id)
            if not match_conversation_url(expected_url, current_url):
                raise ConversationMismatchError(
                    f"TOCTOU violation: URL changed before clicking send from '{expected_url}' to '{current_url}'"
                )

        # TOCTOU prevention: re-verify model and thinking mode before clicking send
        if expected_model or expected_thinking:
            m_check = self.check_model_and_thinking(
                session_id, expected_model=expected_model, expected_thinking=expected_thinking
            )
            if not m_check.get("verified"):
                raise ConversationMismatchError(
                    f"TOCTOU violation: Model or thinking mode mismatch before send: {m_check.get('reason')}"
                )

        click_js = """(() => {
    const btn = document.querySelector('form button[data-testid="send-button"]') ||
                document.querySelector('button[data-testid="send-button"]') ||
                document.querySelector('form button[aria-label="Send prompt"]') ||
                document.querySelector('button[aria-label="Send prompt"]') ||
                document.querySelector('form button[aria-label="发送提示词"]') ||
                document.querySelector('button[aria-label="发送提示词"]') ||
                document.querySelector('form button[aria-label="发送"]') ||
                document.querySelector('button[aria-label="发送"]');
    if (btn && !btn.disabled) {
        btn.click();
        return 'CLICKED';
    }
    return 'WAITING_BUTTON';
})()"""
        send_res = "NO_SEND_ACTION"
        click_start = time.time()
        while time.time() - click_start < 4.0:
            send_res = self.evaluate(session_id, click_js)
            if send_res in ("CLICKED", "SUBMITTED"):
                break
            time.sleep(0.3)

        if send_res not in ("CLICKED", "SUBMITTED"):
            fallback_js = """(() => {
    const el = document.querySelector('form [contenteditable="true"]') ||
               document.querySelector('#prompt-textarea') ||
               document.activeElement;
    if (el) {
        el.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true }));
        return 'ENTER_SENT';
    }
    return 'NO_SEND_ACTION';
})()"""
            send_res = self.evaluate(session_id, fallback_js)

        if send_res not in ("CLICKED", "SUBMITTED", "ENTER_SENT"):
            raise DeliveryError(f"Failed to trigger send action: {send_res}")

        # Verify delivery into formal user turn within delivery_timeout seconds with content hash verification
        start = time.time()
        while time.time() - start < delivery_timeout:
            check_after = self.check_user_turn_exists(session_id, request_id, expected_message_hash=expected_hash)
            if check_after.get("exists"):
                if expected_hash and not check_after.get("hash_matches"):
                    raise DeliveryError(
                        f"Message delivered with request_id={request_id} but content hash mismatch: "
                        f"expected={expected_hash} got={check_after.get('user_text_hash')}"
                    )
                return True
            time.sleep(0.5 if delivery_timeout > 1 else 0.01)

        raise DeliveryError(
            f"Message with request_id={request_id} was submitted but failed verification as a formal user turn in conversation."
        )

    def extract_response_for_request(
        self, session_id: str, request_id: str
    ) -> Dict[str, Any]:
        """Extracts the specific assistant turn following the user turn with request_id."""
        extract_js = build_extract_js(request_id)
        raw = self.evaluate(session_id, extract_js)
        try:
            return json.loads(raw)
        except Exception as exc:
            return {"status": "EVALUATE_PARSE_ERROR", "error": str(exc), "raw": raw, "text": ""}

    def wait_for_response(
        self, session_id: str, request_id: str, timeout_secs: int = 180, poll_interval: float = 4.0
    ) -> Dict[str, Any]:
        """Polls until the assistant response for request_id finishes and is verified complete."""
        start = time.time()
        last_length = -1
        stable_count = 0
        last_extract: Dict[str, Any] = {}

        while time.time() - start < timeout_secs:
            extract = self.extract_response_for_request(session_id, request_id)
            last_extract = extract

            status = extract.get("status")
            if status and status.startswith("BLOCKED"):
                return {
                    "status": "BLOCKED",
                    "reason": status,
                    "diagnostic": extract,
                    "duration_secs": round(time.time() - start, 1),
                }

            if status in ("USER_TURN_NOT_FOUND", "WAITING_FOR_ASSISTANT"):
                time.sleep(poll_interval)
                continue

            is_generating = extract.get("isGenerating", False)
            current_length = extract.get("textLength", 0)
            text = extract.get("text", "")

            if not is_generating:
                if current_length > 0:
                    if current_length == last_length:
                        stable_count += 1
                        if stable_count >= 2:
                            return {
                                "status": "COMPLETE",
                                "text": text,
                                "length": current_length,
                                "duration_secs": round(time.time() - start, 1),
                            }
                    else:
                        stable_count = 0
                        last_length = current_length

            time.sleep(poll_interval)

        # Timeout reached
        return {
            "status": "TIMEOUT",
            "is_generating": last_extract.get("isGenerating", False),
            "text": last_extract.get("text", ""),
            "length": last_extract.get("textLength", 0),
            "timeout_secs": timeout_secs,
            "diagnostic": last_extract,
        }

    def check_model_and_thinking(
        self,
        session_id: str,
        expected_model: Optional[str] = None,
        expected_thinking: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Inspects and validates active model and thinking mode against expected configuration."""
        js = build_check_model_and_thinking_js()
        raw = self.evaluate(session_id, js)
        try:
            data = json.loads(raw)
        except Exception as exc:
            return {
                "verified": False,
                "status": "BLOCKED_MODEL_UNKNOWN",
                "reason": f"Could not parse model check output: {exc}",
                "raw": raw,
            }

        detected_model = (data.get("model") or "").strip()
        detected_thinking = (data.get("thinking_mode") or "").strip()

        if not detected_model:
            return {
                "verified": False,
                "status": "BLOCKED_MODEL_UNKNOWN",
                "reason": f"Could not detect active model from ChatGPT interface (expected '{expected_model or 'any'}')",
                "detected_model": detected_model,
                "detected_thinking": detected_thinking,
            }

        if not detected_thinking or detected_thinking.lower() in ("enabled", "thinking"):
            return {
                "verified": False,
                "status": "BLOCKED_THINKING_MODE_UNKNOWN",
                "reason": f"Concrete thinking mode not detected in DOM (got '{detected_thinking or 'empty'}', expected '{expected_thinking or 'concrete mode'}')",
                "detected_model": detected_model,
                "detected_thinking": detected_thinking,
            }

        if expected_model:
            exp_m = normalize_model_identifier(expected_model)
            det_m = normalize_model_identifier(detected_model)
            if exp_m != det_m:
                return {
                    "verified": False,
                    "status": "BLOCKED_MODEL_MISMATCH",
                    "reason": f"Expected model '{expected_model}', but detected '{detected_model}'",
                    "detected_model": detected_model,
                    "detected_thinking": detected_thinking,
                }

        if expected_thinking:
            exp_t = normalize_model_identifier(expected_thinking)
            det_t = normalize_model_identifier(detected_thinking)
            if exp_t in ("enabled", "thinking"):
                return {
                    "verified": False,
                    "status": "BLOCKED_THINKING_MODE_UNKNOWN",
                    "reason": f"Generic '{expected_thinking}' is not allowed as wildcard; a concrete thinking mode must be specified",
                    "detected_model": detected_model,
                    "detected_thinking": detected_thinking,
                }
            if exp_t != det_t:
                return {
                    "verified": False,
                    "status": "BLOCKED_THINKING_MODE_MISMATCH",
                    "reason": f"Expected thinking mode '{expected_thinking}', but detected '{detected_thinking}'",
                    "detected_model": detected_model,
                    "detected_thinking": detected_thinking,
                }

        return {
            "verified": True,
            "status": "OK",
            "model": detected_model,
            "thinking_mode": detected_thinking,
        }

    def create_new_chat(self, session_id: str) -> Dict[str, Any]:
        """Navigates borrowed tab to https://chatgpt.com/ to start a fresh chat session."""
        try:
            proc = self._run_bsk("navigate", "https://chatgpt.com/", "--session", session_id, timeout=30)
            if proc.returncode != 0:
                return {"ok": False, "error": f"Failed to navigate: {proc.stderr.strip() or proc.stdout.strip()}"}
            return {"ok": True, "url": "https://chatgpt.com/"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def run_e2e_review(
        self,
        task_id: str,
        message: str,
        request_id: str,
        target_tab_id: Optional[str] = None,
        target_url_prefix: Optional[str] = None,
        timeout_secs: int = 180,
        expected_model: Optional[str] = None,
        expected_thinking: Optional[str] = None,
        skip_send: bool = False,
        is_recovery: bool = False,
        on_delivery_confirmed: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Runs the full E2E review cycle safely.

        Guarantees:
        1. Checks status first.
        2. Validates target conversation HTTPS origin and exact conversation ID.
        3. Respects borrow approval; does not bypass if denied/timed out.
        4. Re-verifies live URL after borrow (even with target_tab_id only) to detect redirect or navigation.
        5. Re-verifies URL immediately before input and send to prevent TOCTOU.
        6. Enforces idempotency (request_id and hash) and formal user-turn delivery proof.
        7. Extracts ALL text blocks for this request via DOM node roles (never only the first block).
        8. Returns BLOCKED if DOM does not support turn structure or sequence is invalid.
        9. Guarantees tab return and session cleanup in finally block; failure is recorded in diagnostics.
        """
        msg_hash = hashlib.sha256(message.strip().encode("utf-8")).hexdigest()
        journal_entry: Dict[str, Any] = {
            "task_id": task_id,
            "request_id": request_id,
            "message_hash": msg_hash,
            "target_tab_id": target_tab_id,
            "target_url_prefix": target_url_prefix,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }

        cleanup_info: Dict[str, Any] = {
            "tab_returned": None,
            "session_stopped": None,
            "errors": [],
        }

        status = self.check_status()
        if not status.daemon_running or not status.connected_browser_id:
            journal_entry["status"] = "BLOCKED_NO_BROWSER"
            journal_entry["error"] = status.error or "bsk daemon or browser not available"
            journal_entry["manual_fallback_required"] = True
            return journal_entry

        session_id = self.start_session(status.connected_browser_id, task_id)
        borrowed_tab_id: Optional[str] = None
        try:
            target_tab = self.find_target_tab(
                session_id, target_tab_id=target_tab_id, target_url_prefix=target_url_prefix
            )
            journal_entry["verified_tab_id"] = target_tab.tab_id
            journal_entry["verified_url"] = target_tab.url

            # Borrow tab with human confirmation (60s human confirmation window)
            self.borrow_tab(session_id, target_tab.tab_id, timeout_secs=60)
            borrowed_tab_id = target_tab.tab_id

            # If no specific conversation target URL was provided, ensure clean new chat
            if target_url_prefix:
                expected_target_url = target_url_prefix
            elif target_tab_id:
                expected_target_url = target_tab.url
            else:
                expected_target_url = None

            live_url = self.get_current_url(session_id)

            if not target_url_prefix and not target_tab_id:
                if is_recovery:
                    raise ConversationMismatchError("Cannot recover exchange without explicit target conversation URL or tab ID; create_new_chat is forbidden during recovery")
                if "/c/" in live_url:
                    create_res = self.create_new_chat(session_id)
                    if create_res.get("ok"):
                        live_url = self.get_current_url(session_id)
                expected_target_url = "https://chatgpt.com/"
            else:
                if not match_conversation_url(target_tab.url, live_url):
                    raise ConversationMismatchError(
                        f"Tab navigation detected: tab navigated after borrow from initial target '{target_tab.url}' to '{live_url}'"
                    )
                if expected_target_url and not match_conversation_url(expected_target_url, live_url):
                    raise ConversationMismatchError(
                        f"Tab navigation detected: tab navigated away from target conversation after borrow: from '{expected_target_url}' to '{live_url}'"
                    )
            journal_entry["verified_live_url"] = live_url

            # Unconditionally verify model and thinking mode before sending / proceeding
            m_check = self.check_model_and_thinking(
                session_id, expected_model=expected_model, expected_thinking=expected_thinking
            )
            if not m_check.get("verified"):
                journal_entry["status"] = m_check.get("status", "BLOCKED_MODEL_MISMATCH")
                journal_entry["error"] = m_check.get("reason", "Model or thinking mode verification failed")
                journal_entry["manual_fallback_required"] = True
                return journal_entry
            journal_entry["verified_model"] = m_check.get("model")
            journal_entry["verified_thinking"] = m_check.get("thinking_mode")

            if skip_send:
                user_turn_info = self.verify_user_turn_exists(session_id, request_id)
                if not user_turn_info.get("exists"):
                    journal_entry["status"] = "BLOCKED_DELIVERY_UNCONFIRMED"
                    journal_entry["error"] = f"Recovery check failed: user turn '{request_id}' not found in DOM ({user_turn_info.get('status')})"
                    journal_entry["manual_fallback_required"] = True
                    return journal_entry
                journal_entry["delivery_status"] = "CONFIRMED_USER_TURN"
            else:
                # Send prompt (with TOCTOU checks and formal user turn hash verification)
                self.send_prompt(
                    session_id, message, request_id,
                    expected_message_hash=msg_hash,
                    expected_url=live_url,
                    expected_model=expected_model,
                    expected_thinking=expected_thinking,
                )
                journal_entry["delivery_status"] = "CONFIRMED_USER_TURN"

            # Check if URL transitioned to a new conversation /c/<id>
            after_send_url = self.get_current_url(session_id)
            if "/c/" in after_send_url and validate_chatgpt_origin(after_send_url):
                journal_entry["chat_url"] = after_send_url
            else:
                journal_entry["chat_url"] = live_url

            # On delivery confirmed callback (persists /c/id immediately before wait_for_response)
            if on_delivery_confirmed:
                try:
                    on_delivery_confirmed({
                        "delivery_status": "CONFIRMED_USER_TURN",
                        "chat_url": journal_entry.get("chat_url"),
                        "tab_id": target_tab.tab_id,
                        "model": journal_entry.get("verified_model"),
                        "thinking_mode": journal_entry.get("verified_thinking"),
                    })
                except Exception as exc:
                    journal_entry["status"] = "BLOCKED_CALLBACK_FAILED"
                    journal_entry["error"] = f"on_delivery_confirmed callback failed: {exc}"
                    journal_entry["manual_fallback_required"] = True
                    return journal_entry

            # Wait for response completion
            resp_info = self.wait_for_response(session_id, request_id, timeout_secs=timeout_secs)
            if resp_info.get("status") != "COMPLETE" or not resp_info.get("text"):
                status_code = resp_info.get("status", "INCOMPLETE")
                journal_entry["status"] = "BLOCKED" if status_code.startswith("BLOCKED") else "INCOMPLETE"
                journal_entry["error"] = f"Response generation returned {status_code}: {resp_info.get('reason', '')}"
                journal_entry["diagnostic"] = resp_info
                journal_entry["manual_fallback_required"] = True
                return journal_entry

            journal_entry["status"] = "SUCCESS"
            journal_entry["response_text"] = resp_info["text"]
            journal_entry["response_length"] = resp_info["length"]
            journal_entry["manual_fallback_required"] = False
            return journal_entry

        except ApprovalRequiredError as exc:
            journal_entry["status"] = "BLOCKED_WAITING_HUMAN_APPROVAL"
            journal_entry["error"] = str(exc)
            journal_entry["manual_fallback_required"] = True
            return journal_entry
        except ConversationMismatchError as exc:
            journal_entry["status"] = "BLOCKED_CONVERSATION_MISMATCH"
            journal_entry["error"] = str(exc)
            journal_entry["manual_fallback_required"] = True
            return journal_entry
        except TabNotFoundError as exc:
            journal_entry["status"] = "BLOCKED_TAB_NOT_FOUND"
            journal_entry["error"] = str(exc)
            journal_entry["manual_fallback_required"] = True
            return journal_entry
        except DeliveryError as exc:
            journal_entry["status"] = "BLOCKED_DELIVERY_UNCONFIRMED"
            journal_entry["error"] = str(exc)
            journal_entry["manual_fallback_required"] = True
            return journal_entry
        except Exception as exc:
            journal_entry["status"] = "ERROR"
            journal_entry["error"] = str(exc)
            journal_entry["manual_fallback_required"] = True
            return journal_entry
        finally:
            if borrowed_tab_id:
                try:
                    res_tab = self.return_tab(session_id, borrowed_tab_id)
                    cleanup_info["tab_returned"] = res_tab
                    if not res_tab:
                        cleanup_info["errors"].append(f"return_tab({borrowed_tab_id}) returned False")
                except Exception as exc:
                    cleanup_info["tab_returned"] = False
                    cleanup_info["errors"].append(f"return_tab exception: {exc}")
                    logger.warning("Failed to return tab %s: %s", borrowed_tab_id, exc)
            try:
                res_sess = self.stop_session(session_id)
                cleanup_info["session_stopped"] = res_sess
                if not res_sess:
                    cleanup_info["errors"].append(f"stop_session({session_id}) returned False")
            except Exception as exc:
                cleanup_info["session_stopped"] = False
                cleanup_info["errors"].append(f"stop_session exception: {exc}")
                logger.warning("Failed to stop bsk session %s: %s", session_id, exc)

            journal_entry["cleanup_info"] = cleanup_info
            if cleanup_info["errors"]:
                journal_entry["cleanup_status"] = "CLEANUP_FAILED"
                if journal_entry.get("status") == "SUCCESS":
                    journal_entry["status"] = "WARNING_CLEANUP_FAILED"
                journal_entry["manual_fallback_required"] = True
            else:
                journal_entry["cleanup_status"] = "CLEANUP_SUCCESS"


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="ChatGPT browser relay helper via bsk")
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
    relay_p.add_argument("--target-tab-id", help="Exact tab ID to borrow")
    relay_p.add_argument("--target-url", help="Expected conversation URL prefix")
    relay_p.add_argument("--output-file", help="Path to write collected assistant response")
    relay_p.add_argument("--diagnostic-file", help="Path to write diagnostic report on non-success")
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
        res = relay.run_e2e_review(
            task_id=args.task_id,
            message=message,
            request_id=args.request_id,
            target_tab_id=args.target_tab_id,
            target_url_prefix=args.target_url,
            timeout_secs=args.timeout,
        )
        print(json.dumps(res, indent=2, ensure_ascii=False))

        # Do NOT write to formal output file unless status is SUCCESS
        if res.get("status") == "SUCCESS":
            if args.output_file:
                out_path = Path(args.output_file)
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(res.get("response_text", ""), encoding="utf-8")
                print(f"Saved formal response to {out_path}")
            return 0
        else:
            if args.diagnostic_file:
                diag_path = Path(args.diagnostic_file)
                diag_path.parent.mkdir(parents=True, exist_ok=True)
                diag_path.write_text(json.dumps(res, indent=2, ensure_ascii=False), encoding="utf-8")
                print(f"Saved diagnostic report to {diag_path}")
            return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
