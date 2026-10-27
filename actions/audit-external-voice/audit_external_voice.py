#!/usr/bin/env python3
"""Audita el subconjunto objetivo de la voz que se puede compartir como datos."""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


IGNORED_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "coverage",
    "dist",
    "dist-test",
    "generated",
    "node_modules",
    "venv",
}
CODE_SUFFIXES = {".py", ".js", ".mjs", ".cjs", ".ts", ".tsx"}
FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
LINK_REFERENCE = re.compile(r"^\s{0,3}\[[^\]]+\]:\s+\S+")
JSDOC = re.compile(r"^[ \t]*/\*\*(?!\*)(.*?)\*/", re.DOTALL | re.MULTILINE)
WALL_THRESHOLD = 240


@dataclass(frozen=True)
class SourceText:
    path: Path
    line: int
    kind: str
    text: str


@dataclass(frozen=True)
class Finding:
    source: SourceText
    rule: str
    detail: str


def relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def is_ignored(path: Path, root: Path) -> bool:
    return any(part in IGNORED_DIRS for part in path.relative_to(root).parts)


def markdown_files(root: Path) -> Iterable[Path]:
    for path in root.rglob("*.md"):
        if is_ignored(path, root):
            continue
        if path.name.upper().startswith(("README", "CHANGELOG")):
            yield path


def code_files(root: Path) -> Iterable[Path]:
    for path in root.rglob("*"):
        if path.is_file() and path.suffix.lower() in CODE_SUFFIXES and not is_ignored(path, root):
            yield path


def clean_markdown(text: str, *, changelog: bool) -> str:
    out: list[str] = []
    fence: str | None = None
    skipping_reference_continuation = False
    just_excluded = False

    for line in text.splitlines():
        marker = FENCE.match(line)
        if marker:
            token = marker.group(1)
            if fence is None:
                fence = token[0]
                if out and out[-1] != "":
                    out.append("")
            elif token[0] == fence:
                fence = None
                just_excluded = True
            continue
        if fence is not None:
            continue

        if changelog and LINK_REFERENCE.match(line):
            skipping_reference_continuation = True
            if out and out[-1] != "":
                out.append("")
            just_excluded = True
            continue
        if changelog and skipping_reference_continuation and (line.startswith("  ") or not line.strip()):
            continue
        skipping_reference_continuation = False
        if just_excluded and not line.strip() and out and out[-1] == "":
            continue
        just_excluded = False
        out.append(line)

    return "\n".join(out)


def markdown_sources(root: Path) -> Iterable[SourceText]:
    for path in markdown_files(root):
        raw = path.read_text(encoding="utf-8")
        yield SourceText(
            path=path,
            line=1,
            kind="Markdown",
            text=clean_markdown(raw, changelog=path.name.upper().startswith("CHANGELOG")),
        )


def python_sources(path: Path) -> Iterable[SourceText]:
    raw = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(raw, filename=str(path))
    except SyntaxError:
        return

    nodes = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    for node in ast.walk(tree):
        if not isinstance(node, nodes) or not node.body:
            continue
        first = node.body[0]
        if not isinstance(first, ast.Expr) or not isinstance(first.value, ast.Constant):
            continue
        if not isinstance(first.value.value, str):
            continue
        yield SourceText(
            path=path,
            line=first.lineno,
            kind="docstring",
            text=clean_markdown(first.value.value, changelog=False),
        )


def jsdoc_sources(path: Path) -> Iterable[SourceText]:
    raw = path.read_text(encoding="utf-8")
    for match in JSDOC.finditer(raw):
        cleaned = "\n".join(
            re.sub(r"^\s*\* ?", "", line) for line in match.group(1).splitlines()
        ).strip()
        if cleaned:
            yield SourceText(
                path=path,
                line=raw.count("\n", 0, match.start()) + 1,
                kind="JSDoc",
                text=clean_markdown(cleaned, changelog=False),
            )


def code_sources(root: Path) -> Iterable[SourceText]:
    for path in code_files(root):
        if path.suffix.lower() == ".py":
            yield from python_sources(path)
        else:
            yield from jsdoc_sources(path)


def outside_inline_code(text: str) -> str:
    return " ".join(text.split("`")[::2])


