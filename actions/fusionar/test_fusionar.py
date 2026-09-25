"""Pruebas de la fusión por avance rápido."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fusionar import Failure, HttpError, Merger  # noqa: E402

REPO = "org/repo"
BASE = "a" * 40
HEAD = "b" * 40
VERIKO = {"name": "Veriko", "email": "soporte@veriko.mx"}


def commit(sha: str = HEAD, author: dict[str, str] = VERIKO, committer: dict[str, str] = VERIKO,
           message: str = "Cambiar algo") -> dict[str, Any]:
    return {"sha": sha, "commit": {"author": author, "committer": committer, "message": message}}


class FakeGitHub:
    def __init__(self) -> None:
        self.pull: dict[str, Any] = {
            "state": "open",
            "draft": False,
            "base": {"ref": "main"},
            "head": {"sha": HEAD, "repo": {"full_name": REPO}},
        }
        self.main = BASE
        self.comparison: dict[str, Any] = {"status": "ahead", "behind_by": 0, "total_commits": 1,
                                           "commits": [commit()]}
        self.check_runs: list[dict[str, Any]] = [
            {"name": "test", "status": "completed", "conclusion": "success", "details_url": ""}
        ]
        self.statuses: list[dict[str, Any]] = []
        self.pending_polls = 0
        self.head_after_checks: str | None = None
        self.patch_error: HttpError | None = None
        self.patches: list[dict[str, Any]] = []
        self.pull_reads = 0

    def __call__(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        prefix = f"/repos/{REPO}"
        assert path.startswith(prefix), path
        path = path[len(prefix):]
        if method == "GET" and path == "/pulls/7":
            self.pull_reads += 1
            if self.pull_reads > 1 and self.head_after_checks:
                self.pull["head"]["sha"] = self.head_after_checks
            return self.pull
        if method == "GET" and path == "/git/ref/heads/main":
            return {"object": {"sha": self.main}}
        if method == "GET" and path.startswith("/compare/"):
            return self.comparison
        if method == "GET" and "/check-runs" in path:
            if self.pending_polls > 0:
                self.pending_polls -= 1
                runs = [{"name": "test", "status": "in_progress", "conclusion": None, "details_url": ""}]
            else:
                runs = self.check_runs
            return {"total_count": len(runs), "check_runs": runs}
        if method == "GET" and path.endswith("/status"):
            return {"statuses": self.statuses}
        if method == "PATCH" and path == "/git/refs/heads/main":
            if self.patch_error:
                raise self.patch_error
            self.patches.append(body or {})
            self.main = (body or {})["sha"]
            return {"object": {"sha": self.main}}
        raise AssertionError(f"{method} {path}")


class MergeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.github = FakeGitHub()
        self.sleeps: list[float] = []
        self.now = 0.0

        def sleep(seconds: float) -> None:
            self.sleeps.append(seconds)
            self.now += seconds

        self.merger = Merger(self.github, REPO, "99", sleep=sleep, clock=lambda: self.now)

    def assert_fails(self, text: str, wait: float = 0) -> None:
        with self.assertRaises(Failure) as caught:
            self.merger.merge(7, wait)
        self.assertIn(text, str(caught.exception))
        self.assertEqual(self.github.patches, [])

    def test_green_pull_request_moves_main_without_force(self) -> None:
        self.assertEqual(self.merger.merge(7), (HEAD, 1))
        self.assertEqual(self.github.patches, [{"sha": HEAD, "force": False}])

    def test_draft_is_rejected(self) -> None:
        self.github.pull["draft"] = True
        self.assert_fails("borrador")

    def test_closed_is_rejected(self) -> None:
        self.github.pull["state"] = "closed"
        self.assert_fails("no está abierto")

    def test_other_base_is_rejected(self) -> None:
        self.github.pull["base"]["ref"] = "develop"
        self.assert_fails("no apunta a main")

    def test_fork_is_rejected(self) -> None:
        self.github.pull["head"]["repo"]["full_name"] = "otra/repo"
        self.assert_fails("otro repositorio")

    def test_branch_behind_main_asks_for_rebase(self) -> None:
        self.github.comparison["behind_by"] = 1
        self.github.comparison["status"] = "diverged"
        self.assert_fails("rebásala")

    def test_other_author_is_rejected(self) -> None:
        self.github.comparison["commits"] = [commit(author={"name": "Alguien", "email": "a@b.c"})]
        self.assert_fails("autor no permitido: Alguien <a@b.c>")

    def test_web_committer_is_rejected(self) -> None:
        self.github.comparison["commits"] = [commit(committer={"name": "GitHub", "email": "noreply@github.com"})]
        self.assert_fails("committer no permitido: GitHub")

    def test_co_author_trailer_is_rejected(self) -> None:
        self.github.comparison["commits"] = [commit(message="Cambio\n\n  CO-AUTHORED-BY: x <y@z>")]
        self.assert_fails("línea 3: trailer no permitido")

    def test_without_checks_is_rejected(self) -> None:
        self.github.check_runs = []
        self.assert_fails("no tiene checks")

    def test_failed_check_is_rejected(self) -> None:
        self.github.check_runs[0]["conclusion"] = "failure"
        self.assert_fails("no pasaron: test")

    def test_failed_status_is_rejected(self) -> None:
        self.github.statuses = [{"context": "externo", "state": "failure"}]
        self.assert_fails("no pasaron: externo")

    def test_own_run_is_ignored(self) -> None:
        self.github.check_runs.append({"name": "fusión", "status": "in_progress", "conclusion": None,
                                       "details_url": "https://github.com/org/repo/actions/runs/99/job/1"})
        self.assertEqual(self.merger.merge(7)[0], HEAD)

    def test_pending_without_wait_is_rejected(self) -> None:
        self.github.pending_polls = 1
        self.assert_fails("pendientes: test")

    def test_pending_resolves_while_waiting(self) -> None:
        self.github.pending_polls = 2
        self.assertEqual(self.merger.merge(7, 600)[0], HEAD)
        self.assertEqual(self.sleeps, [15, 15])

    def test_pending_past_deadline_is_rejected(self) -> None:
        self.github.pending_polls = 100
        self.assert_fails("pendientes", wait=30)

    def test_head_changed_during_checks_is_rejected(self) -> None:
        self.github.head_after_checks = "c" * 40
        self.assert_fails("cambió")

    def test_rejected_ref_update_is_reported(self) -> None:
        self.github.patch_error = HttpError(422, "Update is not a fast forward")
        self.assert_fails("No se pudo mover main")


if __name__ == "__main__":
    unittest.main()
