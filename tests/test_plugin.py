import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PluginPackagingTests(unittest.TestCase):
    def test_manifest_uses_one_existing_skill(self):
        manifest = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["name"], "chatgpt-review")
        self.assertTrue(manifest["description"].strip())
        self.assertEqual(set(manifest), {"name", "description"})
        skills = sorted(path.relative_to(ROOT).as_posix() for path in (ROOT / "skills").rglob("SKILL.md"))
        self.assertEqual(skills, ["skills/chatgpt-review/SKILL.md"])
        skill = ROOT / "skills" / "chatgpt-review"
        for name in ("chatgpt.md", "github.md", "contract.md", "executor.md", "browser.md"):
            self.assertTrue((skill / "references" / name).is_file(), name)
        self.assertTrue((skill / "scripts" / "relay.py").is_file())
        self.assertTrue((skill / "templates" / "TEST_RESULTS.template.md").is_file())

    def test_committed_bootstrap_matches_generation_and_tracks_reference_changes(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            shutil.copy2(ROOT / "build_bootstrap.py", target / "build_bootstrap.py")
            refs = target / "skills" / "chatgpt-review" / "references"
            refs.mkdir(parents=True)
            for name in ("chatgpt.md", "github.md", "contract.md"):
                shutil.copy2(ROOT / "skills" / "chatgpt-review" / "references" / name, refs / name)
            result = subprocess.run([sys.executable, str(target / "build_bootstrap.py")], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            generated = (target / "CHATGPT_START.md").read_text(encoding="utf-8")
            self.assertEqual(generated, (ROOT / "CHATGPT_START.md").read_text(encoding="utf-8"))
            marker = "\nReference-change probe for the isolated test.\n"
            with (refs / "github.md").open("a", encoding="utf-8") as stream:
                stream.write(marker)
            result = subprocess.run([sys.executable, str(target / "build_bootstrap.py")], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            updated = (target / "CHATGPT_START.md").read_text(encoding="utf-8")
            self.assertNotEqual(updated, generated)
            self.assertIn(marker, updated)


if __name__ == "__main__":
    unittest.main()
