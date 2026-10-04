"""Materials and UVs within Phase 1 rules: diffuse and normal maps only, alpha-tested cutouts, world-scaled projection; and liquid
materials, the one exception, for the client's water, waterfall, and lava shaders. Runs under Blender's Python."""
import json
import os

import bpy
import numpy

import bridgeClientLight
import bridgeMeshAccess

projectionMethods = ("planar", "box")
uvLayerName = "UVMap"
# What zone export reads a createMaterial material by.
diffuseNodeName = "zonewrightDiffuse"
normalNodeName = "zonewrightNormal"
cutoutPropertyName = "zonewrightCutout"
environmentNodeName = "zonewrightEnvironment"
secondDiffuseNodeName = "zonewrightDiffuse1"
# A liquid material keeps its liquid and shader values in this property; zone export writes them as the client's shader properties.
liquidPropertyName = "zonewrightLiquid"
# Each liquid's textures (beyond its diffuse) and shader values, as the client's water (Opaque_MaxWater.fx), waterfall
# (Opaque_MaxWaterFall.fx), and lava (Opaque_MaxLava.fx) materials carry them.
liquidTextures = {"water": ("normal", "environment"), "waterfall": (), "lava": ("normal", "secondDiffuse")}
liquidValues = {
  "water": ("fresnelBias", "fresnelPower", "reflectionAmount", "reflectionColor", "waterColor1", "waterColor2", "slides"),
  "waterfall": ("slides",), "lava": ("slides",),
}
colorValues = ("reflectionColor", "waterColor1", "waterColor2")


def loadImage(path, colorSpace):
  if not os.path.isabs(path) or not os.path.isfile(path):
    raise FileNotFoundError(f"Texture '{path}' is not an existing absolute path")
  image = bpy.data.images.load(path, check_existing=True)
  image.colorspace_settings.name = colorSpace
  return image


def createMaterial(name, diffuseTexture, normalTexture, cutout, alphaThreshold):
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
  material[cutoutPropertyName] = bool(cutout)
  normal = None
  if normalImage is not None:
    normalNode = nodes.new("ShaderNodeTexImage")
    normalNode.name = normalNodeName
    normalNode.image = normalImage
    normalMap = nodes.new("ShaderNodeNormalMap")
    links.new(normalNode.outputs["Color"], normalMap.inputs["Color"])
    normal = normalMap.outputs["Normal"]
  bridgeClientLight.surfaceOutput(material, diffuse.outputs["Color"], diffuse.outputs["Alpha"], "cutout" if cutout else "opaque", False, alphaThreshold, normal)
  return {"material": name, "diffuseTexture": diffuse.image.name, "normalTexture": os.path.basename(normalTexture) if normalTexture else None, "cutout": cutout}


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
  if len(values["slides"]) != 4:
    raise ValueError(f"slides is [first x, first y, second x, second y] in texture repeats a second, got {values['slides']!r}")
  return {key: values[key] for key in liquidValues[liquid]}


def imageNode(material, name, path):
  node = material.node_tree.nodes.new("ShaderNodeTexImage")
  node.name = name
  node.image = loadImage(path, "Non-Color")
  node.interpolation = "Linear"
  return node


def createLiquidMaterial(name, liquid, diffuseTexture, normalTexture, environmentTexture, secondDiffuseTexture, values):
  """A liquid material: textures and shader values as the client's own water, waterfall, or lava materials carry them, drawn as
  liquidNodes describes."""
  if bpy.data.materials.get(name) is not None:
    raise ValueError(f"A material named '{name}' already exists")
  values = requireLiquidValues(liquid, values)
  textures = {"normal": normalTexture, "environment": environmentTexture, "secondDiffuse": secondDiffuseTexture}
  missing = sorted(key for key in liquidTextures[liquid] if textures[key] is None)
  stray = sorted(key for key, path in textures.items() if path is not None and key not in liquidTextures[liquid])
  if missing or stray:
    raise ValueError(f"A {liquid} material takes a diffuse texture and {list(liquidTextures[liquid]) or 'nothing else'}; missing {missing}, not taken {stray}")
  material = bpy.data.materials.new(name)
  material.use_fake_user = True
  material.use_nodes = True
  material[liquidPropertyName] = json.dumps({"liquid": liquid, "values": values})
  liquidNodes(material, liquid, values, diffuseTexture, {key: path for key, path in textures.items() if path is not None}, False)
  return {"material": name, "liquid": liquid, "values": values}


