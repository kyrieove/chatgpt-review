#!/usr/bin/env python3
"""Create task handoffs and safe, self-contained review ZIPs."""

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
import zipfile
from datetime import datetime
from pathlib import Path, PureWindowsPath


SKILL_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_DIR = SKILL_ROOT / "templates"
REFERENCE_DIR = SKILL_ROOT / "references"
TASK_FILES = {
    "HANDOFF.md": "HANDOFF.template.md",
    "REVIEW_REPORT.md": "REVIEW_REPORT.template.md",
    "TASK_BRIEF.md": "TASK_BRIEF.template.md",
    "CHATGPT_BRIEF.md": "CHATGPT_BRIEF.template.md",
    "TEST_RESULTS.md": "TEST_RESULTS.template.md",
}
POINTER_MARKER = "<!-- chatgpt-review:managed-pointer -->"
TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{2,100}$")
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
DENIED_DIRS = {
    ".git", ".hg", ".svn", ".idea", ".vscode", ".ssh", ".aws",
    "node_modules", "bower_components", "vendor", "pods", ".gradle", ".m2",
    ".next", ".turbo", ".cache", ".venv", "venv", ".tox", ".nox",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", "__pycache__", "coverage",
    "dist", "build", "target", "tmp", "temp", "review-artifacts",
}
DENIED_NAMES = {
    ".npmrc", ".pypirc", ".netrc", "credentials.json", "secrets.json",
    "token.json", "cookies.json", "session.json", "id_rsa", "id_ed25519",
    "known_hosts",
}
DENIED_NAME_RE = re.compile(
    r"(?i)^(?:\.env(?:\..*)?|.*\.(?:pem|p12|pfx|key|sqlite|sqlite3|db|mdb|accdb|"
    r"zip|7z|rar|tar|gz|bz2|xz|tgz|whl|egg))$|"
    r"(?:^|[-_.])(?:cookie|cookies|credential|credentials|secret|secrets|"
    r"private-key|private_key|token)(?:[-_.]|$)"
)
RESERVED_ROOT_FILES = {
    "handoff.md", "chatgpt_start.md", "github_review.md", "review_protocol.md",
    "package_info.json", "sha256sums.txt",
}
SECRET_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
        r"github_pat_[A-Za-z0-9_]{30,}",
        r"gh[pousr]_[A-Za-z0-9]{30,}",
        r"sk-[A-Za-z0-9_-]{30,}",
        r"AKIA[0-9A-Z]{16}",
        r"Bearer\s+[A-Za-z0-9._~+/-]{32,}",
    )
)


class RelayError(Exception):
    """An expected user-facing input or packaging error."""


def iso_now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def write_text(path, content):
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(content)


def project_root(value):
    try:
        root = Path(value).expanduser().resolve(strict=True)
    except OSError as exc:
        raise RelayError(f"Project path does not exist: {value}") from exc
    if not root.is_dir():
        raise RelayError(f"Project path is not a directory: {value}")
    return root


def _slug(value):
    value = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "task"
    value = value[:36].rstrip("-") or "task"
    if not SLUG_RE.fullmatch(value):
        raise RelayError("Slug must use lowercase letters, numbers, and hyphens.")
    return value


def _template_texts(title, task_id, updated):
    rendered = {}
    replacements = {
        "{{TASK_ID}}": task_id,
        "{{TITLE}}": title,
        "{{UPDATED}}": updated,
    }
    for destination, template_name in TASK_FILES.items():
        path = TEMPLATE_DIR / template_name
        _check_link_chain(SKILL_ROOT, ("templates", template_name), str(path))
        if not path.is_file():
            raise RelayError(f"Required skill template is missing: {path}")
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise RelayError(f"Could not read skill template: {path}") from exc
        for token, value in replacements.items():
            content = content.replace(token, value)
        rendered[destination] = content
    return rendered


def _is_link(path):
    try:
        info = path.lstat()
    except OSError:
        return False
    reparse_point = getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return path.is_symlink() or bool(reparse_point)


