import contextlib
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = PROJECT_ROOT / "skills" / "chatgpt-review"
SCRIPT = SKILL_ROOT / "scripts" / "relay.py"
RELAY_SPEC = importlib.util.spec_from_file_location("chatgpt_review_relay", SCRIPT)
relay = importlib.util.module_from_spec(RELAY_SPEC)
RELAY_SPEC.loader.exec_module(relay)
REQUIRED_TASK_FILES = (
    "HANDOFF.md",
    "REVIEW_REPORT.md",
    "TASK_BRIEF.md",
    "CHATGPT_BRIEF.md",
    "TEST_RESULTS.md",
)


def run_relay(project, *args):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args, "--project", str(project)],
        cwd=project,
        capture_output=True,
        text=True,
        check=False,
    )


def task_id_from(result):
    match = re.search(r"^TASK_ID=([A-Za-z0-9-]+)$", result.stdout, re.MULTILINE)
    if not match:
        raise AssertionError(f"No task id in CLI output:\n{result.stdout}\n{result.stderr}")
    return match.group(1)


def archive_from(result):
    match = re.search(r"^REVIEW_ZIP=(.+)$", result.stdout, re.MULTILINE)
    if not match:
        raise AssertionError(f"No archive path in CLI output:\n{result.stdout}\n{result.stderr}")
    return Path(match.group(1))


def verify_manifest(archive):
    with zipfile.ZipFile(archive) as package:
        names = set(package.namelist())
        lines = package.read("SHA256SUMS.txt").decode("utf-8").splitlines()
        manifest = {}
        for line in lines:
            digest, path = line.split("  ", 1)
            manifest[path] = digest
        assert set(manifest) == names - {"SHA256SUMS.txt"}
        for path, expected in manifest.items():
            actual = hashlib.sha256(package.read(path)).hexdigest()
            assert actual == expected, path


