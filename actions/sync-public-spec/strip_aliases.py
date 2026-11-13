#!/usr/bin/env python3
"""Quita del spec público las propiedades marcadas con `x-alias-of`.

El spec de Veriko declara, durante la transición a la API en inglés, los
nombres anteriores en español como propiedades hermanas del nombre canónico,
marcadas con `x-alias-of: <canónico>`. El portal las oculta; los satélites
tampoco deben generarlas. Este filtro hace lo mismo sobre el archivo
descargado, antes de validarlo y versionarlo.

El recorte es de texto, para no reformatear el resto del documento: se borra
el bloque YAML de cada propiedad cuyo primer nivel de hijos contiene
`x-alias-of:`. Después se comprueba contra el mismo recorte hecho sobre el
documento interpretado; si no coinciden, el archivo no se toca y se sale
con error.
"""

from __future__ import annotations

import copy
import re
import sys
from pathlib import Path
from typing import Any

import yaml

KEY_LINE = re.compile(r"^(?P<indent> *)(?P<key>[^\s#'\"-][^:]*|'[^']*'|\"[^\"]*\"):(?:\s.*)?$")
MARK = "x-alias-of"


def indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def is_blank(line: str) -> bool:
    stripped = line.strip()
    return stripped == "" or stripped.startswith("#")


def strip_text(text: str) -> tuple[str, int]:
    lines = text.splitlines(keepends=True)
    keep = [True] * len(lines)
    removed = 0
    index = 0
    while index < len(lines):
        line = lines[index]
        match = KEY_LINE.match(line.rstrip("\r\n"))
        if not match or is_blank(line):
            index += 1
            continue
        own = len(match.group("indent"))
        end = index + 1
        child_indent = None
        marked = False
        while end < len(lines):
            current = lines[end]
            if is_blank(current):
                end += 1
                continue
            level = indent_of(current)
            if level <= own:
                break
            if child_indent is None:
                child_indent = level
            if level == child_indent and current.strip().startswith(f"{MARK}:"):
                marked = True
            end += 1
        if marked:
            for position in range(index, end):
                keep[position] = False
            removed += 1
            index = end
        else:
            index += 1
    return "".join(line for line, kept in zip(lines, keep) if kept), removed


def strip_tree(value: Any) -> Any:
    if isinstance(value, dict):
        result = {}
        for key, child in value.items():
            if isinstance(child, dict) and MARK in child:
                continue
            result[key] = strip_tree(child)
        return result
    if isinstance(value, list):
        return [strip_tree(child) for child in value]
    return value


def check(document: Any) -> None:
    def walk(value: Any, where: str) -> None:
        if isinstance(value, dict):
            if MARK in value:
                raise ValueError(f"queda `{MARK}` en {where}")
            required = value.get("required")
            properties = value.get("properties")
            if "properties" in value and not isinstance(properties, dict):
                raise ValueError(f"`properties` quedó vacío en {where}")
            if isinstance(required, list) and isinstance(properties, dict):
                missing = [name for name in required if name not in properties]
                if missing:
                    raise ValueError(f"`required` nombra propiedades inexistentes en {where}: {missing}")
            for key, child in value.items():
                walk(child, f"{where}/{key}")
        elif isinstance(value, list):
            for position, child in enumerate(value):
                walk(child, f"{where}/{position}")

    walk(document, "#")


def main() -> int:
    if len(sys.argv) != 2:
        print("uso: strip_aliases.py <openapi.yaml>", file=sys.stderr)
        return 2
    path = Path(sys.argv[1])
    original = path.read_text(encoding="utf-8")
    try:
        parsed = yaml.safe_load(original)
        expected = strip_tree(copy.deepcopy(parsed))
        filtered, removed = strip_text(original)
        if removed == 0:
            check(parsed)
            print("Alias del spec: ninguno marcado con x-alias-of.")
            return 0
        obtained = yaml.safe_load(filtered)
        if obtained != expected:
            raise ValueError("el recorte de texto no coincide con el del documento interpretado")
        check(obtained)
    except (TypeError, ValueError, yaml.YAMLError) as error:
        print(f"No se pudieron quitar los alias del spec: {error}", file=sys.stderr)
        return 1
    path.write_text(filtered, encoding="utf-8")
    print(f"Alias del spec: {removed} propiedad(es) marcadas con x-alias-of quitadas.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
