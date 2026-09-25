import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from verify_authorship import error, main, run, run_push

ORGANIZATION = ("Veriko", "soporte@veriko.mx")
BOT = ("dependabot[bot]", "49699333+dependabot[bot]@users.noreply.github.com")
WEB = ("GitHub", "noreply@github.com")
STRANGER = ("Persona Ajena", "ajena@example.org")
TOKEN = "zorro-azul"
CONFIG = f"{TOKEN}\n"
NOTICE = "::notice::Comprobación de contenido omitida: sin configuración de la organización."


class Repository:
    """Repositorio temporal, aislado de la configuración de git de quien corre las pruebas."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.env = {
            **os.environ,
            "HOME": str(root),
            "USERPROFILE": str(root),
            "XDG_CONFIG_HOME": str(root),
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        self.git("init", "-q", "-b", "main")
        self.base = self.commit({"README.md": "Base.\n"}, "Base")

    def git(self, *args: str, env: dict[str, str] | None = None) -> str:
        result = subprocess.run(
            ["git", "-C", str(self.root), "-c", "core.autocrlf=false", "-c", "commit.gpgsign=false", *args],
            check=True,
            capture_output=True,
            env=env or self.env,
        )
        return result.stdout.decode("utf-8").strip()

    def commit(self, files, message, author=ORGANIZATION, committer=None) -> str:
        committer = committer or author
        for name, content in files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
        env = {
            **self.env,
            "GIT_AUTHOR_NAME": author[0],
            "GIT_AUTHOR_EMAIL": author[1],
            "GIT_COMMITTER_NAME": committer[0],
            "GIT_COMMITTER_EMAIL": committer[1],
        }
        self.git("add", "-A", env=env)
        self.git("commit", "-q", "-m", message, env=env)
        return self.git("rev-parse", "HEAD")

    def event(self, title="Ajuste", body="", branch="ajuste", user_type="User") -> dict:
        return {
            "pull_request": {
                "title": title,
                "body": body,
                "head": {"sha": self.git("rev-parse", "HEAD"), "ref": branch},
                "base": {"sha": self.base},
                "user": {"type": user_type, "login": "alguien"},
            }
        }


class RepositoryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.repo = Repository(Path(directory.name))

    def check(self, event=None, config=""):
        return run(self.repo.root, event or self.repo.event(), config)


class AuthorshipTest(RepositoryTestCase):
    def test_accepts_organization_identity(self):
        self.repo.commit({"a.txt": "a\n"}, "Añadir a")
        outcome = self.check()
        self.assertEqual(0, outcome.exit_code)
        self.assertEqual(["Autoría verificada: 1 commit.", NOTICE], outcome.lines)

    def test_counts_every_commit_of_the_range(self):
        self.repo.commit({"a.txt": "a\n"}, "Añadir a")
        self.repo.commit({"b.txt": "b\n"}, "Añadir b")
        self.assertEqual("Autoría verificada: 2 commits.", self.check().lines[0])

    def test_accepts_bot_and_web_committer(self):
        self.repo.commit({"a.txt": "a\n"}, "Actualizar a", author=BOT)
        self.repo.commit({"b.txt": "b\n"}, "Fusionar b", author=ORGANIZATION, committer=WEB)
        outcome = self.check()
        self.assertEqual(0, outcome.exit_code)
        self.assertEqual("Autoría verificada: 2 commits.", outcome.lines[0])

    def test_rejects_another_person_as_author_and_as_committer(self):
        sha = self.repo.commit({"a.txt": "a\n"}, "Añadir a", author=STRANGER, committer=ORGANIZATION)
        outcome = self.check()
        self.assertEqual(1, outcome.exit_code)
        self.assertEqual(
            [error(f"commit {sha[:7]}: autor no permitido: Persona Ajena <ajena@example.org>"), NOTICE],
            outcome.lines,
        )
        self.repo.commit({"b.txt": "b\n"}, "Añadir b", author=ORGANIZATION, committer=STRANGER)
        self.assertIn("committer no permitido", " ".join(self.check().lines))

    def test_rejects_organization_name_with_another_address(self):
        self.repo.commit({"a.txt": "a\n"}, "Añadir a", author=("Veriko", "otro@example.org"))
        self.assertEqual(1, self.check().exit_code)

    def test_rejects_web_identity_as_author(self):
        self.repo.commit({"a.txt": "a\n"}, "Añadir a", author=WEB, committer=ORGANIZATION)
        self.assertEqual(1, self.check().exit_code)

    def test_rejects_personal_noreply_address_with_bot_name(self):
        self.repo.commit(
            {"a.txt": "a\n"}, "Añadir a", author=("Alguien[bot]", "12345+alguien@users.noreply.github.com")
        )
        self.assertEqual(1, self.check().exit_code)

    def test_rejects_one_bad_commit_among_good_ones(self):
        self.repo.commit({"a.txt": "a\n"}, "Añadir a")
        self.repo.commit({"b.txt": "b\n"}, "Añadir b", author=STRANGER)
        outcome = self.check()
        self.assertEqual(1, outcome.exit_code)
        self.assertNotIn("Autoría verificada", " ".join(outcome.lines))


class TrailerTest(RepositoryTestCase):
    def test_rejects_trailer_in_any_case_indentation_and_position(self):
        for message, line in (
            ("Añadir a\n\nCo-authored-by: Persona <p@example.org>", 3),
            ("Añadir a\n\nCO-AUTHORED-BY: Persona <p@example.org>", 3),
            ("Añadir a\n\n   co-authored-by: Persona <p@example.org>", 3),
            ("Añadir a\n\nTexto previo.\nCo-Authored-By: Persona <p@example.org>\nTexto posterior.", 4),
        ):
            with self.subTest(message=message):
                sha = self.repo.commit({"a.txt": message}, message)
                outcome = self.check()
                self.assertEqual(1, outcome.exit_code)
                self.assertIn(error(f"commit {sha[:7]}, mensaje, línea {line}: trailer no permitido"), outcome.lines)

    def test_rejects_trailer_in_pull_request_body(self):
        self.repo.commit({"a.txt": "a\n"}, "Añadir a")
        outcome = self.check(self.repo.event(body="Resumen.\n\n  CO-AUTHORED-BY: Persona <p@example.org>"))
        self.assertEqual(1, outcome.exit_code)
        self.assertEqual(error("pull request, cuerpo, línea 3: trailer no permitido"), outcome.lines[0])

    def test_accepts_text_that_only_mentions_the_word(self):
        self.repo.commit({"a.txt": "a\n"}, "Añadir a\n\nSin Co-authored-by en esta línea inicial.")
        self.assertEqual(0, self.check().exit_code)


class ConfigTest(RepositoryTestCase):
    def test_missing_blank_or_comment_only_config_skips_content(self):
        self.repo.commit({"a.txt": f"{TOKEN}\n"}, "Añadir a")
        for config in ("", "  \n\n", "# Sólo un comentario.\n"):
            with self.subTest(config=config):
                outcome = self.check(config=config)
                self.assertEqual(0, outcome.exit_code)
                self.assertEqual(["Autoría verificada: 1 commit.", NOTICE], outcome.lines)

    def test_broken_expression_fails_with_its_line_number_only(self):
        self.repo.commit({"a.txt": "a\n"}, "Añadir a")
        outcome = self.check(config=f"{TOKEN}\n(abierto\n")
        self.assertEqual(2, outcome.exit_code)
        self.assertEqual([error("La configuración de la organización no es válida (línea 2).")], outcome.lines)
        self.assertNotIn("abierto", "\n".join(outcome.lines))

    def test_expression_that_matches_the_empty_string_is_invalid(self):
        self.repo.commit({"a.txt": "a\n"}, "Añadir a")
        for config in ("x*\n", f"{TOKEN}\n^$\n", "(?:)\n"):
            with self.subTest(config=config):
                self.assertEqual(2, self.check(config=config).exit_code)

    def test_accepts_windows_line_endings_comments_and_blank_lines(self):
        self.repo.commit({"a.txt": f"uno\n{TOKEN}\n"}, "Añadir a")
        outcome = self.check(config=f"# Comentario\r\n\r\n{TOKEN}\r\n")
        self.assertEqual(1, outcome.exit_code)
        self.assertEqual(error("Contenido no permitido", file="a.txt", line=2), outcome.lines[1])


class ContentTest(RepositoryTestCase):
    def assertNoLeak(self, outcome):
        text = "\n".join(outcome.lines)
        self.assertNotIn(TOKEN, text)
        self.assertNotIn(TOKEN.upper(), text)

    def test_clean_content_passes_without_notice(self):
        self.repo.commit({"a.txt": "Texto limpio.\n"}, "Añadir a")
        outcome = self.check(config=CONFIG)
        self.assertEqual(0, outcome.exit_code)
        self.assertEqual(["Autoría verificada: 1 commit."], outcome.lines)

    def test_match_in_file_reports_path_and_line(self):
        self.repo.commit({"docs/nota.md": f"uno\ndos\nsigue {TOKEN} aquí\n"}, "Añadir nota")
        outcome = self.check(config=CONFIG)
        self.assertEqual(1, outcome.exit_code)
        self.assertEqual(
            ["Autoría verificada: 1 commit.", error("Contenido no permitido", file="docs/nota.md", line=3)],
            outcome.lines,
        )
        self.assertNoLeak(outcome)

    def test_reports_one_finding_per_line_even_with_several_expressions(self):
        self.repo.commit({"a.txt": f"{TOKEN}\n"}, "Añadir a")
        outcome = self.check(config=f"{TOKEN}\nazul\n")
        self.assertEqual(1, outcome.findings)

    def test_match_in_path(self):
        self.repo.commit({f"{TOKEN}/nota.md": "Texto limpio.\n"}, "Añadir nota")
        outcome = self.check(config=CONFIG)
        self.assertEqual(
            ["Autoría verificada: 1 commit.", error("Contenido no permitido", file=f"{TOKEN}/nota.md")],
            outcome.lines,
        )

    def test_match_in_commit_message(self):
        sha = self.repo.commit({"a.txt": "a\n"}, f"Añadir a\n\nDetalle: {TOKEN}")
        outcome = self.check(config=CONFIG)
        self.assertEqual(error(f"commit {sha[:7]}, mensaje, línea 3: contenido no permitido"), outcome.lines[1])
        self.assertNoLeak(outcome)

    def test_match_in_title_body_and_branch(self):
        self.repo.commit({"a.txt": "a\n"}, "Añadir a")
        event = self.repo.event(title=f"Añadir {TOKEN}", body=f"Uno.\n{TOKEN.upper()}", branch=f"rama-{TOKEN}")
        outcome = self.check(event, CONFIG)
        self.assertEqual(
            [
                "Autoría verificada: 1 commit.",
                error("pull request, título: contenido no permitido"),
                error("pull request, cuerpo, línea 2: contenido no permitido"),
                error("pull request, rama: contenido no permitido"),
            ],
            outcome.lines,
        )
        self.assertNoLeak(outcome)

    def test_matching_ignores_case_unless_the_expression_asks_for_it(self):
        self.repo.commit({"a.txt": f"{TOKEN.upper()}\n"}, "Añadir a")
        self.assertEqual(1, self.check(config=CONFIG).exit_code)
        self.assertEqual(0, self.check(config=f"(?-i:{TOKEN})\n").exit_code)

    def test_skips_binary_and_oversized_files(self):
        self.repo.commit(
            {
                "imagen.bin": b"\0" + TOKEN.encode(),
                "grande.txt": TOKEN.encode() + b"\n" + b"x" * (2 * 1024 * 1024),
                "chico.txt": f"{TOKEN}\n",
            },
            "Añadir archivos",
        )
        outcome = self.check(config=CONFIG)
        self.assertEqual(
            ["Autoría verificada: 1 commit.", error("Contenido no permitido", file="chico.txt", line=1)],
            outcome.lines,
        )

    def test_bot_pull_request_skips_messages_but_scans_the_tree(self):
        self.repo.commit({"a.txt": "a\n"}, f"Actualizar {TOKEN}", author=BOT)
        event = self.repo.event(title=f"Actualizar {TOKEN}", body=TOKEN, user_type="Bot")
        self.assertEqual(0, self.check(event, CONFIG).exit_code)
        self.repo.commit({"b.txt": f"{TOKEN}\n"}, "Actualizar b", author=BOT)
        outcome = self.check(self.repo.event(user_type="Bot"), CONFIG)
        self.assertEqual(1, outcome.exit_code)
        self.assertEqual(error("Contenido no permitido", file="b.txt", line=1), outcome.lines[-1])

    def test_authorship_and_content_findings_come_together(self):
        self.repo.commit({"a.txt": f"{TOKEN}\n"}, "Añadir a", author=STRANGER, committer=ORGANIZATION)
        outcome = self.check(config=CONFIG)
        self.assertEqual(2, outcome.findings)
        self.assertEqual(1, outcome.exit_code)
        self.assertNotIn("Autoría verificada", " ".join(outcome.lines))


class EventTest(RepositoryTestCase):
    def test_requires_a_pull_request_event(self):
        self.assertEqual(2, run(self.repo.root, {"action": "opened"}, "").exit_code)

    def test_fails_when_the_history_is_missing(self):
        event = self.repo.event()
        event["pull_request"]["base"]["sha"] = "0" * 40
        outcome = run(self.repo.root, event, "")
        self.assertEqual(2, outcome.exit_code)
        self.assertEqual([error("No se pudo leer el historial del pull request.")], outcome.lines)

    def test_rejects_commit_ids_that_are_not_full_hashes(self):
        event = self.repo.event()
        event["pull_request"]["base"]["sha"] = "--output=x"
        self.assertEqual(2, run(self.repo.root, event, "").exit_code)


class PushTest(RepositoryTestCase):
    def push(self, before):
        return {"ref": "refs/heads/main", "before": before, "after": self.repo.git("rev-parse", "HEAD")}

    def test_accepts_organization_commits(self):
        self.repo.commit({"a.txt": "a\n"}, "Añadir a")
        self.repo.commit({"b.txt": "b\n"}, "Añadir b")
        outcome = run_push(self.repo.root, self.push(self.repo.base))
        self.assertEqual(0, outcome.exit_code)
        self.assertEqual(["Autoría verificada: 2 commits."], outcome.lines)

    def test_rejects_web_committer_and_bot(self):
        for author, committer in ((ORGANIZATION, WEB), (BOT, BOT), (STRANGER, ORGANIZATION)):
            with self.subTest(author=author, committer=committer):
                before = self.repo.git("rev-parse", "HEAD")
                self.repo.commit({"c.txt": f"{author[0]}{committer[0]}\n"}, "Cambio", author, committer)
                outcome = run_push(self.repo.root, self.push(before))
                self.assertEqual(1, outcome.exit_code)
                self.assertIn("no permitido", outcome.lines[0])

    def test_rejects_trailer(self):
        self.repo.commit({"a.txt": "a\n"}, "Cambio\n\nCo-authored-by: x <y@z>")
        outcome = run_push(self.repo.root, self.push(self.repo.base))
        self.assertEqual(1, outcome.exit_code)

    def test_new_branch_checks_its_head(self):
        outcome = run_push(self.repo.root, self.push("0" * 40))
        self.assertEqual(["Autoría verificada: 1 commit."], outcome.lines)

    def test_only_main(self):
        event = self.push(self.repo.base)
        event["ref"] = "refs/heads/otra"
        self.assertEqual(2, run_push(self.repo.root, event).exit_code)


class AnnotationTest(unittest.TestCase):
    def test_escapes_workflow_command_characters(self):
        self.assertEqual("::error file=a%2Cb%3Ac,line=3::uno%0Ados%25", error("uno\ndos%", file="a,b:c", line=3))


class MainTest(RepositoryTestCase):
    def call(self, event, config="", event_name="pull_request"):
        event_file = self.repo.root.parent / f"{self.repo.root.name}-evento.json"
        event_file.write_text(json.dumps(event), encoding="utf-8")
        summary = self.repo.root.parent / f"{self.repo.root.name}-resumen.md"
        self.addCleanup(lambda: (event_file.unlink(missing_ok=True), summary.unlink(missing_ok=True)))
        environment = {
            "AUTORIA_CONFIG": config,
            "GITHUB_EVENT_NAME": event_name,
            "GITHUB_STEP_SUMMARY": str(summary),
        }
        output = io.StringIO()
        with mock.patch.dict(os.environ, environment), contextlib.redirect_stdout(output):
            code = main(["--repo", str(self.repo.root), "--event", str(event_file)])
        return code, output.getvalue(), summary.read_text(encoding="utf-8") if summary.exists() else ""

    def test_exit_codes_output_and_summary(self):
        self.repo.commit({"a.txt": "a\n"}, "Añadir a")
        code, output, summary = self.call(self.repo.event())
        self.assertEqual(0, code)
        self.assertEqual(f"Autoría verificada: 1 commit.\n{NOTICE}\n", output)
        self.assertIn("- Commits revisados: 1", summary)
        self.assertIn("- Hallazgos: 0", summary)

        self.repo.commit({"b.txt": f"{TOKEN}\n"}, "Añadir b")
        code, output, summary = self.call(self.repo.event(), CONFIG)
        self.assertEqual(1, code)
        self.assertIn("- Hallazgos: 1", summary)
        self.assertNotIn(TOKEN, output + summary)

        code, output, _ = self.call(self.repo.event(), "(\n")
        self.assertEqual(2, code)
        self.assertEqual(error("La configuración de la organización no es válida (línea 1).") + "\n", output)

    def test_only_runs_on_pull_request_and_push_events(self):
        for event_name in ("pull_request_target", "workflow_dispatch"):
            with self.subTest(event_name=event_name):
                code, output, _ = self.call(self.repo.event(), event_name=event_name)
                self.assertEqual(2, code)
                self.assertEqual(
                    error("Esta comprobación sólo se ejecuta en eventos pull_request y push.") + "\n", output
                )

    def test_push_event_checks_main(self):
        self.repo.commit({"a.txt": "a\n"}, "Añadir a")
        event = {"ref": "refs/heads/main", "before": self.repo.base, "after": self.repo.git("rev-parse", "HEAD")}
        code, output, _ = self.call(event, event_name="push")
        self.assertEqual(0, code)
        self.assertEqual("Autoría verificada: 1 commit.\n", output)


if __name__ == "__main__":
    unittest.main()
