"""EQ models built from the server's model cache (model.npz plus textures) into Blender objects. Runs under Blender's Python."""
import json
import math
import os

import bpy
import mathutils
import numpy

import bridgeClientLight
import bridgeMeshAccess
import bridgeObjects
import bridgeSurfacing

alphaThreshold = 0.5
# Direct3D 9 reads a sampler with no texture bound as 0, 0, 0, 1.
emptySamplerColor = (0.0, 0.0, 0.0, 1.0)
untinted = 0xFFFFFF


def missingTextureMaterial(textureName, lit):
  """Faces whose texture no linked archive holds: the client's effect samples no texture there, which reads black and opaque, so they
  draw black, lit and fogged as any surface (docs/clientRendering.md, EQG zones)."""
  materialName = f"eq_missing_{textureName}{'_lit' if lit else ''}"
  material = bpy.data.materials.get(materialName)
  if material is None:
    material = bpy.data.materials.new(materialName)
    material.use_nodes = True
    empty = material.node_tree.nodes.new("ShaderNodeRGB")
    empty.outputs["Color"].default_value = emptySamplerColor
    bridgeClientLight.surfaceOutput(material, empty.outputs["Color"], None, "opaque", lit, alphaThreshold)
  return material


def modelMaterial(folder, textureName, alphaMode, tint, lit):
  """One material per texture, alpha mode, tint, and lighting, reused across objects built from the same cache folder, drawn as the
  client draws it (bridgeClientLight). A tint other than white multiplies the texture's color, as the client tints hair (0xRRGGBB).
  A cutout keeps the pixels whose alpha passes the threshold; a blended material is as opaque as its alpha. A lit mesh is lit by its
  file's baked colors and normals."""
  materialName = f"eq_{os.path.basename(folder)}_{textureName}{'' if alphaMode == 'opaque' else f'_{alphaMode}'}{'' if tint == untinted else f'_{tint:06x}'}{'_lit' if lit else ''}"
  material = bpy.data.materials.get(materialName)
  texturePath = os.path.join(folder, textureName)
  if material is not None and material.node_tree.nodes["eqDiffuse"].image.filepath == texturePath:
    return material
  material = bpy.data.materials.new(materialName)
  material.use_nodes = True
  nodes = material.node_tree.nodes
  diffuse = nodes.new("ShaderNodeTexImage")
  diffuse.name = "eqDiffuse"
  diffuse.image = bpy.data.images.load(texturePath, check_existing=True)
  diffuse.image.colorspace_settings.name = "Non-Color"
  baseColor = diffuse.outputs["Color"]
  if tint != untinted:
    multiply = nodes.new("ShaderNodeMix")
    multiply.data_type = "RGBA"
    multiply.blend_type = "MULTIPLY"
    multiply.inputs["Factor"].default_value = 1.0
    colorInputs = [socket for socket in multiply.inputs if socket.type == "RGBA"]
    material.node_tree.links.new(diffuse.outputs["Color"], colorInputs[0])
    colorInputs[1].default_value = (*(((tint >> shift) & 0xFF) / 255 for shift in (16, 8, 0)), 1.0)
    baseColor = next(socket for socket in multiply.outputs if socket.type == "RGBA")
  bridgeClientLight.surfaceOutput(material, baseColor, diffuse.outputs["Alpha"], alphaMode, lit, alphaThreshold)
  return material


def liquidModelMaterial(folder, textureName, liquid, lit):
  """A client liquid material, drawn as the preview draws liquids (bridgeSurfacing.liquidNodes), reused across objects from one cache;
  it keeps its liquid, so where its file lets players through it they swim (bridgeMeshAccess.swumFaces)."""
  materialName = f"eq_{os.path.basename(folder)}_{textureName}_{liquid['liquid']}{'_lit' if lit else ''}"
  material = bpy.data.materials.get(materialName)
  if material is not None and material.node_tree.nodes[bridgeSurfacing.diffuseNodeName].image.filepath == os.path.join(folder, textureName):
    return material
  material = bpy.data.materials.new(materialName)
  material[bridgeMeshAccess.clientLiquidProperty] = liquid["liquid"]
  material.use_nodes = True
  paths = {key: os.path.join(folder, name) for key, name in liquid["textures"].items()}
  bridgeSurfacing.liquidNodes(material, liquid["liquid"], liquid["values"], os.path.join(folder, textureName), paths, lit)
  return material


def dataImage(path):
  image = bpy.data.images.load(path, check_existing=True)
  image.colorspace_settings.name = "Non-Color"
  image.alpha_mode = "CHANNEL_PACKED"
  return image


