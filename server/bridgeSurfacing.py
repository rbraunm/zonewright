"""Materials and UVs within Phase 1 rules: diffuse and normal maps only, alpha-tested cutouts, world-scaled projection; and liquid
materials, the one exception, for the client's water, waterfall, and lava shaders. Runs under Blender's Python."""
import json
import os

import bpy
import numpy

import bridgeCaveData
import bridgeClientLight
import bridgeMeshAccess
from bridgeState import state

projectionMethods = ("planar", "box")
uvLayerName = "UVMap"
# Once a layer maps a transition its own way, the mesh's own UVs are kept here per face corner and the UV map is composed from them
# and the transition mappings, as materials are from the layers; each layer's transition mapping is kept under the prefix, not a
# number where the layer maps nothing of its own. Three-component vectors, so Blender does not take them for UV maps.
baseMappingName = "zonewrightSurfaceBaseUV"
transitionMappingPrefix = "zonewrightTransitionUV:"
# What zone export reads a createMaterial material by.
diffuseNodeName = "zonewrightDiffuse"
normalNodeName = "zonewrightNormal"
blockoutPropertyName = "zonewrightBlockout"
transitionPropertyName = "zonewrightTransition"
environmentNodeName = "zonewrightEnvironment"
environmentLookupNodeName = "zonewrightEnvironmentLookup"
secondDiffuseNodeName = "zonewrightDiffuse1"
diffuseAlphaNodeName = "zonewrightDiffuseAlpha"
# The client's effect time modulo 100, which a preview sets on its scene to draw liquids scrolled as at that moment; unset, it is 0.
liquidTimeProperty = "zonewrightLiquidTime"
# Each liquid's textures (beyond its diffuse) and shader values, as the client's water (Opaque_MaxWater.fx), waterfall
# (Opaque_MaxWaterFall.fx), and lava (Opaque_MaxLava.fx) materials carry them.
liquidTextures = {"water": ("normal", "environment"), "waterfall": (), "lava": ("normal", "secondDiffuse")}
liquidValues = {
  "water": ("fresnelBias", "fresnelPower", "reflectionAmount", "reflectionColor", "waterColor1", "waterColor2", "slides"),
  "waterfall": ("slides",), "lava": ("slides",),
}
colorValues = ("reflectionColor", "waterColor1", "waterColor2")
# Water's scalar values where the effect's formula holds them, with the ranges the client's own water materials use (every .eqg
# material surveyed): fresnel bias 0 to 1, power 1 to 10, reflection 0 to 2.
scalarDomains = {
  "fresnelBias": (lambda value: 0 <= value <= 1, "the share of the reflection seen straight down, 0 to 1 (the client's run 0 to 1)"),
  "fresnelPower": (lambda value: value > 0, "how sharply the reflection grows toward grazing angles, above 0 (the client's run 1 to 10)"),
  "reflectionAmount": (lambda value: value >= 0, "how strongly the environment mirrors, 0 or more (the client's run 0 to 2)"),
}


def loadImage(path, colorSpace):
  if not os.path.isabs(path) or not os.path.isfile(path):
    raise ValueError(f"Texture '{path}' is not an existing absolute path")
  image = bpy.data.images.load(path, check_existing=True)
  image.colorspace_settings.name = colorSpace
  return image


def createMaterial(name, diffuseTexture, normalTexture, cutout, alphaThreshold, blockout):
  if bpy.data.materials.get(name) is not None:
    raise ValueError(f"A material named '{name}' already exists")
  if cutout and not 0 < alphaThreshold < 1:
    raise ValueError(f"alphaThreshold must be in (0, 1), got {alphaThreshold}")
  # Texture bytes stay raw: the preview lights them as the client does (bridgeClientLight).
  diffuseImage = loadImage(diffuseTexture, "Non-Color")
  normalImage = loadImage(normalTexture, "Non-Color") if normalTexture is not None else None
  material = bpy.data.materials.new(name)
  # A material made for later painting is kept when the file is saved, whether or not anything uses it yet.
  material.use_fake_user = True
  material.use_nodes = True
  nodes = material.node_tree.nodes
  links = material.node_tree.links
  diffuse = nodes.new("ShaderNodeTexImage")
  diffuse.name = diffuseNodeName
  diffuse.image = diffuseImage
  diffuse.interpolation = "Linear"
  material[bridgeMeshAccess.cutoutProperty] = bool(cutout)
  material[blockoutPropertyName] = bool(blockout)
  normal = None
  if normalImage is not None:
    normalNode = nodes.new("ShaderNodeTexImage")
    normalNode.name = normalNodeName
    normalNode.image = normalImage
    normalMap = nodes.new("ShaderNodeNormalMap")
    links.new(normalNode.outputs["Color"], normalMap.inputs["Color"])
    normal = normalMap.outputs["Normal"]
  bridgeClientLight.surfaceOutput(material, diffuse.outputs["Color"], diffuse.outputs["Alpha"], "cutout" if cutout else "opaque", False, alphaThreshold, normal)
  return {"material": name, "diffuseTexture": diffuse.image.name, "normalTexture": os.path.basename(normalTexture) if normalTexture else None, "cutout": cutout, "blockout": blockout}


