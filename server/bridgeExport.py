"""What an EQG zone export takes from the open scene. The `terrain` collection's meshes become the zone's terrain, merged in world
coordinates with the boundaries (bridgeBoundaries) as triangles without a material; every other rendered mesh becomes a model placed
at its object's transform (copies sharing a mesh and without modifiers share one model), and every collection instance a model of its
collection's meshes. Each triangle carries whether players pass through it (a liquid or cutout material, or an object marked
passable). What is not the zone's own geometry (guides, plot borders, regions, placed client content, anything hidden from renders) is
left out and listed with why. Collecting assumes the scene passed bridgeExportChecks; it writes the meshes to modelArrays.npz and
returns the models, materials, placements, swim volumes and zone lines as regions, and the zone's housing. Runs under Blender's Python."""
import os
import re

import bpy
import numpy

import bridgeBoundaries
import bridgeCaves
import bridgeEnvironment
import bridgeGrading
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
  if bridgeMeshAccess.swimProperty in sceneObject:
    return "a swim volume: written as a .zon region"
  if bridgeMeshAccess.zoneLineProperty in sceneObject:
    return "a zone line: written as a .zon region"
  if bridgeMeshAccess.boundaryProperty in sceneObject:
    return None
  if sceneObject.hide_render:
    return "hidden from renders"
  if sceneObject.type == "EMPTY" and not bridgeEnvironment.isEmitter(sceneObject) and not bridgeMeshAccess.isCollectionInstance(sceneObject):
    return "an empty with nothing to export"
  return None


def classifyObjects():
  """What a zone export ships, each object with its role (terrain, boundary, mesh, instance, light, or emitter); what it leaves out,
  each with why; and what it cannot take, each a failure with why. Nothing is unhidden, retagged, or removed."""
  terrainCollection = bpy.data.collections.get(terrainCollectionName)
  terrainNames = {member.name for member in terrainCollection.all_objects} if terrainCollection is not None else set()
  shipped, excluded, failures = [], [], []
  for sceneObject in bpy.context.scene.objects:
    reason = exclusionReason(sceneObject)
    if reason is not None:
      excluded.append({"object": sceneObject.name, "reason": reason})
    elif bridgeMeshAccess.boundaryProperty in sceneObject:
      # A boundary that is not a mesh is bridgeBoundaries.boundaryErrors' failure, so it is not listed twice.
      if sceneObject.type == "MESH":
        shipped.append((sceneObject, "boundary"))
    elif sceneObject.name in terrainNames:
      if sceneObject.type == "MESH":
        shipped.append((sceneObject, "terrain"))
      else:
        failures.append({"failure": "not a mesh", "object": sceneObject.name, "message": f"'{sceneObject.name}' in the terrain collection is a {sceneObject.type}; terrain takes meshes"})
    elif sceneObject.type == "LIGHT":
      shipped.append((sceneObject, "light"))
    elif bridgeEnvironment.isEmitter(sceneObject):
      shipped.append((sceneObject, "emitter"))
    elif sceneObject.type == "MESH":
      shipped.append((sceneObject, "mesh"))
    elif bridgeMeshAccess.isCollectionInstance(sceneObject):
      shipped.append((sceneObject, "instance"))
    else:
      failures.append({"failure": "not a mesh", "object": sceneObject.name, "message": f"'{sceneObject.name}' is a {sceneObject.type}; zone export takes meshes and collection instances"})
  if terrainCollection is None or not any(member.type == "MESH" for member in terrainCollection.all_objects):
    failures.append({"failure": "no terrain", "message": f"The scene has no '{terrainCollectionName}' collection with meshes; its meshes become the zone's terrain"})
  elif not any(role == "terrain" for _, role in shipped):
    failures.append({"failure": "no terrain", "message": f"Every mesh in the '{terrainCollectionName}' collection is left out: {[entry for entry in excluded if entry['object'] in terrainNames]}"})
  return shipped, sorted(excluded, key=lambda entry: entry["object"]), failures


