#!/usr/bin/env python3
"""Comprueba la autoría de los commits de un pull request y su contenido."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ORGANIZATION_IDENTITY = ("Veriko", "soporte@veriko.mx")
WEB_COMMITTER = ("GitHub", "noreply@github.com")
BOT_EMAIL_SUFFIX = "[bot]@users.noreply.github.com"
CONFIG_VARIABLE = "AUTORIA_CONFIG"
FULL_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
IDENTITY_HEADER = re.compile(r"^(?P<name>.*) <(?P<email>[^<>]*)> \d+ [+-]\d{4}$")
TRAILER = re.compile(r"^\s*co-authored-by:", re.IGNORECASE)
MAX_FILE_BYTES = 2 * 1024 * 1024
BINARY_PROBE_BYTES = 8000
MAX_REPORTED = 200


class ConfigError(Exception):
    def __init__(self, line: int) -> None:
        super().__init__(line)
        self.line = line


class HistoryError(Exception):
    """El historial o el árbol del pull request no se pueden leer."""


@dataclass(frozen=True)
class Commit:
    sha: str
    author: tuple[str, str]
    committer: tuple[str, str]
    message: str

    @property
    def short(self) -> str:
        return self.sha[:7]


@dataclass
class Outcome:
    exit_code: int
    lines: list[str] = field(default_factory=list)
    commits: int = 0
    files: int = 0
    findings: int = 0


def escape_data(value: str) -> str:
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def escape_property(value: str) -> str:
    return escape_data(value).replace(":", "%3A").replace(",", "%2C")


def error(message: str, file: str | None = None, line: int | None = None) -> str:
    properties = ""
    if file is not None:
        properties = f" file={escape_property(file)}"
        if line is not None:
            properties += f",line={line}"
    return f"::error{properties}::{escape_data(message)}"


def git(repo: Path, *args: str) -> bytes:
    try:
        return subprocess.run(
            ["git", "-C", str(repo), *args], check=True, capture_output=True
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        raise HistoryError from None


def parse_identity(header: str) -> tuple[str, str]:
    match = IDENTITY_HEADER.match(header)
    if match is None:
        return ("(ilegible)", "")
    return match["name"], match["email"]


def read_commit(repo: Path, sha: str) -> Commit:
    raw = git(repo, "cat-file", "commit", sha).decode("utf-8", errors="replace")
    header, _, message = raw.partition("\n\n")
    author = committer = ""
    for line in header.split("\n"):
        if line.startswith("author ") and not author:
            author = line[len("author "):]
        elif line.startswith("committer ") and not committer:
            committer = line[len("committer "):]
    return Commit(sha, parse_identity(author), parse_identity(committer), message)


def list_commits(repo: Path, base: str, head: str) -> list[Commit]:
    shas = git(repo, "rev-list", "--reverse", f"{base}..{head}").decode("ascii").split()
    return [read_commit(repo, sha) for sha in shas]


def identity_allowed(identity: tuple[str, str], *, committer: bool) -> bool:
    name, email = identity
    email = email.lower()
    if (name, email) == ORGANIZATION_IDENTITY:
        return True
    if name.endswith("[bot]") and email.endswith(BOT_EMAIL_SUFFIX):
        return True
    return committer and (name, email) == WEB_COMMITTER


def trailer_lines(text: str) -> list[int]:
    return [number for number, line in enumerate(text.split("\n"), start=1) if TRAILER.match(line)]


def check_authorship(commits: list[Commit], body: str) -> list[str]:
    found: list[str] = []
    for commit in commits:
        for role, identity in (("autor", commit.author), ("committer", commit.committer)):
            if not identity_allowed(identity, committer=role == "committer"):
                found.append(error(f"commit {commit.short}: {role} no permitido: {identity[0]} <{identity[1]}>"))
        for number in trailer_lines(commit.message):
            found.append(error(f"commit {commit.short}, mensaje, línea {number}: trailer no permitido"))
    for number in trailer_lines(body):
        found.append(error(f"pull request, cuerpo, línea {number}: trailer no permitido"))
    return found


def load_rules(raw: str) -> list[re.Pattern[str]]:
    rules: list[re.Pattern[str]] = []
    for number, line in enumerate(raw.split("\n"), start=1):
        text = line.rstrip("\r").strip()
        if not text or text.startswith("#"):
            continue
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                rule = re.compile(text, re.IGNORECASE)
        except (re.error, RecursionError, OverflowError):
            raise ConfigError(number) from None
        if rule.search(""):
            raise ConfigError(number)
        rules.append(rule)
    return rules


def matching_lines(rules: list[re.Pattern[str]], text: str) -> list[int]:
    return [
        number
        for number, line in enumerate(text.split("\n"), start=1)
        if any(rule.search(line) for rule in rules)
    ]


def check_text(rules: list[re.Pattern[str]], pull_request: dict[str, Any], commits: list[Commit]) -> list[str]:
    found: list[str] = []
    if matching_lines(rules, pull_request.get("title") or ""):
        found.append(error("pull request, título: contenido no permitido"))
    for number in matching_lines(rules, pull_request.get("body") or ""):
        found.append(error(f"pull request, cuerpo, línea {number}: contenido no permitido"))
    for commit in commits:
        for number in matching_lines(rules, commit.message):
            found.append(error(f"commit {commit.short}, mensaje, línea {number}: contenido no permitido"))
    return found


def check_branch(rules: list[re.Pattern[str]], pull_request: dict[str, Any]) -> list[str]:
    branch = (pull_request.get("head") or {}).get("ref") or ""
    return [error("pull request, rama: contenido no permitido")] if matching_lines(rules, branch) else []


def tree_files(repo: Path, sha: str) -> list[tuple[str, str, int]]:
    files: list[tuple[str, str, int]] = []
    for record in git(repo, "ls-tree", "-r", "-z", "--long", sha).split(b"\0"):
        if not record:
            continue
        meta, _, path = record.partition(b"\t")
        _mode, kind, oid, size = meta.decode("ascii").split()
        if kind == "blob":
            files.append((path.decode("utf-8", errors="replace"), oid, int(size)))
    return files


def check_tree(rules: list[re.Pattern[str]], repo: Path, sha: str) -> tuple[list[str], int]:
    found: list[str] = []
    scanned = 0
    for path, oid, size in tree_files(repo, sha):
        if matching_lines(rules, path):
            found.append(error("Contenido no permitido", file=path))
        if size > MAX_FILE_BYTES:
            continue
        data = git(repo, "cat-file", "blob", oid)
        if b"\0" in data[:BINARY_PROBE_BYTES]:
            continue
        scanned += 1
        for number in matching_lines(rules, data.decode("utf-8", errors="replace")):
            found.append(error("Contenido no permitido", file=path, line=number))
    return found, scanned


def opened_by_bot(pull_request: dict[str, Any]) -> bool:
    user = pull_request.get("user") or {}
    return user.get("type") == "Bot" or str(user.get("login", "")).endswith("[bot]")


def run(repo: Path, event: dict[str, Any], config: str) -> Outcome:
    pull_request = event.get("pull_request")
    if not isinstance(pull_request, dict):
        return Outcome(2, [error("Esta comprobación sólo se ejecuta en pull requests.")])

    try:
        rules = load_rules(config)
    except ConfigError as problem:
        return Outcome(2, [error(f"La configuración de la organización no es válida (línea {problem.line}).")])

    base = str((pull_request.get("base") or {}).get("sha", ""))
    head = str((pull_request.get("head") or {}).get("sha", ""))
    if not FULL_SHA.match(base) or not FULL_SHA.match(head):
        return Outcome(2, [error("El evento no trae los commits del pull request.")])

    try:
        commits = list_commits(repo, base, head)
    except HistoryError:
        return Outcome(2, [error("No se pudo leer el historial del pull request.")])

    outcome = Outcome(0, commits=len(commits))
    authorship = check_authorship(commits, pull_request.get("body") or "")
    content: list[str] = []
    if rules:
        if not opened_by_bot(pull_request):
            content += check_text(rules, pull_request, commits)
        content += check_branch(rules, pull_request)
        try:
            tree_findings, outcome.files = check_tree(rules, repo, head)
        except HistoryError:
            return Outcome(2, [error("No se pudo leer el árbol del pull request.")])
        content += tree_findings

    outcome.findings = len(authorship) + len(content)
    shown_authorship = authorship[:MAX_REPORTED]
    shown_content = content[: MAX_REPORTED - len(shown_authorship)]
    if authorship:
        outcome.lines += shown_authorship
    else:
        noun = "commit" if len(commits) == 1 else "commits"
        outcome.lines.append(f"Autoría verificada: {len(commits)} {noun}.")
    if rules:
        outcome.lines += shown_content
    else:
        outcome.lines.append("::notice::Comprobación de contenido omitida: sin configuración de la organización.")
    omitted = outcome.findings - len(shown_authorship) - len(shown_content)
    if omitted:
        outcome.lines.append(error(f"Se omiten {omitted} hallazgos más."))
    outcome.exit_code = 1 if outcome.findings else 0
    return outcome


def write_summary(outcome: Outcome) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as summary:
        summary.write("### Autoría de commits\n\n")
        summary.write(f"- Commits revisados: {outcome.commits}\n")
        summary.write(f"- Archivos revisados: {outcome.files}\n")
        summary.write(f"- Hallazgos: {outcome.findings}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path("."), help="Raíz del repositorio con el historial completo.")
    parser.add_argument("--event", type=Path, default=os.environ.get("GITHUB_EVENT_PATH"), help="JSON del evento.")
    args = parser.parse_args(argv)
    if args.event is None:
        parser.error("falta --event o GITHUB_EVENT_PATH")

    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(encoding="utf-8")

    if os.environ.get("GITHUB_EVENT_NAME") != "pull_request":
        print(error("Esta comprobación sólo se ejecuta en eventos pull_request."))
        return 2

    event = json.loads(Path(args.event).read_text(encoding="utf-8"))
    outcome = run(args.repo, event, os.environ.get(CONFIG_VARIABLE, ""))
    for line in outcome.lines:
        print(line)
    write_summary(outcome)
    return outcome.exit_code


if __name__ == "__main__":
    sys.exit(main())
