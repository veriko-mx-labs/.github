import json
import tempfile
import unittest
from pathlib import Path

from audit_external_voice import audit, clean_markdown


RULES = {
    "version": 1,
    "rules": [
        {
            "id": "correo-electronico",
            "idioma": "es",
            "patron": r"\bcorreos?\b(?!\s+electrónic)",
            "flags": "u",
            "preprocesa": "fuera-de-literales",
            "dice": "usa correo electrónico",
        }
    ],
}


class ExternalVoiceAuditTest(unittest.TestCase):
    def test_ignores_fences_inline_literals_and_changelog_references(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "README.md").write_text(
                "Texto breve.\n\n```python\ncorreo = 'x'\n```\n\n`correo` es un campo.\n",
                encoding="utf-8",
            )
            (root / "CHANGELOG.md").write_text(
                "# Cambios\n\n[correo]: https://example.com/correo\n",
                encoding="utf-8",
            )
            lexicon = root / "lexicon.json"
            lexicon.write_text(json.dumps(RULES), encoding="utf-8")

            self.assertEqual([], audit(root, lexicon))

    def test_reads_markdown_docstrings_and_jsdoc(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "README.md").write_text("Escribe el correo aquí.\n", encoding="utf-8")
            (root / "client.py").write_text(
                'def send():\n    """Envía el correo guardado."""\n', encoding="utf-8"
            )
            (root / "client.ts").write_text(
                "/** Devuelve el correo guardado. */\nexport const value = 1;\n", encoding="utf-8"
            )
            lexicon = root / "lexicon.json"
            lexicon.write_text(json.dumps(RULES), encoding="utf-8")

            findings = audit(root, lexicon)
            self.assertEqual(3, len(findings))
            self.assertEqual({"Markdown", "docstring", "JSDoc"}, {f.source.kind for f in findings})

    def test_reports_mechanical_defects(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "README.md").write_text(
                "Texto (sin cierre.  \n\n\n- Primera.\n- Segunda\n",
                encoding="utf-8",
            )
            lexicon = root / "lexicon.json"
            lexicon.write_text(json.dumps(RULES), encoding="utf-8")

            rules = {finding.rule for finding in audit(root, lexicon)}
            self.assertEqual(
                {
                    "parentesis-sin-cerrar",
                    "espacio-al-final",
                    "linea-en-blanco-doble",
                    "lista-con-puntuacion-mixta",
                },
                rules,
            )

    def test_clean_markdown_preserves_prose_outside_exclusions(self):
        cleaned = clean_markdown("Antes.\n```\ncorreo\n```\nDespués.\n", changelog=False)
        self.assertIn("Antes.", cleaned)
        self.assertIn("Después.", cleaned)
        self.assertNotIn("correo", cleaned)

    def test_separate_lists_do_not_mix_their_punctuation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "README.md").write_text(
                "- Enlace uno\n- Enlace dos\n\nTexto.\n\n- Frase una.\n- Frase dos.\n",
                encoding="utf-8",
            )
            lexicon = root / "lexicon.json"
            lexicon.write_text(json.dumps(RULES), encoding="utf-8")

            self.assertEqual([], audit(root, lexicon))


if __name__ == "__main__":
    unittest.main()