def exportedObjects():
  """What a zone export ships, each object with its role, and what it leaves out, each with why; a scene export cannot take is refused."""
  shipped, excluded, failures = classifyObjects()
  if failures:
    raise ValueError("; ".join(failure["message"] for failure in failures))
  return shipped, excluded


def decisionsToConfirm(shipped):
  """Shaping passes and surfacing layers that are off on shipped meshes, which leave the zone as if never made (which may be meant); and
  the caves and defined passes whose ground moved since they were made (stale), which export as they stand and a game export refuses."""
  decisions = []
  for sceneObject, role in shipped:
    if sceneObject.type != "MESH":
      continue
    keys = sceneObject.data.shape_keys
    offPasses = [key.name for key in keys.key_blocks if key != keys.reference_key and (key.mute or key.value == 0)] if keys is not None else []
    mutedLayers = [layer["name"] for layer in bridgeMeshAccess.surfaceLayers(sceneObject) if layer["muted"]]
    staleCaves = bridgeCaves.staleCaves(sceneObject)
    staleDefined = [name for entry in bridgeGrading.describeDefinedPasses(sceneObject) if entry["stale"] for name in entry["passes"]]
    if offPasses or mutedLayers or staleCaves or staleDefined:
      decisions.append({"object": sceneObject.name, "passesOff": offPasses, "layersMuted": mutedLayers, "staleCaves": staleCaves, "staleDefinedPasses": staleDefined})
  return decisions


def imagePath(image):
  return os.path.normpath(bpy.path.abspath(image.filepath, library=image.library))


def nodeImagePath(material, nodeName):
  """The file a material's named texture node shows, or None without that node."""
  node = material.node_tree.nodes.get(nodeName)
  return None if node is None else imagePath(node.image)


def materialRecord(material):
  """A createMaterial material's textures (absolute paths) and cutout flag, or a createLiquidMaterial material's textures and shader
  values."""
  liquid = bridgeSurfacing.liquidOf(material)
  record = {"name": material.name, "diffusePath": nodeImagePath(material, bridgeSurfacing.diffuseNodeName), "normalPath": nodeImagePath(material, bridgeSurfacing.normalNodeName), "liquid": None, "cutout": False}
  if liquid is None:
    return record | {"cutout": bool(material[bridgeMeshAccess.cutoutProperty])}
  return record | {"liquid": liquid | {
    "environmentPath": nodeImagePath(material, bridgeSurfacing.environmentNodeName), "secondDiffusePath": nodeImagePath(material, bridgeSurfacing.secondDiffuseNodeName),
  }}


def meshArrays(sceneObject, depsgraph, matrix, materialNames, marked):
  """An object's evaluated mesh as the file keeps it: one vertex per distinct position, corner normal, and texture coordinate (v up
  from the texture's top, as static EQG models store it), transformed by matrix, and each triangle's material name and whether players
  pass through it (its material's, or every triangle when marked)."""
  evaluated = sceneObject.evaluated_get(depsgraph)
  mesh = evaluated.to_mesh()
  try:
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
    "passable": marked | numpy.array([bridgeMeshAccess.isPassableMaterial(slotMaterials[slot]) for slot in triangleSlots], dtype=bool),
  }


def mergeArrays(parts):
  offsets = numpy.cumsum([0] + [len(part["positions"]) for part in parts[:-1]])
  return {
    "positions": numpy.concatenate([part["positions"] for part in parts]), "normals": numpy.concatenate([part["normals"] for part in parts]),
    "uvs": numpy.concatenate([part["uvs"] for part in parts]),
    "triangles": numpy.concatenate([part["triangles"] + offset for part, offset in zip(parts, offsets)]),
    "materials": [name for part in parts for name in part["materials"]], "passable": numpy.concatenate([part["passable"] for part in parts]),
  }