def terrainMaterial(folder, comboIndex):
  """A terrain tile's ecosystems as the client draws them (docs/clientRendering.md, EQ terrain), its passes summed: per ecosystem, its
  color map times the detail textures weighed by its detail mask, times the vertex tint (doubled by the bump effects, tintScale),
  weighed by the color map's coverage."""
  materialName = f"eq_{os.path.basename(folder)}_terrain{comboIndex}"
  material = bpy.data.materials.get(materialName)
  if material is not None and material.get("eqFolder") == folder:
    return material
  combo = json.loads(open(os.path.join(folder, "terrain.json"), encoding="utf-8").read())["combos"][comboIndex]
  material = bpy.data.materials.new(materialName)
  material["eqFolder"] = folder
  material.use_nodes = True
  tree = material.node_tree
  nodes, links = tree.nodes, tree.links

  def vectorMath(operation, first, second):
    node = nodes.new("ShaderNodeVectorMath")
    node.operation = operation
    links.new(first, node.inputs[0])
    if operation == "SCALE" and isinstance(second, float):
      node.inputs["Scale"].default_value = second
    else:
      links.new(second, node.inputs["Scale" if operation == "SCALE" else 1])
    return node.outputs["Vector"]

  def texture(fileName, coordinates, extension):
    node = nodes.new("ShaderNodeTexImage")
    node.image = dataImage(os.path.join(folder, fileName))
    node.interpolation = "Linear"
    node.extension = extension
    links.new(coordinates, node.inputs["Vector"])
    return node

  atlas = nodes.new("ShaderNodeUVMap")
  atlas.uv_map = "UVMap"
  detail = nodes.new("ShaderNodeUVMap")
  detail.uv_map = bridgeClientLight.detailUVMap
  tint = nodes.new("ShaderNodeAttribute")
  tint.attribute_type = "GEOMETRY"
  tint.attribute_name = bridgeClientLight.tintAttribute
  total = None
  for slot in combo:
    colorMap = texture(slot["colorMap"], atlas.outputs["UV"], "EXTEND")
    weights = nodes.new("ShaderNodeSeparateColor")
    links.new(texture(slot["detailMask"], atlas.outputs["UV"], "EXTEND").outputs["Color"], weights.inputs["Color"])
    details = None
    for channel, layer in zip(("Red", "Green", "Blue"), slot["details"]):
      repeated = vectorMath("SCALE", detail.outputs["UV"], float(layer["repeat"]))
      weighed = vectorMath("SCALE", texture(layer["texture"], repeated, "REPEAT").outputs["Color"], weights.outputs[channel])
      details = weighed if details is None else vectorMath("ADD", details, weighed)
    passColor = vectorMath("MULTIPLY", vectorMath("MULTIPLY", colorMap.outputs["Color"], details), vectorMath("SCALE", tint.outputs["Color"], float(slot["tintScale"])))
    covered = vectorMath("SCALE", passColor, colorMap.outputs["Alpha"])
    total = covered if total is None else vectorMath("ADD", total, covered)
  bridgeClientLight.surfaceOutput(material, total, None, "opaque", True, alphaThreshold)
  return material


