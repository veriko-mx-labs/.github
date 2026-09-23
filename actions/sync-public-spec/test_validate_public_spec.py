from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml
from validate_public_spec import verify


def public_spec() -> dict:
    paths = {
        f"/resource-{index}": {"get": {"responses": {"200": {"description": "OK"}}}}
        for index in range(51)
    }
    return {
        "openapi": "3.1.0",
        "info": {"title": "Contrato de prueba", "version": "1.0.0"},
        "paths": paths,
        "components": {"schemas": {"Result": {"type": "object"}}},
        "x-copy": {"$ref": "#/components/schemas/Result"},
    }


class PublicSpecValidationTest(unittest.TestCase):
    def verify_document(self, document: dict) -> tuple[int, int]:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "openapi.yaml"
            path.write_text(yaml.safe_dump(document), encoding="utf-8")
            return verify(path)

    def test_accepts_public_spec_with_resolved_reference(self) -> None:
        self.assertEqual(self.verify_document(public_spec()), (51, 1))

    def test_rejects_invalid_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "openapi.yaml"
            path.write_text("paths:\n  [", encoding="utf-8")
            with self.assertRaises(yaml.YAMLError):
                verify(path)

    def test_rejects_admin_path(self) -> None:
        document = public_spec()
        document["paths"]["/admin/users"] = {}
        with self.assertRaisesRegex(ValueError, "ruta.* /admin"):
            self.verify_document(document)

    def test_rejects_each_internal_extension(self) -> None:
        for extension in (
            "x-visibility",
            "x-permission",
            "x-admin-notes",
            "x-auth",
            "x-integration",
        ):
            with self.subTest(extension=extension):
                document = public_spec()
                document["info"][extension] = "internal"
                with self.assertRaisesRegex(ValueError, extension):
                    self.verify_document(document)

    def test_rejects_small_surface(self) -> None:
        document = public_spec()
        document["paths"] = {"/status": {}}
        with self.assertRaisesRegex(ValueError, "más de 50"):
            self.verify_document(document)

    def test_rejects_broken_reference(self) -> None:
        document = public_spec()
        document["x-copy"]["$ref"] = "#/components/schemas/Missing"
        with self.assertRaisesRegex(ValueError, "sin destino"):
            self.verify_document(document)

    def test_rejects_external_reference(self) -> None:
        document = public_spec()
        document["x-copy"]["$ref"] = "other.yaml#/Result"
        with self.assertRaisesRegex(ValueError, "externo o inválido"):
            self.verify_document(document)


if __name__ == "__main__":
    unittest.main()
