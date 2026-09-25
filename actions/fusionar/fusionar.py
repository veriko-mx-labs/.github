#!/usr/bin/env python3
"""Fusiona un pull request en main por avance rápido, sin reescribir sus commits."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

ORGANIZATION_IDENTITY = ("Veriko", "soporte@veriko.mx")
BASE_BRANCH = "main"
TRAILER = re.compile(r"^\s*co-authored-by:", re.IGNORECASE)
PASSING_CONCLUSIONS = {"success", "skipped", "neutral"}
MAX_COMMITS = 250
POLL_SECONDS = 15


class Failure(Exception):
    """El pull request no cumple una condición para fusionarse."""


class HttpError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"HTTP {status}: {message}")
        self.status = status


Request = Callable[[str, str, "dict[str, Any] | None"], Any]


def github_client(token: str, api: str = "https://api.github.com") -> Request:
    def request(method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(api + path, data=data, method=method)
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                return json.loads(response.read() or b"null")
        except urllib.error.HTTPError as problem:
            detail = problem.read().decode("utf-8", "replace")
            try:
                detail = json.loads(detail).get("message", detail)
            except (ValueError, AttributeError):
                pass
            raise HttpError(problem.code, str(detail)) from None

    return request


@dataclass
class Merger:
    request: Request
    repository: str
    run_id: str = ""
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    required: list[str] = field(default_factory=list)

    def repo(self, path: str) -> str:
        return f"/repos/{self.repository}{path}"

    def pull_request(self, number: int) -> dict[str, Any]:
        pull = self.request("GET", self.repo(f"/pulls/{number}"), None)
        if pull.get("state") != "open":
            raise Failure(f"El pull request #{number} no está abierto.")
        if pull.get("draft"):
            raise Failure(f"El pull request #{number} es un borrador.")
        if (pull.get("base") or {}).get("ref") != BASE_BRANCH:
            raise Failure(f"El pull request #{number} no apunta a {BASE_BRANCH}.")
        head_repo = ((pull.get("head") or {}).get("repo") or {}).get("full_name")
        if head_repo != self.repository:
            raise Failure(f"El pull request #{number} viene de otro repositorio.")
        return pull

    def base_sha(self) -> str:
        ref = self.request("GET", self.repo(f"/git/ref/heads/{BASE_BRANCH}"), None)
        return ref["object"]["sha"]

    def commits(self, base: str, head: str) -> list[dict[str, Any]]:
        comparison = self.request("GET", self.repo(f"/compare/{base}...{head}?per_page=100"), None)
        if comparison.get("behind_by", 0) != 0 or comparison.get("status") != "ahead":
            raise Failure(
                f"{BASE_BRANCH} avanzó desde que se abrió la rama: rebásala en local con la identidad "
                "de la organización y vuelve a empujarla."
            )
        total = int(comparison.get("total_commits", 0))
        if total > MAX_COMMITS:
            raise Failure(f"El pull request trae {total} commits; el máximo es {MAX_COMMITS}.")
        commits = list(comparison.get("commits") or [])
        page = 2
        while len(commits) < total:
            more = self.request("GET", self.repo(f"/compare/{base}...{head}?per_page=100&page={page}"), None)
            batch = more.get("commits") or []
            if not batch:
                break
            commits += batch
            page += 1
        if len(commits) != total:
            raise Failure("No se pudo leer la lista completa de commits.")
        return commits

    @staticmethod
    def check_commits(commits: list[dict[str, Any]]) -> None:
        problems = []
        for item in commits:
            short = item["sha"][:7]
            data = item.get("commit") or {}
            for role in ("author", "committer"):
                person = data.get(role) or {}
                identity = (person.get("name", ""), str(person.get("email", "")).lower())
                if identity != ORGANIZATION_IDENTITY:
                    label = "autor" if role == "author" else "committer"
                    problems.append(f"commit {short}: {label} no permitido: {identity[0]} <{identity[1]}>")
            for number, line in enumerate(str(data.get("message", "")).split("\n"), start=1):
                if TRAILER.match(line):
                    problems.append(f"commit {short}, mensaje, línea {number}: trailer no permitido")
        if problems:
            raise Failure("\n".join(problems))

    def required_checks(self) -> list[str]:
        rules = self.request("GET", self.repo(f"/rules/branches/{BASE_BRANCH}"), None)
        required: list[str] = []
        for rule in rules or []:
            if rule.get("type") != "required_status_checks":
                continue
            for check in (rule.get("parameters") or {}).get("required_status_checks") or []:
                context = check.get("context")
                if context and context not in required:
                    required.append(context)
        return required

    def own_run(self, check: dict[str, Any]) -> bool:
        return bool(self.run_id) and f"/actions/runs/{self.run_id}/" in str(check.get("details_url", ""))

    def check_state(self, sha: str) -> tuple[list[str], list[str]]:
        runs: list[dict[str, Any]] = []
        page = 1
        while True:
            batch = self.request("GET", self.repo(f"/commits/{sha}/check-runs?per_page=100&page={page}"), None)
            runs += batch.get("check_runs") or []
            if len(runs) >= int(batch.get("total_count", 0)) or not batch.get("check_runs"):
                break
            page += 1
        latest: dict[str, dict[str, Any]] = {}
        for check in runs:
            if self.own_run(check):
                continue
            current = latest.get(check["name"])
            if current is None or int(check.get("id", 0)) > int(current.get("id", 0)):
                latest[check["name"]] = check
        if not latest:
            raise Failure("La cabeza del pull request no tiene checks.")
        pending = [name for name, check in latest.items() if check.get("status") != "completed"]
        failed = [
            name
            for name, check in latest.items()
            if check.get("status") == "completed" and check.get("conclusion") not in PASSING_CONCLUSIONS
        ]
        combined = self.request("GET", self.repo(f"/commits/{sha}/status"), None)
        contexts = set(latest)
        for status in combined.get("statuses") or []:
            context = status.get("context", "status")
            contexts.add(context)
            if status.get("state") == "pending":
                pending.append(context)
            elif status.get("state") != "success":
                failed.append(context)
        # Un check requerido que todavía no aparece cuenta como pendiente: su
        # workflow puede no haber arrancado.
        pending += [context for context in self.required if context not in contexts]
        return pending, failed

    def wait_for_checks(self, sha: str, wait: float) -> None:
        self.required = self.required_checks()
        deadline = self.clock() + wait
        while True:
            pending, failed = self.check_state(sha)
            if failed:
                raise Failure("Checks que no pasaron: " + ", ".join(sorted(set(failed))))
            if not pending:
                return
            if self.clock() >= deadline:
                raise Failure("Checks pendientes: " + ", ".join(sorted(set(pending))))
            self.sleep(POLL_SECONDS)

    def merge(self, number: int, wait: float = 0) -> tuple[str, int]:
        pull = self.pull_request(number)
        head = pull["head"]["sha"]
        commits = self.commits(self.base_sha(), head)
        self.check_commits(commits)
        self.wait_for_checks(head, wait)

        if self.pull_request(number)["head"]["sha"] != head:
            raise Failure("La rama del pull request cambió durante la comprobación.")

        try:
            self.request("PATCH", self.repo(f"/git/refs/heads/{BASE_BRANCH}"), {"sha": head, "force": False})
        except HttpError as problem:
            raise Failure(f"No se pudo mover {BASE_BRANCH}: {problem}") from None

        if self.base_sha() != head:
            raise Failure(f"{BASE_BRANCH} no quedó en la cabeza del pull request.")
        return head, len(commits)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pr", type=int, required=True, help="Número del pull request.")
    parser.add_argument("--esperar", type=float, default=0, help="Segundos que se esperan checks pendientes.")
    args = parser.parse_args(argv)

    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(encoding="utf-8")

    token = os.environ.get("GH_TOKEN", "")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    if not token or not repository:
        print("::error::Faltan GH_TOKEN o GITHUB_REPOSITORY.")
        return 2

    merger = Merger(github_client(token), repository, os.environ.get("GITHUB_RUN_ID", ""))
    try:
        head, count = merger.merge(args.pr, args.esperar)
    except (Failure, HttpError) as problem:
        for line in str(problem).split("\n"):
            print(f"::error::{line}")
        return 1

    noun = "commit" if count == 1 else "commits"
    message = f"Fusionado por avance rápido: {head[:7]}, {count} {noun}."
    print(message)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(f"### Fusión\n\n{message}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
