"""Host-only regression tests; Unreal is not required."""
import importlib.util
import json
from pathlib import Path
import struct
import tempfile
import unittest

MODULE_PATH = Path(__file__).resolve().parents[1] / "bin" / "ue_source_exporter.py"
SPEC = importlib.util.spec_from_file_location("ue_source_exporter", MODULE_PATH)
EXPORTER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(EXPORTER)


def write_glb(path: Path, document: dict, binary: bytes = b"") -> None:
    payload = json.dumps(document, separators=(",", ":")).encode("utf-8")
    payload += b" " * ((4 - len(payload) % 4) % 4)
    binary += b"\0" * ((4 - len(binary) % 4) % 4)
    contents = b"glTF" + struct.pack("<II", 2, 12 + 8 + len(payload) + 8 + len(binary))
    contents += struct.pack("<I4s", len(payload), b"JSON") + payload
    contents += struct.pack("<I4s", len(binary), b"BIN\0") + binary
    path.write_bytes(contents)


class HostTests(unittest.TestCase):
    def test_selection_rejects_non_game_path_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            selection = Path(directory) / "selection.txt"
            selection.write_text("/Game/A\n/Game/A # repeated\n/Game/B\n", encoding="utf-8")
            self.assertEqual(EXPORTER.selected_assets(selection), ["/Game/A", "/Game/B"])
            selection.write_text("/Engine/Bad\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                EXPORTER.selected_assets(selection)

    def test_validator_checks_skeleton_and_animation_contracts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            static = root / "static.glb"
            skeletal = root / "skeletal.glb"
            animated = root / "animated.glb"
            write_glb(static, {"asset": {"version": "2.0"}, "meshes": [{}]})
            write_glb(skeletal, {"asset": {"version": "2.0"}, "meshes": [{}], "skins": [{}]})
            write_glb(animated, {"asset": {"version": "2.0"}, "meshes": [{}], "skins": [{}], "animations": [{}]})
            self.assertIsNone(EXPORTER.validate_glb(static, "static_mesh"))
            self.assertEqual(EXPORTER.validate_glb(static, "skeletal_mesh"), "skeletal mesh GLB has no skin")
            self.assertEqual(EXPORTER.validate_glb(skeletal, "animation_sequence"), "animation GLB has no animation")
            self.assertIsNone(EXPORTER.validate_glb(animated, "animation_sequence"))

    def test_resume_uses_only_a_valid_existing_glb(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "Mesh.glb"
            write_glb(output, {"asset": {"version": "2.0"}, "meshes": [{}]})
            manifest = {"assets": [{"asset": "/Game/Mesh", "status": "exported", "kind": "static_mesh", "output": "Mesh.glb"}]}
            (root / EXPORTER.REPORT_NAME).write_text(json.dumps(manifest), encoding="utf-8")
            self.assertIn("/Game/Mesh", EXPORTER.existing_successes(root, False))
            self.assertNotIn("/Game/Mesh", EXPORTER.existing_successes(root, True))
            output.write_bytes(b"broken")
            self.assertNotIn("/Game/Mesh", EXPORTER.existing_successes(root, False))


if __name__ == "__main__":
    unittest.main()