def requireLiquidValues(liquid, values):
  if liquid not in liquidTextures:
    raise ValueError(f"liquid must be one of {list(liquidTextures)}, got '{liquid}'")
  given = {key for key, value in values.items() if value is not None}
  missing = sorted(set(liquidValues[liquid]) - given)
  stray = sorted(given - set(liquidValues[liquid]))
  if missing or stray:
    raise ValueError(f"A {liquid} material takes {list(liquidValues[liquid])}; missing {missing}, not taken {stray}")
  for key in colorValues:
    if key in given and (len(values[key]) != 3 or not all(0 <= component <= 1 for component in values[key])):
      raise ValueError(f"{key} is three numbers from 0 to 1, got {values[key]!r}")
  for key, (holds, meaning) in scalarDomains.items():
    if key in given and not holds(values[key]):
      raise ValueError(f"{key} is {meaning}, got {values[key]!r}")
  if len(values["slides"]) != 4:
    raise ValueError(f"slides is [first x, first y, second x, second y] in texture repeats a second, got {values['slides']!r}")
  return {key: values[key] for key in liquidValues[liquid]}


def imageNode(material, name, path):
  """A liquid's texture node, its color and alpha read as stored: lava's glow shows where its alpha is 0."""
  node = material.node_tree.nodes.new("ShaderNodeTexImage")
  node.name = name
  node.image = loadImage(path, "Non-Color")
  node.image.alpha_mode = "CHANNEL_PACKED"
  node.interpolation = "Linear"
  return node


def createLiquidMaterial(name, liquid, diffuseTexture, normalTexture, environmentTexture, environmentLookup, secondDiffuseTexture, values):
  """A liquid material: textures and shader values as the client's own water, waterfall, or lava materials carry them, drawn as
  liquidNodes describes for a placed object, as water bodies export. A water's environment is a DDS cube map, which the preview looks
  up in its equirectangular image (environmentLookup)."""
  if bpy.data.materials.get(name) is not None:
    raise ValueError(f"A material named '{name}' already exists")
  if (environmentTexture is None) != (environmentLookup is None):
    raise ValueError("An environment cube map comes with its lookup image")
  values = requireLiquidValues(liquid, values)
  textures = {"normal": normalTexture, "environment": environmentTexture, "secondDiffuse": secondDiffuseTexture}
  missing = sorted(key for key in liquidTextures[liquid] if textures[key] is None)
  stray = sorted(key for key, path in textures.items() if path is not None and key not in liquidTextures[liquid])
  if missing or stray:
    raise ValueError(f"A {liquid} material takes a diffuse texture and {list(liquidTextures[liquid]) or 'nothing else'}; missing {missing}, not taken {stray}")
  material = bpy.data.materials.new(name)
  material.use_fake_user = True
  material.use_nodes = True
  material[bridgeMeshAccess.liquidProperty] = json.dumps({"liquid": liquid, "values": values})
  drawn = {key: path for key, path in (textures | {"environment": environmentLookup}).items() if path is not None}
  liquidNodes(material, liquid, values, diffuseTexture, drawn, False, "object")
  # Zone export reads every texture it ships from these nodes, also those the preview does not draw: a water's cube map (drawn from its
  # lookup image) and lava's normal map (which lights only point light 0, left out).
  for key, nodeName in (("normal", normalNodeName), ("environment", environmentNodeName), ("secondDiffuse", secondDiffuseNodeName)):
    if textures[key] is not None and material.node_tree.nodes.get(nodeName) is None:
      imageNode(material, nodeName, textures[key])
  return {"material": name, "liquid": liquid, "values": values}