def start_task(args):
    root = project_root(args.project)
    title = args.title.strip()
    if not title:
        raise RelayError("Title must not be empty.")
    if args.slug and args.slug.strip():
        slug = args.slug.lower().strip("-")
        if not SLUG_RE.fullmatch(slug):
            raise RelayError("Slug must use lowercase letters, numbers, and hyphens.")
        slug = slug[:36].rstrip("-") or "task"
    else:
        slug = _slug(title)
    rendered = _template_texts(title, "pending", iso_now())

    handoffs = root / "handoffs"
    if _is_link(handoffs):
        raise RelayError("Refusing to use a symlink for the handoffs directory.")
    if handoffs.exists() and not handoffs.is_dir():
        raise RelayError("handoffs exists but is not a directory.")
    handoffs.mkdir(exist_ok=True)
    current = handoffs / ".current-task"
    if _is_link(current) or current.exists() and not current.is_file():
        raise RelayError("Refusing to overwrite a non-file handoffs/.current-task pointer.")

    timestamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    suffix = 1
    while True:
        task_slug = slug if suffix == 1 else f"{slug}-{suffix}"
        task_id = f"{timestamp}-{task_slug}"
        task_dir = handoffs / task_id
        try:
            task_dir.mkdir()
            break
        except FileExistsError:
            suffix += 1

    updated = iso_now()
    rendered = _template_texts(title, task_id, updated)
    (task_dir / "logs").mkdir()
    for name, content in rendered.items():
        write_text(task_dir / name, content)

    write_text(current, task_id + "\n")

    root_pointer = root / "HANDOFF.md"
    write_root_pointer = not root_pointer.exists() and not _is_link(root_pointer)
    if root_pointer.is_file() and not _is_link(root_pointer):
        try:
            write_root_pointer = root_pointer.read_text(encoding="utf-8").startswith(POINTER_MARKER)
        except (OSError, UnicodeError):
            write_root_pointer = False
    if write_root_pointer:
        pointer = (
            f"{POINTER_MARKER}\n# Active task handoff\n\n"
            f"Read [handoffs/{task_id}/HANDOFF.md](handoffs/{task_id}/HANDOFF.md) first.\n"
        )
        write_text(root_pointer, pointer)
    elif root_pointer.exists() or _is_link(root_pointer):
        print("WARNING: Preserved existing root HANDOFF.md pointer.", file=sys.stderr)

    print(f"TASK_ID={task_id}")
    print(f"HANDOFF={task_dir / 'HANDOFF.md'}")
    return 0


def _validate_task_id(value):
    if not TASK_ID_RE.fullmatch(value):
        raise RelayError("Invalid task id.")
    return value


def _relative_parts(value):
    if (
        not value
        or Path(value).is_absolute()
        or PureWindowsPath(value).is_absolute()
        or PureWindowsPath(value).root
        or PureWindowsPath(value).drive
    ):
        raise RelayError(f"Path must be non-empty and relative: {value}")
    normalized = value.replace("\\", "/")
    parts = normalized.split("/")
    if any(part == ".." for part in parts):
        raise RelayError(f"Unsafe include path: {value}")
    return tuple(part for part in parts if part not in ("", "."))


def _check_link_chain(root, parts, label):
    current = root
    for part in parts:
        current = current / part
        if _is_link(current):
            raise RelayError(f"Explicit include crosses a symlink: {label}")


def _safe_read(path):
    if _is_link(path):
        raise RelayError(f"Refusing to read a symlink: {path}")
    return path.read_bytes()


def _skip(skipped, relative):
    path = relative.replace("\\", "/") or "."
    if path not in skipped:
        skipped.append(path)


def _excluded_name(name):
    lowered = name.casefold()
    return lowered in DENIED_NAMES or bool(DENIED_NAME_RE.search(name))


def _task_relative(relative, task_id):
    prefix = f"handoffs/{task_id}"
    if relative == prefix:
        return ""
    if relative.startswith(prefix + "/"):
        return relative[len(prefix) + 1 :]
    return None


def _task_evidence_status(relative, is_dir, entries):
    if not entries["evidence_explicit"] or relative in TASK_FILES:
        return "include"
    if is_dir and not relative:
        return "ancestor"
    for selected, selected_is_dir in entries["evidence_scopes"]:
        if not selected or relative == selected or selected_is_dir and relative.startswith(selected + "/"):
            return "include"
        if is_dir and selected.startswith(relative + "/"):
            return "ancestor"
    return "exclude"


