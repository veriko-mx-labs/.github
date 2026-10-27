from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from check_pinned_actions import check_line, problems

PIN = "0123456789abcdef0123456789abcdef01234567"


class CheckLineTest(unittest.TestCase):
    def test_accepts_full_sha_with_version(self) -> None:
        self.assertIsNone(check_line(f"      - uses: actions/checkout@{PIN} # v7.0.1"))

    def test_accepts_action_in_subdirectory(self) -> None:
        self.assertIsNone(check_line(f"        uses: org/repo/actions/one@{PIN} # v1"))

    def test_accepts_local_action(self) -> None:
        self.assertIsNone(check_line("      - uses: ./actions/local"))

    def test_rejects_moving_tag(self) -> None:
        self.assertIn("SHA completo", check_line("      - uses: actions/checkout@v7") or "")

    def test_rejects_branch(self) -> None:
        self.assertIn("SHA completo", check_line("        uses: org/repo/actions/one@main") or "")

    def test_rejects_short_sha(self) -> None:
        self.assertIn("SHA completo", check_line("      - uses: actions/checkout@3d3c42e # v7") or "")

    def test_rejects_missing_version_comment(self) -> None:
        self.assertIn("versión", check_line(f"      - uses: actions/checkout@{PIN}") or "")

    def test_rejects_comment_without_version(self) -> None:
        self.assertIn("versión", check_line(f"      - uses: actions/checkout@{PIN} # latest") or "")

    def test_rejects_docker_image_without_digest(self) -> None:
        self.assertIn("digest", check_line("      - uses: docker://alpine:3.20") or "")

    def test_ignores_other_lines(self) -> None:
        self.assertIsNone(check_line("      - name: Usar la acción"))


class ProblemsTest(unittest.TestCase):
    def test_scans_workflows_and_actions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".github" / "workflows").mkdir(parents=True)
            (root / "actions" / "uno").mkdir(parents=True)
            (root / ".github" / "workflows" / "ci.yml").write_text(
                f"jobs:\n  a:\n    steps:\n      - uses: actions/checkout@{PIN} # v7\n", encoding="utf-8"
            )
            (root / "actions" / "uno" / "action.yml").write_text(
                "runs:\n  steps:\n    - uses: actions/setup-python@v7\n", encoding="utf-8"
            )
            found = problems(root)
            self.assertEqual(len(found), 1)
            self.assertTrue(found[0].startswith("actions/uno/action.yml:3:"))


if __name__ == "__main__":
    unittest.main()
