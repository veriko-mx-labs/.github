#!/usr/bin/env python3
"""Valida que un OpenAPI sea el artefacto público antes de versionarlo."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml

FORBIDDEN_EXTENSIONS = {
    "x-visibility",
    "x-permission",
    "x-admin-notes",
    "x-auth",
    "x-integration",
}


def walk(value: Any) -> Iterator[tuple[str, Any]]:
    if isinstance(value, dict):
        for key, child in value.items():
            yield str(key), child
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def resolve_pointer(document: Any, pointer: str) -> Any:
    if not pointer.startswith("#/"):
        raise ValueError(f"$ref externo o inválido: {pointer}")

    current = document
    for raw_part in pointer[2:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if not isinstance(current, dict) or part not in current:
            raise ValueError(f"$ref sin destino: {pointer}")
        current = current[part]
    return current


def verify(path: Path) -> tuple[int, int]:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise TypeError("el YAML no contiene un documento OpenAPI")

    paths = document.get("paths")
    if not isinstance(paths, dict):
        raise TypeError("el documento no declara `paths`")
    if len(paths) <= 50:
        raise ValueError(
            f"el documento sólo declara {len(paths)} rutas; se esperaban más de 50"
        )

    admin_paths = [route for route in paths if str(route).startswith("/admin")]
    if admin_paths:
        raise ValueError(f"el documento contiene {len(admin_paths)} ruta(s) /admin")

    present_extensions = sorted(
        {key for key, _ in walk(document) if key in FORBIDDEN_EXTENSIONS}
    )
    if present_extensions:
        raise ValueError(
            "el documento conserva extensiones internas: "
            + ", ".join(present_extensions)
        )

    references = [value for key, value in walk(document) if key == "$ref"]
    for reference in references:
        if not isinstance(reference, str):
            raise TypeError("el documento contiene un $ref que no es texto")
        resolve_pointer(document, reference)

    return len(paths), len(references)


def main() -> int:
    if len(sys.argv) != 2:
        print("uso: validate_public_spec.py <openapi.yaml>", file=sys.stderr)
        return 2

    try:
        path_count, reference_count = verify(Path(sys.argv[1]))
    except (OSError, TypeError, ValueError, yaml.YAMLError) as error:
        print(f"Spec público rechazado: {error}", file=sys.stderr)
        return 1

    print(
        f"Spec público válido: {path_count} rutas, 0 rutas /admin, "
        f"{reference_count} referencias resueltas."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
