"""The client's lighting and fog as one shared shader node group, so the preview draws what the client draws (docs/clientRendering.md):
each surface unlit by Blender, its texture times the light the client's vertex shader computes from the vertex's baked color, its share
of scene light, and its normal, then fogged exponentially squared. Values are raw, as the client multiplies texture bytes: textures load
as Non-Color and the preview renders with the Raw view transform. Runs under Blender's Python."""
import math

import bpy

groupName = "eqClientLight"
# Raised whenever buildGroup changes, so a group saved in an older .blend is rebuilt in place.
groupVersion = 2
bakedAttribute = "eqColor"
normalAttribute = "eqNormal"
# RegionOldA.fxo's fFogRange: the fog ramp spans ten units of density between fog start and end.
fogRange = 10.0
environmentColors = ("ambientColor", "specialAmbientColor", "bounceColor", "sunColor", "fogColor")
environmentValues = ("fogStart", "fogEnd", "fogDensity")
# What the group outputs: the drawn color, or one of its inputs for calibration (bridgeViews.renderPasses).
passes = ("lit", "base", "normal", "baked", "share", "position")


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
  tree.nodes.clear()
  tree.interface.clear()
  tree["eqVersion"] = groupVersion
  for socketName, socketType in (("Base", "NodeSocketColor"), ("Baked", "NodeSocketColor"), ("Share", "NodeSocketFloat"), ("Normal", "NodeSocketVector")):
    tree.interface.new_socket(socketName, in_out="INPUT", socket_type=socketType)
  tree.interface.new_socket("Color", in_out="OUTPUT", socket_type="NodeSocketColor")
  build = GroupBuilder(tree)
  inputs = build.node("NodeGroupInput").outputs
  toSun = build.node("ShaderNodeCombineXYZ", name="towardSun").outputs["Vector"]
  facing = build.vectorMath("DOT_PRODUCT", inputs["Normal"], toSun)
  sunTerm = build.scale(build.color("sunColor"), build.math("MAXIMUM", facing, 0.0))
  bounceTerm = build.scale(build.color("bounceColor"), build.math("MAXIMUM", build.math("MULTIPLY", facing, -1.0), 0.0))
  sceneLight = build.vectorMath("ADD", build.vectorMath("ADD", build.color("ambientColor"), bounceTerm), sunTerm)
  light = build.vectorMath("ADD", build.vectorMath("ADD", inputs["Baked"], build.scale(sceneLight, inputs["Share"])), build.color("specialAmbientColor"))
  ones = build.node("ShaderNodeCombineXYZ")
  for axis in "XYZ":
    ones.inputs[axis].default_value = 1.0
  clamped = build.vectorMath("MINIMUM", build.vectorMath("MAXIMUM", light, build.node("ShaderNodeCombineXYZ").outputs["Vector"]), ones.outputs["Vector"])
  lit = build.vectorMath("MULTIPLY", inputs["Base"], clamped)
  distance = build.node("ShaderNodeCameraData").outputs["View Distance"]
  ramp = build.math("MULTIPLY", build.math("SUBTRACT", distance, build.value("fogStart")), build.value("fogRampScale"))
  rampClamped = build.math("MINIMUM", build.math("MAXIMUM", ramp, 0.0), fogRange)
  dense = build.math("MULTIPLY", rampClamped, build.value("fogDensity"))
  visibility = build.math("EXPONENT", build.math("MULTIPLY", build.math("MULTIPLY", dense, dense), -1.0))
  fogged = build.node("ShaderNodeMix", data_type="RGBA")
  colorInputs = [socket for socket in fogged.inputs if socket.type == "RGBA"]
  tree.links.new(visibility, fogged.inputs["Factor"])
  tree.links.new(build.color("fogColor"), colorInputs[0])
  tree.links.new(lit, colorInputs[1])
  shareColor = build.node("ShaderNodeCombineXYZ")
  for axis in "XYZ":
    tree.links.new(inputs["Share"], shareColor.inputs[axis])
  selected = next(socket for socket in fogged.outputs if socket.type == "RGBA")
  passSelect = build.value("passSelect")
  position = build.node("ShaderNodeNewGeometry").outputs["Position"]
  for index, passOutput in enumerate((inputs["Base"], inputs["Normal"], inputs["Baked"], shareColor.outputs["Vector"], position), 1):
    choose = build.node("ShaderNodeMix", data_type="RGBA")
    colorInputs = [socket for socket in choose.inputs if socket.type == "RGBA"]
    tree.links.new(build.math("COMPARE", passSelect, float(index)), choose.inputs["Factor"])
    tree.links.new(selected, colorInputs[0])
    tree.links.new(passOutput, colorInputs[1])
    selected = next(socket for socket in choose.outputs if socket.type == "RGBA")
  tree.links.new(selected, build.node("NodeGroupOutput").inputs["Color"])
  return tree


def selectPass(name):
  group().nodes["passSelect"].outputs["Value"].default_value = float(passes.index(name))


def group():
  tree = bpy.data.node_groups.get(groupName)
  if tree is None:
    tree = bpy.data.node_groups.new(groupName, "ShaderNodeTree")
  if tree.get("eqVersion") != groupVersion:
    buildGroup(tree)
  return tree


def applyEnvironment(zone):
  """Set the group's environment from the zone's properties."""
  nodes = group().nodes
  for key in environmentColors:
    nodes[key].outputs["Color"].default_value = (*zone[key], 1.0)
  sun = towardSun(zone["sunAzimuthDegrees"], zone["sunElevationDegrees"])
  for axis, component in zip("XYZ", sun):
    nodes["towardSun"].inputs[axis].default_value = component
  nodes["fogStart"].outputs["Value"].default_value = zone["fogStart"]
  nodes["fogRampScale"].outputs["Value"].default_value = fogRange / (zone["fogEnd"] - zone["fogStart"])
  nodes["fogDensity"].outputs["Value"].default_value = zone["fogDensity"]


def surfaceOutput(material, baseColor, alpha, alphaMode, lit, threshold, normal=None):
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
    material.surface_render_method = "DITHERED"
  output = next(node for node in tree.nodes if node.type == "OUTPUT_MATERIAL")
  links.new(surface, output.inputs["Surface"])
