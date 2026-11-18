#!/usr/bin/env python3
"""Renderiza el README del perfil de la organización desde la plantilla del repositorio veriko.

Lee `brand/redes-sociales/github/README.md` del checkout del repositorio veriko, extrae el
README citado (el bloque de líneas `> ` que contiene `# Veriko`), sustituye `<org>`
por la organización y lo escribe en `profile/README.md` del repo `.github`, junto
con las tres imágenes (`hero.png`, `hero-dark.png`, `kpi.png`).

La regla de extracción es la misma que la del candado del repositorio veriko
(`tests/contract/GitHub/ProfileReadmeExampleMatchesSpecTest.php`): el bloque
citado que contiene la línea `# Veriko`.

Uso:
    python3 scripts/sync-profile-readme.py --repo veriko --profile profile --org veriko-mx-labs

Imprime `changed=true` o `changed=false` y, si existe `GITHUB_OUTPUT`, lo escribe
ahí para que el workflow decida si abre el PR. Sale con 1 si falta la plantilla,
una imagen o queda algún `<org>` sin sustituir.
"""

import argparse
import hashlib
import os
import sys

TEMPLATE = "brand/redes-sociales/github/README.md"

IMAGES = [
    ("brand/redes-sociales/linkedin/linkedin-portada-a-veredicto.png", "hero.png"),
    ("brand/redes-sociales/github/hero-dark.png", "hero-dark.png"),
    ("brand/redes-sociales/github/kpi.png", "kpi.png"),
]


def extract_quoted_readme(template: str) -> str:
    runs: list[list[str]] = []
    current: list[str] = []
    for line in template.split("\n"):
        if line.startswith("> ") or line == ">":
            current.append("" if line == ">" else line[2:])
        else:
            if current:
                runs.append(current)
                current = []
    if current:
        runs.append(current)
    for run in runs:
        if "# Veriko" in run:
            return "\n".join(run).strip("\n") + "\n"
    raise SystemExit("La plantilla no trae el README del perfil citado (se busca el bloque que contiene '# Veriko').")


def sha(path: str) -> str | None:
    if not os.path.isfile(path):
        return None
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def main() -> int:
    args = argparse.Namespace()  # replaced below; keeps type checkers quiet
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="Checkout del repositorio veriko (privado).")
    parser.add_argument("--profile", required=True, help="Carpeta profile/ del repo .github.")
    parser.add_argument("--org", required=True, help="Organización, p. ej. veriko-mx-labs.")
    args = parser.parse_args()

    template_path = os.path.join(args.repo, TEMPLATE)
    if not os.path.isfile(template_path):
        print(f"Falta la plantilla: {template_path}", file=sys.stderr)
        return 1
    with open(template_path, encoding="utf-8") as f:
        readme = extract_quoted_readme(f.read())
    readme = readme.replace("<org>", args.org)
    if "<org>" in readme:
        print("Quedó un <org> sin sustituir en el README.", file=sys.stderr)
        return 1
    if "# Veriko" not in readme:
        print("El README renderizado no trae el título '# Veriko'.", file=sys.stderr)
        return 1

    outputs: dict[str, bytes] = {"README.md": readme.encode("utf-8")}
    for source, dest in IMAGES:
        source_path = os.path.join(args.repo, source)
        if not os.path.isfile(source_path):
            print(f"Falta la imagen: {source_path}", file=sys.stderr)
            return 1
        with open(source_path, "rb") as f:
            outputs[dest] = f.read()

    os.makedirs(args.profile, exist_ok=True)
    changed = False
    for name, data in outputs.items():
        dest_path = os.path.join(args.profile, name)
        if sha(dest_path) != hashlib.sha256(data).hexdigest():
            changed = True
        with open(dest_path, "wb") as f:
            f.write(data)
        print(f"{name}: {len(data)} bytes")

    result = f"changed={'true' if changed else 'false'}"
    print(result)
    output_file = os.environ.get("GITHUB_OUTPUT")
    if output_file:
        with open(output_file, "a", encoding="utf-8") as f:
            f.write(result + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