def _prepare_evidence(task_dir, values):
    scopes = []
    requested = []
    seen = set()
    for value in values:
        parts = _relative_parts(value)
        _check_link_chain(task_dir, parts, f"evidence path {value}")
        source = task_dir.joinpath(*parts)
        if not source.exists():
            raise RelayError(f"Evidence path does not exist: {value}")
        try:
            source.resolve(strict=True).relative_to(task_dir.resolve())
        except (OSError, ValueError) as exc:
            raise RelayError(f"Evidence path escapes the active task: {value}") from exc
        relative = "/".join(parts)
        label = relative or "."
        if label not in seen:
            seen.add(label)
            scopes.append((relative, source.is_dir()))
            requested.append(label)
    return bool(values), scopes, requested


def _record_evidence_exclusions(task_dir, task_id, entries, skipped):
    if not entries["evidence_explicit"]:
        return

    def visit(directory, relative=""):
        try:
            children = sorted(directory.iterdir(), key=lambda item: item.name.casefold())
        except OSError:
            _skip(skipped, f"handoffs/{task_id}/{relative}".rstrip("/"))
            return
        for child in children:
            child_relative = f"{relative}/{child.name}" if relative else child.name
            archive_path = f"handoffs/{task_id}/{child_relative}"
            if _is_link(child):
                _skip(skipped, archive_path)
                continue
            is_dir = child.is_dir()
            status = _task_evidence_status(child_relative, is_dir, entries)
            if status == "exclude":
                _skip(skipped, archive_path)
                continue
            if is_dir:
                visit(child, child_relative)

    visit(task_dir)


def _copy_selected(root, source, relative, stage, max_bytes, skipped, entries, mandatory=False):
    relative = relative.replace("\\", "/").strip("/")
    if not relative:
        raise RelayError("Cannot package the project root as a file.")
    parts = tuple(relative.split("/"))
    if parts and parts[0].casefold() == "handoffs":
        if len(parts) < 2 or parts[1] != entries["task_id"]:
            _skip(skipped, relative)
            return
        task_relative = _task_relative(relative, entries["task_id"])
        if _task_evidence_status(task_relative, False, entries) != "include":
            _skip(skipped, relative)
            return
    if len(parts) == 1 and parts[0].casefold() in RESERVED_ROOT_FILES:
        _skip(skipped, relative)
        return
    if _is_link(source):
        if mandatory:
            raise RelayError(f"Required task file is a symlink: {relative}")
        _skip(skipped, relative)
        return
    try:
        size = source.stat().st_size
    except OSError as exc:
        if mandatory:
            raise RelayError(f"Required task file cannot be inspected: {relative}") from exc
        _skip(skipped, relative)
        return
    if _excluded_name(source.name):
        if mandatory:
            raise RelayError(f"Required task file has a denied filename: {relative}")
        _skip(skipped, relative)
        return
    if size > max_bytes:
        if mandatory:
            raise RelayError(f"Required task file exceeds --max-file-mb: {relative}")
        _skip(skipped, relative)
        return
    try:
        data = _safe_read(source)
    except OSError as exc:
        if mandatory:
            raise RelayError(f"Required task file cannot be read: {relative}") from exc
        _skip(skipped, relative)
        return
    if len(data) > max_bytes:
        if mandatory:
            raise RelayError(f"Required task file exceeds --max-file-mb: {relative}")
        _skip(skipped, relative)
        return
    if relative in entries["files"]:
        return
    destination = stage.joinpath(*relative.split("/"))
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)
    entries["files"].add(relative)


def _walk_tree(root, source, relative, stage, active_task, max_bytes, skipped, entries):
    relative = relative.replace("\\", "/").strip("/")
    parts = tuple(relative.split("/")) if relative else ()
    if parts and parts[0].casefold() == "handoffs":
        if len(parts) == 1:
            try:
                children = sorted(source.iterdir(), key=lambda item: item.name.casefold())
            except OSError:
                _skip(skipped, relative)
                return
            for child in children:
                child_rel = f"handoffs/{child.name}"
                if child.name == active_task and child.is_dir() and not _is_link(child):
                    _walk_tree(root, child, child_rel, stage, active_task, max_bytes, skipped, entries)
                else:
                    _skip(skipped, child_rel)
            return
        if parts[1] != active_task:
            _skip(skipped, relative)
            return
        if entries["evidence_explicit"]:
            task_relative = "/".join(parts[2:])
            if _task_evidence_status(task_relative, source.is_dir(), entries) == "exclude":
                _skip(skipped, relative)
                return

    active_task_dir = len(parts) == 2 and parts[0].casefold() == "handoffs" and parts[1] == active_task
    if relative and source.is_dir() and not active_task_dir and (
        source.name.casefold() in DENIED_DIRS or _excluded_name(source.name)
    ):
        _skip(skipped, relative)
        return

    if source.is_file():
        _copy_selected(root, source, relative, stage, max_bytes, skipped, entries)
        return
    try:
        children = sorted(source.iterdir(), key=lambda item: item.name.casefold())
    except OSError:
        _skip(skipped, relative)
        return
    for child in children:
        child_rel = f"{relative}/{child.name}" if relative else child.name
        if _is_link(child):
            _skip(skipped, child_rel)
        elif child.is_dir():
            if child.name.casefold() in DENIED_DIRS or _excluded_name(child.name):
                _skip(skipped, child_rel)
            else:
                _walk_tree(root, child, child_rel, stage, active_task, max_bytes, skipped, entries)
        else:
            _copy_selected(root, child, child_rel, stage, max_bytes, skipped, entries)


