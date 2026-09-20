"""Unreal-side worker for UE Source Exporter.

This file never opens the editor UI and never enables material baking. It is
launched by UnrealEditor-Cmd in a short-lived process for each host batch.
"""
import json
import os
import re
import hashlib
import sys
import traceback

import unreal


def _argument(name):
    prefix = "-{}=".format(name)
    for value in sys.argv:
        if value.startswith(prefix):
            return value[len(prefix):].strip('"')
    command_line = unreal.SystemLibrary.get_command_line()
    match = re.search(r"(?:^|\s)-{}=(?:\"([^\"]+)\"|(\S+))".format(re.escape(name)), command_line)
    return (match.group(1) or match.group(2)) if match else ""


def _safe_output_path(asset_path, output_root):
    relative = asset_path.removeprefix("/Game/")
    parts = [re.sub(r"[^a-zA-Z0-9_.-]+", "_", part) for part in relative.split("/")]
    return os.path.join(output_root, *parts) + ".glb"


def _safe_property(value, name, default=None):
    try:
        return value.get_editor_property(name)
    except Exception:
        return default


def _asset_path(value):
    if value is None:
        return ""
    try:
        return unreal.EditorAssetLibrary.get_path_name_for_loaded_asset(value)
    except Exception:
        return str(value)


def _safe_filename(value):
    return re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(value)).strip("._") or "unnamed"


def _texture_output(asset_path):
    stem = _safe_filename(asset_path.rsplit(".", 1)[-1])
    digest = hashlib.sha256(asset_path.encode("utf-8")).hexdigest()[:12]
    return os.path.join("textures", "{}_{}.png".format(stem, digest))


def _texture_role(parameter_name, asset_path):
    value = "{} {}".format(parameter_name, asset_path).lower()
    if any(token in value for token in ("normal", "_n", " n_")):
        return "normal"
    if any(token in value for token in ("orm", "rma", "mra", "rough", "metal", "ao", "occlusion", "mask")):
        return "packed"
    if any(token in value for token in ("emiss", "glow")):
        return "emission"
    if any(token in value for token in ("opacity", "alpha")):
        return "opacity"
    return "albedo"


def _scalar_role(name):
    value = str(name).lower()
    for role in ("roughness", "metallic", "specular", "opacity"):
        if role in value:
            return role
    if "emiss" in value:
        return "emission_strength"
    return ""


def _vector_role(name):
    value = str(name).lower()
    if "emiss" in value:
        return "emission_color"
    if any(token in value for token in ("base", "albedo", "diffuse", "tint", "color")):
        return "albedo_color"
    return ""


def _color_values(value):
    try:
        return [float(value.r), float(value.g), float(value.b), float(value.a)]
    except Exception:
        return []


def _material_pbr(material):
    """Capture named PBR overrides/defaults; unsupported graph logic stays source-only."""
    result = {}
    seen = set()
    current = material
    base_material = None
    while current is not None:
        if _is(current, "Material"):
            base_material = current
        for item in _safe_property(current, "scalar_parameter_values", []) or []:
            info = _safe_property(item, "parameter_info")
            name = str(_safe_property(info, "name", ""))
            role = _scalar_role(name)
            if role and role not in seen:
                seen.add(role)
                result[role] = _safe_property(item, "parameter_value")
        for item in _safe_property(current, "vector_parameter_values", []) or []:
            info = _safe_property(item, "parameter_info")
            name = str(_safe_property(info, "name", ""))
            role = _vector_role(name)
            if role and role not in seen:
                seen.add(role)
                color = _color_values(_safe_property(item, "parameter_value"))
                if color:
                    result[role] = color
        parent = _safe_property(current, "parent")
        if parent is current:
            break
        current = parent
    if base_material is not None:
        library = getattr(unreal, "MaterialEditingLibrary", None)
        if library is not None:
            for names_method, value_method, classifier in (("get_scalar_parameter_names", "get_material_default_scalar_parameter_value", _scalar_role), ("get_vector_parameter_names", "get_material_default_vector_parameter_value", _vector_role)):
                if not hasattr(library, names_method) or not hasattr(library, value_method):
                    continue
                try:
                    names = getattr(library, names_method)(base_material) or []
                except Exception:
                    names = []
                for name in names:
                    role = classifier(name)
                    if not role or role in seen:
                        continue
                    try:
                        value = getattr(library, value_method)(base_material, name)
                    except Exception:
                        continue
                    result[role] = _color_values(value) if classifier is _vector_role else value
                    seen.add(role)
    return result


