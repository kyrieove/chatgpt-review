#!/usr/bin/env python3
"""Persistent task workflow and browser exchange coordinator for chatgpt-review.

CLI Commands:
- start: Initialize a persistent workflow task bound to current git HEAD.
- status: Display current task stage, round, commit, model, and next action.
- record-work: Record real commit SHA and structured test evidence before review exchange.
- exchange: Perform browser exchange with ChatGPT, parse JSON response, advance stage.
- resume: Recover after restart, or continue across chat sessions preserving context.
- resolve: User explicit decision to unpause when repair limit is exceeded.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from .browser_relay import BrowserRelay, normalize_model_identifier, validate_chatgpt_origin
    from .relay import POINTER_MARKER, project_root, start_task
except ImportError:
    SCRIPT_DIR = Path(__file__).resolve().parent
    sys.path.insert(0, str(SCRIPT_DIR))
    from browser_relay import BrowserRelay, normalize_model_identifier, validate_chatgpt_origin
    from relay import POINTER_MARKER, project_root, start_task

# State Machine Stages
STAGE_WAITING_PLAN = "WAITING_PLAN"  # 待计划
STAGE_WAITING_EXECUTION = "WAITING_EXECUTION"  # 待执行
STAGE_WAITING_REVIEW = "WAITING_REVIEW"  # 待审查
STAGE_WAITING_FIX_OR_DIAGNOSIS = "WAITING_FIX_OR_DIAGNOSIS"  # 待修复或诊断
STAGE_WAITING_ACCEPTANCE_DONE = "WAITING_ACCEPTANCE_DONE"  # 待验收完成
STAGE_BLOCKED = "BLOCKED"  # 阻塞
STAGE_PAUSED_LIMIT_EXCEEDED = "PAUSED_LIMIT_EXCEEDED"  # 超限暂停
STAGE_SIMULATED_TEST_PASSED = "SIMULATED_TEST_PASSED"  # 模拟测试通过（非生产验收）

# Decision enum constants
DECISION_PLAN_READY = "PLAN_READY"
DECISION_CHANGES_REQUESTED = "CHANGES_REQUESTED"
DECISION_ACCEPTED = "ACCEPTED"
DECISION_NEEDS_INPUT = "NEEDS_INPUT"
DECISION_BLOCKED = "BLOCKED"
VALID_DECISIONS = {
    DECISION_PLAN_READY,
    DECISION_CHANGES_REQUESTED,
    DECISION_ACCEPTED,
    DECISION_NEEDS_INPUT,
    DECISION_BLOCKED,
}

MAX_REPAIR_ROUNDS = 5


class WorkflowError(Exception):
    """Base exception for workflow operations."""


class DeliveryError(WorkflowError):
    """Raised when message delivery fails or content collision occurs."""


class WorkflowBusyError(WorkflowError):
    """Raised when task is locked by another process (exit code 5)."""


class TaskLock:
    """Inter-process file lock for per-task single-writer mutual exclusion."""

    def __init__(self, task_dir: Path):
        self.lock_path = task_dir / ".workflow.lock"
        self._fd: Optional[int] = None
        self._file_obj: Optional[Any] = None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()

    def acquire(self) -> None:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._file_obj = open(self.lock_path, "a+b")
            self._fd = self._file_obj.fileno()
        except OSError as exc:
            raise WorkflowError(f"Could not open task lock file {self.lock_path}: {exc}") from exc

        if sys.platform == "win32":
            import msvcrt
            try:
                self._file_obj.seek(0)
                msvcrt.locking(self._fd, msvcrt.LK_NBLCK, 1)
            except (OSError, IOError) as exc:
                self.release()
                raise WorkflowBusyError(f"Task is locked by another process: {self.lock_path}") from exc
        else:
            import fcntl
            try:
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (OSError, IOError) as exc:
                self.release()
                raise WorkflowBusyError(f"Task is locked by another process: {self.lock_path}") from exc

    def release(self) -> None:
        if self._file_obj is not None:
            if self._fd is not None:
                try:
                    if sys.platform == "win32":
                        import msvcrt
                        self._file_obj.seek(0)
                        try:
                            msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
                        except OSError:
                            pass
                    else:
                        import fcntl
                        try:
                            fcntl.flock(self._fd, fcntl.LOCK_UN)
                        except OSError:
                            pass
                except Exception:
                    pass
            try:
                self._file_obj.close()
            except OSError:
                pass
            self._file_obj = None
            self._fd = None


def iso_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def compute_hash(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


def get_git_head_sha(project_path: Path) -> str:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=project_path,
            check=True,
            capture_output=True,
            text=True,
        )
        return proc.stdout.strip()
    except Exception as exc:
        raise WorkflowError(f"Could not determine git HEAD SHA in {project_path}: {exc}") from exc


def is_managed_root_handoff_pointer(
    project_path: Optional[Path],
    rel_path: str,
    expected_task_id: Optional[str] = None,
) -> bool:
    """Verifies whether rel_path is the root HANDOFF.md managed pointer for the expected task."""
    norm = rel_path.strip().replace("\\", "/")
    if norm != "HANDOFF.md" or project_path is None:
        return False

    pointer_path = project_path / "HANDOFF.md"
    try:
        if pointer_path.is_symlink() or not pointer_path.is_file():
            return False
    except OSError:
        return False

    # If HANDOFF.md was tracked in HEAD, ensure the committed version was also a managed pointer
    try:
        head_proc = subprocess.run(
            ["git", "show", "HEAD:HANDOFF.md"],
            cwd=project_path,
            capture_output=True,
            text=True,
        )
        if head_proc.returncode == 0 and not head_proc.stdout.startswith(POINTER_MARKER):
            return False
    except Exception:
        return False

    task_id = (expected_task_id or "").strip()
    if not task_id:
        current_ptr = project_path / "handoffs" / ".current-task"
        try:
            if current_ptr.is_symlink() or not current_ptr.is_file():
                return False
            task_id = current_ptr.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            return False

    if not task_id or "/" in task_id or "\\" in task_id or ".." in task_id:
        return False

    target_handoff = project_path / "handoffs" / task_id / "HANDOFF.md"
    try:
        if target_handoff.is_symlink() or not target_handoff.is_file():
            return False
        content = pointer_path.read_text(encoding="utf-8").replace("\r\n", "\n")
    except (OSError, UnicodeError):
        return False

    expected_pointer = (
        f"{POINTER_MARKER}\n# Active task handoff\n\n"
        f"Read [handoffs/{task_id}/HANDOFF.md](handoffs/{task_id}/HANDOFF.md) first.\n"
    )
    return content == expected_pointer


def is_workflow_internal_path(
    rel_path: str,
    project_path: Optional[Path] = None,
    expected_task_id: Optional[str] = None,
) -> bool:
    norm = rel_path.strip().replace("\\", "/")
    if norm.startswith("handoffs/") or norm == "handoffs":
        return True
    if norm.endswith(".workflow.lock") or norm == ".workflow.lock":
        return True
    if ".tmp." in norm or norm.endswith(".tmp"):
        return True
    if norm.startswith(".git/") or norm == ".git":
        return True
    if norm == "HANDOFF.md":
        return is_managed_root_handoff_pointer(project_path, norm, expected_task_id=expected_task_id)
    return False


def verify_source_identity(
    project_path: Path,
    expected_sha: str,
    expected_task_id: Optional[str] = None,
) -> None:
    """Verifies that current git HEAD equals expected_sha and working tree has no drift.

    All tracked modifications and untracked files are treated as source drift,
    excluding only internal workflow paths (handoffs/, .workflow.lock, *.tmp*, .git/,
    and a verified root HANDOFF.md managed pointer for the expected task).
    """
    current_head = get_git_head_sha(project_path)
    if current_head.strip().lower() != expected_sha.strip().lower():
        raise WorkflowError(
            f"BLOCKED_SOURCE_CHANGED: HEAD SHA drift detected. Expected '{expected_sha}', but current HEAD is '{current_head}'."
        )

    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=project_path,
            check=True,
            capture_output=True,
            text=True,
        )
    except Exception as exc:
        raise WorkflowError(f"Failed to check git status in {project_path}: {exc}") from exc

    drifted: List[str] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(maxsplit=1)
        if len(parts) == 2:
            fpath = parts[1]
            if " -> " in fpath:
                fpath = fpath.split(" -> ")[1]
            if not is_workflow_internal_path(fpath, project_path=project_path, expected_task_id=expected_task_id):
                drifted.append(f"{parts[0]} {fpath}")

    if drifted:
        raise WorkflowError(
            f"BLOCKED_SOURCE_CHANGED: Working tree has {len(drifted)} uncommitted or untracked source file(s): "
            f"{', '.join(drifted[:5])}. All source files must match commit '{expected_sha}'."
        )


def check_working_tree_clean(project_path: Path) -> None:
    """Verifies that git working tree has no uncommitted modifications to tracked files."""
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=project_path,
            check=True,
            capture_output=True,
            text=True,
        )
        lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
        modified_tracked = [l for l in lines if not l.startswith("??")]
        if modified_tracked:
            raise WorkflowError(
                f"Working tree has uncommitted modifications ({len(modified_tracked)} files: "
                f"{', '.join(modified_tracked[:3])}). "
                "Commit all changes before recording work or performing review exchange."
            )
    except subprocess.CalledProcessError as exc:
        raise WorkflowError(f"Failed to check git status in {project_path}: {exc}") from exc


def sanitize_and_normalize_repo_url(url: str) -> str:
    """Sanitizes repository URL, removing user credentials and converting git/ssh to https."""
    clean = url.strip()
    clean = re.sub(r"https?://[^/@]+@", "https://", clean)
    m = re.match(r"^git@([^:]+):(.+?)(?:\.git)?$", clean)
    if m:
        clean = f"https://{m.group(1)}/{m.group(2)}"
    elif clean.endswith(".git"):
        clean = clean[:-4]
    return clean.rstrip("/")


def detect_remote_repo_url(project_path: Path) -> Optional[str]:
    """Attempts to detect public/safe remote origin URL from git config."""
    try:
        proc = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            cwd=project_path,
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return sanitize_and_normalize_repo_url(proc.stdout.strip())
    except Exception:
        pass
    return None


def check_commit_pushed(project_path: Path, sha: str, remote: str = "origin") -> bool:
    """Checks whether the given commit SHA has been pushed to a remote branch."""
    try:
        proc = subprocess.run(
            ["git", "branch", "-r", "--contains", sha],
            cwd=project_path,
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return True
    except Exception:
        pass
    return False


def verify_commit_exists(project_path: Path, sha: str) -> str:
    sha = sha.strip()
    if not re.fullmatch(r"^[0-9a-fA-F]{40}$", sha):
        raise WorkflowError(f"Commit SHA must be a 40-character hexadecimal string: {sha}")
    try:
        subprocess.run(
            ["git", "cat-file", "-e", f"{sha}^{{commit}}"],
            cwd=project_path,
            check=True,
            capture_output=True,
        )
        return sha
    except Exception as exc:
        raise WorkflowError(f"Commit SHA {sha} does not exist in git repository: {project_path}") from exc


def get_task_dir(project_path: Path, task_id: Optional[str] = None) -> Path:
    handoffs = project_path / "handoffs"
    if not handoffs.is_dir():
        raise WorkflowError(f"handoffs directory not found in {project_path}")
    if task_id:
        td = handoffs / task_id
        if not td.is_dir():
            raise WorkflowError(f"Task directory does not exist: {td}")
        return td
    current_ptr = handoffs / ".current-task"
    if not current_ptr.is_file():
        raise WorkflowError("No active task specified and handoffs/.current-task pointer is missing.")
    tid = current_ptr.read_text(encoding="utf-8").strip()
    td = handoffs / tid
    if not td.is_dir():
        raise WorkflowError(f"Active task pointer references missing directory: {td}")
    return td


def load_state(task_dir: Path) -> Dict[str, Any]:
    state_file = task_dir / "workflow.json"
    if not state_file.is_file():
        raise WorkflowError(f"workflow.json not found in {task_dir}")
    try:
        return json.loads(state_file.read_text(encoding="utf-8"))
    except Exception as exc:
        raise WorkflowError(f"Failed to read workflow.json in {task_dir}: {exc}") from exc


def save_state(task_dir: Path, state: Dict[str, Any]) -> None:
    """Saves workflow state using atomic temporary file replacement."""
    state["updated_at"] = iso_now()
    task_dir.mkdir(parents=True, exist_ok=True)
    temp_file = task_dir / f"workflow.tmp.{os.getpid()}.json"
    try:
        temp_file.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(temp_file, task_dir / "workflow.json")
    finally:
        if temp_file.exists():
            try:
                temp_file.unlink()
            except OSError:
                pass


def update_chat_url(task_dir: Path, new_chat_url: str) -> None:
    if not validate_chatgpt_origin(new_chat_url):
        raise WorkflowError(f"Invalid ChatGPT origin URL: {new_chat_url}")
    state = load_state(task_dir)
    current_url = state.get("chat_url")
    if current_url and current_url != new_chat_url:
        prev_urls = state.setdefault("previous_chat_urls", [])
        if current_url not in prev_urls:
            prev_urls.append(current_url)
    state["chat_url"] = new_chat_url
    save_state(task_dir, state)


def parse_and_validate_test_evidence(evidence_raw: str, expected_sha: str) -> Dict[str, Any]:
    """Validates structured test evidence. Rejects plain strings like 'PASS'."""
    text = evidence_raw.strip()
    if not text:
        raise WorkflowError("Test evidence is empty.")

    # Plain string rejection
    if text.upper() in ("PASS", "PASSED", "OK", "SUCCESS", "TRUE", "1", "YES"):
        raise WorkflowError(
            f"Plain string '{text}' is rejected as test evidence. "
            "Evidence must be structured with 'command', 'exit_code': 0, 'tested_sha', and 'executed_at'."
        )

    # Try parsing as JSON first
    parsed_json = None
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            parsed_json = data
    except Exception:
        pass

    if parsed_json:
        cmd = parsed_json.get("command") or parsed_json.get("cmd")
        exit_code = parsed_json.get("exit_code")
        tested_sha = parsed_json.get("tested_sha") or parsed_json.get("sha")
        executed_at = parsed_json.get("executed_at") or parsed_json.get("timestamp")

        if not cmd or not isinstance(cmd, str):
            raise WorkflowError("Structured test evidence JSON missing valid 'command' string.")
        if exit_code is None or exit_code != 0:
            raise WorkflowError(f"Test evidence exit_code must be 0 for passing tests (got: {exit_code}).")
        if not tested_sha or str(tested_sha).strip().lower() != expected_sha.strip().lower():
            raise WorkflowError(
                f"Test evidence tested_sha mismatch: expected '{expected_sha}', got '{tested_sha}'."
            )
        return {
            "command": cmd,
            "exit_code": 0,
            "tested_sha": expected_sha,
            "executed_at": str(executed_at or iso_now()),
            "raw_output": text,
            "evidence_hash": compute_hash(text),
        }

    # If not JSON, check for structured key-value lines in text/markdown
    cmd_match = re.search(r"(?:command|cmd)\s*[:=]\s*[`\"']?([^`\"'\n\r]+)", text, re.IGNORECASE)
    exit_match = re.search(r"exit_code\s*[:=]\s*(\d+)", text, re.IGNORECASE)
    sha_match = re.search(r"(?:tested_sha|commit|sha)\s*[:=]\s*[`\"']?([0-9a-fA-F]{40})", text, re.IGNORECASE)

    if cmd_match and exit_match and sha_match:
        cmd = cmd_match.group(1).strip()
        ec = int(exit_match.group(1).strip())
        t_sha = sha_match.group(1).strip()

        if ec != 0:
            raise WorkflowError(f"Test evidence exit_code must be 0 for passing tests (got: {ec}).")
        if t_sha.lower() != expected_sha.lower():
            raise WorkflowError(
                f"Test evidence tested_sha mismatch: expected '{expected_sha}', got '{t_sha}'."
            )
        return {
            "command": cmd,
            "exit_code": 0,
            "tested_sha": expected_sha,
            "executed_at": iso_now(),
            "raw_output": text,
            "evidence_hash": compute_hash(text),
        }

    raise WorkflowError(
        "Invalid test evidence: unstructured text is rejected. "
        "Test evidence must provide structured 'command', 'exit_code': 0, 'tested_sha' matching current HEAD, and timestamp."
    )


def parse_and_validate_review_response(
    response_text: str,
    expected_task_id: str,
    expected_round: int,
    expected_head_sha: str,
    expected_stage: str = STAGE_WAITING_REVIEW,
) -> Dict[str, Any]:
    """Extracts and strictly validates machine-readable JSON block from ChatGPT response.

    Fails closed if block is missing, ambiguous (>1 blocks), has malformed round or SHA mismatch,
    or decision is invalid for current stage.
    """
    if not response_text:
        return {"valid": False, "status": "BLOCKED_EMPTY_RESPONSE", "reason": "Empty response text from ChatGPT", "data": None}

    pattern = re.compile(
        r"```(?:json:chatgpt-review|json|chatgpt-review)?\s*(\{[\s\S]*?\})\s*```",
        re.MULTILINE,
    )
    raw_blocks = pattern.findall(response_text)
    candidate_blocks: List[Dict[str, Any]] = []

    for block in raw_blocks:
        try:
            data = json.loads(block)
            if isinstance(data, dict) and "task_id" in data and "decision" in data:
                candidate_blocks.append(data)
        except Exception:
            pass

    if len(candidate_blocks) == 0:
        # Fallback for DOM-extracted text where code blocks were stripped of markdown backticks
        raw_json_blocks = re.findall(r"(\{\s*\"task_id\"[\s\S]*?\n\})", response_text)
        for block in raw_json_blocks:
            try:
                data = json.loads(block)
                if isinstance(data, dict) and "task_id" in data and "decision" in data:
                    candidate_blocks.append(data)
            except Exception:
                pass

    if len(candidate_blocks) == 0:
        return {
            "valid": False,
            "status": "BLOCKED_NO_CONTRACT_BLOCK",
            "reason": "Missing machine-readable JSON block matching schema in ChatGPT response.",
            "data": None,
        }

    if len(candidate_blocks) > 1:
        return {
            "valid": False,
            "status": "BLOCKED_AMBIGUOUS_FEEDBACK",
            "reason": f"Ambiguous response: found {len(candidate_blocks)} conflicting contract JSON blocks in response.",
            "data": None,
        }

    data = candidate_blocks[0]

    # Validate task_id
    if data.get("task_id") != expected_task_id:
        return {
            "valid": False,
            "status": "BLOCKED_TASK_ID_MISMATCH",
            "reason": f"Task ID mismatch: expected '{expected_task_id}', got '{data.get('task_id')}'",
            "data": data,
        }

    # Safe validate round
    raw_round = data.get("round")
    try:
        parsed_round = int(raw_round)
    except (TypeError, ValueError):
        return {
            "valid": False,
            "status": "BLOCKED_MALFORMED_ROUND",
            "reason": f"Invalid round format in JSON: expected integer {expected_round}, got '{raw_round}'",
            "data": data,
        }

    if parsed_round != int(expected_round):
        return {
            "valid": False,
            "status": "BLOCKED_ROUND_MISMATCH",
            "reason": f"Round mismatch: expected {expected_round}, got {parsed_round}",
            "data": data,
        }

    # Validate reviewed_head_sha
    reviewed_sha = str(data.get("reviewed_head_sha") or "").strip().lower()
    expected_sha = str(expected_head_sha).strip().lower()
    if reviewed_sha != expected_sha:
        return {
            "valid": False,
            "status": "BLOCKED_SHA_MISMATCH",
            "reason": f"SHA mismatch: expected '{expected_sha}', got '{reviewed_sha}'",
            "data": data,
        }

    # Validate decision
    decision = data.get("decision")
    if decision not in VALID_DECISIONS:
        return {
            "valid": False,
            "status": "BLOCKED_INVALID_DECISION",
            "reason": f"Invalid decision '{decision}'. Must be one of {sorted(VALID_DECISIONS)}",
            "data": data,
        }

    # Validate stage-allowed decisions
    if expected_stage == STAGE_WAITING_PLAN:
        if decision not in (DECISION_PLAN_READY, DECISION_NEEDS_INPUT, DECISION_BLOCKED):
            return {
                "valid": False,
                "status": "BLOCKED_INVALID_STAGE_DECISION",
                "reason": f"Decision '{decision}' is not allowed during planning stage (allowed: PLAN_READY, NEEDS_INPUT, BLOCKED).",
                "data": data,
            }
    elif expected_stage == STAGE_WAITING_REVIEW:
        if decision not in (DECISION_ACCEPTED, DECISION_CHANGES_REQUESTED, DECISION_NEEDS_INPUT, DECISION_BLOCKED):
            return {
                "valid": False,
                "status": "BLOCKED_INVALID_STAGE_DECISION",
                "reason": f"Decision '{decision}' is not allowed during review stage (allowed: ACCEPTED, CHANGES_REQUESTED, NEEDS_INPUT, BLOCKED).",
                "data": data,
            }

    # Validate next_action
    next_action = data.get("next_action")
    if not isinstance(next_action, str) or not next_action.strip():
        return {
            "valid": False,
            "status": "BLOCKED_MISSING_NEXT_ACTION",
            "reason": "Missing or empty 'next_action' field in review feedback.",
            "data": data,
        }

    # Validate findings
    if "findings" not in data or not isinstance(data.get("findings"), list):
        return {
            "valid": False,
            "status": "BLOCKED_INVALID_FINDINGS",
            "reason": "'findings' must be present and must be a list.",
            "data": data,
        }

    findings = data["findings"]
    if decision == DECISION_CHANGES_REQUESTED:
        if len(findings) == 0:
            return {
                "valid": False,
                "status": "BLOCKED_EMPTY_FINDINGS",
                "reason": "Decision 'CHANGES_REQUESTED' requires a non-empty findings list.",
                "data": data,
            }

    for idx, item in enumerate(findings):
        if not isinstance(item, dict):
            return {
                "valid": False,
                "status": "BLOCKED_MALFORMED_FINDING",
                "reason": f"Finding at index {idx} must be a dictionary/object.",
                "data": data,
            }
        fid = item.get("id") or item.get("code")
        desc = item.get("description") or item.get("summary") or item.get("detail") or item.get("message")
        if not fid or not isinstance(fid, str) or not fid.strip():
            return {
                "valid": False,
                "status": "BLOCKED_MALFORMED_FINDING",
                "reason": f"Finding at index {idx} must have a non-empty 'id' or 'code' string.",
                "data": data,
            }
        if not desc or not isinstance(desc, str) or not desc.strip():
            return {
                "valid": False,
                "status": "BLOCKED_MALFORMED_FINDING",
                "reason": f"Finding '{fid}' must have a non-empty description.",
                "data": data,
            }
        item["id"] = fid
        item["description"] = desc

    return {"valid": True, "status": "OK", "reason": "OK", "data": data}


def apply_review_decision(
    task_dir: Path,
    decision: str,
    findings: List[Any],
    reviewed_sha: str,
    next_action: str,
    is_simulated: bool = False,
) -> Dict[str, Any]:
    """Applies a verified decision from ChatGPT to update the state machine."""
    state = load_state(task_dir)
    root = project_root(task_dir.parent.parent)

    # 1. Source identity verification at feedback application
    verify_source_identity(root, reviewed_sha, expected_task_id=state.get("task_id") or task_dir.name)

    # 2. Stage check
    eff_stage = state.get("business_stage") or state.get("stage")
    if eff_stage == STAGE_WAITING_PLAN:
        if decision not in (DECISION_PLAN_READY, DECISION_NEEDS_INPUT, DECISION_BLOCKED):
            raise WorkflowError(
                f"Decision '{decision}' is not allowed during planning stage (allowed: PLAN_READY, NEEDS_INPUT, BLOCKED)."
            )
    elif eff_stage in (STAGE_WAITING_REVIEW, STAGE_WAITING_FIX_OR_DIAGNOSIS):
        if decision not in (DECISION_ACCEPTED, DECISION_CHANGES_REQUESTED, DECISION_NEEDS_INPUT, DECISION_BLOCKED):
            raise WorkflowError(
                f"Decision '{decision}' is not allowed during review stage (allowed: ACCEPTED, CHANGES_REQUESTED, NEEDS_INPUT, BLOCKED)."
            )
    else:
        raise WorkflowError(f"Cannot apply review decision during stage '{eff_stage}'.")

    if is_simulated:
        state["is_simulated_test"] = True

    # Preconditions for ACCEPTED:
    if decision == DECISION_ACCEPTED:
        if not state.get("test_evidence_recorded"):
            raise WorkflowError("Cannot mark ACCEPTED: test evidence was never recorded for this task.")

        last_ev = state.get("last_test_evidence")
        if not isinstance(last_ev, dict) or last_ev.get("tested_sha") != state.get("head_sha"):
            raise WorkflowError("Cannot mark ACCEPTED: recorded test evidence does not match current task head_sha.")

        # If simulated test, cannot reach production WAITING_ACCEPTANCE_DONE!
        if state.get("is_simulated_test"):
            state["stage"] = STAGE_SIMULATED_TEST_PASSED
            state["business_stage"] = STAGE_SIMULATED_TEST_PASSED
            state["next_action"] = (
                "Simulated mock test accepted. Production acceptance requires real ChatGPT review."
            )
            state["last_feedback"] = {
                "decision": decision,
                "findings": findings,
                "reviewed_head_sha": reviewed_sha,
                "next_action": next_action,
                "timestamp": iso_now(),
            }
            save_state(task_dir, state)
            return state

        state["stage"] = STAGE_WAITING_ACCEPTANCE_DONE
        state["business_stage"] = STAGE_WAITING_ACCEPTANCE_DONE
        state["blocked_from_stage"] = None
        state["next_action"] = next_action or "Task accepted. Awaiting final user coordinator sign-off."

    elif decision == DECISION_PLAN_READY:
        state["stage"] = STAGE_WAITING_EXECUTION
        state["business_stage"] = STAGE_WAITING_EXECUTION
        state["blocked_from_stage"] = None
        state["next_action"] = next_action or "Implement planned changes, verify tests, then record-work."

    elif decision == DECISION_CHANGES_REQUESTED:
        state["total_repair_rounds"] = state.get("total_repair_rounds", 0) + 1
        curr_repair_round = state["total_repair_rounds"]

        # Track findings for diagnosis calculation:
        # A finding triggers diagnosis only after 2 failed repair attempts following its introduction!
        tracked = state.setdefault("tracked_findings", {})
        has_two_failed_repairs = False

        for f in findings:
            if isinstance(f, dict):
                fid = f.get("id")
                if fid:
                    if fid not in tracked:
                        # Initial report of this finding (0 failed repairs yet)
                        tracked[fid] = {
                            "first_round": curr_repair_round,
                            "failed_repairs": [],
                        }
                    else:
                        rec = tracked[fid]
                        if curr_repair_round > rec["first_round"]:
                            if curr_repair_round not in rec["failed_repairs"]:
                                rec["failed_repairs"].append(curr_repair_round)
                            if len(rec["failed_repairs"]) >= 2:
                                has_two_failed_repairs = True

        if has_two_failed_repairs:
            state["is_diagnosis"] = True
            state["consecutive_no_improvement"] = 2
        else:
            state["consecutive_no_improvement"] = 0

        state["findings_history"] = findings

        if state["total_repair_rounds"] >= MAX_REPAIR_ROUNDS:
            state["stage"] = STAGE_PAUSED_LIMIT_EXCEEDED
            state["business_stage"] = STAGE_PAUSED_LIMIT_EXCEEDED
            state["next_action"] = (
                f"Maximum repair rounds ({MAX_REPAIR_ROUNDS}) exceeded. "
                "Pausing workflow for human user review and decision. Run workflow resolve to resume."
            )
        else:
            state["stage"] = STAGE_WAITING_FIX_OR_DIAGNOSIS
            state["business_stage"] = STAGE_WAITING_FIX_OR_DIAGNOSIS
            state["blocked_from_stage"] = None
            if state.get("is_diagnosis"):
                state["next_action"] = (
                    "Two consecutive repair attempts failed to resolve findings: transitioned to diagnosis mode. "
                    "Execute diagnostic tests, record-work, and report findings."
                )
            else:
                state["next_action"] = next_action or "Fix requested findings, run tests, and record-work."

    elif decision in (DECISION_NEEDS_INPUT, DECISION_BLOCKED):
        state["stage"] = STAGE_BLOCKED
        state["blocked_from_stage"] = eff_stage
        state["next_action"] = f"Workflow blocked by ChatGPT decision '{decision}': {next_action}"

    state["last_feedback"] = {
        "decision": decision,
        "findings": findings,
        "reviewed_head_sha": reviewed_sha,
        "next_action": next_action,
        "timestamp": iso_now(),
    }
    save_state(task_dir, state)
    return state


def start_workflow(
    project: Path,
    title: str,
    task_id: Optional[str] = None,
    slug: Optional[str] = None,
    chat_url: Optional[str] = None,
    model: Optional[str] = None,
    thinking_mode: Optional[str] = None,
    repo_url: Optional[str] = None,
    pr_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Initializes task files and persistent workflow state bound to current git HEAD."""
    root = project_root(project)
    head_sha = get_git_head_sha(root)

    ns = argparse.Namespace(
        project=str(root),
        title=title,
        task_id=task_id,
        slug=slug,
    )
    start_task(ns)

    task_dir = get_task_dir(root, task_id)
    actual_task_id = task_dir.name

    safe_repo_url = sanitize_and_normalize_repo_url(repo_url) if repo_url else detect_remote_repo_url(root)
    safe_pr_url = pr_url.strip() if pr_url else None
    commit_pushed = check_commit_pushed(root, head_sha)

    with TaskLock(task_dir):
        state: Dict[str, Any] = {
            "task_id": actual_task_id,
            "title": title,
            "round": 0,
            "stage": STAGE_WAITING_PLAN,
            "business_stage": STAGE_WAITING_PLAN,
            "blocked_from_stage": None,
            "head_sha": head_sha,
            "repo_url": safe_repo_url,
            "pr_url": safe_pr_url,
            "commit_publish_status": "PUSHED" if commit_pushed else "PENDING_PUBLISH",
            "chat_url": chat_url,
            "previous_chat_urls": [],
            "model": model,
            "thinking_mode": thinking_mode,
            "active_transaction": None,
            "next_action": "Run workflow exchange to request initial task plan from ChatGPT.",
            "findings_history": [],
            "tracked_findings": {},
            "consecutive_no_improvement": 0,
            "is_diagnosis": False,
            "total_repair_rounds": 0,
            "test_evidence_recorded": False,
            "last_test_evidence": None,
            "last_feedback": None,
            "continuation_brief": None,
            "created_at": iso_now(),
            "updated_at": iso_now(),
        }
        save_state(task_dir, state)
        return state


