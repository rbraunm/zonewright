"""What an EQG zone export takes from the open scene. The `terrain` collection's meshes become the zone's terrain, merged in world
coordinates; every other rendered mesh becomes a model placed at its object's transform (copies sharing a mesh and without modifiers
share one model), and every collection instance a model of its collection's meshes. What is not the zone's own geometry (guides, plot
borders, regions, placed client content, anything hidden from renders) is left out and listed with why. Writes the meshes to
modelArrays.npz and returns the models, materials, placements, the water bodies' swim volumes, and the zone's housing. Runs under
Blender's Python."""
import os
import re

import bpy
import numpy

import bridgeEnvironment
import bridgeHousing
import bridgeMeshAccess
import bridgeSurfacing
import bridgeSwim

terrainCollectionName = "terrain"
modelArraysFileName = "modelArrays.npz"
uniformScaleTolerance = 1e-5
clientContentReasons = {
  "spawn": "a client spawn: the server's data, not zone geometry",
  "door": "a client door: the server's data, not zone geometry",
  "object": "a client object: client models do not export yet",
  "zone": "part of an imported client zone: reference",
  "zoneFile": "part of an imported zone archive: reference",
}


def exclusionReason(sceneObject):
  """Why an object is not part of the exported zone, or None when it is."""
  kind = sceneObject.get(bridgeMeshAccess.clientContentProperty)
  if kind is not None:
    return clientContentReasons[kind]
  if sceneObject.type == "CAMERA":
    return "a camera"
  if bridgeMeshAccess.plotBorderProperty in sceneObject:
    return "a plot border: exported as a door in the housing list"
  if bridgeMeshAccess.guideProperty in sceneObject:
    return "a guide"
  if bridgeMeshAccess.regionIntentProperty in sceneObject:
    return "a region: the plan"
  if bridgeSwim.swimProperty in sceneObject:
    return "a swim volume: written as a .zon region"
  if sceneObject.hide_render:
    return "hidden from renders"
  if sceneObject.type == "EMPTY" and not bridgeEnvironment.isEmitter(sceneObject) and not bridgeMeshAccess.isCollectionInstance(sceneObject):
    return "an empty with nothing to export"
  return None


def exportedObjects():
  """What a zone export ships, each object with its role (terrain, mesh, instance, light, or emitter), and what it leaves out, each
  with why. Nothing is unhidden, retagged, or removed."""
  terrainCollection = bpy.data.collections.get(terrainCollectionName)
  if terrainCollection is None or not any(member.type == "MESH" for member in terrainCollection.all_objects):
    raise ValueError(f"The scene has no '{terrainCollectionName}' collection with meshes; its meshes become the zone's terrain")
  terrainNames = {member.name for member in terrainCollection.all_objects}
  shipped, excluded = [], []
  for sceneObject in bpy.context.scene.objects:
    reason = exclusionReason(sceneObject)
    if reason is not None:
      excluded.append({"object": sceneObject.name, "reason": reason})
    elif sceneObject.name in terrainNames:
      if sceneObject.type != "MESH":
        raise ValueError(f"'{sceneObject.name}' in the terrain collection is a {sceneObject.type}; terrain takes meshes")
      shipped.append((sceneObject, "terrain"))
    elif sceneObject.type == "LIGHT":
      shipped.append((sceneObject, "light"))
    elif bridgeEnvironment.isEmitter(sceneObject):
      shipped.append((sceneObject, "emitter"))
    elif sceneObject.type == "MESH":
      shipped.append((sceneObject, "mesh"))
    elif bridgeMeshAccess.isCollectionInstance(sceneObject):
      shipped.append((sceneObject, "instance"))
    else:
      raise ValueError(f"'{sceneObject.name}' is a {sceneObject.type}; zone export takes meshes and collection instances")
  if not any(role == "terrain" for _, role in shipped):
    raise ValueError(f"Every mesh in the '{terrainCollectionName}' collection is left out: {[entry for entry in excluded if entry['object'] in terrainNames]}")
  return shipped, sorted(excluded, key=lambda entry: entry["object"])


def decisionsToConfirm(shipped):
  """Shaping passes and surfacing layers that are off on shipped meshes: they leave the zone as if never made, which may be meant."""
  decisions = []
  for sceneObject, role in shipped:
    if sceneObject.type != "MESH":
      continue
    keys = sceneObject.data.shape_keys
    offPasses = [key.name for key in keys.key_blocks if key != keys.reference_key and (key.mute or key.value == 0)] if keys is not None else []
    mutedLayers = [layer["name"] for layer in bridgeMeshAccess.surfaceLayers(sceneObject) if layer["muted"]]
    if offPasses or mutedLayers:
      decisions.append({"object": sceneObject.name, "passesOff": offPasses, "layersMuted": mutedLayers})
  return decisions


