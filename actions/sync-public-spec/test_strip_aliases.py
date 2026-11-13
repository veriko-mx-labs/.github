from __future__ import annotations

import tempfile
import textwrap
import unittest
from pathlib import Path

import yaml
from strip_aliases import main, strip_text

SPEC = textwrap.dedent(
    """\
    openapi: 3.1.0
    components:
      schemas:
        ValidationRequest:
          type: object
          required:
            - amount
          properties:
            amount:
              type: number
              description: Importe. También se acepta `monto` (nombre anterior).
            monto:
              x-alias-of: amount
              type: number
              x-invalid-examples:
                - 0
                - -100
            tracking_key:
              type: string
            clave_rastreo:
              type: string
              x-alias-of: tracking_key
              x-translations:
                en:
                  description: Alias of `tracking_key`.
        Scores:
          type: object
          properties:
            confidence_by_field:
              type: object
              properties:
                send_date:
                  type: number
                fecha:
                  x-alias-of: send_date
                  type: number
    """
)


class StripAliasesTest(unittest.TestCase):
    def run_on(self, text: str) -> tuple[int, str]:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "openapi.yaml"
            path.write_text(text, encoding="utf-8", newline="\n")
            import sys

            saved = sys.argv
            sys.argv = ["strip_aliases.py", str(path)]
            try:
                code = main()
            finally:
                sys.argv = saved
            return code, path.read_text(encoding="utf-8")

    def test_removes_every_marked_property_with_its_block(self) -> None:
        code, result = self.run_on(SPEC)
        self.assertEqual(code, 0)
        document = yaml.safe_load(result)
        schemas = document["components"]["schemas"]
        self.assertEqual(
            sorted(schemas["ValidationRequest"]["properties"]), ["amount", "tracking_key"]
        )
        self.assertEqual(
            list(schemas["Scores"]["properties"]["confidence_by_field"]["properties"]), ["send_date"]
        )
        self.assertNotIn("x-alias-of", result)
        self.assertNotIn("Alias of", result)

    def test_keeps_the_rest_of_the_text_byte_for_byte(self) -> None:
        filtered, removed = strip_text(SPEC)
        self.assertEqual(removed, 3)
        kept = [line for line in SPEC.splitlines() if line in filtered.splitlines()]
        self.assertIn("              description: Importe. También se acepta `monto` (nombre anterior).", kept)
        self.assertTrue(filtered.startswith("openapi: 3.1.0\ncomponents:\n"))

    def test_without_marks_the_file_is_untouched(self) -> None:
        text = "openapi: 3.1.0\npaths: {}\n"
        code, result = self.run_on(text)
        self.assertEqual(code, 0)
        self.assertEqual(result, text)

    def test_required_naming_a_removed_alias_is_an_error(self) -> None:
        text = SPEC.replace("            - amount\n", "            - monto\n")
        code, result = self.run_on(text)
        self.assertEqual(code, 1)
        self.assertEqual(result, text)

    def test_emptied_properties_is_an_error(self) -> None:
        text = SPEC.replace("                send_date:\n                  type: number\n", "")
        code, result = self.run_on(text)
        self.assertEqual(code, 1)
        self.assertEqual(result, text)


if __name__ == "__main__":
    unittest.main()