def is_recoverable_source_drift_block(state: Dict[str, Any]) -> bool:
    """Returns True only when the task is BLOCKED specifically due to BLOCKED_SOURCE_CHANGED
    from an active pre-acceptance work/review business stage."""
    if state.get("stage") != STAGE_BLOCKED:
        return False
    b_stage = state.get("business_stage")
    blocked_from = state.get("blocked_from_stage") or b_stage
    allowed_stages = (
        STAGE_WAITING_EXECUTION,
        STAGE_WAITING_FIX_OR_DIAGNOSIS,
        STAGE_WAITING_REVIEW,
    )
    if b_stage not in allowed_stages or blocked_from not in allowed_stages:
        return False
    reason = str(state.get("next_action") or "").strip()
    return reason.startswith("BLOCKED_SOURCE_CHANGED:")


VALID_DELIVERY_STATUSES = {
    "SEND_ATTEMPTED",
    "SENT_UNCONFIRMED",
    "DELIVERY_UNKNOWN",
    "CONFIRMED_USER_TURN",
}


def validate_transaction_structure(
    active_tx: Any,
    expected_task_id: str,
) -> Dict[str, Any]:
    """Validates transaction internal structure, types, hashes, and consistency fail-closed.

    Independent of current Git HEAD or round exchange expectations, ensuring that any
    active or historical transaction in state is structurally valid before any
    operations (including controlled invalidation) can inspect or alter it.
    """
    if not isinstance(active_tx, dict):
        raise DeliveryError("Malformed active_transaction: must be a JSON object/dict.")

    if "applied" not in active_tx or type(active_tx["applied"]) is not bool:
        raise DeliveryError(
            "Malformed active_transaction: missing or invalid 'applied' field (must be a strict boolean)."
        )
    is_applied = active_tx["applied"]

    required_str_fields = ("request_id", "kind", "head_sha", "message", "message_hash", "delivery_status")
    for field in required_str_fields:
        val = active_tx.get(field)
        if not isinstance(val, str) or not val.strip():
            raise DeliveryError(
                f"Malformed active_transaction: missing or invalid required string field '{field}'."
            )

    tx_kind = active_tx["kind"].strip()
    if tx_kind not in ("plan", "review"):
        raise DeliveryError(
            f"Malformed active_transaction: invalid 'kind' '{tx_kind}' (must be 'plan' or 'review')."
        )

    tx_head_sha = active_tx["head_sha"].strip().lower()
    if not re.fullmatch(r"^[0-9a-f]{40}$", tx_head_sha):
        raise DeliveryError(
            f"Malformed active_transaction: 'head_sha' must be a 40-character hex SHA (got '{active_tx['head_sha']}')."
        )

    raw_round = active_tx.get("round")
    if type(raw_round) is not int:
        raise DeliveryError(
            f"Malformed active_transaction: 'round' must be a strict integer (got {type(raw_round).__name__}: {raw_round!r})."
        )
    tx_round = raw_round
    if tx_round < 1:
        raise DeliveryError(f"Malformed active_transaction: 'round' must be >= 1 (got {tx_round}).")

    delivery_status = active_tx["delivery_status"].strip()
    if delivery_status not in VALID_DELIVERY_STATUSES:
        raise DeliveryError(
            f"Malformed active_transaction: invalid delivery_status '{delivery_status}'."
        )

    tx_req_id = active_tx["request_id"].strip()
    expected_tx_req_id = f"req-{expected_task_id}-r{tx_round}-{tx_kind}"
    if tx_req_id != expected_tx_req_id:
        raise DeliveryError(
            f"Transaction request_id conflict: active_transaction has '{tx_req_id}', "
            f"expected '{expected_tx_req_id}'."
        )

    computed_hash = compute_hash(active_tx["message"]).lower()
    recorded_hash = active_tx["message_hash"].strip().lower()
    if computed_hash != recorded_hash:
        raise DeliveryError(
            f"Transaction content collision for request '{tx_req_id}': "
            f"recorded message_hash '{recorded_hash}' does not match message content hash '{computed_hash}'."
        )

    if is_applied:
        if delivery_status != "CONFIRMED_USER_TURN":
            raise DeliveryError(
                f"Malformed active_transaction: applied=True requires delivery_status 'CONFIRMED_USER_TURN' "
                f"(got '{delivery_status}')."
            )

    return active_tx


