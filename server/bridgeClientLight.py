"""The client's lighting and fog as one shared shader node group, so the preview draws what the client draws (docs/clientRendering.md):
each surface unlit by Blender, its texture times the light the client's vertex shader computes from the vertex's baked color, its share
of scene light, and its normal, then fogged exponentially squared. Values are raw, as the client multiplies texture bytes: textures load
as Non-Color and the preview renders with the Raw view transform. Runs under Blender's Python."""
import math

import bpy

groupName = "eqClientLight"
# Raised whenever buildGroup changes, so a group saved in an older .blend is rebuilt in place.
groupVersion = 10
bakedAttribute = "eqColor"
normalAttribute = "eqNormal"
tintAttribute = "eqTint"
# The point lights' light per corner, which a preview puts on the copies it draws lit meshes from (bridgePointLights).
pointLightAttribute = "eqPointLight"
# Which vertices of an imported client zone take every point light, rather than only those marked for baked geometry.
takesAllLightsAttribute = "eqTakesAllLights"
detailUVMap = "eqDetailUV"
# RegionOldA.fxo's fFogRange: the fog ramp spans ten units of density between fog start and end.
fogRange = 10.0
environmentColors = ("ambientColor", "specialAmbientColor", "bounceColor", "sunColor", "fogColor")
environmentValues = ("fogStart", "fogEnd", "fogDensity")
# eqgame.exe pulls a fog end that reaches the far clip back to this share of the clip's distance past the fog start (0x48ac00).
fogPullIn = 0.15
# What the group outputs: the drawn color, or one of its inputs for calibration (bridgeViews.renderPasses). The render keeps only values
# of 0 or more, so the normal pass holds normal * 0.5 + 0.5; the distance pass holds the distance from the camera.
passes = ("lit", "base", "normal", "baked", "share", "distance")


def towardSun(azimuthDegrees, elevationDegrees):
  azimuth, elevation = math.radians(azimuthDegrees), math.radians(elevationDegrees)
  return (math.sin(azimuth) * math.cos(elevation), math.cos(azimuth) * math.cos(elevation), math.sin(elevation))


class GroupBuilder:
  def __init__(self, tree):
    self.tree = tree

  def node(self, kind, name=None, **settings):
    node = self.tree.nodes.new(kind)
    if name is not None:
      node.name = name
    for key, value in settings.items():
      setattr(node, key, value)
    return node

  def vectorMath(self, operation, first, second=None):
    node = self.node("ShaderNodeVectorMath", operation=operation)
    self.tree.links.new(first, node.inputs[0])
    if second is not None:
      self.tree.links.new(second, node.inputs[1])
    return node.outputs["Value" if operation == "DOT_PRODUCT" else "Vector"]

  def scale(self, vector, factor):
    node = self.node("ShaderNodeVectorMath", operation="SCALE")
    self.tree.links.new(vector, node.inputs[0])
    self.tree.links.new(factor, node.inputs["Scale"])
    return node.outputs["Vector"]

  def math(self, operation, first, second=None):
    node = self.node("ShaderNodeMath", operation=operation)
    for index, operand in enumerate((first, second)):
      if operand is None:
        continue
      if isinstance(operand, (int, float)):
        node.inputs[index].default_value = operand
      else:
        self.tree.links.new(operand, node.inputs[index])
    return node.outputs["Value"]

  def color(self, name):
    return self.node("ShaderNodeRGB", name=name).outputs["Color"]

  def value(self, name):
    return self.node("ShaderNodeValue", name=name).outputs["Value"]