# Lava's glow on the zone's terrain (RegionLava.fxo ps_1_4 0x3360: lrp with Diffuse1_x2) and on a placed object (SModelLava.fxo ps_1_4
# 0x47a4: Diffuse1 times 1 plus point light 0 by the normal, which the preview leaves out).
lavaGlow = {"terrain": 2.0, "object": 1.0}


def liquidNodes(material, liquid, values, diffusePath, texturePaths, lit, mesh):
  """A liquid's look in the preview, as the client's liquid effects draw it on the given mesh ("terrain" or "object") at the effect
  time the preview sets (0 unless a view asks for another), also used for the client's own liquid materials when a zone is imported
  (then lit by their baked light). Water (RegionWater.fxo / SModelWater.fxo, ps_2_0): no diffuse; its color runs from waterColor1 seen
  from straight above to waterColor2 at grazing angles under a normal map sampled at the texture coordinates and at twice them,
  flattening with distance until flat 300 units off, and is lit by the mesh's own normal like any surface, as the client's vertex light
  is (the ripples choose its color and what it mirrors, not its light); plus its environment cube map, looked up along the view mirrored
  about that normal (texturePaths' environment is its equirectangular lookup image), times fresnel (bias + (1 - bias) times the grazing
  term to fresnelPower), reflectionAmount, and reflectionColor, added unlit before fog. A waterfall (RegionWaterFall.fxo): its
  diffuse's color lit, as see-through as its alpha, each sampled at its own slide. Lava (RegionLava.fxo, SModelLava.fxo, ps_1_4): the
  diffuse's alpha is how much crust shows, lit; the rest glows unlit with the second diffuse, at twice its color on terrain and once on
  an object. Each layer scrolls as scrolledCoordinates says. A liquid a material leaves without its values or textures is drawn without
  what they give."""
  if mesh not in lavaGlow:
    raise ValueError(f"mesh must be one of {list(lavaGlow)}, got {mesh!r}")
  tree = material.node_tree
  slides = values.get("slides", [0.0, 0.0, 0.0, 0.0])
  diffuse = imageNode(material, diffuseNodeName, diffusePath)
  if liquid == "water" and all(key in values for key in ("waterColor1", "waterColor2", "fresnelBias", "fresnelPower")):
    baseColor, added = clientWater(material, values, texturePaths, slides)
    bridgeClientLight.surfaceOutput(material, baseColor, None, "opaque", lit, 0.5, None, added)
    return
  tree.links.new(scrolledCoordinates(tree, 1.0, slides[:2]), diffuse.inputs["Vector"])
  if liquid == "lava":
    baseColor, added = clientLava(material, diffuse, texturePaths, lavaGlow[mesh], slides[2:])
    bridgeClientLight.surfaceOutput(material, baseColor, None, "opaque", lit, 0.5, None, added)
    return
  alpha = diffuse.outputs["Alpha"]
  if liquid == "waterfall":
    alphaNode = imageNode(material, diffuseAlphaNodeName, diffusePath)
    tree.links.new(scrolledCoordinates(tree, 1.0, slides[2:]), alphaNode.inputs["Vector"])
    alpha = alphaNode.outputs["Alpha"]
  bridgeClientLight.surfaceOutput(material, diffuse.outputs["Color"], alpha, "blended" if liquid == "waterfall" else "opaque", lit, 0.5)


def clientLava(material, diffuse, texturePaths, glow, secondSlide):
  """The client's lava as preview nodes: its crust to light (the diffuse times its alpha) and its glow to add (the second diffuse,
  scrolled by secondSlide, times glow and the rest of the alpha), or no glow without a second diffuse."""
  tree = material.node_tree
  crust = tree.nodes.new("ShaderNodeVectorMath")
  crust.operation = "SCALE"
  tree.links.new(diffuse.outputs["Color"], crust.inputs["Vector"])
  tree.links.new(diffuse.outputs["Alpha"], crust.inputs["Scale"])
  if "secondDiffuse" not in texturePaths:
    return crust.outputs["Vector"], None
  second = imageNode(material, secondDiffuseNodeName, texturePaths["secondDiffuse"])
  tree.links.new(scrolledCoordinates(tree, 1.0, secondSlide), second.inputs["Vector"])
  share = tree.nodes.new("ShaderNodeMath")
  share.operation = "MULTIPLY_ADD"
  tree.links.new(diffuse.outputs["Alpha"], share.inputs[0])
  share.inputs[1].default_value = -glow
  share.inputs[2].default_value = glow
  added = tree.nodes.new("ShaderNodeVectorMath")
  added.operation = "SCALE"
  tree.links.new(second.outputs["Color"], added.inputs["Vector"])
  tree.links.new(share.outputs["Value"], added.inputs["Scale"])
  return crust.outputs["Vector"], added.outputs["Vector"]