def _invalidate_stale_transaction(
    state: Dict[str, Any],
    reason: str,
    superseded_by_sha: str,
) -> Optional[str]:
    """Invalidates an existing active_transaction when superseded by source drift, preserving audit trail."""
    active_tx = state.get("active_transaction")
    if active_tx is None:
        return None
    task_id = str(state.get("task_id") or "")
    validate_transaction_structure(active_tx, task_id)

    if active_tx["applied"] is True:
        return None

    tx_sha = str(active_tx.get("head_sha") or "").strip().lower()
    sup_sha = str(superseded_by_sha or "").strip().lower()
    if tx_sha and sup_sha and tx_sha != sup_sha:
        req_id = str(active_tx.get("request_id") or "")
        invalidated_entry = {
            **active_tx,
            "invalidated_at": iso_now(),
            "invalidated_reason": reason,
            "superseded_by_head_sha": superseded_by_sha,
        }
        state.setdefault("invalidated_transactions", []).append(invalidated_entry)
        state["active_transaction"] = None
        return req_id
    return None


def record_work(
    project: Path,
    task_id: Optional[str] = None,
    head_sha: Optional[str] = None,
    test_evidence: Optional[str] = None,
    summary: Optional[str] = None,
) -> Dict[str, Any]:
    """Records real commit SHA and structured test evidence; advances stage to WAITING_REVIEW."""
    root = project_root(project)
    task_dir = get_task_dir(root, task_id)

    with TaskLock(task_dir):
        state = load_state(task_dir)
        eff_stage = state.get("business_stage") or state.get("stage")

        # Fail-closed transaction integrity validation on any active_transaction in state
        task_id_for_tx = str(state.get("task_id") or task_dir.name)
        active_tx = state.get("active_transaction")
        if active_tx is not None:
            validate_transaction_structure(active_tx, task_id_for_tx)

        if state.get("stage") == STAGE_PAUSED_LIMIT_EXCEEDED or eff_stage == STAGE_PAUSED_LIMIT_EXCEEDED:
            raise WorkflowError(
                "Workflow is paused because repair round limit was exceeded. Run workflow resolve to resume."
            )
        if state.get("stage") == STAGE_WAITING_ACCEPTANCE_DONE or eff_stage == STAGE_WAITING_ACCEPTANCE_DONE:
            raise WorkflowError("Task has already been ACCEPTED. Cannot record further work.")

        is_drift_recovery = is_recoverable_source_drift_block(state)
        if state.get("stage") == STAGE_BLOCKED:
            if not is_drift_recovery:
                raise WorkflowError(
                    f"Cannot record work during stage '{eff_stage}'. "
                    "Task is BLOCKED for a non-source-drift reason or non-recoverable stage."
                )
        elif eff_stage not in (STAGE_WAITING_EXECUTION, STAGE_WAITING_FIX_OR_DIAGNOSIS):
            raise WorkflowError(
                f"Cannot record work during stage '{eff_stage}'. "
                "Work can only be recorded when business stage is WAITING_EXECUTION or WAITING_FIX_OR_DIAGNOSIS."
            )

        previous_head_sha = str(state.get("head_sha") or "").strip()
        block_reason_before = str(state.get("next_action") or "").strip()

        current_head = get_git_head_sha(root)
        if head_sha:
            if head_sha.strip().lower() != current_head.lower():
                raise WorkflowError(
                    f"Specified head_sha '{head_sha}' does not match current git HEAD '{current_head}'."
                )

        verified_sha = verify_commit_exists(root, current_head)

        if is_drift_recovery and eff_stage == STAGE_WAITING_REVIEW:
            if verified_sha.lower() == previous_head_sha.lower():
                raise WorkflowError(
                    f"Cannot recover source-drift blocked review using previously recorded stale head_sha '{verified_sha}'. "
                    "Commit the new changes and record work with the new HEAD SHA."
                )

        # Source identity verification: HEAD must equal commit and no uncommitted/untracked drift
        verify_source_identity(root, verified_sha, expected_task_id=state.get("task_id") or task_dir.name)

        evidence_arg = (test_evidence or "").strip()
        if not evidence_arg:
            raise WorkflowError("Test evidence must be specified.")

        evidence_file = Path(evidence_arg)
        if not evidence_file.is_file():
            evidence_file = root / evidence_arg
        if not evidence_file.is_file():
            raise WorkflowError(
                f"Test evidence must be an existing readable file containing structured test evidence (got raw string or non-existent path: '{evidence_arg}')."
            )

        evidence_content = evidence_file.read_text(encoding="utf-8").strip()
        parsed_ev = parse_and_validate_test_evidence(evidence_content, verified_sha)
        parsed_ev["summary"] = summary or "Implementation work"
        parsed_ev["file_path"] = str(evidence_file)
        parsed_ev["file_hash"] = compute_hash(evidence_content)

        invalidated_req_id: Optional[str] = None
        if is_drift_recovery or (
            isinstance(state.get("active_transaction"), dict)
            and state["active_transaction"].get("applied") is False
        ):
            invalidated_req_id = _invalidate_stale_transaction(
                state=state,
                reason=(
                    f"Superseded by new commit '{verified_sha}' during record_work "
                    f"(previous head_sha '{previous_head_sha}')."
                ),
                superseded_by_sha=verified_sha,
            )

        if is_drift_recovery:
            state.setdefault("drift_recovery_history", []).append(
                {
                    "recovered_at": iso_now(),
                    "blocked_from_stage": state.get("blocked_from_stage") or eff_stage,
                    "blocked_reason": block_reason_before,
                    "previous_head_sha": previous_head_sha,
                    "new_head_sha": verified_sha,
                    "previous_round": state.get("round", 0),
                    "new_round": state.get("round", 0) + 1,
                    "invalidated_request_id": invalidated_req_id,
                    "evidence_hash": parsed_ev["file_hash"],
                }
            )

        # Append to TEST_RESULTS.md in task_dir
        test_results_path = task_dir / "TEST_RESULTS.md"
        recovery_line = (
            f"- Recovery Mode: `SOURCE_DRIFT_RECOVERY` (previous SHA: `{previous_head_sha}`, "
            f"invalidated tx: `{invalidated_req_id or 'none'}`)\n"
            if is_drift_recovery
            else ""
        )
        entry = (
            f"\n\n## Work Record - {iso_now()}\n"
            f"- Commit SHA: `{verified_sha}`\n"
            f"{recovery_line}"
            f"- Evidence File: `{evidence_file}`\n"
            f"- Command: `{parsed_ev['command']}`\n"
            f"- Exit Code: `{parsed_ev['exit_code']}`\n"
            f"- Executed At: `{parsed_ev['executed_at']}`\n"
            f"- Evidence Hash: `{parsed_ev['file_hash']}`\n"
            f"- Summary: {summary or 'Implementation work'}\n"
            f"- Test Evidence Content:\n```\n{evidence_content}\n```\n"
        )
        with test_results_path.open("a", encoding="utf-8") as f:
            f.write(entry)

        # Update state
        state["head_sha"] = verified_sha
        state["test_evidence_recorded"] = True
        state["last_test_evidence"] = parsed_ev
        state["stage"] = STAGE_WAITING_REVIEW
        state["business_stage"] = STAGE_WAITING_REVIEW
        state["blocked_from_stage"] = None
        state["commit_publish_status"] = "PUSHED" if check_commit_pushed(root, verified_sha) else "PENDING_PUBLISH"
        state["round"] = state.get("round", 0) + 1
        state["next_action"] = "Run workflow exchange to send review request to ChatGPT."
        save_state(task_dir, state)
        return state