def liquidNodes(material, liquid, values, diffusePath, texturePaths, lit):
  """A liquid's look in the preview, as the client's DX9 liquid effects draw it, still (at time 0), also used for the client's own liquid
  materials when a zone is imported (then lit by their baked light). Water (RegionWater.fxo / SModelWater.fxo, ps_2_0): no diffuse; its
  color runs from waterColor1 seen from straight above to waterColor2 at grazing angles, lit like any surface, under a normal map
  sampled at the texture coordinates and at twice them, flattening with distance until flat 300 units off; plus its environment
  mirrored by fresnel (bias + (1 - bias) times the grazing term to fresnelPower) times reflectionAmount and reflectionColor, added
  unlit before fog. The environment is a cube map the preview cannot look up, so its average color stands for it. A waterfall
  (RegionWaterFall.fxo): its diffuse lit, as see-through as the diffuse's alpha. Lava: its two diffuses averaged (its effect is not
  read yet). Water a material leaves without its values or textures is drawn without what they give."""
  tree = material.node_tree
  diffuse = imageNode(material, diffuseNodeName, diffusePath)
  if liquid == "water" and all(key in values for key in ("waterColor1", "waterColor2", "fresnelBias", "fresnelPower")):
    baseColor, normal, added = clientWater(material, values, texturePaths)
    bridgeClientLight.surfaceOutput(material, baseColor, None, "opaque", lit, 0.5, normal, added)
    return
  baseColor = diffuse.outputs["Color"]
  if liquid == "lava" and "secondDiffuse" in texturePaths:
    second = imageNode(material, secondDiffuseNodeName, texturePaths["secondDiffuse"])
    baseColor = mixColors(tree, diffuse.outputs["Color"], second.outputs["Color"], 0.5)
  bridgeClientLight.surfaceOutput(material, baseColor, diffuse.outputs["Alpha"], "blended" if liquid == "waterfall" else "opaque", lit, 0.5)


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


def imageAverage(path):
  pixels = numpy.array(loadImage(path, "Non-Color").pixels[:], dtype=numpy.float64).reshape(-1, 4)
  return pixels[:, :3].mean(axis=0)


def clientWater(material, values, texturePaths):
  """The client's DX9 water as preview nodes: its base color to light, its rippled normal, and its mirrored environment to add."""
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
    coordinates = tree.nodes.new("ShaderNodeTexCoord").outputs["UV"]
    first = imageNode(material, normalNodeName, texturePaths["normal"])
    links.new(coordinates, first.inputs["Vector"])
    second = imageNode(material, normalNodeName + "Twice", texturePaths["normal"])
    links.new(scaled(coordinates, 2.0), second.inputs["Vector"])
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
  if "environment" in texturePaths:
    # The preview cannot look the cube map up, but zone export reads the texture from this node.
    imageNode(material, environmentNodeName, texturePaths["environment"])
  if "environment" in texturePaths and "reflectionAmount" in values and "reflectionColor" in values:
    bias = values["fresnelBias"]
    fresnel = math("ADD", bias, math("MULTIPLY", 1 - bias, math("POWER", grazing, values["fresnelPower"])))
    mirrored = numpy.array(values["reflectionColor"]) * imageAverage(texturePaths["environment"]) * values["reflectionAmount"]
    added = scaled(tuple(float(component) for component in mirrored), fresnel)
  return baseColor, normal, added


def liquidOf(material):
  """A liquid material's liquid and shader values, or None for any other material."""
  if material is None or liquidPropertyName not in material:
    return None
  return json.loads(material[liquidPropertyName])


def assignMaterial(objectName, materialName, selector):
  sceneObject = bridgeMeshAccess.requireMeshObject(objectName)
  if bridgeMeshAccess.surfaceLayersProperty in sceneObject:
    raise ValueError(f"'{objectName}' is surfaced by layers, which set its face materials; paint into a layer with paintSurface instead")
  material = bpy.data.materials.get(materialName)
  if material is None:
    raise ValueError(f"No material named '{materialName}'")
  mask = bridgeMeshAccess.evaluateSelector(selector, sceneObject, "faces")
  count = bridgeMeshAccess.requireSelection(mask, selector, sceneObject, "faces")
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
}