def buildGroup(tree):
  """Build the group's nodes, adding the sockets it lacks, and set the inputs its materials' own nodes decide (markStoredNormals)."""
  # Sockets are kept, not rebuilt: clearing them would drop every saved material's links into the group, and a material saved before
  # an input existed takes that input's default (added light: none).
  tree.nodes.clear()
  tree["eqVersion"] = groupVersion
  sockets = {(item.in_out, item.name) for item in tree.interface.items_tree if item.item_type == "SOCKET"}
  for socketName, socketType in (
    ("Base", "NodeSocketColor"), ("Baked", "NodeSocketColor"), ("Share", "NodeSocketFloat"), ("Normal", "NodeSocketVector"), ("Added", "NodeSocketColor"),
    ("Stored", "NodeSocketFloat"),
  ):
    if ("INPUT", socketName) not in sockets:
      socket = tree.interface.new_socket(socketName, in_out="INPUT", socket_type=socketType)
      if socketName == "Added":
        socket.default_value = (0.0, 0.0, 0.0, 1.0)
  if ("OUTPUT", "Color") not in sockets:
    tree.interface.new_socket("Color", in_out="OUTPUT", socket_type="NodeSocketColor")
  build = GroupBuilder(tree)
  inputs = build.node("NodeGroupInput").outputs
  # Blender turns a surface's shading normal toward the camera on a face seen from behind, where the client's vertex shader lights a
  # face by its own normal whichever side is seen; a normal that is not a mesh's stored one (Stored 0) is turned back.
  backfacing = build.node("ShaderNodeNewGeometry").outputs["Backfacing"]
  turn = build.math("SUBTRACT", 1.0, build.math("MULTIPLY", build.math("MULTIPLY", backfacing, 2.0), build.math("SUBTRACT", 1.0, inputs["Stored"])))
  normal = build.scale(inputs["Normal"], turn)
  toSun = build.node("ShaderNodeCombineXYZ", name="towardSun").outputs["Vector"]
  facing = build.vectorMath("DOT_PRODUCT", normal, toSun)
  sunTerm = build.scale(build.color("sunColor"), build.math("MAXIMUM", facing, 0.0))
  bounceTerm = build.scale(build.color("bounceColor"), build.math("MAXIMUM", build.math("MULTIPLY", facing, -1.0), 0.0))
  sceneLight = build.vectorMath("ADD", build.vectorMath("ADD", build.color("ambientColor"), bounceTerm), sunTerm)
  light = build.vectorMath("ADD", build.vectorMath("ADD", inputs["Baked"], build.scale(sceneLight, inputs["Share"])), build.color("specialAmbientColor"))
  # The client adds its point lights unscaled by the share of scene light; a surface without the attribute reads none.
  pointLight = build.node("ShaderNodeAttribute", attribute_type="GEOMETRY", attribute_name=pointLightAttribute)
  light = build.vectorMath("ADD", light, pointLight.outputs["Color"])
  ones = build.node("ShaderNodeCombineXYZ")
  for axis in "XYZ":
    ones.inputs[axis].default_value = 1.0
  clamped = build.vectorMath("MINIMUM", build.vectorMath("MAXIMUM", light, build.node("ShaderNodeCombineXYZ").outputs["Vector"]), ones.outputs["Vector"])
  # The framebuffer holds no more than 1: a terrain texture doubled by its effect can light past it.
  lit = build.vectorMath("MINIMUM", build.vectorMath("MULTIPLY", inputs["Base"], clamped), ones.outputs["Vector"])
  distance = build.node("ShaderNodeCameraData").outputs["View Distance"]
  ramp = build.math("MULTIPLY", build.math("SUBTRACT", distance, build.value("fogStart")), build.value("fogRampScale"))
  rampClamped = build.math("MINIMUM", build.math("MAXIMUM", ramp, 0.0), fogRange)
  dense = build.math("MULTIPLY", rampClamped, build.value("fogDensity"))
  visibility = build.math("EXPONENT", build.math("MULTIPLY", build.math("MULTIPLY", dense, dense), -1.0))
  fogged = build.node("ShaderNodeMix", data_type="RGBA")
  colorInputs = [socket for socket in fogged.inputs if socket.type == "RGBA"]
  tree.links.new(visibility, fogged.inputs["Factor"])
  tree.links.new(build.color("fogColor"), colorInputs[0])
  # A shader's own unlit term (water's mirrored environment, lava's glow) adds to the lit color before fog, as the client's liquid
  # effects add it, and the sum is held to 1 as their pixel shaders' output is before fog blends it.
  tree.links.new(build.vectorMath("MINIMUM", build.vectorMath("ADD", lit, inputs["Added"]), ones.outputs["Vector"]), colorInputs[1])
  shareColor = build.node("ShaderNodeCombineXYZ")
  for axis in "XYZ":
    tree.links.new(inputs["Share"], shareColor.inputs[axis])
  selected = next(socket for socket in fogged.outputs if socket.type == "RGBA")
  passSelect = build.value("passSelect")
  halves = build.node("ShaderNodeCombineXYZ")
  for axis in "XYZ":
    halves.inputs[axis].default_value = 0.5
  encodedNormal = build.vectorMath("ADD", build.scale(normal, build.math("ADD", 0.5, 0.0)), halves.outputs["Vector"])
  distanceColor = build.node("ShaderNodeCombineXYZ")
  for axis in "XYZ":
    tree.links.new(distance, distanceColor.inputs[axis])
  for index, passOutput in enumerate((inputs["Base"], encodedNormal, inputs["Baked"], shareColor.outputs["Vector"], distanceColor.outputs["Vector"]), 1):
    choose = build.node("ShaderNodeMix", data_type="RGBA")
    colorInputs = [socket for socket in choose.inputs if socket.type == "RGBA"]
    tree.links.new(build.math("COMPARE", passSelect, float(index)), choose.inputs["Factor"])
    tree.links.new(selected, colorInputs[0])
    tree.links.new(passOutput, colorInputs[1])
    selected = next(socket for socket in choose.outputs if socket.type == "RGBA")
  tree.links.new(selected, build.node("NodeGroupOutput").inputs["Color"])
  markStoredNormals(tree)
  return tree


