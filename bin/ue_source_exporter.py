#!/usr/bin/env python3
"""Crash-resistant, host-side launcher for Unreal source exports.

The Unreal process is deliberately disposable. A failed batch cannot corrupt a
later one, and the host validates every completed GLB before it is resumed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import shutil
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
UNREAL_SCRIPT = ROOT / "unreal" / "ue_source_export.py"
REPORT_NAME = "ue_source_export_manifest.json"


def selected_assets(selection: Path) -> list[str]:
    """Read one Unreal object path per line, retaining stable input order."""
    assets: list[str] = []
    seen: set[str] = set()
    for line_number, line in enumerate(selection.read_text(encoding="utf-8").splitlines(), 1):
        value = line.split("#", 1)[0].strip().rstrip("/")
        if not value:
            continue
        if not value.startswith("/Game/"):
            raise ValueError("{}:{} must begin with /Game/: {}".format(selection, line_number, value))
        if value not in seen:
            assets.append(value)
            seen.add(value)
    if not assets:
        raise ValueError("selection has no assets")
    return assets


def validate_glb(path: Path, expected_kind: str | None = None) -> str | None:
    """Check the container before placing trust in an interrupted UE export."""
    try:
        data = path.read_bytes()
        if len(data) < 20:
            return "file is shorter than the GLB header"
        magic, version, declared_size = struct.unpack_from("<4sII", data)
        if magic != b"glTF" or version != 2:
            return "not a GLB 2.0 file"
        if declared_size != len(data):
            return "GLB declared size differs from file size"
        json_size, json_type = struct.unpack_from("<I4s", data, 12)
        if json_type != b"JSON" or 20 + json_size > len(data):
            return "invalid JSON chunk"
        document = json.loads(data[20:20 + json_size])
        if not document.get("meshes"):
            return "GLB has no mesh"
        if expected_kind == "skeletal_mesh" and not document.get("skins"):
            return "skeletal mesh GLB has no skin"
        if expected_kind == "animation_sequence":
            if not document.get("skins"):
                return "animation GLB has no skin"
            if not document.get("animations"):
                return "animation GLB has no animation"
    except (OSError, ValueError, struct.error, json.JSONDecodeError) as error:
        return str(error)
    return None


def glb_semantic_digest(path: Path) -> str:
    """Hash referenced GLB content while ignoring permitted binary padding."""
    data = path.read_bytes()
    json_size = struct.unpack_from("<I", data, 12)[0]
    document = json.loads(data[20:20 + json_size])
    binary_offset = 20 + json_size + 8
    binary = data[binary_offset:]
    digest = hashlib.sha256()
    digest.update(json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    for view in document.get("bufferViews", []):
        start = view.get("byteOffset", 0)
        digest.update(binary[start:start + view["byteLength"]])
    return digest.hexdigest()


def _read_glb(path: Path) -> tuple[dict[str, Any], bytes]:
    data = path.read_bytes()
    json_size, json_type = struct.unpack_from("<I4s", data, 12)
    if json_type != b"JSON":
        raise ValueError("GLB has no JSON chunk")
    json_start = 20
    json_end = json_start + json_size
    binary_size, binary_type = struct.unpack_from("<I4s", data, json_end)
    if binary_type != b"BIN\0":
        raise ValueError("GLB has no binary chunk")
    binary = data[json_end + 8:json_end + 8 + binary_size]
    return json.loads(data[json_start:json_end]), binary


def _write_glb(path: Path, document: dict[str, Any], binary: bytes) -> None:
    encoded = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    encoded += b" " * ((4 - len(encoded) % 4) % 4)
    binary += b"\0" * ((4 - len(binary) % 4) % 4)
    contents = b"glTF" + struct.pack("<II", 2, 12 + 8 + len(encoded) + 8 + len(binary))
    contents += struct.pack("<I4s", len(encoded), b"JSON") + encoded
    contents += struct.pack("<I4s", len(binary), b"BIN\0") + binary
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(contents)
    temporary.replace(path)


def _texture_index(document: dict[str, Any], uri: str) -> int:
    images = document.setdefault("images", [])
    textures = document.setdefault("textures", [])
    image_index = next((index for index, image in enumerate(images) if image.get("uri") == uri), None)
    if image_index is None:
        image_index = len(images)
        images.append({"uri": uri})
    existing = next((index for index, texture in enumerate(textures) if texture.get("source") == image_index), None)
    if existing is not None:
        return existing
    textures.append({"source": image_index})
    return len(textures) - 1


def _material_candidates(slot: dict[str, Any]) -> set[str]:
    material = str(slot.get("material", ""))
    return {str(slot.get("slot_name", "")).lower(), material.rsplit("/", 1)[-1].split(".", 1)[0].lower()}


def bind_asset_textures(output: Path, entry: dict[str, Any]) -> dict[str, Any]:
    """Attach separately exported PNGs to a GLB through standard glTF PBR URIs."""
    glb = output / str(entry.get("output", ""))
    if not glb.is_file():
        return {"asset": entry.get("asset"), "status": "missing_glb"}
    document, binary = _read_glb(glb)
    material_targets = {str(material.get("name", "")).lower(): material for material in document.get("materials", [])}
    changed = 0
    for slot in entry.get("materials", []):
        candidates = _material_candidates(slot)
        targets = [material for name, material in material_targets.items() if name in candidates]
        if not targets and len(document.get("materials", [])) == 1:
            targets = document["materials"]
        if not targets:
            continue
        role_textures: dict[str, int] = {}
        for texture in slot.get("textures", []):
            texture_file = output / str(texture.get("output", ""))
            if not texture_file.is_file():
                continue
            uri = os.path.relpath(texture_file, glb.parent).replace(os.sep, "/")
            role = str(texture.get("role", ""))
            role_textures.setdefault(role, _texture_index(document, uri))
        for material in targets:
            pbr = material.setdefault("pbrMetallicRoughness", {})
            if "albedo" in role_textures:
                pbr["baseColorTexture"] = {"index": role_textures["albedo"]}
            if "packed" in role_textures:
                pbr["metallicRoughnessTexture"] = {"index": role_textures["packed"]}
                material["occlusionTexture"] = {"index": role_textures["packed"]}
            if "normal" in role_textures:
                material["normalTexture"] = {"index": role_textures["normal"]}
            if "emission" in role_textures:
                material["emissiveTexture"] = {"index": role_textures["emission"]}
            if role_textures:
                changed += 1
    if changed:
        _write_glb(glb, document, binary)
    return {"asset": entry.get("asset"), "status": "bound" if changed else "no_matching_material", "materials": changed}


def bind_textures(arguments: argparse.Namespace) -> int:
    output = Path(arguments.output).expanduser().resolve()
    entries: dict[str, dict[str, Any]] = {}
    for report_path in sorted(output.glob("ue_source_export_batch_*.json")):
        for entry in read_json(report_path, {}).get("assets", []):
            if entry.get("status") == "exported":
                entries[entry.get("asset", "")] = entry
    result = {"assets": [bind_asset_textures(output, entry) for entry in entries.values()]}
    result["bound"] = sum(item["status"] == "bound" for item in result["assets"])
    (output / "ue_source_texture_binding.json").write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result["bound"] else 1


def read_json(path: Path, fallback: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fallback


def existing_successes(output: Path, require_textures: bool) -> dict[str, dict[str, Any]]:
    """Return only previous successes whose GLB still passes validation."""
    manifest = read_json(output / REPORT_NAME, {})
    if require_textures and not manifest.get("textures_requested"):
        return {}
    successes: dict[str, dict[str, Any]] = {}
    for entry in manifest.get("assets", []):
        if entry.get("status") != "exported" or not entry.get("output"):
            continue
        if validate_glb(output / entry["output"], entry.get("kind")) is None:
            successes[entry.get("asset", "")] = entry
    return successes


def batches(values: list[str], size: int) -> list[list[str]]:
    return [values[index:index + size] for index in range(0, len(values), size)]


def run_unreal_batch(arguments: argparse.Namespace, output: Path, assets: list[str], index: int) -> tuple[int, dict[str, Any]]:
    report_name = "ue_source_export_batch_{:03d}.json".format(index)
    job_path = output / "ue_source_export_job_{:03d}.json".format(index)
    job = {"schema_version": 2, "mode": "export", "assets": assets, "output_root": str(output), "report_name": report_name, "export_animations": arguments.animations, "export_textures": arguments.textures}
    job_path.write_text(json.dumps(job, indent=2) + "\n", encoding="utf-8")
    command = [str(Path(arguments.unreal_cmd).expanduser()), str(Path(arguments.project).expanduser()), "-run=pythonscript", "-script={}".format(UNREAL_SCRIPT), "-WaveJob={}".format(job_path), "-Unattended", "-NoSplash", "-NoSound"]
    if not arguments.with_rhi:
        command.append("-NullRHI")
    with (output / "ue_source_export_{:03d}.log".format(index)).open("w", encoding="utf-8") as log_file:
        process = subprocess.run(command, text=True, stdout=log_file, stderr=subprocess.STDOUT)
    return process.returncode, read_json(output / report_name, {"assets": [], "textures": []})


def shrink_texture_files(output: Path, maximum: int, relative_paths: list[str] | None = None) -> list[dict[str, Any]]:
    """Cap exported PNGs outside UE, preserving source channel data first."""
    results: list[dict[str, Any]] = []
    if maximum <= 0:
        return results
    candidates = [output / relative for relative in relative_paths] if relative_paths is not None else (sorted((output / "textures").glob("*.png")) if (output / "textures").is_dir() else [])
    for path in candidates:
        if not path.is_file():
            continue
        probe = subprocess.run(["magick", "identify", "-format", "%w %h %[channels]", str(path)], text=True, capture_output=True)
        values = probe.stdout.split()
        entry = {"output": str(path.relative_to(output)), "status": "unchanged"}
        if probe.returncode != 0 or len(values) < 2:
            entry.update(status="unreadable", error=probe.stderr.strip())
        else:
            width, height = int(values[0]), int(values[1])
            entry.update(width=width, height=height, channels=" ".join(values[2:]))
            if max(width, height) > maximum:
                resized = subprocess.run(["magick", str(path), "-resize", "{}x{}>".format(maximum, maximum), str(path)], text=True, capture_output=True)
                if resized.returncode:
                    entry.update(status="resize_failed", error=resized.stderr.strip())
                else:
                    entry["status"] = "resized"
        results.append(entry)
    return results


def make_manifest(requested: list[str], entries: dict[str, dict[str, Any]], textures: list[dict[str, Any]], exit_codes: list[int], batch_size: int, textures_requested: bool, texture_resize: list[dict[str, Any]]) -> dict[str, Any]:
    ordered = [entries.get(asset, {"asset": asset, "status": "not_reported"}) for asset in requested]
    validation: list[dict[str, Any]] = []
    for entry in ordered:
        if entry.get("status") != "exported":
            validation.append({"asset": entry["asset"], "valid": False, "error": entry.get("error", entry.get("status"))})
            continue
        error = validate_glb(Path(entry["output_root"]) / entry["output"], entry.get("kind"))
        validation.append({"asset": entry["asset"], "valid": error is None, "error": error})
    return {"schema_version": 2, "mode": "geometry_without_bake", "requested": len(requested), "batch_size": batch_size, "textures_requested": textures_requested, "textures": textures, "texture_resize": texture_resize, "unreal_exit_codes": exit_codes, "assets": ordered, "validation": validation, "exported": sum(item.get("status") == "exported" for item in ordered), "passed": bool(ordered) and all(item["valid"] for item in validation)}


def run(arguments: argparse.Namespace) -> int:
    output = Path(arguments.output).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    requested = selected_assets(Path(arguments.selection).expanduser())
    entries = existing_successes(output, arguments.textures) if arguments.resume else {}
    pending = [asset for asset in requested if asset not in entries]
    if arguments.dry_run:
        print(json.dumps({"requested": requested, "already_valid": list(entries), "pending": pending}, indent=2))
        return 0
    exit_codes: list[int] = []
    texture_entries: dict[str, dict[str, Any]] = {}
    texture_resize: list[dict[str, Any]] = []
    for index, asset_batch in enumerate(batches(pending, arguments.batch_size), 1):
        exit_code, report = run_unreal_batch(arguments, output, asset_batch, index)
        exit_codes.append(exit_code)
        for entry in report.get("assets", []):
            entry["output_root"] = str(output)
            entries[entry.get("asset", "")] = entry
        for texture in report.get("textures", []):
            texture_entries[texture.get("asset", texture.get("output", ""))] = texture
        if arguments.textures:
            texture_resize.extend(shrink_texture_files(output, arguments.max_texture_size, [str(item.get("output", "")) for item in report.get("textures", [])]))
    for entry in entries.values():
        entry["output_root"] = str(output)
    manifest = make_manifest(requested, entries, list(texture_entries.values()), exit_codes, arguments.batch_size, arguments.textures, texture_resize)
    (output / REPORT_NAME).write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(manifest, indent=2, ensure_ascii=False))
    return 0 if manifest["passed"] else 1


def compare(arguments: argparse.Namespace) -> int:
    directories = [Path(value).expanduser().resolve() for value in arguments.outputs]
    manifests = [{str(path.relative_to(directory)): glb_semantic_digest(path) for path in sorted(directory.rglob("*.glb"))} for directory in directories]
    result = {"outputs": [str(directory) for directory in directories], "glb_counts": [len(manifest) for manifest in manifests], "identical_semantic_content": len(manifests) >= 2 and all(manifest == manifests[0] for manifest in manifests[1:])}
    print(json.dumps(result, indent=2))
    return 0 if result["identical_semantic_content"] else 1


def stage_godot(arguments: argparse.Namespace) -> int:
    """Copy a reviewed external bundle to a relative directory in a Godot project."""
    source_manifest = Path(arguments.manifest).expanduser().resolve()
    manifest = read_json(source_manifest, None)
    if not isinstance(manifest, dict):
        raise ValueError("manifest is not valid JSON: {}".format(source_manifest))
    destination = Path(arguments.destination)
    if destination.is_absolute() or ".." in destination.parts:
        raise ValueError("--destination must be a safe relative project path")
    project = Path(arguments.godot_project).expanduser().resolve()
    if not (project / "project.godot").is_file():
        raise ValueError("--godot-project has no project.godot: {}".format(project))
    source_root = source_manifest.parent
    target_root = project / destination
    copied: list[str] = []
    output_paths = {entry.get("output", "") for entry in manifest.get("assets", []) if entry.get("status") == "exported"}
    output_paths.update(entry.get("output", "") for entry in manifest.get("textures", []) if entry.get("status") in ("exported", "reused"))
    for relative in sorted(path for path in output_paths if path):
        source, target = source_root / relative, target_root / relative
        if not source.is_file():
            raise FileNotFoundError("manifest output is missing: {}".format(source))
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied.append(str(target.relative_to(project)))
    staged = json.loads(json.dumps(manifest))
    for entry in staged.get("assets", []):
        entry.pop("output_root", None)
        entry.pop("traceback", None)
    target_root.mkdir(parents=True, exist_ok=True)
    target_manifest = target_root / REPORT_NAME
    target_manifest.write_text(json.dumps(staged, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"godot_project": str(project), "destination": str(destination), "manifest": str(target_manifest.relative_to(project)), "copied": copied}, indent=2, ensure_ascii=False))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    command = subcommands.add_parser("run", help="Export a selection through disposable NullRHI UE processes.")
    command.add_argument("--unreal-cmd", required=True)
    command.add_argument("--project", required=True)
    command.add_argument("--selection", required=True)
    command.add_argument("--output", required=True)
    command.add_argument("--batch-size", type=int, default=3)
    command.add_argument("--animations", action=argparse.BooleanOptionalAction, default=True)
    command.add_argument("--textures", action=argparse.BooleanOptionalAction, default=False, help="Export source PNGs separately; never enables material baking.")
    command.add_argument("--max-texture-size", type=int, default=2048, help="Host-side cap for exported PNGs; 0 keeps source resolution.")
    command.add_argument("--resume", action=argparse.BooleanOptionalAction, default=True)
    command.add_argument("--dry-run", action="store_true")
    command.add_argument("--with-rhi", action="store_true", help="Only use after a specific asset needs it and has been tested.")
    comparison = subcommands.add_parser("compare", help="Compare semantic GLB content from two or more outputs.")
    comparison.add_argument("outputs", nargs="+", help="Two or more export output directories.")
    stage = subcommands.add_parser("stage-godot", help="Copy a reviewed bundle into a relative Godot project directory.")
    stage.add_argument("--manifest", required=True)
    stage.add_argument("--godot-project", required=True)
    stage.add_argument("--destination", required=True, help="Relative path, e.g. assets_runtime/_staging/castle.")
    binding = subcommands.add_parser("bind-textures", help="Attach exported PNGs to GLBs with standard glTF PBR references.")
    binding.add_argument("--output", required=True)
    arguments = parser.parse_args()
    if arguments.command == "run" and (arguments.batch_size < 1 or arguments.max_texture_size < 0):
        parser.error("--batch-size must be positive and --max-texture-size cannot be negative")
    if arguments.command == "run":
        return run(arguments)
    if arguments.command == "compare":
        return compare(arguments)
    if arguments.command == "stage-godot":
        return stage_godot(arguments)
    return bind_textures(arguments)


if __name__ == "__main__":
    sys.exit(main())