def _select_project_files(root, task_id, includes, stage, max_bytes, skipped, entries):
    if includes:
        entries["selection_explicit"] = True
        entries["include_scopes"] = []
        for include in includes:
            parts = _relative_parts(include)
            _check_link_chain(root, parts, include)
            source = root.joinpath(*parts) if parts else root
            if not source.exists():
                raise RelayError(f"Explicit include does not exist: {include}")
            directory_parts = parts if source.is_dir() else parts[:-1]
            if directory_parts[:1] == ("handoffs",) and len(directory_parts) > 1:
                directory_parts = directory_parts[2:] if directory_parts[1] == task_id else ()
            if any(
                part.casefold() in DENIED_DIRS or _excluded_name(part)
                for part in directory_parts
            ):
                _skip(skipped, "/".join(parts))
                continue
            try:
                source.resolve(strict=True).relative_to(root)
            except (OSError, ValueError) as exc:
                raise RelayError(f"Explicit include escapes the project: {include}") from exc
            entries["include_scopes"].append(("/".join(parts), source.is_dir()))
            _walk_tree(root, source, "/".join(parts), stage, task_id, max_bytes, skipped, entries)
    else:
        entries["selection_explicit"] = False
        entries["include_scopes"] = []
        _walk_tree(root, root, "", stage, task_id, max_bytes, skipped, entries)


def _git(root, *args, text=False):
    options = {"capture_output": True, "check": False, "shell": False}
    if text:
        options.update(encoding="utf-8", errors="replace", text=True)
    try:
        return subprocess.run(["git", "-C", str(root), *args], **options)
    except OSError:
        return None


def _git_path_allowed(root, path, task_id, entries, max_bytes):
    normalized = path.replace("\\", "/")
    parts = tuple(normalized.split("/"))
    if not normalized or normalized.startswith("/") or any(part in ("", ".", "..") for part in parts):
        return False
    if parts[0].casefold() == "handoffs":
        if len(parts) < 3 or parts[1] != task_id:
            return False
        task_relative = "/".join(parts[2:])
        if _task_evidence_status(task_relative, False, entries) != "include":
            return False
        directory_parts = parts[2:-1]
        selected_task_file = True
    else:
        if len(parts) == 1 and parts[0].casefold() in RESERVED_ROOT_FILES:
            return False
        directory_parts = parts[:-1]
        selected_task_file = False
    if _excluded_name(parts[-1]) or any(
        part.casefold() in DENIED_DIRS or _excluded_name(part) for part in directory_parts
    ):
        return False
    if entries["selection_explicit"] and not selected_task_file:
        selected = False
        for included, is_dir in entries["include_scopes"]:
            if included == "" or path == included or is_dir and path.startswith(included + "/"):
                selected = True
                break
        if not selected:
            return False

    source = root.joinpath(*parts)
    current = root
    for part in parts:
        current = current / part
        if _is_link(current):
            return False
    if source.is_file():
        try:
            if source.stat().st_size > max_bytes:
                return False
        except OSError:
            return False
    return True


def _git_paths(root, staged=False):
    args = ["diff"]
    if staged:
        args.append("--cached")
    command = _git(root, *args, "--name-only", "--no-renames", "-z", "--relative", "--", ".")
    if command is None or command.returncode != 0:
        return None
    return [os.fsdecode(item) for item in command.stdout.split(b"\0") if item]