def build_plan_request_message(
    state: Dict[str, Any], task_dir: Path, request_id: str, project_path: Path
) -> str:
    task_id = state["task_id"]
    head_sha = state["head_sha"]
    title = state.get("title", task_id)

    brief_file = task_dir / "TASK_BRIEF.md"
    if not brief_file.is_file():
        raise WorkflowError(f"Required review material 'TASK_BRIEF.md' is missing in {task_dir}.")
    task_brief = brief_file.read_text(encoding="utf-8").strip()
    if not task_brief:
        raise WorkflowError(f"Required review material 'TASK_BRIEF.md' is empty in {task_dir}.")

    repo_posix = project_path.resolve().as_posix()
    repo_url = state.get("repo_url")
    pr_url = state.get("pr_url")
    commit_pushed = (state.get("commit_publish_status") == "PUSHED")

    commit_url = f"{repo_url}/commit/{head_sha}" if repo_url else "Not configured (BLOCKED_MATERIALS)"
    tree_url = f"{repo_url}/tree/{head_sha}" if repo_url else "Not configured (BLOCKED_MATERIALS)"
    pr_link = pr_url if pr_url else "Not configured"
    remote_status = "PUSHED_TO_REMOTE" if commit_pushed else "UNPUSHED / PENDING_PUBLISH (Commit exists locally only)"
    remote_brief_path = f"handoffs/{task_id}/TASK_BRIEF.md"

    try:
        git_log = subprocess.run(
            ["git", "log", "-n", "3", "--oneline"],
            cwd=project_path,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except Exception:
        git_log = f"{head_sha[:8]} Baseline commit"

    continuation_section = ""
    if state.get("continuation_brief"):
        continuation_section = f"\n## Continuation Context\n{state['continuation_brief']}\n"

    findings_section = ""
    history = state.get("findings_history", [])
    if history:
        items = "\n".join(f"- `{f.get('id', 'F-?')}`: {f.get('description', '')}" for f in history if isinstance(f, dict))
        findings_section = f"\n## Previous Unresolved Findings\n{items}\n"

    return f"""# Antigravity Task Plan Request [{request_id}]

- **Task ID**: `{task_id}`
- **Round**: 1 (Planning)
- **Input Baseline Commit SHA**: `{head_sha}`
- **Repository Root (Local Context)**: `{repo_posix}`
- **Task Title**: {title}

## Readable Materials & Remote Entry Points
- **Exact Commit URL**: {commit_url}
- **Source Tree at Commit**: {tree_url}
- **Pull Request URL**: {pr_link}
- **Commit Publish Status**: {remote_status}
- **Task Brief Remote Path**: `{remote_brief_path}`

## Task Brief & Requirements
{task_brief}
{continuation_section}{findings_section}
## Planning Instructions
请务必实际访问并阅读上述精确 SHA (`{head_sha}`) 的代码及改动。
若因未推送或网络无法读取指定 SHA 代码，必须直接返回 decision 为 BLOCKED 或 NEEDS_INPUT，严禁在未读取真实源码的情况下直接判定。
请在回复末尾附带严格符合下述契约的机器可读 JSON 区块：

```json:chatgpt-review
{{
  "task_id": "{task_id}",
  "round": 1,
  "reviewed_head_sha": "{head_sha}",
  "decision": "<PLAN_READY | NEEDS_INPUT | BLOCKED>",
  "findings": [],
  "next_action": "实施计划说明"
}}
```
"""


def build_review_request_message(
    state: Dict[str, Any], task_dir: Path, request_id: str, project_path: Path
) -> str:
    task_id = state["task_id"]
    head_sha = state["head_sha"]
    rnd = state.get("round", 1)
    is_diag = state.get("is_diagnosis", False)
    diag_flag = " (DIAGNOSIS MODE)" if is_diag else ""

    brief_file = task_dir / "TASK_BRIEF.md"
    if not brief_file.is_file():
        raise WorkflowError(f"Required review material 'TASK_BRIEF.md' is missing in {task_dir}.")
    task_brief = brief_file.read_text(encoding="utf-8").strip()
    if not task_brief:
        raise WorkflowError(f"Required review material 'TASK_BRIEF.md' is empty in {task_dir}.")

    repo_posix = project_path.resolve().as_posix()
    repo_url = state.get("repo_url")
    pr_url = state.get("pr_url")
    commit_pushed = (state.get("commit_publish_status") == "PUSHED")

    commit_url = f"{repo_url}/commit/{head_sha}" if repo_url else "Not configured (BLOCKED_MATERIALS)"
    tree_url = f"{repo_url}/tree/{head_sha}" if repo_url else "Not configured (BLOCKED_MATERIALS)"
    pr_link = pr_url if pr_url else "Not configured"
    remote_status = "PUSHED_TO_REMOTE" if commit_pushed else "UNPUSHED / PENDING_PUBLISH (Commit exists locally only)"
    remote_brief_path = f"handoffs/{task_id}/TASK_BRIEF.md"
    remote_test_results_path = f"handoffs/{task_id}/TEST_RESULTS.md"

    try:
        git_log = subprocess.run(
            ["git", "log", "-n", "5", "--oneline"],
            cwd=project_path,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except Exception:
        git_log = f"{head_sha[:8]} Reviewed commit"

    try:
        git_diff = subprocess.run(
            ["git", "diff", "--stat", f"{head_sha}~1", head_sha],
            cwd=project_path,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except Exception:
        git_diff = "Diff stat not available"

    last_evidence = state.get("last_test_evidence")
    if isinstance(last_evidence, dict):
        evidence_str = (
            f"- Command: `{last_evidence.get('command')}`\n"
            f"- Exit Code: `{last_evidence.get('exit_code')}`\n"
            f"- Tested Commit: `{last_evidence.get('tested_sha')}`\n"
            f"- Execution Timestamp: `{last_evidence.get('executed_at')}`\n"
            f"- Evidence File: `{last_evidence.get('file_path', 'structured')}`\n"
            f"- Evidence Hash: `{last_evidence.get('file_hash', last_evidence.get('evidence_hash'))}`\n"
            f"```\n{last_evidence.get('raw_output', '')}\n```"
        )
    else:
        evidence_str = str(last_evidence or "No structured test evidence recorded.")

    continuation_section = ""
    if state.get("continuation_brief"):
        continuation_section = f"\n## Continuation Context\n{state['continuation_brief']}\n"

    findings_section = ""
    history = state.get("findings_history", [])
    if history:
        items = "\n".join(f"- `{f.get('id', 'F-?')}`: {f.get('description', '')}" for f in history if isinstance(f, dict))
        findings_section = f"\n## Previous Unresolved Findings (Please explicitly re-verify each)\n{items}\n"

    return f"""# Antigravity Code Review Request [{request_id}]{diag_flag}

- **Task ID**: `{task_id}`
- **Round**: {rnd}
- **Reviewed Head Commit SHA**: `{head_sha}`
- **Repository Root (Local Context)**: `{repo_posix}`

## Readable Materials & Remote Entry Points
- **Exact Commit URL**: {commit_url}
- **Source Tree at Commit**: {tree_url}
- **Pull Request URL**: {pr_link}
- **Commit Publish Status**: {remote_status}
- **Task Brief Remote Path**: `{remote_brief_path}`
- **Test Evidence Remote Path**: `{remote_test_results_path}`

## Task Brief & Acceptance Scope
{task_brief}

## Recent Git Commits
```
{git_log}
```

## Diff Stat for Reviewed Commit (Metadata Only)
```
{git_diff}
```

## Verified Test Evidence
{evidence_str}
{continuation_section}{findings_section}
## Review Instructions
请针对上述精确 SHA (`{head_sha}`) 的实际源码与已验证测试证据进行严格、独立的审查（注意：Diff Stat 仅为摘要，不能替代真实代码）。
若因未推送或网络无法读取指定 SHA 代码，必须直接返回 decision 为 BLOCKED 或 NEEDS_INPUT，严禁在未读取真实源码的情况下直接给出 ACCEPTED。
请在回复末尾附带严格符合下述契约的机器可读 JSON 区块（注意：禁止使用默认 ACCEPTED，请根据真实审查结论判定）：

```json:chatgpt-review
{{
  "task_id": "{task_id}",
  "round": {rnd},
  "reviewed_head_sha": "{head_sha}",
  "decision": "<ACCEPTED | CHANGES_REQUESTED | NEEDS_INPUT | BLOCKED>",
  "findings": [
    // 若 decision 为 CHANGES_REQUESTED，必须提供非空列表：
    // {{"id": "F-001", "description": "问题描述", "file": "路径", "line": 0}}
  ],
  "next_action": "下一步动作说明"
}}
```
"""


def validate_active_transaction(
    active_tx: Any,
    expected_task_id: str,
    expected_req_id: str,
    expected_kind: str,
    expected_round: int,
    expected_head_sha: str,
) -> Dict[str, Any]:
    """Validates any existing active_transaction fail-closed for structure, types, hash, and stage consistency."""
    validate_transaction_structure(active_tx, expected_task_id)

    is_applied = active_tx["applied"]
    tx_kind = active_tx["kind"].strip()
    tx_head_sha = active_tx["head_sha"].strip().lower()
    tx_round = active_tx["round"]
    tx_req_id = active_tx["request_id"].strip()
    delivery_status = active_tx["delivery_status"].strip()

    if not is_applied:
        if tx_kind != expected_kind:
            raise DeliveryError(
                f"Transaction kind conflict: active_transaction has kind '{tx_kind}', "
                f"expected '{expected_kind}'."
            )
        if tx_round != expected_round:
            raise DeliveryError(
                f"Transaction round conflict: active_transaction has round {tx_round}, "
                f"expected {expected_round}."
            )
        if tx_req_id != expected_req_id:
            raise DeliveryError(
                f"Transaction request_id conflict: active_transaction has '{tx_req_id}', "
                f"expected '{expected_req_id}'."
            )
        if tx_head_sha != str(expected_head_sha).strip().lower():
            raise DeliveryError(
                f"Transaction head_sha conflict: active_transaction has '{active_tx['head_sha']}', "
                f"expected '{expected_head_sha}'."
            )
    else:
        valid_prior_plan = (tx_kind == "plan" and tx_round == 1 and expected_kind == "review" and expected_round >= 1)
        valid_prior_review = (tx_kind == "review" and expected_kind == "review" and tx_round < expected_round)
        if not (valid_prior_plan or valid_prior_review):
            raise DeliveryError(
                f"Transaction state conflict: applied active_transaction '{tx_req_id}' "
                f"cannot precede or replace current request '{expected_req_id}'."
            )

    return active_tx


def execute_exchange(
    task_dir: Path,
    chat_url: Optional[str] = None,
    target_tab_id: Optional[str] = None,
    timeout: int = 180,
    expected_model: Optional[str] = None,
    expected_thinking: Optional[str] = None,
    dry_run: bool = False,
    _test_transport_response: Optional[str] = None,
) -> Dict[str, Any]:
    """Prepares and executes a formal round exchange with ChatGPT under task file lock."""
    with TaskLock(task_dir):
        state = load_state(task_dir)
        stage = state.get("stage")
        root = project_root(task_dir.parent.parent)

        # Fail-closed transaction integrity validation on any active_transaction in state
        task_id = str(state.get("task_id") or task_dir.name)
        active_tx = state.get("active_transaction")
        if active_tx is not None:
            validate_transaction_structure(active_tx, task_id)

        # Source identity verification at exchange entry point
        try:
            verify_source_identity(root, state["head_sha"], expected_task_id=state.get("task_id") or task_dir.name)
        except WorkflowError as exc:
            if not dry_run:
                current_head_now = ""
                try:
                    current_head_now = get_git_head_sha(root)
                except WorkflowError:
                    pass
                if current_head_now and current_head_now.strip().lower() != str(state.get("head_sha") or "").strip().lower():
                    _invalidate_stale_transaction(
                        state=state,
                        reason=str(exc),
                        superseded_by_sha=current_head_now,
                    )
                state["stage"] = STAGE_BLOCKED
                state["blocked_from_stage"] = state.get("business_stage")
                state["next_action"] = str(exc)
                save_state(task_dir, state)
            return {"status": "BLOCKED", "reason": str(exc)}

        if stage == STAGE_WAITING_EXECUTION:
            return {
                "status": "BLOCKED",
                "reason": "Work has not been recorded yet. Implement changes, verify tests, and run record-work first.",
            }
        if stage == STAGE_PAUSED_LIMIT_EXCEEDED:
            return {
                "status": "BLOCKED",
                "reason": "Workflow is paused because repair round limit was exceeded. Run workflow resolve to resume.",
            }
        if stage == STAGE_WAITING_ACCEPTANCE_DONE:
            return {
                "status": "ACCEPTED",
                "reason": "Task has already been ACCEPTED and completed.",
            }

        raw_state_round = state.get("round", 1)
        if stage == STAGE_WAITING_PLAN:
            expected_kind = "plan"
            expected_round = 1
        elif stage in (STAGE_WAITING_REVIEW, STAGE_WAITING_FIX_OR_DIAGNOSIS):
            expected_kind = "review"
            if isinstance(raw_state_round, bool) or not isinstance(raw_state_round, int) or raw_state_round < 1:
                raise DeliveryError(f"Malformed workflow state: 'round' must be a positive integer (got {raw_state_round!r}).")
            expected_round = raw_state_round
        elif stage == STAGE_BLOCKED:
            b_stage = state.get("business_stage") or state.get("blocked_from_stage") or STAGE_WAITING_PLAN
            if b_stage == STAGE_WAITING_PLAN:
                expected_kind = "plan"
                expected_round = 1
            elif b_stage in (STAGE_WAITING_REVIEW, STAGE_WAITING_FIX_OR_DIAGNOSIS):
                expected_kind = "review"
                if isinstance(raw_state_round, bool) or not isinstance(raw_state_round, int) or raw_state_round < 1:
                    raise DeliveryError(f"Malformed workflow state: 'round' must be a positive integer (got {raw_state_round!r}).")
                expected_round = raw_state_round
            else:
                return {"status": "BLOCKED", "reason": f"Unknown or invalid workflow stage: {b_stage}"}
        else:
            return {"status": "BLOCKED", "reason": f"Unknown or invalid workflow stage: {stage}"}

        expected_req_id = f"req-{state['task_id']}-r{expected_round}-{expected_kind}"
        expected_head_sha = state["head_sha"]

        active_tx = state.get("active_transaction")
        if active_tx is not None:
            validate_active_transaction(
                active_tx=active_tx,
                expected_task_id=state["task_id"],
                expected_req_id=expected_req_id,
                expected_kind=expected_kind,
                expected_round=expected_round,
                expected_head_sha=expected_head_sha,
            )

        if active_tx is not None and active_tx["applied"] is False:
            # Resumption from existing validated unapplied transaction: take all fields from transaction
            is_recovery = True
            req_id = active_tx["request_id"]
            expected_round = active_tx["round"]
            req_kind = active_tx["kind"]
            expected_head_sha = active_tx["head_sha"]
            message = active_tx["message"]
            req_hash = active_tx["message_hash"]
            target_chat_url = active_tx.get("chat_url") or chat_url or state.get("chat_url")
            target_tab_id = active_tx.get("tab_id") or target_tab_id
            req_model = active_tx.get("model") or expected_model or state.get("model")
            req_thinking = active_tx.get("thinking_mode") or expected_thinking or state.get("thinking_mode")
        else:
            is_recovery = False
            req_kind = expected_kind
            req_id = expected_req_id
            if req_kind == "plan":
                message = build_plan_request_message(state, task_dir, req_id, root)
            else:
                message = build_review_request_message(state, task_dir, req_id, root)
            req_hash = compute_hash(message)
            target_chat_url = chat_url or state.get("chat_url")
            req_model = expected_model or state.get("model")
            req_thinking = expected_thinking or state.get("thinking_mode")

        if dry_run:
            if chat_url and not validate_chatgpt_origin(chat_url):
                raise WorkflowError(f"Invalid ChatGPT origin URL: {chat_url}")
            return {
                "status": "DRY_RUN",
                "request_id": req_id,
                "request_hash": req_hash,
                "head_sha": expected_head_sha,
                "model": req_model,
                "thinking_mode": req_thinking,
                "message": message,
            }

        # Check readable materials entry point before formal sending
        has_readable_materials = bool(state.get("repo_url") or state.get("pr_url"))
        if not has_readable_materials and _test_transport_response is None:
            state["stage"] = STAGE_BLOCKED
            state["blocked_from_stage"] = state.get("business_stage")
            err_msg = (
                "BLOCKED_MATERIALS: No public/safe remote repository URL (--repo-url) or PR URL (--pr-url) configured. "
                "ChatGPT cannot read local paths. Provide a readable material entry point to proceed."
            )
            state["next_action"] = err_msg
            save_state(task_dir, state)
            return {"status": "BLOCKED", "reason": err_msg}

        if not is_recovery:
            if chat_url:
                update_chat_url(task_dir, chat_url)
                state = load_state(task_dir)

            target_chat_url = state.get("chat_url")
            active_tx = {
                "request_id": req_id,
                "kind": req_kind,
                "round": expected_round,
                "head_sha": expected_head_sha,
                "message": message,
                "message_hash": req_hash,
                "chat_url": target_chat_url,
                "tab_id": target_tab_id,
                "model": req_model,
                "thinking_mode": req_thinking,
                "delivery_status": "SEND_ATTEMPTED",
                "created_at": iso_now(),
                "applied": False,
            }
            state["active_transaction"] = active_tx
            save_state(task_dir, state)
        elif chat_url and not active_tx.get("chat_url"):
            update_chat_url(task_dir, chat_url)
            state = load_state(task_dir)
            active_tx = state.get("active_transaction") or active_tx
            target_chat_url = active_tx.get("chat_url") or state.get("chat_url")

        # Save request markdown to logs
        logs_dir = task_dir / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        req_file = logs_dir / f"round-{expected_round}-request.md"
        req_file.write_text(message, encoding="utf-8")

        # Determine skip_send: unknown delivery recovery queries DOM without re-sending
        skip_send = False
        if is_recovery:
            if active_tx.get("delivery_status") in VALID_DELIVERY_STATUSES:
                skip_send = True
        else:
            active_tx["delivery_status"] = "SEND_ATTEMPTED"
            save_state(task_dir, state)

        # On delivery confirmed callback (persists /c/id immediately before wait_for_response)
        def on_delivery_confirmed_cb(dinfo: Dict[str, Any]) -> None:
            cur_st = load_state(task_dir)
            cur_tx = cur_st.get("active_transaction") or {}
            cur_tx["delivery_status"] = "CONFIRMED_USER_TURN"
            if dinfo.get("chat_url"):
                cur_tx["chat_url"] = dinfo["chat_url"]
                if validate_chatgpt_origin(dinfo["chat_url"]):
                    cur_st["chat_url"] = dinfo["chat_url"]
            if dinfo.get("model"):
                cur_tx["model"] = dinfo["model"]
                cur_st["model"] = dinfo["model"]
            if dinfo.get("thinking_mode"):
                cur_tx["thinking_mode"] = dinfo["thinking_mode"]
                cur_st["thinking_mode"] = dinfo["thinking_mode"]
            cur_st["active_transaction"] = cur_tx
            save_state(task_dir, cur_st)

        # Transport execution
        if _test_transport_response is not None:
            raw_response = _test_transport_response
            state["is_simulated_test"] = True
            active_tx["delivery_status"] = "CONFIRMED_USER_TURN"
            save_state(task_dir, state)
        else:
            relay = BrowserRelay()
            res = relay.run_e2e_review(
                task_id=state["task_id"],
                message=message,
                request_id=req_id,
                target_tab_id=target_tab_id,
                target_url_prefix=target_chat_url,
                timeout_secs=timeout,
                expected_model=req_model,
                expected_thinking=req_thinking,
                skip_send=skip_send,
                is_recovery=is_recovery,
                on_delivery_confirmed=on_delivery_confirmed_cb,
            )

            # Re-read state after callback may have updated it
            state = load_state(task_dir)
            active_tx = state.get("active_transaction") or active_tx

            if res.get("chat_url"):
                update_chat_url(task_dir, res["chat_url"])
                state = load_state(task_dir)
                active_tx = state.get("active_transaction") or active_tx

            if res.get("status") != "SUCCESS":
                if active_tx.get("delivery_status") != "CONFIRMED_USER_TURN":
                    active_tx["delivery_status"] = "DELIVERY_UNKNOWN"
                state["stage"] = STAGE_BLOCKED
                state["blocked_from_stage"] = state.get("business_stage")
                err_msg = res.get("error") or res.get("status") or "Browser exchange failed"
                state["next_action"] = f"Address browser exchange failure: {err_msg}"
                save_state(task_dir, state)
                return {"status": "BLOCKED", "error": err_msg, "journal": res}

            active_tx["delivery_status"] = "CONFIRMED_USER_TURN"
            if res.get("verified_model") and not state.get("model"):
                state["model"] = res["verified_model"]
                active_tx["model"] = res["verified_model"]
            if res.get("verified_thinking") and not state.get("thinking_mode"):
                state["thinking_mode"] = res["verified_thinking"]
                active_tx["thinking_mode"] = res["verified_thinking"]

            save_state(task_dir, state)
            raw_response = res.get("response_text", "")

        # Save raw response to logs
        resp_file = logs_dir / f"round-{expected_round}-chatgpt.txt"
        resp_file.write_text(raw_response, encoding="utf-8")

        # Parse and validate response
        eff_parse_stage = (
            (state.get("business_stage") or state.get("blocked_from_stage") or stage)
            if stage == STAGE_BLOCKED
            else stage
        )
        parsed = parse_and_validate_review_response(
            response_text=raw_response,
            expected_task_id=state["task_id"],
            expected_round=expected_round,
            expected_head_sha=state["head_sha"],
            expected_stage=eff_parse_stage,
        )

        if not parsed["valid"]:
            state["stage"] = STAGE_BLOCKED
            state["blocked_from_stage"] = state.get("business_stage")
            state["next_action"] = f"Response rejected: {parsed['reason']}"
            save_state(task_dir, state)
            return {
                "status": "BLOCKED",
                "reason": parsed["reason"],
                "raw_response": raw_response,
            }

        pdata = parsed["data"]
        # Apply decision (ensure idempotency: do not re-apply already applied round)
        if active_tx.get("applied"):
            return {
                "status": "SUCCESS",
                "decision": pdata["decision"],
                "stage": state["stage"],
                "data": pdata,
            }

        updated_state = apply_review_decision(
            task_dir=task_dir,
            decision=pdata["decision"],
            findings=pdata.get("findings", []),
            reviewed_sha=state["head_sha"],
            next_action=pdata.get("next_action", ""),
            is_simulated=(_test_transport_response is not None),
        )
        updated_state["active_transaction"]["applied"] = True
        save_state(task_dir, updated_state)

        return {
            "status": "SUCCESS",
            "decision": pdata["decision"],
            "stage": updated_state["stage"],
            "data": pdata,
        }


def resume_workflow(
    project: Path,
    task_id: Optional[str] = None,
    chat_url: Optional[str] = None,
) -> str:
    """Resumes task workflow, supporting cross-chat continuation with history context."""
    root = project_root(project)
    task_dir = get_task_dir(root, task_id)

    with TaskLock(task_dir):
        state = load_state(task_dir)

        if chat_url and chat_url != state.get("chat_url"):
            update_chat_url(task_dir, chat_url)
            state = load_state(task_dir)

        brief_file = task_dir / "TASK_BRIEF.md"
        brief_text = brief_file.read_text(encoding="utf-8").strip() if brief_file.is_file() else "No brief"

        history = state.get("findings_history", [])
        findings_str = "\n".join(f"  - [{f.get('id')}]: {f.get('description')}" for f in history if isinstance(f, dict)) or "  None"

        continuation = (
            f"# Continuation Brief for Task {state['task_id']}\n"
            f"- Title / Goal: {state.get('title')}\n"
            f"- Stage: {state.get('stage')}\n"
            f"- Round: {state.get('round')}\n"
            f"- Fixed Commit SHA: {state.get('head_sha')}\n"
            f"- Active Chat URL: {state.get('chat_url') or 'Not set'}\n"
            f"- Previous Chat URLs: {', '.join(state.get('previous_chat_urls', [])) or 'None'}\n"
            f"- Diagnosis Mode: {state.get('is_diagnosis', False)}\n"
            f"- Total Repair Rounds: {state.get('total_repair_rounds', 0)}/{MAX_REPAIR_ROUNDS}\n"
            f"- Last Feedback: {json.dumps(state.get('last_feedback'), ensure_ascii=False)}\n"
            f"- Unresolved Findings:\n{findings_str}\n"
            f"- Next Action: {state.get('next_action')}\n"
        )
        state["continuation_brief"] = continuation
        save_state(task_dir, state)
        return continuation


def resolve_limit_exceeded(
    project: Path,
    decision: str,
    task_id: Optional[str] = None,
    notes: Optional[str] = None,
) -> Dict[str, Any]:
    """Allows explicit human user intervention to unpause a task that hit MAX_REPAIR_ROUNDS."""
    root = project_root(project)
    task_dir = get_task_dir(root, task_id)

    with TaskLock(task_dir):
        state = load_state(task_dir)
        if state.get("stage") != STAGE_PAUSED_LIMIT_EXCEEDED:
            raise WorkflowError(
                f"Task '{state['task_id']}' is not in PAUSED_LIMIT_EXCEEDED stage (current: {state.get('stage')}). "
                "The resolve command is only for unpausing tasks that exceeded repair limits."
            )

        dec = decision.upper().strip()
        if dec == "CONTINUE":
            state["stage"] = STAGE_WAITING_FIX_OR_DIAGNOSIS
            state["business_stage"] = STAGE_WAITING_FIX_OR_DIAGNOSIS
            state["blocked_from_stage"] = None
            state["total_repair_rounds"] = 0  # Reset repair allowance for authorized continuation
            state["next_action"] = notes or "User authorized workflow continuation. Fix findings and record-work."
        elif dec == "ABORT":
            state["stage"] = STAGE_BLOCKED
            state["business_stage"] = STAGE_BLOCKED
            state["blocked_from_stage"] = STAGE_PAUSED_LIMIT_EXCEEDED
            state["next_action"] = notes or "Workflow terminated by human user decision."
        else:
            raise WorkflowError(f"Invalid resolve decision '{decision}'. Must be 'CONTINUE' or 'ABORT'.")

        state["resolved_by_user"] = {
            "decision": dec,
            "notes": notes,
            "timestamp": iso_now(),
        }
        save_state(task_dir, state)
        return state


def status_workflow(
    project: Path,
    task_id: Optional[str] = None,
    as_json: bool = False,
) -> int:
    root = project_root(project)
    task_dir = get_task_dir(root, task_id)
    with TaskLock(task_dir):
        state = load_state(task_dir)

        # If task was marked ACCEPTED, verify working tree and HEAD have not drifted
        if state.get("stage") == STAGE_WAITING_ACCEPTANCE_DONE:
            try:
                verify_source_identity(root, state["head_sha"], expected_task_id=state.get("task_id") or task_dir.name)
            except WorkflowError as exc:
                state["stage"] = STAGE_BLOCKED
                state["blocked_from_stage"] = state.get("business_stage")
                state["next_action"] = str(exc)
                save_state(task_dir, state)

        if as_json:
            print(json.dumps(state, indent=2, ensure_ascii=False))
            return 0

        print("========================================")
        print(f"Task ID:       {state.get('task_id')}")
        print(f"Title:         {state.get('title')}")
        print(f"Stage:         {state.get('stage')}")
        print(f"Round:         {state.get('round')}")
        print(f"Head SHA:      {state.get('head_sha')}")
        print(f"Chat URL:      {state.get('chat_url') or 'Not set'}")
        print(f"Model:         {state.get('model') or 'Not specified'}")
        print(f"Diagnosis:     {state.get('is_diagnosis')}")
        print(f"Repair Rounds: {state.get('total_repair_rounds')}/{MAX_REPAIR_ROUNDS}")
        print(f"Next Action:   {state.get('next_action')}")
        print("========================================")
        return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Antigravity single-entry workflow CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # start
    p_start = subparsers.add_parser("start", help="Initialize new workflow task")
    p_start.add_argument("--project", default=".", help="Project path")
    p_start.add_argument("--title", required=True, help="Task title")
    p_start.add_argument("--task-id", help="Explicit task ID")
    p_start.add_argument("--slug", help="Task slug")
    p_start.add_argument("--chat-url", help="ChatGPT conversation URL")
    p_start.add_argument("--model", help="ChatGPT model (no default silent replacement)")
    p_start.add_argument("--thinking-mode", help="Reasoning/thinking mode")
    p_start.add_argument("--repo-url", help="Public/safe remote git repository URL")
    p_start.add_argument("--pr-url", help="Pull Request URL")

    # status
    p_status = subparsers.add_parser("status", help="Show task status")
    p_status.add_argument("--project", default=".", help="Project path")
    p_status.add_argument("--task-id", help="Task ID (default: active pointer)")
    p_status.add_argument("--json", action="store_true", help="Output JSON")

    # record-work
    p_rec = subparsers.add_parser("record-work", help="Record real git commit and structured test evidence")
    p_rec.add_argument("--project", default=".", help="Project path")
    p_rec.add_argument("--task-id", help="Task ID")
    p_rec.add_argument("--head-sha", help="Git commit SHA (default: git rev-parse HEAD)")
    p_rec.add_argument("--test-evidence", required=True, help="Structured test evidence string or file path")
    p_rec.add_argument("--summary", help="Summary of work completed")

    # exchange
    p_ex = subparsers.add_parser("exchange", help="Exchange prompt and parse ChatGPT response")
    p_ex.add_argument("--project", default=".", help="Project path")
    p_ex.add_argument("--task-id", help="Task ID")
    p_ex.add_argument("--chat-url", help="ChatGPT conversation URL")
    p_ex.add_argument("--tab-id", help="Target tab ID")
    p_ex.add_argument("--timeout", type=int, default=180, help="Response timeout")
    p_ex.add_argument("--model", help="Expected model name")
    p_ex.add_argument("--thinking-mode", help="Expected thinking mode")
    p_ex.add_argument("--dry-run", action="store_true", help="Print request without sending")

    # resume
    p_res = subparsers.add_parser("resume", help="Resume workflow or switch chat URL")
    p_res.add_argument("--project", default=".", help="Project path")
    p_res.add_argument("--task-id", help="Task ID")
    p_res.add_argument("--chat-url", help="New ChatGPT conversation URL")

    # resolve
    p_resolv = subparsers.add_parser("resolve", help="Unpause workflow after repair limit exceeded")
    p_resolv.add_argument("--project", default=".", help="Project path")
    p_resolv.add_argument("--task-id", help="Task ID")
    p_resolv.add_argument("--decision", required=True, choices=["CONTINUE", "ABORT"], help="Human decision")
    p_resolv.add_argument("--notes", help="Resolution justification / notes")

    args = parser.parse_args(argv)

    try:
        if args.command == "start":
            st = start_workflow(
                project=Path(args.project),
                title=args.title,
                task_id=args.task_id,
                slug=args.slug,
                chat_url=args.chat_url,
                model=args.model,
                thinking_mode=args.thinking_mode,
                repo_url=args.repo_url,
                pr_url=args.pr_url,
            )
            print(f"TASK_ID={st['task_id']}")
            print(f"STAGE={st['stage']}")
            print(f"HEAD_SHA={st['head_sha']}")
            return 0

        if args.command == "status":
            return status_workflow(Path(args.project), task_id=args.task_id, as_json=args.json)

        if args.command == "record-work":
            st = record_work(
                project=Path(args.project),
                task_id=args.task_id,
                head_sha=args.head_sha,
                test_evidence=args.test_evidence,
                summary=args.summary,
            )
            print(f"STAGE={st['stage']}")
            print(f"ROUND={st['round']}")
            print(f"HEAD_SHA={st['head_sha']}")
            return 0

        if args.command == "exchange":
            root = project_root(Path(args.project))
            td = get_task_dir(root, args.task_id)
            res = execute_exchange(
                task_dir=td,
                chat_url=args.chat_url,
                target_tab_id=args.tab_id,
                timeout=args.timeout,
                expected_model=args.model,
                expected_thinking=args.thinking_mode,
                dry_run=args.dry_run,
            )
            print(json.dumps(res, indent=2, ensure_ascii=False))
            return 0 if res.get("status") in ("SUCCESS", "ACCEPTED", "DRY_RUN") else 1

        if args.command == "resume":
            brief = resume_workflow(
                project=Path(args.project),
                task_id=args.task_id,
                chat_url=args.chat_url,
            )
            print(brief)
            return 0

        if args.command == "resolve":
            st = resolve_limit_exceeded(
                project=Path(args.project),
                decision=args.decision,
                task_id=args.task_id,
                notes=args.notes,
            )
            print(f"STAGE={st['stage']}")
            print(f"NEXT_ACTION={st['next_action']}")
            return 0

    except WorkflowBusyError as exc:
        print(f"Workflow busy: {exc}", file=sys.stderr)
        return 5
    except WorkflowError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Unexpected error: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