def _messages(values):
    return {"suggestions": [str(value) for value in values.suggestions], "warnings": [str(value) for value in values.warnings], "errors": [str(value) for value in values.errors]}


def _write_report(path, report):
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as report_file:
        json.dump(report, report_file, indent=2, ensure_ascii=False)
        report_file.write("\n")
    os.replace(temporary, path)


def _is(asset, unreal_class):
    return hasattr(unreal, unreal_class) and isinstance(asset, getattr(unreal, unreal_class))


def _resolve_export_object(asset):
    if _is(asset, "StaticMesh"):
        return "static_mesh", asset
    if _is(asset, "SkeletalMesh"):
        return "skeletal_mesh", asset
    if _is(asset, "AnimSequence"):
        return "animation_sequence", asset
    if _is(asset, "FoliageType"):
        mesh = _safe_property(asset, "mesh")
        return "foliage_type", mesh if _is(mesh, "StaticMesh") else None
    return "unsupported", None


def _iter_material_textures(material):
    """Read texture parameters without compiling a material or rendering it."""
    seen = set()
    current = material
    base_material = None
    while current is not None:
        if _is(current, "Material"):
            base_material = current
        for value in _safe_property(current, "texture_parameter_values", []) or []:
            texture = _safe_property(value, "parameter_value")
            path = _asset_path(texture)
            if texture is not None and path and path not in seen:
                seen.add(path)
                info = _safe_property(value, "parameter_info")
                yield str(_safe_property(info, "name", "")), texture
        parent = _safe_property(current, "parent")
        if parent is current:
            break
        current = parent
    # A MaterialInstance's parent defaults are often unrelated fallback art.
    # If it supplied any override, preserve that set rather than mixing two
    # competing albedo/normal/ORM maps into one Godot material.
    if not seen and base_material is not None:
        library = getattr(unreal, "MaterialEditingLibrary", None)
        if library is not None and hasattr(library, "get_texture_parameter_names"):
            try:
                names = library.get_texture_parameter_names(base_material) or []
            except Exception:
                names = []
            for name in names:
                try:
                    texture = library.get_material_default_texture_parameter_value(base_material, name)
                except Exception:
                    texture = None
                path = _asset_path(texture)
                if texture is not None and path and path not in seen:
                    seen.add(path)
                    yield str(name), texture


def _material_metadata(asset):
    materials = _safe_property(asset, "static_materials")
    if materials is None:
        materials = _safe_property(asset, "materials", [])
    values = []
    for slot in materials or []:
        material = _safe_property(slot, "material_interface") or _safe_property(slot, "material")
        textures = []
        for parameter, texture in _iter_material_textures(material):
            path = _asset_path(texture)
            textures.append({"parameter": parameter, "role": _texture_role(parameter, path), "asset": path, "output": _texture_output(path)})
        values.append({"slot_name": str(_safe_property(slot, "material_slot_name", "")), "material": _asset_path(material), "pbr": _material_pbr(material), "textures": textures})
    return values


def _collect_textures(materials, collected):
    for material in materials:
        for texture in material.get("textures", []):
            path = texture.get("asset", "")
            if path and path not in collected:
                candidate = unreal.load_asset(path)
                if _is(candidate, "Texture2D"):
                    collected[path] = candidate


def _export_textures(textures, output_root):
    """Export raw 2D source art. UE chooses a compatible exporter itself.

    Do not assign TextureExporterPNG: it asserts on unsupported HDR/EXR source
    art and can terminate the whole editor process.
    """
    results = []
    for asset_path, texture in sorted(textures.items()):
        output = os.path.join(output_root, _texture_output(asset_path))
        os.makedirs(os.path.dirname(output), exist_ok=True)
        result = {"asset": asset_path, "output": os.path.relpath(output, output_root), "status": "unsupported"}
        if os.path.isfile(output):
            result["status"] = "reused"
        else:
            task = unreal.AssetExportTask()
            task.object = texture
            task.filename = output
            task.automated = True
            task.prompt = False
            task.replace_identical = True
            try:
                unreal.Exporter.run_asset_export_tasks([task])
                result["status"] = "exported" if os.path.isfile(output) else "unsupported"
            except Exception as error:
                result["status"] = "failed"
                result["error"] = str(error)
        results.append(result)
    return results