def _project_relative_status_path(path, prefix):
    path = path.replace("\\", "/")
    prefix = prefix.replace("\\", "/").strip("/")
    if not prefix:
        return path
    if path.startswith(prefix + "/"):
        return path[len(prefix) + 1 :]
    return None


def _git_diff(root, task_id, entries, max_bytes, skipped, staged=False):
    paths = _git_paths(root, staged=staged)
    name = "git-diff-staged.patch" if staged else "git-diff-unstaged.patch"
    if paths is None:
        return None
    command = ["diff"]
    if staged:
        command.append("--cached")
    command.extend(("--no-ext-diff", "--no-renames", "--relative"))
    chunks = []
    total = 0
    for path in dict.fromkeys(paths):
        if not _git_path_allowed(root, path, task_id, entries, max_bytes):
            continue
        diff = _git(root, *command, "--", f":(literal){path}")
        if diff is None or diff.returncode != 0:
            return None
        if len(diff.stdout) > max_bytes or total + len(diff.stdout) > max_bytes:
            _skip(skipped, path)
            continue
        chunks.append(diff.stdout)
        total += len(diff.stdout)
    return b"".join(chunks)


def _git_snapshot(root, task_id, entries, max_bytes, skipped):
    git_available = shutil.which("git") is not None
    info = {
        "git_executable_available": git_available,
        "repository_available": False,
        "head": None,
        "branch": None,
        "base": None,
        "status_available": False,
        "staged_diff_available": False,
        "unstaged_diff_available": False,
    }
    unavailable = b"Git is unavailable; status and diffs were not captured.\n"
    snapshots = {
        "git-status.txt": unavailable,
        "git-diff-staged.patch": unavailable,
        "git-diff-unstaged.patch": unavailable,
    }
    if not git_available:
        return info, snapshots

    check = _git(root, "rev-parse", "--is-inside-work-tree", text=True)
    if check is None or check.returncode != 0 or check.stdout.strip() != "true":
        snapshots = {
            name: b"Project is not a Git worktree; status and diffs were not captured.\n"
            for name in snapshots
        }
        return info, snapshots

    info["repository_available"] = True
    prefix_result = _git(root, "rev-parse", "--show-prefix", text=True)
    project_prefix = prefix_result.stdout.strip() if prefix_result and prefix_result.returncode == 0 else ""
    head = _git(root, "rev-parse", "--verify", "HEAD", text=True)
    if head is not None and head.returncode == 0:
        info["head"] = head.stdout.strip() or None
    branch = _git(root, "branch", "--show-current", text=True)
    if branch is not None and branch.returncode == 0:
        info["branch"] = branch.stdout.strip() or None
    status = _git(root, "status", "--short", "--untracked-files=all", "--no-renames", "-z", "--", ".")
    staged = _git_diff(root, task_id, entries, max_bytes, skipped, staged=True)
    unstaged = _git_diff(root, task_id, entries, max_bytes, skipped)
    if status is None or status.returncode != 0:
        status_text = b"Git status command failed; status was not captured.\n"
        status_ok = False
    else:
        status_lines = []
        for record in status.stdout.split(b"\0"):
            if len(record) < 4:
                continue
            code = record[:2].decode("ascii", errors="replace")
            path = _project_relative_status_path(os.fsdecode(record[3:]), project_prefix)
            if path is None:
                continue
            if _git_path_allowed(root, path, task_id, entries, max_bytes):
                status_lines.append(f"{code} {path}\n")
        status_text = "".join(status_lines).encode("utf-8", errors="replace")
        status_ok = True
    for key, command, name in (
        ("status_available", status_text, "git-status.txt"),
        ("staged_diff_available", staged, "git-diff-staged.patch"),
        ("unstaged_diff_available", unstaged, "git-diff-unstaged.patch"),
    ):
        ok = status_ok if key == "status_available" else command is not None
        if ok:
            info[key] = True
            snapshots[name] = command
        else:
            snapshots[name] = f"Git command failed; {name} was not captured.\n".encode("utf-8")
    return info, snapshots


def _secret_paths(stage):
    found = []
    for path in sorted(stage.rglob("*")):
        if not path.is_file():
            continue
        data = path.read_bytes()
        try:
            if data.startswith((b"\xff\xfe", b"\xfe\xff")):
                text = data.decode("utf-16")
            else:
                text = data.decode("utf-8", errors="replace")
        except UnicodeDecodeError:
            continue
        if any(pattern.search(text) for pattern in SECRET_PATTERNS):
            found.append(path.relative_to(stage).as_posix())
    return found


