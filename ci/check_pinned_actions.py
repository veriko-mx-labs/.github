#!/usr/bin/env python3
"""Comprueba que toda acción externa esté fijada a un SHA completo con su versión en comentario."""

from __future__ import annotations

import re
import sys
from pathlib import Path

USES = re.compile(r"^\s*(?:-\s+)?uses:\s*(?P<ref>[^\s#]+)[ \t]*(?:#[ \t]*(?P<comment>.*?))?[ \t]*$")
REMOTE = re.compile(r"^[\w.-]+/[\w.-]+(?:/[\w./-]+)?@[0-9a-f]{40}$")
DOCKER = re.compile(r"^docker://\S+@sha256:[0-9a-f]{64}$")
VERSION = re.compile(r"^v?\d+(?:\.\d+){0,2}(?:[-+][\w.]+)?$")


def workflow_files(root: Path) -> list[Path]:
    files = [*(root / ".github" / "workflows").glob("*.y*ml")]
    files += [path for path in (root / "actions").rglob("action.y*ml")]
    return sorted(files)


def check_line(line: str) -> str | None:
    match = USES.match(line)
    if match is None:
        return None
    reference = match.group("ref")
    comment = (match.group("comment") or "").strip()
    if reference.startswith("./"):
        return None
    if reference.startswith("docker://"):
        return None if DOCKER.match(reference) else f"imagen sin digest: {reference}"
    if not REMOTE.match(reference):
        return f"no está fijada a un SHA completo: {reference}"
    if not VERSION.match(comment.split(" ")[0] if comment else ""):
        return f"falta la versión en el comentario: {reference}"
    return None


def problems(root: Path) -> list[str]:
    found: list[str] = []
    for path in workflow_files(root):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            problem = check_line(line)
            if problem:
                found.append(f"{path.relative_to(root).as_posix()}:{number}: {problem}")
    return found


def main(argv: list[str]) -> int:
    root = Path(argv[1]) if len(argv) > 1 else Path(".")
    found = problems(root)
    for item in found:
        print(item)
    if found:
        print(f"{len(found)} referencia(s) sin fijar.", file=sys.stderr)
        return 1
    print(f"Todas las referencias de {len(workflow_files(root))} archivo(s) están fijadas.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