def buildModelMesh(folder, meshName):
  data = numpy.load(os.path.join(folder, "model.npz"))
  vertices, triangles, uvs = data["vertices"], data["triangles"], data["uvs"]
  mesh = bpy.data.meshes.new(meshName)
  mesh.vertices.add(len(vertices))
  mesh.vertices.foreach_set("co", vertices.astype(numpy.float32).ravel())
  mesh.loops.add(len(triangles) * 3)
  mesh.loops.foreach_set("vertex_index", triangles.astype(numpy.int32).ravel())
  mesh.polygons.add(len(triangles))
  mesh.polygons.foreach_set("loop_start", numpy.arange(0, len(triangles) * 3, 3, dtype=numpy.int32))
  mesh.polygons.foreach_set("loop_total", numpy.full(len(triangles), 3, dtype=numpy.int32))
  uvLayer = mesh.uv_layers.new(name="UVMap")
  uvLayer.data.foreach_set("uv", uvs[triangles.ravel()].astype(numpy.float32).ravel())
  lit = "colors" in data
  if lit:
    normals = mesh.attributes.new(bridgeClientLight.normalAttribute, "FLOAT_VECTOR", "POINT")
    normals.data.foreach_set("vector", data["normals"].astype(numpy.float32).ravel())
    baked = mesh.color_attributes.new(bridgeClientLight.bakedAttribute, "FLOAT_COLOR", "POINT")
    baked.data.foreach_set("color", (data["colors"].astype(numpy.float32) / 255).ravel())
  if "vertexTakesAllLights" in data:
    takesAll = mesh.attributes.new(bridgeClientLight.takesAllLightsAttribute, "BOOLEAN", "POINT")
    takesAll.data.foreach_set("value", data["vertexTakesAllLights"].astype(bool))
  if "trianglePassable" in data:
    passable = mesh.attributes.new(bridgeMeshAccess.passableAttribute, "BOOLEAN", "FACE")
    passable.data.foreach_set("value", data["trianglePassable"].astype(bool))
  if "heightsWithoutParked" in data:
    mesh[bridgeMeshAccess.heightsWithoutParkedProperty] = [float(value) for value in data["heightsWithoutParked"]]
  if "detailUVs" in data:
    detailLayer = mesh.uv_layers.new(name=bridgeClientLight.detailUVMap)
    detailLayer.data.foreach_set("uv", data["detailUVs"][triangles.ravel()].astype(numpy.float32).ravel())
    tints = mesh.color_attributes.new(bridgeClientLight.tintAttribute, "FLOAT_COLOR", "POINT")
    tints.data.foreach_set("color", (data["vertexTints"].astype(numpy.float32) / 255).ravel())
  missing = {str(name) for name in data["missingTextures"]}
  for textureName, alphaMode, tint, liquid in zip(data["materialTextures"], data["materialAlphaModes"], data["materialTints"], data["materialLiquids"]):
    key = (str(textureName), str(alphaMode), int(tint), lit)
    if key[0].startswith("terrain:"):
      mesh.materials.append(terrainMaterial(folder, int(key[0].split(":")[1])))
    elif key[0] in missing:
      mesh.materials.append(missingTextureMaterial(key[0], lit))
    elif str(liquid):
      mesh.materials.append(liquidModelMaterial(folder, key[0], json.loads(str(liquid)), lit))
    else:
      mesh.materials.append(modelMaterial(folder, *key))
  mesh.polygons.foreach_set("material_index", data["triangleMaterials"].astype(numpy.int32))
  mesh.update()
  mesh.validate()
  return mesh


def modelObject(folder, name, scale, location, rotationDegrees):
  """An object built from a model cache, scaled, turned rotationDegrees about Z (counter-clockwise from above), its model origin at `location`."""
  modelObjectInstance = bpy.data.objects.new(name, buildModelMesh(folder, name))
  modelObjectInstance.scale = (scale, scale, scale)
  modelObjectInstance.rotation_euler = (0, 0, math.radians(rotationDegrees))
  modelObjectInstance.location = location
  return modelObjectInstance


def placeModel(modelFolder, name, location, rotationDegrees, scale, avatarHeight, snapToGround, collection, clientContent):
  """Place a cached EQ model with its origin at `location`, marked as the client content it is (bridgeMeshAccess.clientContentKinds);
  snapToGround instead stands the origin avatarHeight above the surface below, as the client stands a spawn."""
  if clientContent not in bridgeMeshAccess.clientContentKinds:
    raise ValueError(f"clientContent must be one of {list(bridgeMeshAccess.clientContentKinds)}, got {clientContent!r}")
  bridgeObjects.requireNewName(name)
  origin = mathutils.Vector(location)
  ground = None
  if snapToGround:
    hit = bridgeMeshAccess.rayCast(origin + mathutils.Vector((0, 0, 1)), (0, 0, -1), 1000)
    if hit is None:
      raise ValueError(f"No ground below {list(location)} for '{name}'")
    ground = hit[0]
    origin = ground + mathutils.Vector((0, 0, avatarHeight))
  placed = modelObject(modelFolder, name, scale, origin, rotationDegrees)
  placed[bridgeMeshAccess.clientContentProperty] = clientContent
  bridgeObjects.targetCollection(collection).objects.link(placed)
  bpy.context.view_layer.update()
  return bridgeObjects.describeTransform(placed) | {"ground": ground and bridgeObjects.roundVector(ground), "dimensions": bridgeObjects.roundVector(placed.dimensions)}


def importedZones():
  """The client zones importZone placed in the scene: each object's name (the zone's short name) and whether it still stands where the
  import placed it (unmoved, unturned, unscaled)."""
  return {"zones": [
    {"name": sceneObject.name, "asImported": sceneObject.matrix_world == mathutils.Matrix.Identity(4)}
    for sceneObject in bpy.context.scene.objects if sceneObject.type == "MESH" and sceneObject.get(bridgeMeshAccess.clientContentProperty) == "zone"
  ]}


commands = {
  "placeModel": (placeModel, True),
  "importedZones": (importedZones, False),
}