def _add_required_source(source, stage, destination):
    if source.is_relative_to(SKILL_ROOT):
        _check_link_chain(SKILL_ROOT, source.relative_to(SKILL_ROOT).parts, str(source))
    if not source.is_file():
        raise RelayError(f"Required skill file is missing: {source}")
    target = stage.joinpath(*destination.split("/"))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(_safe_read(source))


def _add_active_task(root, task_dir, task_id, stage, max_bytes, skipped, entries):
    task_rel = f"handoffs/{task_id}"
    for name in TASK_FILES:
        source = task_dir / name
        if not source.is_file():
            raise RelayError(f"Required task file is missing: {task_rel}/{name}")
        _check_link_chain(root, ("handoffs", task_id, name), f"{task_rel}/{name}")
        _copy_selected(root, source, f"{task_rel}/{name}", stage, max_bytes, skipped, entries, mandatory=True)
    if entries["evidence_explicit"]:
        for relative, _ in entries["evidence_scopes"]:
            parts = relative.split("/") if relative else []
            source = task_dir.joinpath(*parts)
            archive_relative = f"{task_rel}/{relative}" if relative else task_rel
            _walk_tree(root, source, archive_relative, stage, task_id, max_bytes, skipped, entries)
    else:
        _walk_tree(root, task_dir, task_rel, stage, task_id, max_bytes, skipped, entries)


def _write_stage_text(stage, relative, content):
    target = stage.joinpath(*relative.split("/"))
    target.parent.mkdir(parents=True, exist_ok=True)
    write_text(target, content)


def _manifest(stage):
    lines = []
    for path in sorted(item for item in stage.rglob("*") if item.is_file()):
        relative = path.relative_to(stage).as_posix()
        if relative == "SHA256SUMS.txt":
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        lines.append(f"{digest}  {relative}")
    _write_stage_text(stage, "SHA256SUMS.txt", "\n".join(lines) + "\n")


def _stage_metrics(stage):
    files = [path for path in stage.rglob("*") if path.is_file()]
    return len(files), sum(path.stat().st_size for path in files)


def _largest_stage_files(stage, limit=5):
    files = ((path.stat().st_size, path.relative_to(stage).as_posix()) for path in stage.rglob("*") if path.is_file())
    return sorted(files, reverse=True)[:limit]


def _file_sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_archive(archive_path, task_id, round_number, package_id):
    with zipfile.ZipFile(archive_path, "r") as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise RelayError("ZIP validation failed: duplicate entries.")
        if "SHA256SUMS.txt" not in names:
            raise RelayError("ZIP validation failed: missing SHA256SUMS.txt.")
        required = {
            "HANDOFF.md",
            "CHATGPT_START.md",
            "GITHUB_REVIEW.md",
            "REVIEW_PROTOCOL.md",
            "PACKAGE_INFO.json",
            "SHA256SUMS.txt",
            f"handoffs/{task_id}/logs/relay-package/git-status.txt",
            f"handoffs/{task_id}/logs/relay-package/git-diff-staged.patch",
            f"handoffs/{task_id}/logs/relay-package/git-diff-unstaged.patch",
            *(f"handoffs/{task_id}/{name}" for name in TASK_FILES),
        }
        if not required.issubset(names):
            raise RelayError("ZIP validation failed: required package contents are missing.")
        info = json.loads(archive.read("PACKAGE_INFO.json"))
        if (info.get("task_id"), info.get("round"), info.get("package_id")) != (
            task_id,
            round_number,
            package_id,
        ):
            raise RelayError("ZIP validation failed: package identity does not match.")
        manifest = {}
        for line in archive.read("SHA256SUMS.txt").decode("utf-8").splitlines():
            digest, relative = line.split("  ", 1)
            manifest[relative] = digest
        if set(manifest) != set(names) - {"SHA256SUMS.txt"}:
            raise RelayError("ZIP validation failed: manifest entries do not match the archive.")
        for relative, expected in manifest.items():
            actual = hashlib.sha256(archive.read(relative)).hexdigest()
            if actual != expected:
                raise RelayError(f"ZIP validation failed: checksum mismatch for {relative}.")
        bad = archive.testzip()
        if bad is not None:
            raise RelayError(f"ZIP validation failed: corrupt entry {bad}.")