def nodeImagePath(material, nodeName):
  """The file a material's named texture node shows, or None without that node."""
  node = material.node_tree.nodes.get(nodeName) if material.node_tree else None
  if node is None or node.image is None:
    return None
  image = node.image
  if image.source != "FILE" or image.packed_file is not None:
    raise ValueError(f"Material '{material.name}' uses image '{image.name}', which is not a file on disk")
  return os.path.normpath(bpy.path.abspath(image.filepath, library=image.library))


def materialRecord(material):
  """A createMaterial material's textures (absolute paths) and cutout flag, or a createLiquidMaterial material's textures and shader
  values; any other material is refused, as only these map onto the client's shaders."""
  diffusePath = nodeImagePath(material, bridgeSurfacing.diffuseNodeName)
  liquid = bridgeSurfacing.liquidOf(material)
  if diffusePath is None or (liquid is None and bridgeSurfacing.cutoutPropertyName not in material):
    raise ValueError(f"Material '{material.name}' was not made by createMaterial or createLiquidMaterial; zone export reads only those")
  record = {"name": material.name, "diffusePath": diffusePath, "normalPath": nodeImagePath(material, bridgeSurfacing.normalNodeName), "liquid": None, "cutout": False}
  if liquid is None:
    return record | {"cutout": bool(material[bridgeSurfacing.cutoutPropertyName])}
  return record | {"liquid": liquid | {
    "environmentPath": nodeImagePath(material, bridgeSurfacing.environmentNodeName), "secondDiffusePath": nodeImagePath(material, bridgeSurfacing.secondDiffuseNodeName),
  }}


def meshArrays(sceneObject, depsgraph, matrix, materialNames):
  """An object's evaluated mesh as the file keeps it: one vertex per distinct position, corner normal, and texture coordinate (v up
  from the texture's top, as static EQG models store it), transformed by matrix, and each triangle's material name."""
  evaluated = sceneObject.evaluated_get(depsgraph)
  mesh = evaluated.to_mesh()
  try:
    if not mesh.uv_layers:
      raise ValueError(f"'{sceneObject.name}' has no texture coordinates")
    mesh.calc_loop_triangles()
    loopCount = len(mesh.loops)
    positions = numpy.empty(len(mesh.vertices) * 3, dtype=numpy.float64)
    mesh.vertices.foreach_get("co", positions)
    loopVertices = numpy.empty(loopCount, dtype=numpy.int64)
    mesh.loops.foreach_get("vertex_index", loopVertices)
    normals = numpy.empty(loopCount * 3, dtype=numpy.float64)
    mesh.corner_normals.foreach_get("vector", normals)
    uvs = numpy.empty(loopCount * 2, dtype=numpy.float64)
    mesh.uv_layers.active.data.foreach_get("uv", uvs)
    triangleLoops = numpy.empty(len(mesh.loop_triangles) * 3, dtype=numpy.int64)
    mesh.loop_triangles.foreach_get("loops", triangleLoops)
    triangleSlots = numpy.empty(len(mesh.loop_triangles), dtype=numpy.int64)
    mesh.loop_triangles.foreach_get("material_index", triangleSlots)
    slotMaterials = [slot.material for slot in evaluated.material_slots]
  finally:
    evaluated.to_mesh_clear()
  if len(triangleLoops) == 0:
    raise ValueError(f"'{sceneObject.name}' has no faces")
  for slot in numpy.unique(triangleSlots):
    if slot >= len(slotMaterials) or slotMaterials[slot] is None:
      raise ValueError(f"'{sceneObject.name}' has faces without a material (slot {slot})")
  positions = positions.reshape(-1, 3) @ matrix[:3, :3].T + matrix[:3, 3]
  normalMatrix = numpy.linalg.inv(matrix[:3, :3]).T
  normals = normals.reshape(-1, 3) @ normalMatrix.T
  normals /= numpy.maximum(numpy.linalg.norm(normals, axis=1, keepdims=True), 1e-12)
  uvs = uvs.reshape(-1, 2) * (1, -1) + (0, 1)
  corners = numpy.concatenate([loopVertices[:, None].astype(numpy.float64), normals, uvs], axis=1)
  unique, firstLoop, loopToVertex = numpy.unique(corners, axis=0, return_index=True, return_inverse=True)
  loopToVertex = loopToVertex.ravel()
  return {
    "positions": positions[loopVertices[firstLoop]], "normals": normals[firstLoop], "uvs": uvs[firstLoop],
    "triangles": loopToVertex[triangleLoops].reshape(-1, 3),
    "materials": [materialNames(slotMaterials[slot]) for slot in triangleSlots],
  }


def mergeArrays(parts):
  offsets = numpy.cumsum([0] + [len(part["positions"]) for part in parts[:-1]])
  return {
    "positions": numpy.concatenate([part["positions"] for part in parts]), "normals": numpy.concatenate([part["normals"] for part in parts]),
    "uvs": numpy.concatenate([part["uvs"] for part in parts]),
    "triangles": numpy.concatenate([part["triangles"] + offset for part, offset in zip(parts, offsets)]),
    "materials": [name for part in parts for name in part["materials"]],
  }