def markStoredNormals(tree):
  """Set Stored on each material's use of the group from its own nodes: 1 where it lights by a lit mesh's stored normal, else 0."""
  for material in bpy.data.materials:
    if material.node_tree is None:
      continue
    nodes = material.node_tree.nodes
    stored = any(node.type == "ATTRIBUTE" and node.attribute_name == normalAttribute for node in nodes)
    for node in nodes:
      if node.type == "GROUP" and node.node_tree == tree:
        node.inputs["Stored"].default_value = float(stored)


def selectPass(name):
  for tree in groups():
    tree.nodes["passSelect"].outputs["Value"].default_value = float(passes.index(name))


def group():
  """The open file's own group, which its materials use."""
  tree = next((tree for tree in bpy.data.node_groups if tree.name == groupName and tree.library is None), None)
  if tree is None:
    tree = bpy.data.node_groups.new(groupName, "ShaderNodeTree")
  if tree.get("eqVersion") != groupVersion:
    buildGroup(tree)
  return tree


def groups():
  """The file's own group and each kit's linked copy, a kit saved with an older one rebuilt for this session (linked data never saves)."""
  linked = [tree for tree in bpy.data.node_groups if tree.name == groupName and tree.library is not None]
  for tree in linked:
    if tree.get("eqVersion") != groupVersion:
      buildGroup(tree)
  return [group()] + linked


def effectiveFog(zone):
  """The fog the client draws: start, end, and density; none when the zone's fog is off (the zone type 0), and an end that reaches the
  far clip pulled in short of it."""
  start, end, clip = zone["fogStart"], zone["fogEnd"], zone["maxClip"]
  if end >= clip:
    end = clip - fogPullIn * (clip - start)
  return start, end, zone["fogDensity"] if zone["fogOn"] else 0.0


def applyEnvironment(zone):
  """Set every copy of the group's environment (groups) from the zone's properties."""
  sun = towardSun(zone["sunAzimuthDegrees"], zone["sunElevationDegrees"])
  start, end, density = effectiveFog(zone)
  for tree in groups():
    nodes = tree.nodes
    for key in environmentColors:
      nodes[key].outputs["Color"].default_value = (*zone[key], 1.0)
    for axis, component in zip("XYZ", sun):
      nodes["towardSun"].inputs[axis].default_value = component
    nodes["fogStart"].outputs["Value"].default_value = start
    nodes["fogRampScale"].outputs["Value"].default_value = fogRange / (end - start)
    nodes["fogDensity"].outputs["Value"].default_value = density