class RelayCliTests(unittest.TestCase):
    def start(self, project, title="Example task", task_id=None):
        args = ["start", "--title", title]
        if task_id is not None:
            args.extend(["--task-id", task_id])
        result = run_relay(project, *args)
        self.assertEqual(result.returncode, 0, result.stderr)
        return task_id_from(result)

    def pack(self, project, round_number=1, *extra):
        result = run_relay(
            project,
            "pack",
            "--round",
            str(round_number),
            *extra,
        )
        return result

    def test_start_creates_unique_task_files_and_preserves_user_pointer(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            (project / "HANDOFF.md").write_text("user-owned pointer\n", encoding="utf-8")

            first = self.start(project, "First task")
            second = self.start(project, "Second task")

            self.assertNotEqual(first, second)
            for task_id in (first, second):
                task_dir = project / "handoffs" / task_id
                for filename in REQUIRED_TASK_FILES:
                    self.assertTrue((task_dir / filename).is_file(), filename)
                for filename in REQUIRED_TASK_FILES:
                    text = (task_dir / filename).read_text(encoding="utf-8")
                    self.assertNotIn("{{TASK_ID}}", text)
                    self.assertNotIn("{{TITLE}}", text)
                    self.assertNotIn("{{UPDATED}}", text)
            self.assertEqual((project / "HANDOFF.md").read_text(encoding="utf-8"), "user-owned pointer\n")
            self.assertEqual((project / "handoffs" / ".current-task").read_text(encoding="utf-8").strip(), second)
            self.assertIn("First task", (project / "handoffs" / first / "HANDOFF.md").read_text(encoding="utf-8"))

    def test_start_updates_only_its_managed_root_pointer(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            first = self.start(project, "First task")
            pointer = project / "HANDOFF.md"
            self.assertIn(first, pointer.read_text(encoding="utf-8"))

            second = self.start(project, "Second task")

            self.assertIn("<!-- chatgpt-review:managed-pointer -->", pointer.read_text(encoding="utf-8"))
            self.assertIn(second, pointer.read_text(encoding="utf-8"))
            self.assertNotIn(first, pointer.read_text(encoding="utf-8"))

    def test_start_uses_the_exact_specified_task_id(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            requested = "20261015-chatgpt-review-self-review"

            task_id = self.start(project, "Review round one", requested)

            self.assertEqual(task_id, requested)
            task_dir = project / "handoffs" / requested
            for filename in REQUIRED_TASK_FILES:
                self.assertIn(requested, (task_dir / filename).read_text(encoding="utf-8"))
            self.assertEqual((project / "handoffs" / ".current-task").read_text().strip(), requested)
            self.assertIn(requested, (project / "HANDOFF.md").read_text(encoding="utf-8"))

    def test_start_rejects_invalid_task_ids_without_creating_handoffs(self):
        for invalid in ("traversal", "separator", "too-short"):
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as temp:
                project = Path(temp)
                traversal_target = project.parent / f"{project.name}-outside"
                task_id = {
                    "traversal": f"../../{project.name}-outside",
                    "separator": "bad/id",
                    "too-short": "ab",
                }[invalid]

                result = run_relay(project, "start", "--title", "Invalid ID", "--task-id", task_id)

                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("unrecognized arguments", result.stderr)
                self.assertFalse((project / "handoffs").exists())
                self.assertFalse(traversal_target.exists())

    def test_start_collision_preserves_records_and_pointers(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            original_id = "20261015-chatgpt-review-self-review"
            self.start(project, "Original title", original_id)
            original_dir = project / "handoffs" / original_id
            original_records = {
                filename: (original_dir / filename).read_bytes()
                for filename in REQUIRED_TASK_FILES
            }
            self.start(project, "Move active pointer")
            current_path = project / "handoffs" / ".current-task"
            root_pointer = project / "HANDOFF.md"
            current_before = current_path.read_bytes()
            root_before = root_pointer.read_bytes()

            result = run_relay(
                project,
                "start",
                "--title",
                "Replacement title",
                "--task-id",
                original_id,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("unrecognized arguments", result.stderr)
            self.assertEqual(
                {filename: (original_dir / filename).read_bytes() for filename in REQUIRED_TASK_FILES},
                original_records,
            )
            self.assertEqual(current_path.read_bytes(), current_before)
            self.assertEqual(root_pointer.read_bytes(), root_before)
            self.assertEqual(
                sorted(path.name for path in (project / "handoffs").iterdir() if path.is_dir()),
                sorted([original_id, current_before.decode("utf-8").strip()]),
            )

    def test_default_package_is_self_contained_and_hashes_copied_bytes(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            task_id = self.start(project)
            (project / "app.py").write_text("print('untracked')\n", encoding="utf-8")
            (project / ".env").write_text("TOKEN=not-for-sharing\n", encoding="utf-8")
            private_config = project / ".env.production" / "config.py"
            private_config.parent.mkdir()
            private_config.write_text("setting = 'private'\n", encoding="utf-8")
            other = project / "handoffs" / "other-task"
            other.mkdir()
            (other / "private-note.md").write_text("other task\n", encoding="utf-8")

            first_result = self.pack(project, 1)
            second_result = self.pack(project, 2)
            self.assertEqual(first_result.returncode, 0, first_result.stderr)
            self.assertEqual(second_result.returncode, 0, second_result.stderr)
            first_zip = archive_from(first_result)
            second_zip = archive_from(second_result)
            self.assertIn(f"TASK_ID={task_id}", first_result.stdout)
            self.assertIn("ROUND=1", first_result.stdout)
            self.assertRegex(first_result.stdout, r"(?m)^SIZE_BYTES=[1-9][0-9]*$", msg=first_result.stdout)
            self.assertEqual(
                int(re.search(r"(?m)^SIZE_BYTES=([1-9][0-9]*)$", first_result.stdout).group(1)),
                first_zip.stat().st_size,
            )
            self.assertNotEqual(first_zip.name, second_zip.name)
            verify_manifest(first_zip)
            verify_manifest(second_zip)

            with zipfile.ZipFile(first_zip) as package:
                names = set(package.namelist())
                task_prefix = f"handoffs/{task_id}/"
                self.assertTrue(all(task_prefix + name in names for name in REQUIRED_TASK_FILES))
                self.assertIn("app.py", names)
                self.assertNotIn(".env", names)
                self.assertFalse(any(name.startswith(".env.production/") for name in names))
                self.assertFalse(any(name.startswith("handoffs/other-task/") for name in names))
                self.assertIn("HANDOFF.md", names)
                self.assertIn("CHATGPT_START.md", names)
                self.assertIn("GITHUB_REVIEW.md", names)
                self.assertIn("REVIEW_PROTOCOL.md", names)
                self.assertEqual(
                    package.read("GITHUB_REVIEW.md"),
                    (SKILL_ROOT / "references" / "github.md").read_bytes(),
                )
                self.assertIn("PACKAGE_INFO.json", names)
                info = json.loads(package.read("PACKAGE_INFO.json"))
                self.assertTrue(package.read("HANDOFF.md").startswith(b"# chatgpt-review: active task"))
                with zipfile.ZipFile(second_zip) as second_package:
                    second_info = json.loads(second_package.read("PACKAGE_INFO.json"))
                self.assertEqual(info["schema_version"], 1)
                self.assertEqual(info["task_id"], task_id)
                self.assertEqual(info["round"], 1)
                self.assertNotEqual(info["package_id"], second_info["package_id"])
                self.assertFalse(info["tests_executed"])
                self.assertFalse(info["full_snapshot"])
                self.assertEqual(info["package_budget"]["max_compressed_bytes"], 25 * 1024 * 1024)
                self.assertGreater(info["package_budget"]["selected_file_count"], 0)
                self.assertGreater(info["package_budget"]["selected_file_bytes"], 0)
                self.assertIn(".env", info["skipped_paths"])
                self.assertIn(".env.production", info["skipped_paths"])
                self.assertIsNone(info["git"]["head"])
                self.assertFalse(info["git"]["repository_available"])
                self.assertNotIn("fatal:", package.read(f"{task_prefix}logs/relay-package/git-status.txt").decode("utf-8"))

    def test_explicit_task_selection_does_not_mix_other_tasks(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            first = self.start(project, "First task")
            second = self.start(project, "Second task")

            result = self.pack(project, 1, "--task", first)
            self.assertEqual(result.returncode, 0, result.stderr)
            with zipfile.ZipFile(archive_from(result)) as package:
                names = package.namelist()
                self.assertTrue(all(f"handoffs/{first}/{name}" in names for name in REQUIRED_TASK_FILES))
                self.assertFalse(any(name.startswith(f"handoffs/{second}/") for name in names))
                info = json.loads(package.read("PACKAGE_INFO.json"))
                self.assertEqual(info["selection_scope"]["mode"], "all_safe_project_files")

    @unittest.skipUnless(shutil.which("git"), "git is not installed")
    def test_git_status_and_both_diffs_are_captured_with_root_untracked_files(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            project = repo / "app"
            project.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Relay Test"], check=True)
            tracked = project / "tracked.py"
            tracked.write_text("base\n", encoding="utf-8")
            env_file = project / ".env"
            env_file.write_text("BASE_ENV=initial\n", encoding="utf-8")
            database = project / "customers.db"
            database.write_bytes(b"\x00BASE_DATABASE")
            sibling = repo / "sibling.py"
            sibling.write_text("sibling base\n", encoding="utf-8")
            subprocess.run(
                ["git", "-C", str(repo), "add", "app/tracked.py", "app/.env", "app/customers.db", "sibling.py"],
                check=True,
            )
            subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True)
            head = subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            subprocess.run(["git", "-C", str(repo), "update-ref", "refs/remotes/origin/main", head], check=True)
            subprocess.run(
                ["git", "-C", str(repo), "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main"],
                check=True,
            )
            task_id = self.start(project)

            tracked.write_text("base\nstaged marker\n", encoding="utf-8")
            env_file.write_text("BASE_ENV=initial\nSTAGED_ENV_MARKER\n", encoding="utf-8")
            database.write_bytes(b"\x00BASE_DATABASE_STAGED_DATABASE_MARKER")
            subprocess.run(
                ["git", "-C", str(project), "add", "tracked.py", ".env", "customers.db"],
                check=True,
            )
            tracked.write_text("base\nstaged marker\nunstaged marker\n", encoding="utf-8")
            env_file.write_text("BASE_ENV=initial\nSTAGED_ENV_MARKER\nUNSTAGED_ENV_MARKER\n", encoding="utf-8")
            database.write_bytes(b"\x00BASE_DATABASE_STAGED_DATABASE_MARKER_UNSTAGED_DATABASE_MARKER")
            sibling.write_text("sibling base\noutside sibling staged\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "sibling.py"], check=True)
            sibling.write_text("sibling base\noutside sibling staged\noutside sibling unstaged\n", encoding="utf-8")
            (project / "root-untracked.py").write_text("print('new')\n", encoding="utf-8")

            result = self.pack(project)
            self.assertEqual(result.returncode, 0, result.stderr)
            with zipfile.ZipFile(archive_from(result)) as package:
                prefix = f"handoffs/{task_id}/logs/relay-package/"
                self.assertIn("root-untracked.py", package.namelist())
                status = package.read(prefix + "git-status.txt").decode("utf-8")
                staged = package.read(prefix + "git-diff-staged.patch").decode("utf-8")
                unstaged = package.read(prefix + "git-diff-unstaged.patch").decode("utf-8")
                self.assertRegex(status, r"(?m)^\?\? root-untracked\.py$")
                self.assertNotIn("app/root-untracked.py", status)
                self.assertNotIn("app/tracked.py", status)
                self.assertNotIn("sibling.py", status)
                self.assertNotIn(".env", status)
                self.assertNotIn("customers.db", status)
                self.assertIn("staged marker", staged)
                self.assertIn("unstaged marker", unstaged)
                self.assertNotIn("STAGED_ENV_MARKER", staged)
                self.assertNotIn("UNSTAGED_ENV_MARKER", unstaged)
                self.assertNotIn(b"DATABASE_MARKER", package.read(prefix + "git-diff-staged.patch"))
                self.assertNotIn(b"DATABASE_MARKER", package.read(prefix + "git-diff-unstaged.patch"))
                self.assertNotIn("outside sibling", staged)
                self.assertNotIn("outside sibling", unstaged)
                info = json.loads(package.read("PACKAGE_INFO.json"))
                self.assertEqual(info["git"]["head"], head)
                self.assertIsNone(info["git"]["base"])

    @unittest.skipUnless(shutil.which("git"), "git is not installed")
    def test_git_diffs_follow_explicit_include_scope(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            project = repo / "app"
            source_dir = project / "src"
            source_dir.mkdir(parents=True)
            selected = source_dir / "app.py"
            selected.write_text("base\n", encoding="utf-8")
            excluded = project / "other.py"
            excluded.write_text("other base\n", encoding="utf-8")
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Relay Test"], check=True)
            subprocess.run(["git", "-C", str(repo), "add", "app/src/app.py", "app/other.py"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True)
            task_id = self.start(project)
            selected.write_text("base\nselected staged marker\n", encoding="utf-8")
            excluded.write_text("other base\nexcluded staged marker\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(project), "add", "src/app.py", "other.py"], check=True)
            selected.write_text("base\nselected staged marker\nselected unstaged marker\n", encoding="utf-8")
            excluded.write_text("other base\nexcluded staged marker\nexcluded unstaged marker\n", encoding="utf-8")

            result = self.pack(project, 1, "--include", "src")

            self.assertEqual(result.returncode, 0, result.stderr)
            with zipfile.ZipFile(archive_from(result)) as package:
                prefix = f"handoffs/{task_id}/logs/relay-package/"
                status = package.read(prefix + "git-status.txt").decode("utf-8")
                staged = package.read(prefix + "git-diff-staged.patch").decode("utf-8")
                unstaged = package.read(prefix + "git-diff-unstaged.patch").decode("utf-8")
                self.assertRegex(status, r"(?m)^MM src/app\.py$")
                self.assertNotIn("app/src/app.py", status)
                self.assertNotIn("other.py", status)
                self.assertIn("selected staged marker", staged)
                self.assertIn("selected unstaged marker", unstaged)
                self.assertNotIn("excluded staged marker", staged)
                self.assertNotIn("excluded unstaged marker", unstaged)
                info = json.loads(package.read("PACKAGE_INFO.json"))
                self.assertEqual(info["selection_scope"]["mode"], "explicit")
                self.assertFalse(info["full_snapshot"])
                self.assertNotIn("other.py", package.namelist())

    def test_explicit_file_include_is_added_to_the_archive(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            self.start(project)
            selected = project / "src" / "app.py"
            selected.parent.mkdir()
            selected.write_text("print('selected')\n", encoding="utf-8")

            result = self.pack(project, 1, "--include", "src/app.py")

            self.assertEqual(result.returncode, 0, result.stderr)
            with zipfile.ZipFile(archive_from(result)) as package:
                self.assertEqual(package.read("src/app.py"), selected.read_bytes())
                info = json.loads(package.read("PACKAGE_INFO.json"))
                self.assertEqual(info["selection_scope"]["includes"], ["src/app.py"])

    def test_explicit_oversized_file_fails_without_archive(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            self.start(project)
            oversized = project / "large.bin"
            oversized.write_bytes(b"x" * (1024 * 1024 + 1))

            result = self.pack(project, 1, "--include", "large.bin", "--max-file-mb", "1")

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("large.bin", result.stderr + result.stdout)
            self.assertFalse((project / "review-artifacts").exists())

    def test_explicit_sensitive_file_fails_without_exposing_content(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            self.start(project)
            selected = project / "api-token.txt"
            selected.write_text("DO_NOT_PRINT_THIS_TOKEN\n", encoding="utf-8")

            result = self.pack(project, 1, "--include", "api-token.txt")

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("api-token.txt", result.stderr + result.stdout)
            self.assertNotIn("DO_NOT_PRINT_THIS_TOKEN", result.stderr + result.stdout)
            self.assertFalse((project / "review-artifacts").exists())

    def test_explicit_unreadable_file_fails_without_archive(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            self.start(project)
            selected = project / "source.py"
            selected.write_text("print('selected')\n", encoding="utf-8")
            real_safe_read = relay._safe_read

            def deny_selected(path):
                if path == selected:
                    raise PermissionError("access denied")
                return real_safe_read(path)

            stdout = io.StringIO()
            stderr = io.StringIO()
            with mock.patch.object(relay, "_safe_read", side_effect=deny_selected):
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    result = relay.main(
                        ["pack", "--round", "1", "--include", "source.py", "--project", str(project)]
                    )

            self.assertNotEqual(result, 0)
            self.assertIn("source.py", stderr.getvalue() + stdout.getvalue())
            self.assertFalse((project / "review-artifacts").exists())

    def test_explicit_nonregular_file_fails_without_archive(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            self.start(project)
            special = project / "special-source"
            special.write_text("not a regular-file selection\n", encoding="utf-8")
            real_is_file = Path.is_file

            def mark_nonregular(path):
                if path == special:
                    return False
                return real_is_file(path)

            stdout = io.StringIO()
            stderr = io.StringIO()
            with mock.patch.object(Path, "is_file", mark_nonregular):
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    result = relay.main(
                        ["pack", "--round", "1", "--include", "special-source", "--project", str(project)]
                    )

            self.assertNotEqual(result, 0)
            self.assertIn("special-source", stderr.getvalue() + stdout.getvalue())
            self.assertFalse((project / "review-artifacts").exists())

    def test_explicit_directory_include_reports_filtered_children(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            self.start(project)
            selected_dir = project / "src"
            selected_dir.mkdir()
            (selected_dir / "app.py").write_text("print('selected')\n", encoding="utf-8")
            (selected_dir / ".env").write_text("TOKEN=private\n", encoding="utf-8")
            (selected_dir / "large.bin").write_bytes(b"x" * (1024 * 1024 + 1))

            result = self.pack(project, 1, "--include", "src", "--max-file-mb", "1")

            self.assertEqual(result.returncode, 0, result.stderr)
            with zipfile.ZipFile(archive_from(result)) as package:
                self.assertIn("src/app.py", package.namelist())
                self.assertNotIn("src/.env", package.namelist())
                self.assertNotIn("src/large.bin", package.namelist())
                info = json.loads(package.read("PACKAGE_INFO.json"))
                self.assertIn("src/.env", info["skipped_paths"])
                self.assertIn("src/large.bin", info["skipped_paths"])

    def test_explicit_file_under_filtered_directory_fails_without_archive(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            self.start(project)
            filtered_dir = project / ".env.private"
            filtered_dir.mkdir()
            (filtered_dir / "source.py").write_text("print('private')\n", encoding="utf-8")

            result = self.pack(project, 1, "--include", ".env.private/source.py")

            self.assertNotEqual(result.returncode, 0)
            self.assertIn(".env.private/source.py", result.stderr + result.stdout)
            self.assertFalse((project / "review-artifacts").exists())

    def test_secret_blocks_archive_before_output_and_reports_path_only(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            self.start(project)
            source = project / "src" / "app.py"
            source.parent.mkdir()
            secret = "ghp_" + "A" * 36
            source.write_text(f'token = "{secret}"\n', encoding="utf-8")

            result = self.pack(project)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("src/app.py", result.stderr + result.stdout)
            self.assertNotIn(secret, result.stderr + result.stdout)
            self.assertFalse((project / "review-artifacts").exists())

    def test_invalid_or_missing_explicit_include_fails(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            self.start(project)

            traversal = self.pack(project, 1, "--include", "../outside")
            missing = self.pack(project, 1, "--include", "missing-source")
            absolute = self.pack(project, 1, "--include", "/outside")

            self.assertNotEqual(traversal.returncode, 0)
            self.assertNotEqual(missing.returncode, 0)
            self.assertNotEqual(absolute.returncode, 0)
            self.assertFalse((project / "review-artifacts").exists())

    def test_default_pack_traverses_projects_named_build(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "build"
            project.mkdir()
            self.start(project)
            (project / "app.py").write_text("print('kept')\n", encoding="utf-8")

            result = self.pack(project)

            self.assertEqual(result.returncode, 0, result.stderr)
            with zipfile.ZipFile(archive_from(result)) as package:
                self.assertIn("app.py", package.namelist())

    def test_explicit_include_rejects_symlink_ancestor_when_available(self):
        with tempfile.TemporaryDirectory() as temp, tempfile.TemporaryDirectory() as outside:
            project = Path(temp)
            self.start(project)
            external = Path(outside)
            (external / "source.py").write_text("print('outside')\n", encoding="utf-8")
            link = project / "linked-source"
            try:
                os.symlink(external, link, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("directory symlinks are unavailable")

            result = self.pack(project, 1, "--include", "linked-source/source.py")

            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((project / "review-artifacts").exists())

    def test_explicit_include_rejects_mocked_link_without_platform_symlink(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            self.start(project)
            source_dir = project / "linked-source"
            source_dir.mkdir()
            (source_dir / "source.py").write_text("print('outside')\n", encoding="utf-8")
            real_is_link = relay._is_link

            def simulated_link(path):
                return path == source_dir or real_is_link(path)

            stdout = io.StringIO()
            stderr = io.StringIO()
            with mock.patch.object(relay, "_is_link", side_effect=simulated_link):
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    result = relay.main(
                        [
                            "pack",
                            "--round",
                            "1",
                            "--include",
                            "linked-source/source.py",
                            "--project",
                            str(project),
                        ]
                    )

            self.assertNotEqual(result, 0)
            self.assertIn("linked-source/source.py", stderr.getvalue() + stdout.getvalue())
            self.assertFalse((project / "review-artifacts").exists())

    def test_missing_required_task_file_fails_without_archive(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            task_id = self.start(project)
            (project / "handoffs" / task_id / "TASK_BRIEF.md").unlink()

            result = self.pack(project)

            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((project / "review-artifacts").exists())

    def test_default_evidence_scope_keeps_task_history(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            task_id = self.start(project)
            task_dir = project / "handoffs" / task_id
            old_log = task_dir / "logs" / "r1" / "resolved.log"
            old_feedback = task_dir / "feedback" / "round-1.md"
            old_log.parent.mkdir(parents=True)
            old_feedback.parent.mkdir(parents=True)
            old_log.write_text("resolved history\n", encoding="utf-8")
            old_feedback.write_text("old feedback\n", encoding="utf-8")

            result = self.pack(project)

            self.assertEqual(result.returncode, 0, result.stderr)
            with zipfile.ZipFile(archive_from(result)) as package:
                names = set(package.namelist())
                self.assertIn(f"handoffs/{task_id}/logs/r1/resolved.log", names)
                self.assertIn(f"handoffs/{task_id}/feedback/round-1.md", names)
                info = json.loads(package.read("PACKAGE_INFO.json"))
                self.assertEqual(info["evidence_scope"]["mode"], "all")

    def test_explicit_evidence_keeps_required_records_and_excludes_history(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            task_id = self.start(project)
            task_dir = project / "handoffs" / task_id
            old_log = task_dir / "logs" / "r1" / "large-resolved.log"
            current_log = task_dir / "logs" / "r3" / "current.log"
            old_feedback = task_dir / "feedback" / "round-1.md"
            current_feedback = task_dir / "feedback" / "round-2.md"
            old_log.parent.mkdir(parents=True)
            current_log.parent.mkdir(parents=True)
            old_feedback.parent.mkdir(parents=True)
            old_log.write_bytes(os.urandom(2 * 1024 * 1024))
            current_log.write_text("current run evidence\n", encoding="utf-8")
            old_feedback.write_text("resolved old feedback\n", encoding="utf-8")
            current_feedback.write_text("current unresolved feedback\n", encoding="utf-8")

            result = self.pack(
                project,
                3,
                "--evidence",
                "logs/r3",
                "--evidence",
                "feedback/round-2.md",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            archive = archive_from(result)
            verify_manifest(archive)
            with zipfile.ZipFile(archive) as package:
                names = set(package.namelist())
                task_prefix = f"handoffs/{task_id}/"
                self.assertTrue(all(task_prefix + name in names for name in REQUIRED_TASK_FILES))
                self.assertIn(task_prefix + "logs/r3/current.log", names)
                self.assertIn(task_prefix + "feedback/round-2.md", names)
                self.assertNotIn(task_prefix + "logs/r1/large-resolved.log", names)
                self.assertNotIn(task_prefix + "feedback/round-1.md", names)
                info = json.loads(package.read("PACKAGE_INFO.json"))
                evidence = info["evidence_scope"]
                self.assertEqual(evidence["mode"], "explicit")
                self.assertEqual(evidence["requested_paths"], ["logs/r3", "feedback/round-2.md"])
                self.assertIn("logs/r1", evidence["excluded_paths"])
                self.assertIn("feedback/round-1.md", evidence["excluded_paths"])
                self.assertFalse(info["full_snapshot"])

    def test_evidence_paths_reject_missing_and_traversal(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            self.start(project)

            traversal = self.pack(project, 1, "--evidence", "../outside")
            missing = self.pack(project, 1, "--evidence", "logs/missing")

            self.assertNotEqual(traversal.returncode, 0)
            self.assertNotEqual(missing.returncode, 0)
            self.assertFalse((project / "review-artifacts").exists())

    def test_evidence_path_rejects_symlink_ancestor_when_available(self):
        with tempfile.TemporaryDirectory() as temp, tempfile.TemporaryDirectory() as outside:
            project = Path(temp)
            task_id = self.start(project)
            external = Path(outside)
            (external / "evidence.log").write_text("external evidence\n", encoding="utf-8")
            link = project / "handoffs" / task_id / "external"
            try:
                os.symlink(external, link, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("directory symlinks are unavailable")

            result = self.pack(project, 1, "--evidence", "external/evidence.log")

            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((project / "review-artifacts").exists())

    def test_evidence_path_rejects_mocked_link_without_platform_symlink(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            task_id = self.start(project)
            external = project / "handoffs" / task_id / "external"
            external.mkdir()
            (external / "evidence.log").write_text("external evidence\n", encoding="utf-8")
            real_is_link = relay._is_link

            def simulated_link(path):
                return path == external or real_is_link(path)

            stdout = io.StringIO()
            stderr = io.StringIO()
            with mock.patch.object(relay, "_is_link", side_effect=simulated_link):
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    result = relay.main(
                        [
                            "pack",
                            "--round",
                            "1",
                            "--evidence",
                            "external/evidence.log",
                            "--project",
                            str(project),
                        ]
                    )

            self.assertNotEqual(result, 0)
            self.assertIn("external/evidence.log", stderr.getvalue() + stdout.getvalue())
            self.assertFalse((project / "review-artifacts").exists())

    @unittest.skipUnless(shutil.which("git"), "git is not installed")
    def test_explicit_evidence_filters_git_status_and_diffs(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            subprocess.run(["git", "init", "-q", str(project)], check=True)
            subprocess.run(["git", "-C", str(project), "config", "user.email", "test@example.com"], check=True)
            subprocess.run(["git", "-C", str(project), "config", "user.name", "Relay Test"], check=True)
            subprocess.run(["git", "-C", str(project), "config", "core.autocrlf", "false"], check=True)
            (project / "seed.py").write_text("base\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(project), "add", "seed.py"], check=True)
            subprocess.run(["git", "-C", str(project), "commit", "-qm", "base"], check=True)
            task_id = self.start(project)
            task_dir = project / "handoffs" / task_id
            old_log = task_dir / "logs" / "r1" / "old.log"
            current_log = task_dir / "logs" / "r3" / "current.log"
            old_log.parent.mkdir(parents=True)
            current_log.parent.mkdir(parents=True)
            old_log.write_text("old base\n", encoding="utf-8")
            current_log.write_text("current base\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(project), "add", f"handoffs/{task_id}"], check=True)
            subprocess.run(["git", "-C", str(project), "commit", "-qm", "task evidence baseline"], check=True)
            old_log.write_text("old base\nOMITTED_HISTORY_STAGED\n", encoding="utf-8")
            current_log.write_text("current base\nCURRENT_EVIDENCE_STAGED\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(project), "add", str(current_log.relative_to(project))], check=True)
            current_log.write_text(
                "current base\nCURRENT_EVIDENCE_STAGED\nCURRENT_EVIDENCE_UNSTAGED\n",
                encoding="utf-8",
            )

            result = self.pack(project, 3, "--evidence", "logs/r3")

            self.assertEqual(result.returncode, 0, result.stderr)
            with zipfile.ZipFile(archive_from(result)) as package:
                prefix = f"handoffs/{task_id}/logs/relay-package/"
                status = package.read(prefix + "git-status.txt").decode("utf-8")
                staged = package.read(prefix + "git-diff-staged.patch").decode("utf-8")
                unstaged = package.read(prefix + "git-diff-unstaged.patch").decode("utf-8")
                self.assertIn("logs/r3/current.log", status)
                self.assertNotIn("logs/r1/old.log", status)
                self.assertIn("CURRENT_EVIDENCE_STAGED", staged)
                self.assertIn("CURRENT_EVIDENCE_UNSTAGED", unstaged)
                self.assertNotIn("OMITTED_HISTORY_STAGED", staged)
                self.assertNotIn("OMITTED_HISTORY_STAGED", unstaged)

    def test_compressed_package_budget_blocks_new_final_zip_and_preserves_old(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            self.start(project)
            payload = project / "payload.bin"
            payload.write_bytes(os.urandom(2 * 1024 * 1024))
            artifacts = project / "review-artifacts"
            artifacts.mkdir()
            old_artifact = artifacts / "previous.zip"
            old_artifact.write_bytes(b"keep this artifact")

            result = self.pack(project, 1, "--max-package-mb", "1")

            self.assertNotEqual(result.returncode, 0)
            self.assertRegex(result.stderr, r"Compressed ZIP size [1-9][0-9]+ bytes exceeds budget 1048576 bytes")
            self.assertRegex(result.stderr, r"payload\.bin 2097152 bytes")
            self.assertEqual(old_artifact.read_bytes(), b"keep this artifact")
            self.assertEqual(sorted(path.name for path in artifacts.iterdir()), ["previous.zip"])

    def test_pack_preserves_source_chatgpt_start_sha_and_passes_plugin_check(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            self.start(project, title="test-task")
            source_content = (PROJECT_ROOT / "CHATGPT_START.md").read_bytes()
            (project / "CHATGPT_START.md").write_bytes(source_content)
            source_sha = hashlib.sha256(source_content).hexdigest()

            result = self.pack(project, 1)
            self.assertEqual(result.returncode, 0, result.stderr)

            artifacts = project / "review-artifacts"
            zips = list(artifacts.glob("*.zip"))
            self.assertEqual(len(zips), 1)
            packed_zip = zips[0]

            with zipfile.ZipFile(packed_zip, "r") as archive:
                names = archive.namelist()
                self.assertIn("CHATGPT_START.md", names)
                self.assertIn("CHATGPT_COORDINATION.md", names)
                packed_content = archive.read("CHATGPT_START.md")
                packed_sha = hashlib.sha256(packed_content).hexdigest()
                self.assertEqual(packed_sha, source_sha)

            with tempfile.TemporaryDirectory() as unpack_temp:
                unpack_dir = Path(unpack_temp)
                with zipfile.ZipFile(packed_zip, "r") as archive:
                    archive.extractall(unpack_dir)

                extracted_start = (unpack_dir / "CHATGPT_START.md").read_text(encoding="utf-8")
                refs = SKILL_ROOT / "references"
                expected_bootstrap = (
                    "# chatgpt-review 首次启动\n\n"
                    "将本文件上传给 ChatGPT，描述你的目标，请它按协调与审查端流程生成任务书。"
                    "本文由 references/chatgpt.md、github.md 和 contract.md 自动生成。"
                    "后续上传的审查 ZIP 自带这两份说明。\n\n"
                    + (refs / "chatgpt.md").read_text(encoding="utf-8")
                    + "\n\n"
                    + (refs / "github.md").read_text(encoding="utf-8")
                    + "\n\n"
                    + (refs / "contract.md").read_text(encoding="utf-8")
                )
                self.assertEqual(extracted_start, expected_bootstrap)

    def test_pack_preserves_both_chatgpt_start_and_coordination_when_both_exist(self):
        """F-011: When user project already has both CHATGPT_START.md and CHATGPT_COORDINATION.md, neither is overwritten."""
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            self.start(project, title="test-task")
            custom_start = b"# User Custom CHATGPT_START.md"
            custom_coord = b"# User Custom CHATGPT_COORDINATION.md"
            (project / "CHATGPT_START.md").write_bytes(custom_start)
            (project / "CHATGPT_COORDINATION.md").write_bytes(custom_coord)

            result = self.pack(project, 1)
            self.assertEqual(result.returncode, 0, result.stderr)

            artifacts = project / "review-artifacts"
            zips = list(artifacts.glob("*.zip"))
            packed_zip = zips[0]

            with zipfile.ZipFile(packed_zip, "r") as archive:
                self.assertEqual(archive.read("CHATGPT_START.md"), custom_start)
                self.assertEqual(archive.read("CHATGPT_COORDINATION.md"), custom_coord)
                names = archive.namelist()
                has_alt_coord = any(
                    name.startswith("CHATGPT_COORDINATION_") or name == "CHATGPT_REVIEW_COORDINATION.md"
                    for name in names
                )
                self.assertTrue(has_alt_coord, f"Expected non-conflicting coordination file in {names}")


if __name__ == "__main__":
    unittest.main()