def scrolledCoordinates(tree, scale, clientOffset):
  """The texture coordinates times scale, moved by clientOffset (texture repeats per unit of effect time, [u, v] with v counted down
  from the texture's top as the client counts it) times the effect time the preview sets (liquidTimeProperty, the time modulo 100 as
  each effect's preshader takes it). Water moves its first sample's coordinates by minus its first slide and its second's, at twice the
  coordinates, by plus its second slide (RegionWater.fxo: mad oT0, r0, 1/256, -c7 and mad oT1, r0, 1/128, +c9); a waterfall moves its
  color's and its alpha's coordinates (RegionWaterFall.fxo) and lava its two diffuses' (RegionLava.fxo) each by plus its slide. A
  pattern moves against its coordinates."""
  coordinates = tree.nodes.new("ShaderNodeTexCoord").outputs["UV"]
  time = tree.nodes.new("ShaderNodeAttribute")
  time.attribute_type = "VIEW_LAYER"
  time.attribute_name = liquidTimeProperty
  scaled = tree.nodes.new("ShaderNodeVectorMath")
  scaled.operation = "SCALE"
  tree.links.new(coordinates, scaled.inputs["Vector"])
  scaled.inputs["Scale"].default_value = scale
  moved = tree.nodes.new("ShaderNodeVectorMath")
  moved.operation = "MULTIPLY_ADD"
  # Blender counts v up from the texture's bottom.
  moved.inputs[0].default_value = (clientOffset[0], -clientOffset[1], 0.0)
  tree.links.new(time.outputs["Fac"], moved.inputs[1])
  tree.links.new(scaled.outputs["Vector"], moved.inputs[2])
  return moved.outputs["Vector"]


def scrollsInPreview(material):
  """Whether a liquid material's nodes take the effect time a preview sets: those made before previews scrolled liquids do not."""
  return any(node.type == "ATTRIBUTE" and node.attribute_name == liquidTimeProperty for node in material.node_tree.nodes)


def mixColors(tree, first, second, factor, blendType="MIX"):
  mix = tree.nodes.new("ShaderNodeMix")
  mix.data_type = "RGBA"
  mix.blend_type = blendType
  colorInputs = [socket for socket in mix.inputs if socket.type == "RGBA"]
  for socket, operand in ((mix.inputs["Factor"], factor), (colorInputs[0], first), (colorInputs[1], second)):
    if isinstance(operand, bpy.types.NodeSocket):
      tree.links.new(operand, socket)
    else:
      socket.default_value = operand
  return next(socket for socket in mix.outputs if socket.type == "RGBA")


# The client's water flattens its ripples from the camera out to this distance.
waterFlatDistance = 300.0
lookupNudge = (1e-5, 1e-5, 0.0)