def surfaceOutput(material, baseColor, alpha, alphaMode, lit, threshold, normal=None, added=None):
  """Finish a material as the client draws it: the base color lit and fogged, emitted unlit, with the alpha mode's transparency. A lit
  mesh carries the file's baked colors and normals as attributes; any other surface has no baked light, the full share of scene
  light, and its geometric normal or the given one (a normal map's)."""
  for unused in [node for node in material.node_tree.nodes if node.type == "BSDF_PRINCIPLED"]:
    material.node_tree.nodes.remove(unused)
  tree = material.node_tree
  links = tree.links
  lighting = tree.nodes.new("ShaderNodeGroup")
  lighting.node_tree = group()
  links.new(baseColor, lighting.inputs["Base"])
  if added is not None:
    links.new(added, lighting.inputs["Added"])
  else:
    lighting.inputs["Added"].default_value = (0.0, 0.0, 0.0, 1.0)
  if lit:
    baked = tree.nodes.new("ShaderNodeAttribute")
    baked.attribute_type = "GEOMETRY"
    baked.attribute_name = bakedAttribute
    links.new(baked.outputs["Color"], lighting.inputs["Baked"])
    links.new(baked.outputs["Alpha"], lighting.inputs["Share"])
    stored = tree.nodes.new("ShaderNodeAttribute")
    stored.attribute_type = "GEOMETRY"
    stored.attribute_name = normalAttribute
    toWorld = tree.nodes.new("ShaderNodeVectorTransform")
    toWorld.vector_type = "NORMAL"
    toWorld.convert_from = "OBJECT"
    toWorld.convert_to = "WORLD"
    links.new(stored.outputs["Vector"], toWorld.inputs["Vector"])
    unit = tree.nodes.new("ShaderNodeVectorMath")
    unit.operation = "NORMALIZE"
    links.new(toWorld.outputs["Vector"], unit.inputs[0])
    links.new(unit.outputs["Vector"], lighting.inputs["Normal"])
    lighting.inputs["Stored"].default_value = 1.0
  else:
    lighting.inputs["Baked"].default_value = (0.0, 0.0, 0.0, 1.0)
    lighting.inputs["Share"].default_value = 1.0
    links.new(normal if normal is not None else tree.nodes.new("ShaderNodeNewGeometry").outputs["Normal"], lighting.inputs["Normal"])
  emission = tree.nodes.new("ShaderNodeEmission")
  emission.inputs["Strength"].default_value = 1.0
  links.new(lighting.outputs["Color"], emission.inputs["Color"])
  surface = emission.outputs["Emission"]
  if alphaMode != "opaque":
    coverage = alpha
    if alphaMode == "cutout":
      test = tree.nodes.new("ShaderNodeMath")
      test.operation = "GREATER_THAN"
      test.inputs[1].default_value = threshold
      links.new(alpha, test.inputs[0])
      coverage = test.outputs["Value"]
    elif alphaMode != "blended":
      raise ValueError(f"Alpha mode '{alphaMode}' is not opaque, cutout, or blended")
    mix = tree.nodes.new("ShaderNodeMixShader")
    links.new(coverage, mix.inputs["Fac"])
    links.new(tree.nodes.new("ShaderNodeBsdfTransparent").outputs["BSDF"], mix.inputs[1])
    links.new(emission.outputs["Emission"], mix.inputs[2])
    surface = mix.outputs["Shader"]
    # A blended surface is drawn over what lies behind it by its alpha, as the client's source-alpha blend draws it, not dithered, whose
    # grain at the preview's few samples speckles a fall with the rock behind it; a cutout is all or nothing either way.
    material.surface_render_method = "BLENDED" if alphaMode == "blended" else "DITHERED"
  output = next(node for node in tree.nodes if node.type == "OUTPUT_MATERIAL")
  links.new(surface, output.inputs["Surface"])