def pack_review(args):
    root = project_root(args.project)
    if args.round < 1:
        raise RelayError("Round must be a positive integer.")
    if args.max_file_mb < 1:
        raise RelayError("--max-file-mb must be a positive integer.")
    if args.max_package_mb < 1:
        raise RelayError("--max-package-mb must be a positive integer.")

    task_id = _validate_task_id(args.task) if args.task else None
    handoffs = root / "handoffs"
    if _is_link(handoffs):
        raise RelayError("Refusing to use a symlink for the handoffs directory.")
    if task_id is None:
        pointer = handoffs / ".current-task"
        if not pointer.is_file() or _is_link(pointer):
            raise RelayError("No active task. Run start first or pass --task.")
        task_id = _validate_task_id(pointer.read_text(encoding="utf-8").strip())
    task_dir = handoffs / task_id
    if _is_link(task_dir) or not task_dir.is_dir():
        raise RelayError(f"Task directory does not exist: handoffs/{task_id}")
    _check_link_chain(root, ("handoffs", task_id), f"handoffs/{task_id}")
    for name in TASK_FILES:
        if not (task_dir / name).is_file():
            raise RelayError(f"Required task file is missing: handoffs/{task_id}/{name}")
    evidence_explicit, evidence_scopes, evidence_requested = _prepare_evidence(task_dir, args.evidence)

    required_references = {
        "CHATGPT_START.md": REFERENCE_DIR / "chatgpt.md",
        "GITHUB_REVIEW.md": REFERENCE_DIR / "github.md",
        "REVIEW_PROTOCOL.md": REFERENCE_DIR / "contract.md",
    }
    for source in required_references.values():
        if not source.is_file() or _is_link(source):
            raise RelayError(f"Required skill reference is missing: {source}")

    max_bytes = args.max_file_mb * 1024 * 1024
    max_package_bytes = args.max_package_mb * 1024 * 1024
    skipped = []
    entries = {
        "task_id": task_id,
        "files": set(),
        "selection_explicit": False,
        "include_scopes": [],
        "evidence_explicit": evidence_explicit,
        "evidence_scopes": evidence_scopes,
    }
    _record_evidence_exclusions(task_dir, task_id, entries, skipped)

    output_dir = root / "review-artifacts"
    if _is_link(output_dir):
        raise RelayError("Refusing to write through a symlink at review-artifacts.")

    with tempfile.TemporaryDirectory(prefix="chatgpt-review-") as temp:
        stage = Path(temp)
        _select_project_files(root, task_id, args.include, stage, max_bytes, skipped, entries)
        _add_active_task(root, task_dir, task_id, stage, max_bytes, skipped, entries)
        git_info, git_logs = _git_snapshot(root, task_id, entries, max_bytes, skipped)

        generated_log_dir = f"handoffs/{task_id}/logs/relay-package"
        for name, content in git_logs.items():
            relative = f"{generated_log_dir}/{name}"
            data = content if isinstance(content, bytes) else content.encode("utf-8")
            if relative in entries["files"]:
                raise RelayError(f"Reserved package log path already exists: {relative}")
            path = stage.joinpath(*relative.split("/"))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            entries["files"].add(relative)

        pointer = (
            "# chatgpt-review: active task\n\n"
            f"Read [the authoritative task handoff](handoffs/{task_id}/HANDOFF.md) first.\n"
        )
        _write_stage_text(stage, "HANDOFF.md", pointer)
        for destination, source in required_references.items():
            _add_required_source(source, stage, destination)

        package_id = uuid.uuid4().hex
        skipped_paths = sorted(skipped)
        task_prefix = f"handoffs/{task_id}/"
        excluded_evidence = sorted(
            path[len(task_prefix) :]
            for path in skipped_paths
            if path.startswith(task_prefix)
        )
        selected_file_count, selected_file_bytes = _stage_metrics(stage)
        package_info = {
            "schema_version": 1,
            "task_id": task_id,
            "round": args.round,
            "package_id": package_id,
            "created_at": iso_now(),
            "git": git_info,
            "selection_scope": {
                "mode": "explicit" if entries["selection_explicit"] else "all_safe_project_files",
                "includes": args.include if entries["selection_explicit"] else None,
            },
            "evidence_scope": {
                "mode": "explicit" if evidence_explicit else "all",
                "requested_paths": evidence_requested if evidence_explicit else None,
                "excluded_paths": excluded_evidence,
            },
            "package_budget": {
                "max_compressed_bytes": max_package_bytes,
                "description": "Operational compressed ZIP size ceiling; not a platform limit.",
                "selected_file_count": selected_file_count,
                "selected_file_bytes": selected_file_bytes,
            },
            "skipped_paths": skipped_paths,
            "tests_executed": False,
            "test_execution_note": "The pack command did not execute project tests.",
            "full_snapshot": not entries["selection_explicit"] and not evidence_explicit and not skipped_paths,
        }
        _write_stage_text(stage, "PACKAGE_INFO.json", json.dumps(package_info, indent=2) + "\n")
        _manifest(stage)

        secret_paths = _secret_paths(stage)
        if secret_paths:
            names = ", ".join(secret_paths)
            raise RelayError(f"Packaging blocked: possible credentials found in: {names}")

        with tempfile.TemporaryDirectory(prefix="chatgpt-review-zip-") as zip_temp:
            staged_zip = Path(zip_temp) / "review.zip"
            with zipfile.ZipFile(staged_zip, "x", compression=zipfile.ZIP_DEFLATED) as archive:
                for path in sorted(item for item in stage.rglob("*") if item.is_file()):
                    archive.write(path, path.relative_to(stage).as_posix())
            _verify_archive(staged_zip, task_id, args.round, package_id)
            compressed_bytes = staged_zip.stat().st_size
            if compressed_bytes > max_package_bytes:
                largest = "\n".join(
                    f"{relative} {size} bytes" for size, relative in _largest_stage_files(stage)
                )
                raise RelayError(
                    f"Compressed ZIP size {compressed_bytes} bytes exceeds budget "
                    f"{max_package_bytes} bytes. Largest staged files (path, size):\n{largest}"
                )

            output_dir.mkdir(exist_ok=True)
            if not output_dir.is_dir():
                raise RelayError("review-artifacts exists but is not a directory.")
            archive_path = output_dir / f"{task_id}-r{args.round}-{package_id}.zip"
            staged_hash = _file_sha256(staged_zip)
            created = False
            try:
                with archive_path.open("xb") as destination:
                    created = True
                    with staged_zip.open("rb") as source:
                        shutil.copyfileobj(source, destination)
                    destination.flush()
                    os.fsync(destination.fileno())
                if archive_path.stat().st_size != compressed_bytes or _file_sha256(archive_path) != staged_hash:
                    raise RelayError("Published ZIP does not match the verified temporary archive.")
                _verify_archive(archive_path, task_id, args.round, package_id)
            except Exception:
                if created and archive_path.exists():
                    archive_path.unlink()
                raise
            print(f"REVIEW_ZIP={archive_path}")
            print(f"TASK_ID={task_id}")
            print(f"ROUND={args.round}")
            print(f"PACKAGE_ID={package_id}")
            print(f"SIZE_BYTES={compressed_bytes}")
            print(f"SKIPPED_PATHS={len(skipped_paths)}")
    return 0


def positive_int(value):
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if number < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def build_parser():
    parser = argparse.ArgumentParser(description="Create handoffs and portable ChatGPT review packages.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    start = subparsers.add_parser("start", help="create a new immutable task handoff")
    start.add_argument("--project", required=True, help="project directory")
    start.add_argument("--title", required=True, help="task title")
    start.add_argument("--slug", help="optional lowercase task slug")
    start.set_defaults(handler=start_task)

    pack = subparsers.add_parser("pack", help="create a self-contained review ZIP")
    pack.add_argument("--project", required=True, help="project directory")
    pack.add_argument("--task", help="task id; defaults to handoffs/.current-task")
    pack.add_argument("--round", type=positive_int, required=True, help="positive review round")
    pack.add_argument("--include", action="append", default=[], help="relative path to include; repeatable")
    pack.add_argument("--evidence", action="append", default=[], help="active task evidence path; repeatable")
    pack.add_argument("--max-file-mb", type=positive_int, default=50)
    pack.add_argument("--max-package-mb", type=positive_int, default=25)
    pack.set_defaults(handler=pack_review)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.handler(args)
    except (RelayError, OSError, UnicodeError, zipfile.BadZipFile) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