def prepared_text(rule: dict[str, object], text: str) -> str:
    preprocessing = rule.get("preprocesa", "ninguno")
    if preprocessing == "fuera-de-literales":
        return outside_inline_code(text)
    if preprocessing == "espacios":
        return re.sub(r"\s+", " ", text)
    return text


def regex_flags(flags: str) -> int:
    value = 0
    if "i" in flags:
        value |= re.IGNORECASE
    if "m" in flags:
        value |= re.MULTILINE
    if "s" in flags:
        value |= re.DOTALL
    return value


def lexical_findings(source: SourceText, rules: list[dict[str, object]]) -> Iterable[Finding]:
    for rule in rules:
        language = rule.get("idioma")
        if language not in {"es", "ambos"}:
            continue
        pattern = str(rule.get("patron", ""))
        if not pattern:
            continue
        text = prepared_text(rule, source.text)
        if re.search(pattern, text, regex_flags(str(rule.get("flags", "")))):
            yield Finding(source, str(rule.get("id", "léxico")), str(rule.get("dice", "")))


def bullet_groups(text: str) -> list[list[str]]:
    groups: list[list[str]] = []
    current: list[str] = []
    for line in text.splitlines():
        match = re.match(r"^[ \t]*[-*] (.*)$", line)
        if match:
            current.append(match.group(1))
        elif current and line.strip() and re.match(r"^[ \t]", line):
            current[-1] += f" {line.strip()}"
        elif current:
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    return groups


def coherent_list(items: list[str]) -> bool:
    endings = [item.rstrip()[-1:] for item in items]
    if all(char == "." for char in endings):
        return True
    if all(char not in ".;,:" for char in endings):
        return True
    return all(char == ";" for char in endings[:-1]) and endings[-1] == "."


def mechanical_findings(source: SourceText) -> Iterable[Finding]:
    text = source.text
    opens = text.count("(")
    closes = text.count(")")
    if opens != closes:
        yield Finding(source, "parentesis-sin-cerrar", f"{opens} abren, {closes} cierran")
    if re.search(r"[ \t]+\n", text):
        yield Finding(source, "espacio-al-final", "hay espacios al final de línea")
    if "\n\n\n" in text:
        yield Finding(source, "linea-en-blanco-doble", "hay más de una línea en blanco seguida")

    for items in bullet_groups(text):
        if len(items) > 1 and not coherent_list(items):
            with_period = sum(item.rstrip().endswith(".") for item in items)
            yield Finding(
                source,
                "lista-con-puntuacion-mixta",
                f"{with_period} de {len(items)} viñetas terminan en punto",
            )
    if len(text) > WALL_THRESHOLD and not re.search(r"\n\s*\n", text):
        yield Finding(
            source,
            "muro-de-texto",
            f"{len(text)} caracteres sin una línea en blanco",
        )


def load_rules(path: Path) -> list[dict[str, object]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("version") != 1 or not isinstance(data.get("rules"), list):
        raise ValueError("El catálogo de voz no tiene la versión o la lista de reglas esperada.")
    rules = data["rules"]
    for rule in rules:
        re.compile(str(rule.get("patron", "")), regex_flags(str(rule.get("flags", ""))))
    return rules


def audit(root: Path, lexicon: Path) -> list[Finding]:
    rules = load_rules(lexicon)
    findings: list[Finding] = []
    for source in [*markdown_sources(root), *code_sources(root)]:
        findings.extend(lexical_findings(source, rules))
        findings.extend(mechanical_findings(source))
    return findings


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--lexicon", type=Path, required=True)
    args = parser.parse_args()

    root = args.root.resolve()
    findings = audit(root, args.lexicon.resolve())
    if not findings:
        print("Voz externa: léxico y mecánica sin desvíos.")
        return 0

    print(f"Voz externa: {len(findings)} desvíos.", file=sys.stderr)
    for finding in findings:
        source = finding.source
        print(
            f"- {relative(source.path, root)}:{source.line} [{source.kind}] "
            f"{finding.rule}: {finding.detail}",
            file=sys.stderr,
        )
    print(
        "\nEste auditor cubre vocabulario y mecánica. La repetición, los rótulos "
        "coloquiales y el ritmo se revisan a mano.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