def clientWater(material, values, texturePaths, slides):
  """The client's DX9 water as preview nodes: its base color to light, chosen by its rippled normal, and its mirrored environment to
  add."""
  tree = material.node_tree
  links = tree.links

  def connect(socket, operand):
    if isinstance(operand, (int, float, tuple)):
      socket.default_value = operand
    else:
      links.new(operand, socket)

  def math(operation, first, second):
    node = tree.nodes.new("ShaderNodeMath")
    node.operation = operation
    connect(node.inputs[0], first)
    connect(node.inputs[1], second)
    return node.outputs["Value"]

  def vector(operation, first, second=None):
    node = tree.nodes.new("ShaderNodeVectorMath")
    node.operation = operation
    connect(node.inputs[0], first)
    if second is not None:
      connect(node.inputs[1], second)
    return node.outputs["Value" if operation == "DOT_PRODUCT" else "Vector"]

  def scaled(operand, factor):
    node = tree.nodes.new("ShaderNodeVectorMath")
    node.operation = "SCALE"
    connect(node.inputs["Vector"], operand)
    connect(node.inputs["Scale"], factor)
    return node.outputs["Vector"]

  geometry = tree.nodes.new("ShaderNodeNewGeometry")
  normal = geometry.outputs["Normal"]
  if "normal" in texturePaths:
    first = imageNode(material, normalNodeName, texturePaths["normal"])
    links.new(scrolledCoordinates(tree, 1.0, [-slides[0], -slides[1]]), first.inputs["Vector"])
    second = imageNode(material, normalNodeName + "Twice", texturePaths["normal"])
    links.new(scrolledCoordinates(tree, 2.0, slides[2:]), second.inputs["Vector"])
    summed = vector("SUBTRACT", vector("ADD", first.outputs["Color"], second.outputs["Color"]), (1.0, 1.0, 1.0))
    distance = tree.nodes.new("ShaderNodeCameraData").outputs["View Distance"]
    near = math("MINIMUM", math("MAXIMUM", math("DIVIDE", math("SUBTRACT", waterFlatDistance, distance), waterFlatDistance), 0.0), 1.0)
    split = tree.nodes.new("ShaderNodeSeparateXYZ")
    links.new(summed, split.inputs["Vector"])
    flattened = tree.nodes.new("ShaderNodeCombineXYZ")
    links.new(split.outputs["X"], flattened.inputs["X"])
    links.new(split.outputs["Y"], flattened.inputs["Y"])
    links.new(math("ADD", split.outputs["Z"], math("MULTIPLY", math("SUBTRACT", 1.0, near), 2.0)), flattened.inputs["Z"])
    encoded = vector("ADD", scaled(vector("NORMALIZE", flattened.outputs["Vector"]), 0.5), (0.5, 0.5, 0.5))
    normalMap = tree.nodes.new("ShaderNodeNormalMap")
    links.new(encoded, normalMap.inputs["Color"])
    normal = normalMap.outputs["Normal"]
  facing = math("MINIMUM", math("MAXIMUM", vector("DOT_PRODUCT", normal, geometry.outputs["Incoming"]), 0.0), 1.0)
  grazing = math("SUBTRACT", 1.0, facing)
  baseColor = mixColors(tree, (*values["waterColor1"], 1.0), (*values["waterColor2"], 1.0), grazing)
  added = None
  if "environment" in texturePaths and "reflectionAmount" in values and "reflectionColor" in values:
    bias = values["fresnelBias"]
    fresnel = math("ADD", bias, math("MULTIPLY", 1 - bias, math("POWER", grazing, values["fresnelPower"])))
    lookup = tree.nodes.new("ShaderNodeTexEnvironment")
    lookup.name = environmentLookupNodeName
    lookup.image = loadImage(texturePaths["environment"], "Non-Color")
    lookup.interpolation = "Linear"
    # The node reads a direction whose x or y is exactly 0 (calm water seen straight down in a map) as its image's first texel; a
    # hundred-thousandth of a unit turns it off that singularity.
    links.new(vector("ADD", vector("REFLECT", scaled(geometry.outputs["Incoming"], -1.0), normal), lookupNudge), lookup.inputs["Vector"])
    mirrored = vector("MULTIPLY", lookup.outputs["Color"], tuple(component * values["reflectionAmount"] for component in values["reflectionColor"]))
    added = scaled(mirrored, fresnel)
  return baseColor, added


def liquidOf(material):
  """A liquid material's liquid and shader values, or None for any other material."""
  if material is None or bridgeMeshAccess.liquidProperty not in material:
    return None
  return json.loads(material[bridgeMeshAccess.liquidProperty])


def environmentLookups():
  """Each water material's environment cube map and the lookup image its preview mirrors (made from the cube map under the tooling
  root, which a work file only points to): [{material, cubeMap, lookup}], absolute paths as this file resolves them."""
  found = []
  for material in bpy.data.materials:
    nodes = material.node_tree.nodes if liquidOf(material) is not None else {}
    if environmentNodeName in nodes and environmentLookupNodeName in nodes:
      found.append({
        "material": material.name, "cubeMap": os.path.normpath(bpy.path.abspath(nodes[environmentNodeName].image.filepath)),
        "lookup": os.path.normpath(bpy.path.abspath(nodes[environmentLookupNodeName].image.filepath)),
      })
  return {"lookups": found}


def restoreEnvironmentLookups(lookups):
  """Point each named water material's preview at its lookup image ({material: absolute path}) and read the image again; the file
  changes only where a path does."""
  for name, path in lookups.items():
    node = bpy.data.materials[name].node_tree.nodes[environmentLookupNodeName]
    if os.path.normcase(os.path.normpath(bpy.path.abspath(node.image.filepath))) == os.path.normcase(os.path.normpath(path)):
      node.image.reload()
      continue
    old = node.image
    node.image = loadImage(path, "Non-Color")
    if old.users == 0:
      bpy.data.images.remove(old)
    state.unsavedChanges = True
  return {"restored": sorted(lookups)}