def placementTransform(sceneObject):
  """An object's world transform as a placement: position, the turns about X, Y, and Z in radians (applied X first, as the zone reader
  composes them), and one scale."""
  location, rotation, scale = sceneObject.matrix_world.decompose()
  turns = rotation.to_euler("XYZ")
  return {"position": [float(component) for component in location], "rotation": [turns.z, turns.y, turns.x], "scale": float(scale[0])}


def isOnePositiveScale(sceneObject):
  scale = sceneObject.matrix_world.to_scale()
  return max(scale) - min(scale) <= uniformScaleTolerance * max(abs(component) for component in scale) and min(scale) > 0


def fileStem(text):
  return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def modelKey(sceneObject, role):
  """The model a shipped mesh or collection instance places: copies sharing a mesh and without modifiers share one, unless one is
  marked passable and the other not; the name is the key's second entry, whether it is marked its last."""
  marked = bridgeMeshAccess.passableProperty in sceneObject
  if role == "mesh":
    return ("mesh", sceneObject.data.name, marked) if not sceneObject.modifiers else ("object", sceneObject.name, marked)
  collection = sceneObject.instance_collection
  return ("collection", collection.name, collection.library.filepath if collection.library else "", marked)


def modelStem(key):
  return fileStem(key[1]) + ("_passable" if key[-1] else "")


def collectionMembers(collection):
  """What a collection's model is made of: its rendered objects other than lights and cameras."""
  return [member for member in collection.all_objects if not member.hide_render and member.type not in ("LIGHT", "CAMERA")]


def collectZoneExport(outputFolder, zoneName):
  depsgraph = bpy.context.evaluated_depsgraph_get()
  materials = {}

  def materialName(material):
    if material.name not in materials:
      materials[material.name] = materialRecord(material)
    return material.name

  shipped, _ = exportedObjects()
  regions = bridgeSwim.swimRegions() + bridgeBoundaries.zoneLineRegions()
  terrainParts, models, placements, lights, emitters = [], {}, [], [], []
  for sceneObject, role in shipped:
    if role == "light":
      lights.append(bridgeEnvironment.lightRecord(sceneObject))
      continue
    if role == "emitter":
      emitters.append(bridgeEnvironment.emitterRecord(sceneObject))
      continue
    if role == "terrain":
      terrainParts.append(meshArrays(sceneObject, depsgraph, numpy.array(sceneObject.matrix_world), materialName, bridgeMeshAccess.passableProperty in sceneObject))
      continue
    if role == "boundary":
      terrainParts.append(bridgeBoundaries.boundaryArrays(sceneObject, depsgraph))
      continue
    key = modelKey(sceneObject, role)
    if key not in models and role == "mesh":
      models[key] = {"stem": modelStem(key), "arrays": meshArrays(sceneObject, depsgraph, numpy.identity(4), materialName, key[-1])}
    elif key not in models:
      collection = sceneObject.instance_collection
      offset = numpy.identity(4)
      offset[:3, 3] = -numpy.array(collection.instance_offset)
      parts = [meshArrays(member, depsgraph, offset @ numpy.array(member.matrix_world), materialName, key[-1] or bridgeMeshAccess.passableProperty in member) for member in collectionMembers(collection)]
      models[key] = {"stem": modelStem(key), "arrays": mergeArrays(parts)}
    placements.append({"key": key, "object": sceneObject.name} | placementTransform(sceneObject))
  arrays, modelList = {}, []
  for index, (key, model) in enumerate(models.items()):
    modelList.append({"file": f"obj_{model['stem']}.mod", "materials": model["arrays"]["materials"], "arrays": f"model{index}"})
    model["file"] = modelList[-1]["file"]
    for field in ("positions", "normals", "uvs", "triangles", "passable"):
      arrays[f"model{index}_{field}"] = model["arrays"][field]
  terrain = mergeArrays(terrainParts)
  for field in ("positions", "normals", "uvs", "triangles", "passable"):
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
    "regions": regions, "housing": bridgeHousing.collectHousing(),
  }
