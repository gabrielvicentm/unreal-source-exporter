extends SceneTree

# Runs in a Godot editor process after `stage-godot` copied a reviewed bundle.
# It writes wrappers and materials only inside the selected destination.

func _init() -> void:
	var argument := _read_argument("--bundle-manifest")
	if argument.is_empty():
		push_error("UE Source Exporter: pass --bundle-manifest res://...")
		quit(1)
		return
	var manifest_path := argument if argument.begins_with("res://") else ProjectSettings.globalize_path(argument)
	var content := FileAccess.get_file_as_string(manifest_path)
	var parsed: Variant = JSON.parse_string(content)
	if not (parsed is Dictionary):
		push_error("UE Source Exporter: invalid manifest: %s" % manifest_path)
		quit(1)
		return
	var manifest: Dictionary = parsed as Dictionary
	var bundle_dir := manifest_path.get_base_dir()
	var materials: Dictionary = _save_materials(manifest.get("assets", []), bundle_dir)
	_save_wrappers(manifest.get("assets", []), bundle_dir, materials)
	print("UE Source Exporter: imported %d material(s) from %s" % [materials.size(), manifest_path])
	quit(0)

func _read_argument(name: String) -> String:
	var values := OS.get_cmdline_user_args()
	for index in range(values.size() - 1):
		if values[index] == name:
			return values[index + 1]
	return ""

func _save_materials(assets: Array, bundle_dir: String) -> Dictionary:
	var written: Dictionary = {}
	for asset_value in assets:
		if not (asset_value is Dictionary):
			continue
		var asset: Dictionary = asset_value
		for slot_value in asset.get("materials", []):
			if not (slot_value is Dictionary):
				continue
			var slot: Dictionary = slot_value
			var key := str(slot.get("material", slot.get("slot_name", "material")))
			if written.has(key):
				continue
			var material := StandardMaterial3D.new()
			material.resource_name = str(slot.get("slot_name", "material"))
			_apply_pbr(material, slot, bundle_dir)
			var file_name := "%s_%s.tres" % [_safe_name(material.resource_name), _short_hash(key)]
			var output := "%s/materials/%s" % [bundle_dir, file_name]
			DirAccess.make_dir_recursive_absolute(ProjectSettings.globalize_path(output.get_base_dir()))
			var error := ResourceSaver.save(material, output)
			if error != OK:
				push_warning("UE Source Exporter: could not save %s: %s" % [output, error])
				continue
			written[key] = output
	return written

func _apply_pbr(material: StandardMaterial3D, slot: Dictionary, bundle_dir: String) -> void:
	var pbr: Dictionary = slot.get("pbr", {})
	if pbr.has("roughness"):
		material.roughness = float(pbr["roughness"])
	if pbr.has("metallic"):
		material.metallic = float(pbr["metallic"])
	if pbr.has("albedo_color") and pbr["albedo_color"] is Array and pbr["albedo_color"].size() == 4:
		var color: Array = pbr["albedo_color"]
		material.albedo_color = Color(color[0], color[1], color[2], color[3])
	for texture_value in slot.get("textures", []):
		if not (texture_value is Dictionary):
			continue
		var texture: Dictionary = texture_value
		var path := "%s/%s" % [bundle_dir, str(texture.get("output", ""))]
		var resource: Resource = load(path)
		if not (resource is Texture2D):
			continue
		match str(texture.get("role", "")):
			"albedo": material.albedo_texture = resource
			"normal":
				material.normal_enabled = true
				material.normal_texture = resource
			"emission":
				material.emission_enabled = true
				material.emission_texture = resource
			"opacity": material.albedo_texture = resource
			"packed": _apply_packed_map(material, resource, str(texture.get("parameter", "")))

func _apply_packed_map(material: StandardMaterial3D, texture: Texture2D, parameter: String) -> void:
	var name := parameter.to_lower()
	if "rma" in name or "mra" in name:
		material.roughness_texture = texture
		material.roughness_texture_channel = BaseMaterial3D.TEXTURE_CHANNEL_RED
		material.metallic_texture = texture
		material.metallic_texture_channel = BaseMaterial3D.TEXTURE_CHANNEL_GREEN
		material.ao_enabled = true
		material.ao_texture = texture
		material.ao_texture_channel = BaseMaterial3D.TEXTURE_CHANNEL_BLUE
	else:
		# UE ORM convention: R ambient occlusion, G roughness, B metallic.
		material.ao_enabled = true
		material.ao_texture = texture
		material.ao_texture_channel = BaseMaterial3D.TEXTURE_CHANNEL_RED
		material.roughness_texture = texture
		material.roughness_texture_channel = BaseMaterial3D.TEXTURE_CHANNEL_GREEN
		material.metallic_texture = texture
		material.metallic_texture_channel = BaseMaterial3D.TEXTURE_CHANNEL_BLUE

func _save_wrappers(assets: Array, bundle_dir: String, materials: Dictionary) -> void:
	for asset_value in assets:
		if not (asset_value is Dictionary):
			continue
		var asset: Dictionary = asset_value
		if asset.get("status") != "exported":
			continue
		var glb_path := "%s/%s" % [bundle_dir, str(asset.get("output", ""))]
		var document := GLTFDocument.new()
		var state := GLTFState.new()
		if document.append_from_file(ProjectSettings.globalize_path(glb_path), state) != OK:
			push_warning("UE Source Exporter: could not read %s" % glb_path)
			continue
		var scene: Node = document.generate_scene(state)
		if scene == null:
			continue
		_apply_material_overrides(scene, asset.get("materials", []), materials)
		var packed := PackedScene.new()
		if packed.pack(scene) != OK:
			scene.free()
			continue
		var wrapper := "%s/scenes/%s.tscn" % [bundle_dir, str(asset.get("output", "")).trim_suffix(".glb")]
		DirAccess.make_dir_recursive_absolute(ProjectSettings.globalize_path(wrapper.get_base_dir()))
		ResourceSaver.save(packed, wrapper)
		scene.free()

func _apply_material_overrides(node: Node, slots: Array, materials: Dictionary) -> void:
	if node is MeshInstance3D:
		for surface in node.mesh.get_surface_count():
			var source: Material = node.mesh.surface_get_material(surface)
			var candidate: String = source.resource_name if source != null else ""
			for slot_value in slots:
				if not (slot_value is Dictionary):
					continue
				var slot: Dictionary = slot_value
				if candidate == str(slot.get("slot_name", "")) or candidate == str(slot.get("material", "")).get_file().get_basename():
					var key := str(slot.get("material", slot.get("slot_name", "material")))
					if materials.has(key):
						node.set_surface_override_material(surface, load(materials[key]))
					break
	for child in node.get_children():
		_apply_material_overrides(child, slots, materials)

func _safe_name(value: String) -> String:
	return value.replace("/", "_").replace("\\", "_").replace(" ", "_")

func _short_hash(value: String) -> String:
	return value.sha256_text().left(12)