def placementTransform(sceneObject):
  """An object's world transform as a placement: position, the turns about X, Y, and Z in radians (applied X first, as the zone reader
  composes them), and one scale."""
  location, rotation, scale = sceneObject.matrix_world.decompose()
  if max(scale) - min(scale) > uniformScaleTolerance * max(abs(component) for component in scale) or min(scale) <= 0:
    raise ValueError(f"'{sceneObject.name}' is scaled {tuple(round(component, 6) for component in scale)}; a placed model takes one positive scale")
  turns = rotation.to_euler("XYZ")
  return {"position": [float(component) for component in location], "rotation": [turns.z, turns.y, turns.x], "scale": float(scale[0])}


def fileStem(text):
  return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def collectZoneExport(outputFolder, zoneName):
  depsgraph = bpy.context.evaluated_depsgraph_get()
  materials = {}

  def materialName(material):
    if material.name not in materials:
      materials[material.name] = materialRecord(material)
    return material.name

  shipped, excluded = exportedObjects()
  regions, swimDecisions = bridgeSwim.swimRegions()
  terrainParts, models, placements, lights, emitters = [], {}, [], [], []
  for sceneObject, role in shipped:
    if role == "light":
      lights.append(bridgeEnvironment.lightRecord(sceneObject))
      continue
    if role == "emitter":
      emitters.append(bridgeEnvironment.emitterRecord(sceneObject))
      continue
    if role == "terrain":
      terrainParts.append(meshArrays(sceneObject, depsgraph, numpy.array(sceneObject.matrix_world), materialName))
      continue
    if role == "mesh":
      key = ("mesh", sceneObject.data.name) if not sceneObject.modifiers else ("object", sceneObject.name)
      if key not in models:
        models[key] = {"stem": fileStem(key[1]), "arrays": meshArrays(sceneObject, depsgraph, numpy.identity(4), materialName)}
    else:
      collection = sceneObject.instance_collection
      key = ("collection", collection.name, collection.library.filepath if collection.library else "")
      if key not in models:
        offset = numpy.identity(4)
        offset[:3, 3] = -numpy.array(collection.instance_offset)
        members = [member for member in collection.all_objects if not member.hide_render and member.type not in ("LIGHT", "CAMERA")]
        others = [member.name for member in members if member.type != "MESH"]
        if others:
          raise ValueError(f"Collection '{collection.name}' holds {others}; a collection exports as one model of its meshes")
        models[key] = {"stem": fileStem(collection.name), "arrays": mergeArrays([meshArrays(member, depsgraph, offset @ numpy.array(member.matrix_world), materialName) for member in members])}
    placements.append({"key": key, "object": sceneObject.name} | placementTransform(sceneObject))
  stems = [model["stem"] for model in models.values()]
  duplicates = sorted({stem for stem in stems if stems.count(stem) > 1})
  if duplicates or any(not stem for stem in stems):
    raise ValueError(f"Model names must be distinct once lowercased to letters, digits, and underscores: {duplicates or 'an empty name'}")
  arrays, modelList = {}, []
  for index, (key, model) in enumerate(models.items()):
    modelList.append({"file": f"obj_{model['stem']}.mod", "materials": model["arrays"]["materials"], "arrays": f"model{index}"})
    model["file"] = modelList[-1]["file"]
    for field in ("positions", "normals", "uvs", "triangles"):
      arrays[f"model{index}_{field}"] = model["arrays"][field]
  terrain = mergeArrays(terrainParts)
  for field in ("positions", "normals", "uvs", "triangles"):
    arrays[f"terrain_{field}"] = terrain[field]
  os.makedirs(outputFolder, exist_ok=True)
  numpy.savez(os.path.join(outputFolder, modelArraysFileName), **arrays)
  counts = {}
  placementList = []
  for placement in placements:
    model = models[placement["key"]]
    counts[model["stem"]] = counts.get(model["stem"], 0) + 1
    placementList.append({
      "model": model["file"], "name": f"OBJ_{model['stem']}{counts[model['stem']]:02d}", "object": placement["object"],
      "position": placement["position"], "rotation": placement["rotation"], "scale": placement["scale"],
    })
  return {
    "zone": zoneName, "arrays": os.path.join(outputFolder, modelArraysFileName), "terrain": {"file": f"ter_{zoneName}.ter", "materials": terrain["materials"], "arrays": "terrain"},
    "models": modelList, "materials": list(materials.values()), "placements": placementList, "lights": lights, "emitters": emitters,
    "regions": regions, "swim": swimDecisions, "housing": bridgeHousing.collectHousing(), "excluded": excluded, "toConfirm": decisionsToConfirm(shipped),
  }


commands = {
  "collectZoneExport": (collectZoneExport, False),
}
