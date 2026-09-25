"""Pruebas de comportamiento para sync_public_spec.sh.

Ejecuta el script real contra un repositorio Git temporal (origin + copia de
trabajo), con `gh` y `curl` sustituidos por dobles que registran cómo se
invocan en vez de hablar con GitHub. `git` corre de verdad; `jq` también,
salvo que no esté disponible en el equipo (no es el caso de los runners de
CI), en cuyo caso se usa un sustituto mínimo sólo para desarrollo local.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = ROOT / "actions" / "sync-public-spec" / "sync_public_spec.sh"
VALIDATOR = ROOT / "actions" / "sync-public-spec" / "validate_public_spec.py"

# En Windows, CreateProcess resuelve un nombre sin ruta contra System32 antes
# que contra el PATH, y ahí vive el bash.exe de WSL, no el de Git. Resolver
# la ruta explícita evita ese choque; en Linux (los runners de CI) equivale
# a "bash".
BASH = shutil.which("bash") or "bash"

FAKE_GH = textwrap.dedent(
    """\
    #!/usr/bin/env bash
    set -euo pipefail
    log="${FAKE_GH_LOG:?}"
    printf 'CALL: %s\\n' "$*" >> "$log"

    args=("$@")
    for ((i = 0; i < ${#args[@]}; i++)); do
      if [[ "${args[$i]}" == "--body-file" ]]; then
        path="${args[$((i + 1))]}"
        {
          echo 'BODY-BEGIN'
          cat "$path"
          echo 'BODY-END'
        } >> "$log"
      fi
    done

    case "${1:-} ${2:-}" in
      "pr list")
        printf '%s' "${FAKE_GH_PR_NUMBER:-}"
        ;;
      "pr create")
        echo "https://github.com/veriko-mx-labs/test-repo/pull/${FAKE_GH_NEW_PR_NUMBER:-99}"
        ;;
      "pr edit")
        :
        ;;
      "pr checks")
        printf '%s' "${FAKE_GH_CHECKS_JSON:-[]}"
        ;;
      "pr merge")
        :
        ;;
      "run view")
        echo 'salida de prueba de un run fallido'
        ;;
      *)
        echo "fake gh: subcomando no soportado: $*" >&2
        exit 1
        ;;
    esac
    """
)

FAKE_CURL = textwrap.dedent(
    """\
    #!/usr/bin/env bash
    set -euo pipefail
    output=""
    args=("$@")
    for ((i = 0; i < ${#args[@]}; i++)); do
      if [[ "${args[$i]}" == "--output" ]]; then
        output="${args[$((i + 1))]}"
      fi
    done
    cp "${FAKE_CURL_SOURCE:?}" "$output"
    """
)

# Sustituto mínimo de jq, sólo para equipos sin jq en el PATH. Cubre
# exactamente los filtros que usa sync_public_spec.sh.
FAKE_JQ_PY = textwrap.dedent(
    """\
    import json
    import sys
    from pathlib import Path

    argv = sys.argv[1:]
    positional = [a for a in argv if a != "-r"]
    filter_ = positional[0]
    source = positional[1] if len(positional) > 1 else None
    data = json.loads(Path(source).read_text(encoding="utf-8")) if source else json.load(sys.stdin)

    if filter_ == "length":
        print(len(data))
    elif "pending" in filter_:
        print(len([c for c in data if c.get("bucket") == "pending"]))
    elif "fail" in filter_ and "select" in filter_ and "link" not in filter_.split("|")[-1]:
        print(len([c for c in data if c.get("bucket") in ("fail", "cancel")]))
    elif filter_.startswith('.[] | select(.bucket == "fail" or .bucket == "cancel") | .link'):
        for c in data:
            if c.get("bucket") in ("fail", "cancel"):
                print(c.get("link", ""))
    elif filter_.startswith(".[] |"):
        for c in data:
            workflow = c.get("workflow") or "CI"
            print(f"- {workflow} / {c.get('name')}: {c.get('bucket')}")
    else:
        raise SystemExit(f"fake jq: filtro no soportado: {filter_}")
    """
)

FAKE_JQ_SH = textwrap.dedent(
    """\
    #!/usr/bin/env bash
    exec python3 "$(dirname "$0")/fake_jq.py" "$@"
    """
)


def _make_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8", newline="\n")
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _public_spec_document(marker: str) -> dict:
    paths = {
        f"/resource-{index}": {"get": {"responses": {"200": {"description": marker}}}}
        for index in range(51)
    }
    return {
        "openapi": "3.1.0",
        "info": {"title": "Contrato de prueba", "version": marker},
        "paths": paths,
    }


class SyncPublicSpecTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sync-public-spec-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        self.bin_dir = self.tmp / "bin"
        self.bin_dir.mkdir()
        _make_executable(self.bin_dir / "gh", FAKE_GH)
        _make_executable(self.bin_dir / "curl", FAKE_CURL)
        if shutil.which("jq") is None:
            (self.bin_dir / "fake_jq.py").write_text(FAKE_JQ_PY, encoding="utf-8")
            _make_executable(self.bin_dir / "jq", FAKE_JQ_SH)

        self.origin = self.tmp / "origin.git"
        subprocess.run(
            ["git", "init", "--quiet", "--bare", "-b", "main", str(self.origin)],
            check=True,
        )

        self.work = self.tmp / "work"
        subprocess.run(
            ["git", "clone", "--quiet", str(self.origin), str(self.work)], check=True
        )
        self._git("config", "user.name", "Prueba")
        self._git("config", "user.email", "prueba@example.com")

        self.spec_path = self.work / "spec" / "openapi.yaml"
        self.spec_path.parent.mkdir(parents=True)
        self.spec_path.write_text(
            yaml.safe_dump(_public_spec_document("anterior")), encoding="utf-8"
        )
        (self.work / "README.md").write_text("inicial\n", encoding="utf-8")
        self._git("add", "--all")
        self._git("commit", "--quiet", "--message", "inicial")
        self._git("push", "--quiet", "origin", "HEAD:main")

        self.new_spec = self.tmp / "nuevo-openapi.yaml"
        self.new_spec.write_text(
            yaml.safe_dump(_public_spec_document("nueva")), encoding="utf-8"
        )

        self.log_file = self.tmp / "gh.log"
        self.log_file.write_text("", encoding="utf-8")

        self.fusionar = self.tmp / "fusionar.py"
        self.fusionar.write_text(
            "import os, sys\n"
            "with open(os.environ['FAKE_GH_LOG'], 'a', encoding='utf-8') as log:\n"
            "    log.write('CALL: fusionar ' + ' '.join(sys.argv[1:]) + '\\n')\n",
            encoding="utf-8",
        )

        self.env = {
            **os.environ,
            "PATH": f"{self.bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            "GH_TOKEN": "token-de-prueba",
            "GITHUB_REPOSITORY": "veriko-mx-labs/test-repo",
            "SPEC_PATH": "spec/openapi.yaml",
            "REGENERATE_COMMAND": "true",
            "SYNC_BRANCH": "automation/sync-public-spec",
            "BASE_BRANCH": "main",
            "AUTO_MERGE": "false",
            "DRAFT": "false",
            "REVIEWERS": "",
            "SUMMARY_FILE": "",
            "VALIDATOR": str(VALIDATOR),
            "FUSIONAR": str(self.fusionar),
            "FAKE_GH_LOG": str(self.log_file),
            "FAKE_GH_PR_NUMBER": "",
            "FAKE_GH_CHECKS_JSON": '[{"name":"job","bucket":"pass","link":"x","workflow":"CI"}]',
            "FAKE_CURL_SOURCE": str(self.new_spec),
        }

    def _git(self, *args: str) -> str:
        result = subprocess.run(
            ["git", *args], cwd=self.work, check=True, capture_output=True, text=True
        )
        return result.stdout

    def _run(self, **overrides: str) -> subprocess.CompletedProcess:
        env = {**self.env, **overrides}
        return subprocess.run(
            [BASH, str(SCRIPT)],
            cwd=self.work,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def _log(self) -> str:
        return self.log_file.read_text(encoding="utf-8")

    # -- auto_merge y draft ------------------------------------------------

    def test_auto_merge_and_draft_reject_each_other(self) -> None:
        result = self._run(AUTO_MERGE="true", DRAFT="true")
        self.assertEqual(result.returncode, 1)
        self.assertIn("auto_merge y draft no pueden ser true a la vez", result.stderr)
        self.assertEqual(self._log(), "", "no debía llamarse a gh")

    # -- PR nuevo como borrador, con revisores y resumen --------------------

    def test_new_pr_is_draft_requests_reviewers_and_carries_summary(self) -> None:
        summary_file = self.tmp / "resumen.md"
        result = self._run(
            DRAFT="true",
            REVIEWERS="revisor-de-prueba",
            SUMMARY_FILE=str(summary_file),
            REGENERATE_COMMAND='printf "Detalle de prueba" > "$VERIKO_SYNC_SUMMARY_FILE"',
        )
        self.assertEqual(result.returncode, 0, result.stderr)

        log = self._log()
        create_calls = [line for line in log.splitlines() if line.startswith("CALL: pr create")]
        self.assertEqual(len(create_calls), 1)
        self.assertIn("--draft", create_calls[0])
        self.assertIn("--reviewer revisor-de-prueba", create_calls[0])

        # El resumen viaja en cada versión del cuerpo del PR (pendiente y verde).
        self.assertEqual(log.count("## Resumen"), 2)
        self.assertEqual(log.count("Detalle de prueba"), 2)

        # Un borrador nunca se marca como listo ni se fusiona.
        self.assertNotIn("pr ready", log)
        self.assertNotIn("CALL: fusionar", log)

    # -- fusión en verde, sin borrador ------------------------------

    def test_green_check_with_auto_merge_merges_and_skips_draft_flag(self) -> None:
        result = self._run(AUTO_MERGE="true", DRAFT="false")
        self.assertEqual(result.returncode, 0, result.stderr)

        log = self._log()
        create_calls = [line for line in log.splitlines() if line.startswith("CALL: pr create")]
        self.assertEqual(len(create_calls), 1)
        self.assertNotIn("--draft", create_calls[0])
        self.assertIn("CALL: fusionar --pr 99 --esperar 600", log)
        self.assertNotIn("CALL: pr merge", log)

    # -- un PR existente no vuelve a pedir revisores ni se marca listo ------

    def test_existing_pr_does_not_request_reviewers_again(self) -> None:
        # Deja la rama del bot ya creada en origin, como si un sync anterior
        # hubiera abierto el PR.
        self._git("switch", "--quiet", "-c", "automation/sync-public-spec")
        self.spec_path.write_text(
            yaml.safe_dump(_public_spec_document("intermedia")), encoding="utf-8"
        )
        self._git("commit", "--quiet", "--all", "--message", "sincronización previa")
        self._git("push", "--quiet", "origin", "HEAD:automation/sync-public-spec")
        self._git("switch", "--quiet", "main")

        result = self._run(
            DRAFT="true",
            REVIEWERS="revisor-de-prueba",
            FAKE_GH_PR_NUMBER="42",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

        log = self._log()
        self.assertNotIn("CALL: pr create", log)
        edit_calls = [line for line in log.splitlines() if line.startswith("CALL: pr edit")]
        self.assertTrue(edit_calls)
        for call in edit_calls:
            self.assertNotIn("--reviewer", call)
        self.assertNotIn("pr ready", log)


if __name__ == "__main__":
    unittest.main()