def assignMaterial(objectName, materialName, selector):
  sceneObject = bridgeMeshAccess.requireMeshObject(objectName)
  if bridgeMeshAccess.surfaceLayersProperty in sceneObject:
    raise ValueError(f"'{objectName}' is surfaced by layers, which set its face materials; paint into a layer with paintSurface instead")
  material = bpy.data.materials.get(materialName)
  if material is None:
    raise ValueError(f"No material named '{materialName}'")
  bridgeCaveData.refuseLiningMapping(sceneObject, selector, "assignMaterial")
  mask = bridgeMeshAccess.evaluateSelector(selector, sceneObject, "faces")
  bridgeMeshAccess.requireSelection(mask, selector, sceneObject, "faces")
  mask = bridgeCaveData.requireSurfaceSelection(sceneObject, selector, mask)
  count = int(mask.sum())
  slotIndex = next((index for index, slot in enumerate(sceneObject.material_slots) if slot.material == material), None)
  if slotIndex is None:
    sceneObject.data.materials.append(material)
    slotIndex = len(sceneObject.material_slots) - 1
  materialIndices = numpy.empty(len(sceneObject.data.polygons), dtype=numpy.int32)
  sceneObject.data.polygons.foreach_get("material_index", materialIndices)
  materialIndices[mask] = slotIndex
  sceneObject.data.polygons.foreach_set("material_index", materialIndices)
  sceneObject.data.update()
  return {"object": objectName, "material": materialName, "faces": count, "slot": slotIndex}


def planarAxes(direction):
  normal = numpy.array(direction, dtype=numpy.float64)
  normal /= numpy.linalg.norm(normal)
  helper = numpy.array([0, 0, 1.0]) if abs(normal[2]) < 0.9 else numpy.array([0, 1.0, 0])
  across = numpy.cross(helper, normal)
  across /= numpy.linalg.norm(across)
  return across, numpy.cross(normal, across)


def projectedUVs(sceneObject, method, worldUnitsPerRepeat, selector, direction):
  """UVs projected in world units onto the corners of the selector's faces: which faces, which corners, and every corner's projection."""
  if method not in projectionMethods:
    raise ValueError(f"method must be one of {list(projectionMethods)}, got '{method}'")
  if worldUnitsPerRepeat <= 0:
    raise ValueError(f"worldUnitsPerRepeat must be positive, got {worldUnitsPerRepeat}")
  if (method == "planar") != (direction is not None):
    raise ValueError("planar projection needs a direction, and only planar takes one")
  mesh = sceneObject.data
  faceMask = bridgeMeshAccess.evaluateSelector(selector, sceneObject, "faces")
  bridgeMeshAccess.requireSelection(faceMask, selector, sceneObject, "faces")
  vertexPositions, _ = bridgeMeshAccess.readVertexArrays(sceneObject)
  _, faceNormals, _ = bridgeMeshAccess.readFaceArrays(sceneObject)
  loopVertices = numpy.empty(len(mesh.loops), dtype=numpy.int64)
  mesh.loops.foreach_get("vertex_index", loopVertices)
  loopTotals = numpy.empty(len(mesh.polygons), dtype=numpy.int64)
  mesh.polygons.foreach_get("loop_total", loopTotals)
  loopFaces = numpy.repeat(numpy.arange(len(mesh.polygons)), loopTotals)
  if method == "planar":
    loopAxes = numpy.broadcast_to(numpy.array(planarAxes(direction)), (len(mesh.loops), 2, 3))
  else:
    boxAxes = numpy.array([planarAxes(numpy.eye(3)[axis]) for axis in range(3)])
    loopAxes = boxAxes[numpy.abs(faceNormals).argmax(axis=1)[loopFaces]]
  points = vertexPositions[loopVertices]
  return faceMask, faceMask[loopFaces], numpy.einsum("lj,laj->la", points, loopAxes) / worldUnitsPerRepeat


commands = {
  "createMaterial": (createMaterial, True),
  "createLiquidMaterial": (createLiquidMaterial, True),
  "assignMaterial": (assignMaterial, True),
  "environmentLookups": (environmentLookups, False),
  "restoreEnvironmentLookups": (restoreEnvironmentLookups, False),
}