def _collision_metadata(asset):
    if not _is(asset, "StaticMesh"):
        return {"kind": "not_static_mesh"}
    aggregate = _safe_property(_safe_property(asset, "body_setup"), "agg_geom")
    if aggregate is None:
        return {"kind": "unknown"}
    return {"kind": "simple_collision", "boxes": len(_safe_property(aggregate, "box_elems", []) or []), "spheres": len(_safe_property(aggregate, "sphere_elems", []) or []), "capsules": len(_safe_property(aggregate, "sphyl_elems", []) or []), "convexes": len(_safe_property(aggregate, "convex_elems", []) or [])}


def _animation_metadata(asset):
    if not _is(asset, "AnimSequence"):
        return {}
    return {"skeleton": _asset_path(_safe_property(asset, "skeleton")), "duration_seconds": _safe_property(asset, "sequence_length"), "frames": _safe_property(asset, "number_of_sampled_keys")}


def _configure_options(export_animations):
    options = unreal.GLTFExportOptions()
    configured = [("export_uniform_scale", 0.01), ("export_source_model", True), ("export_vertex_colors", False), ("export_lightmaps", False), ("export_animation_sequences", export_animations), ("bake_material_inputs", unreal.GLTFMaterialBakeMode.DISABLED), ("export_material_variants", unreal.GLTFMaterialVariantMode.NONE), ("texture_image_format", unreal.GLTFTextureImageFormat.NONE)]
    for property_name, value in configured:
        try:
            options.set_editor_property(property_name, value)
        except Exception as error:
            raise RuntimeError("required exporter option {} is unavailable: {}".format(property_name, error))
    return options


def main():
    job_path = os.path.abspath(_argument("WaveJob"))
    if not os.path.isfile(job_path):
        raise RuntimeError("-WaveJob must name an existing JSON job file")
    with open(job_path, encoding="utf-8") as job_file:
        job = json.load(job_file)
    assets = job.get("assets", [])
    output_root = os.path.abspath(job.get("output_root", ""))
    if not assets or not output_root:
        raise RuntimeError("job requires assets and output_root")
    options = _configure_options(bool(job.get("export_animations", True)))
    os.makedirs(output_root, exist_ok=True)
    report_path = os.path.join(output_root, job.get("report_name", "ue_source_export_batch.json"))
    report = {"schema_version": 2, "mode": "geometry_without_bake", "requested": len(assets), "assets": [], "textures": []}
    collected_textures = {}
    for asset_path in assets:
        entry = {"asset": asset_path, "status": "failed", "output": ""}
        try:
            source_asset = unreal.load_asset(asset_path)
            if source_asset is None:
                raise RuntimeError("asset was not found")
            kind, export_object = _resolve_export_object(source_asset)
            entry["kind"] = kind
            entry["source_asset"] = _asset_path(source_asset)
            entry["materials"] = _material_metadata(export_object) if export_object else []
            _collect_textures(entry["materials"], collected_textures)
            entry["collision"] = _collision_metadata(export_object) if export_object else {"kind": "unavailable"}
            entry["animation"] = _animation_metadata(source_asset)
            if export_object is None:
                entry["status"] = "skipped_unsupported"
                entry["error"] = "FoliageType has no StaticMesh source" if kind == "foliage_type" else "Supported: StaticMesh, SkeletalMesh, AnimSequence, FoliageType with StaticMesh"
            else:
                output_path = _safe_output_path(asset_path, output_root)
                os.makedirs(os.path.dirname(output_path), exist_ok=True)
                messages = unreal.GLTFExporter.export_to_gltf(export_object, output_path, options, set())
                entry["messages"] = _messages(messages)
                entry["output"] = os.path.relpath(output_path, output_root)
                if messages.errors:
                    raise RuntimeError("GLTFExporter reported errors")
                if not os.path.isfile(output_path):
                    raise RuntimeError("GLTFExporter did not create a GLB")
                entry["status"] = "exported"
        except Exception as error:
            entry["error"] = str(error)
            entry["traceback"] = traceback.format_exc()
            unreal.log_error("UE Source Exporter: {}: {}".format(asset_path, error))
        report["assets"].append(entry)
        _write_report(report_path, report)
    if job.get("export_textures"):
        report["textures"] = _export_textures(collected_textures, output_root)
        _write_report(report_path, report)
    report["exported"] = sum(entry["status"] == "exported" for entry in report["assets"])
    report["failed"] = sum(entry["status"] == "failed" for entry in report["assets"])
    _write_report(report_path, report)
    if report["failed"]:
        raise RuntimeError("{} assets failed; see {}".format(report["failed"], report_path))


main()
