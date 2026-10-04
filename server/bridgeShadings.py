"""Views drawn from a value on each face or vertex instead of the client's light, and which objects a view shows. Every drawn mesh (each
part of a collection instance as placed, and the scale figure) is replaced by a copy of its evaluated mesh carrying its values. An object
pass draws each pixel's object, which tells what the view shows, and a value pass each pixel's density, for the range it shows; then
each object draws in a color of its own, or its curvature (convex warm, concave cool, flat neutral), triangle density, or texel density
on a color ramp, lit softly from the northwest so the form still reads. Runs under Blender's Python."""
import math
import os

import bpy
import bpy_extras.object_utils
import mathutils
import numpy

import bridgeExportChecks
import bridgeMeshAccess
import bridgeSurfacing
import bridgeViews

passAttributeName = "zonewrightPass"
shadeAttributeName = "zonewrightShade"
figureName = "scale figure"
# A value shading keeps this share of its color facing away from the northwest light, so the light shows the form without hiding the color.
shadeAmbient = 0.6
# The objects a view shows most of take these colors in turn; any more share otherColor.
objectColors = (
  ("blue", (67, 99, 216)), ("orange", (245, 130, 49)), ("green", (60, 180, 75)), ("red", (230, 25, 75)), ("purple", (145, 30, 180)),
  ("yellow", (255, 225, 25)), ("cyan", (66, 212, 244)), ("magenta", (240, 50, 230)), ("lime", (191, 239, 69)), ("pink", (250, 190, 212)),
  ("teal", (70, 153, 144)), ("lavender", (220, 190, 255)), ("brown", (154, 99, 36)), ("olive", (128, 128, 0)),
)
otherColor = ("grey", (128, 128, 128))
# A form curved to this radius draws at half its color, sharper forms fuller.
halfColorRadius = 16.0
flatColor = (160, 160, 155)
convexColor = (235, 100, 30)
concaveColor = (40, 110, 230)
rampColors = (("blue", (40, 70, 200)), ("cyan", (40, 190, 230)), ("green", (60, 190, 80)), ("yellow", (240, 220, 40)), ("red", (220, 40, 30)))
densityArea = 10000.0
# Triangle density ramps over fixed decades, so a color reads alike in every view: the client's EQG terrains run from 8 to 5,083
# triangles per 10,000 square units (10th to 90th percentile), 244 at the median.
densityDecades = (0.0, 4.0)
untexturedColor = (60, 60, 60)
tinyArea = 1e-9
# A pass draws through half floats and a slight scale, so a number goes as whole digits in base passBase, each a fraction of
# passBase - 1, read back within passTolerance of a whole digit; further off means samples blended. An object's number takes two digits.
passBase = 128
passTolerance = 0.25
valueAttributeName = "zonewrightValue"
# A density shading's value pass carries each face's density for the range the view shows: its base-10 logarithm placed along
# valueSpan as a whole digit and the fraction past it (half floats keep that fraction to a ten-thousandth of a digit); its third
# channel marks a face without one.
valueSpan = (-6.0, 10.0)


def drawnParts(owner):
  """The meshes an object draws in a view, each with its world matrix."""
  if owner.type == "MESH" or bridgeMeshAccess.isCollectionInstance(owner):
    return bridgeMeshAccess.objectParts(owner)
  if owner.type in ("EMPTY", "ARMATURE"):
    return []
  raise ValueError(f"'{owner.name}' is a {owner.type}; views drawn from face values take meshes and collection instances")


class ShadedCopies:
  """The preview's drawn objects replaced by copies of their evaluated meshes, one per part as placed, drawing values instead of
  their materials; restore puts the objects back."""

  def __init__(self, preview):
    self.preview = preview
    self.linked = [sceneObject for sceneObject in preview.scene.collection.objects if sceneObject is not preview.camera]
    depsgraph = preview.depsgraph()
    readParts = {}
    self.owners, self.copies = [], []
    for owner in self.linked:
      parts = drawnParts(owner)
      if not parts:
        continue
      ownerIndex = len(self.owners)
      self.owners.append(figureName if owner in preview.createdObjects else owner.name)
      for part, matrix in parts:
        if part.name not in readParts:
          readParts[part.name] = bridgeExportChecks.readPart(part, depsgraph)
        data = readParts[part.name]
        if not len(data["loopStarts"]):
          continue
        copy = preview.addObject(bpy.data.objects.new(bridgeViews.previewName + "Shade", bridgeExportChecks.meshFromData(data, bridgeViews.previewName + "Shade")))
        copy.matrix_world = matrix
        world = numpy.array(matrix)
        self.copies.append({"owner": ownerIndex, "object": copy, "data": data, "positions": data["positions"] @ world[:3, :3].T + world[:3, 3]})
    for owner in self.linked:
      preview.scene.collection.objects.unlink(owner)
    if len(self.owners) >= passBase * passBase:
      raise ValueError(f"A view drawn from face values tells apart up to {passBase * passBase - 1} objects; this one draws {len(self.owners)}")

  def restore(self):
    for entry in self.copies:
      mesh = entry["object"].data
      self.preview.createdObjects.remove(entry["object"])
      bpy.data.objects.remove(entry["object"])
      bpy.data.meshes.remove(mesh)
    self.copies = []
    for owner in self.linked:
      self.preview.scene.collection.objects.link(owner)

  def ownerBounds(self, ownerIndex):
    positions = numpy.concatenate([entry["positions"] for entry in self.copies if entry["owner"] == ownerIndex])
    return positions.min(0), positions.max(0)

  def paint(self, attributeName, material, colorsOf):
    """Give each copy an attribute of colors (colorsOf(entry) is its domain and RGB rows) and the material that draws it."""
    self.preview.createdMaterials.append(material)
    for entry in self.copies:
      domain, colors = colorsOf(entry)
      rows = numpy.ones((len(colors), 4), dtype=numpy.float32)
      rows[:, :3] = colors
      mesh = entry["object"].data
      mesh.attributes.new(attributeName, "FLOAT_COLOR", domain).data.foreach_set("color", rows.ravel())
      mesh.materials.clear()
      mesh.materials.append(material)

  def renderPass(self, attributeName, colorsOf, passPath):
    """Render the copies drawing an attribute exactly; returns where something is drawn and the pixels, top row first."""
    self.paint(attributeName, attributeMaterial(attributeName, False), colorsOf)
    pixels = renderExactly(self.preview.scene, passPath)
    return pixels[:, :, 3] > 0.5, pixels

  def objectPass(self, passPath):
    """Render each pixel's object."""
    def colors(entry):
      number = entry["owner"] + 1
      return "FACE", numpy.tile([number % passBase / (passBase - 1), number // passBase / (passBase - 1), 0.0], (faceCount(entry), 1))
    drawn, pixels = self.renderPass(passAttributeName, colors, passPath)
    digits = wholeDigits(pixels[:, :, :2], drawn)
    return Seen(self.owners, numpy.where(drawn, digits[:, :, 0] + passBase * digits[:, :, 1] - 1, -1).astype(numpy.int64))

  def valuePass(self, logarithmsOf, passPath):
    """Render each pixel's face's base-10 logarithm (logarithmsOf(entry), NaN for a face without one); returns those the view shows
    and how many pixels show a face without one."""
    low, high = valueSpan

    def colors(entry):
      logarithms = logarithmsOf(entry)
      place = numpy.clip((numpy.nan_to_num(logarithms, nan=low) - low) / (high - low), 0, 1) * (passBase - 1)
      digit = numpy.floor(place)
      return "FACE", numpy.stack([digit / (passBase - 1), place - digit, numpy.isnan(logarithms).astype(float)], axis=1)
    drawn, pixels = self.renderPass(valueAttributeName, colors, passPath)
    valued = drawn & (pixels[:, :, 2] < 0.5)
    place = wholeDigits(pixels[:, :, :1], valued)[:, :, 0] + pixels[:, :, 1]
    return (low + place[valued] / (passBase - 1) * (high - low)), int((drawn & ~valued).sum())


def wholeDigits(channels, drawn):
  """Pass channels read back as whole digits."""
  digits = channels * (passBase - 1)
  whole = numpy.rint(digits)
  if drawn.any() and numpy.abs(digits - whole)[drawn].max() > passTolerance:
    raise RuntimeError("A pass blended samples at the edges of faces; it must take one sample a pixel")
  return whole


class Seen:
  """What an object pass found: each pixel's owner (-1 where nothing is drawn), top row first."""

  def __init__(self, owners, ownerPixels):
    self.owners, self.ownerPixels = owners, ownerPixels
    self.counts = numpy.bincount(ownerPixels[ownerPixels >= 0], minlength=len(owners))

  def share(self, count):
    return round(float(count) / self.ownerPixels.size, 4)


def renderExactly(scene, path):
  """Render the scene once, one sample a pixel and unfiltered, to float pixels (RGBA, top row first); alpha 0 where nothing is drawn."""
  render, eevee, image = scene.render, scene.eevee, scene.render.image_settings
  saved = (render.filter_size, eevee.taa_render_samples, render.film_transparent, image.file_format, image.color_mode, image.color_depth, render.filepath)
  try:
    render.filter_size = 0.0
    eevee.taa_render_samples = 1
    render.film_transparent = True
    image.file_format = "OPEN_EXR"
    image.color_mode = "RGBA"
    image.color_depth = "32"
    render.filepath = path
    bpy.ops.render.render(write_still=True, scene=scene.name)
    pixels = bridgeViews.readImagePixels(path)
  finally:
    render.filter_size, eevee.taa_render_samples, render.film_transparent = saved[:3]
    image.file_format, image.color_mode, image.color_depth = saved[3:6]
    render.filepath = saved[6]
    if os.path.exists(path):
      os.remove(path)
  return pixels[::-1]


def attributeMaterial(attributeName, lit):
  """Emits a color attribute, lit softly from the northwest (the layout light) or as stored."""
  material = bpy.data.materials.new(bridgeViews.previewName + "Shade")
  material.use_nodes = True
  nodes, links = material.node_tree.nodes, material.node_tree.links
  nodes.clear()
  attribute = nodes.new("ShaderNodeAttribute")
  attribute.attribute_type = "GEOMETRY"
  attribute.attribute_name = attributeName
  color = attribute.outputs["Color"]
  if lit:
    geometry = nodes.new("ShaderNodeNewGeometry")
    facing = nodes.new("ShaderNodeVectorMath")
    facing.operation = "DOT_PRODUCT"
    links.new(geometry.outputs["Normal"], facing.inputs[0])
    facing.inputs[1].default_value = bridgeViews.layoutLightDirection
    toward = nodes.new("ShaderNodeMath")
    toward.operation = "MAXIMUM"
    links.new(facing.outputs["Value"], toward.inputs[0])
    toward.inputs[1].default_value = 0.0
    shade = nodes.new("ShaderNodeMath")
    shade.operation = "MULTIPLY_ADD"
    links.new(toward.outputs["Value"], shade.inputs[0])
    shade.inputs[1].default_value = 1 - shadeAmbient
    shade.inputs[2].default_value = shadeAmbient
    shaded = nodes.new("ShaderNodeVectorMath")
    shaded.operation = "SCALE"
    links.new(color, shaded.inputs[0])
    links.new(shade.outputs["Value"], shaded.inputs["Scale"])
    color = shaded.outputs["Vector"]
  emission = nodes.new("ShaderNodeEmission")
  links.new(color, emission.inputs["Color"])
  output = nodes.new("ShaderNodeOutputMaterial")
  links.new(emission.outputs["Emission"], output.inputs["Surface"])
  return material


def triangleCorners(entry):
  data = entry["data"]
  return entry["positions"][data["loopVertices"][data["triangleLoops"]]]


def triangleAreas(corners):
  return numpy.linalg.norm(numpy.cross(corners[:, 1] - corners[:, 0], corners[:, 2] - corners[:, 0]), axis=1) / 2


def faceSums(entry, weights):
  return numpy.bincount(entry["data"]["trianglePolygons"], weights=weights, minlength=len(entry["data"]["loopStarts"]))


def faceCount(entry):
  return len(entry["data"]["loopStarts"])


def curvatures(entry):
  """Each vertex's curvature (per unit; 1 / the radius across a ridge or trough): the signed bend of the edges around it, convex
  positive, each by its length, over twice the area around it."""
  data, positions = entry["data"], entry["positions"]
  corners = triangleCorners(entry)
  crosses = numpy.cross(corners[:, 1] - corners[:, 0], corners[:, 2] - corners[:, 0])
  faceCross = numpy.stack([faceSums(entry, crosses[:, axis]) for axis in range(3)], axis=1)
  normals = faceCross / numpy.maximum(numpy.linalg.norm(faceCross, axis=1, keepdims=True), tinyArea)
  centers = numpy.add.reduceat(positions[data["loopVertices"]], data["loopStarts"]) / data["loopTotals"][:, None]
  faceA, faceB = data["faceA"], data["faceB"]
  # Across an edge whose faces are wound against each other, the far face's normal is turned to the near face's side first.
  farNormals = normals[faceB] * numpy.where(data["consistent"], 1.0, -1.0)[:, None]
  angles = numpy.arccos(numpy.clip((normals[faceA] * farNormals).sum(1), -1.0, 1.0))
  convex = ((centers[faceB] - centers[faceA]) * normals[faceA]).sum(1) < 0
  edges = data["edges"][data["sharedEdges"]]
  lengths = numpy.linalg.norm(positions[edges[:, 0]] - positions[edges[:, 1]], axis=1)
  bend = numpy.bincount(edges.ravel(), weights=numpy.repeat(numpy.where(convex, angles, -angles) * lengths, 2), minlength=len(positions))
  vertexAreas = numpy.bincount(data["loopVertices"][data["triangleLoops"]].ravel(), weights=numpy.repeat(numpy.linalg.norm(crosses, axis=1) / 6, 3), minlength=len(positions))
  return bend / (2 * numpy.maximum(vertexAreas, tinyArea))


def triangleDensities(entry):
  """Each face's triangles per 10,000 square units of its own area, as its base-10 logarithm."""
  data = entry["data"]
  areas = faceSums(entry, triangleAreas(triangleCorners(entry)))
  return numpy.log10(numpy.bincount(data["trianglePolygons"], minlength=faceCount(entry)) * densityArea / numpy.maximum(areas, tinyArea))


def texturePixels(material):
  """The width and height of a material's diffuse texture, or None when it has none drawn."""
  nodes = material.node_tree.nodes if material is not None and material.node_tree is not None else {}
  node = nodes.get(bridgeSurfacing.diffuseNodeName)
  if node is None or node.image is None or min(node.image.size) <= 0:
    return None
  return tuple(node.image.size)


def texelDensities(entry):
  """Each face's texture pixels per world unit, as its base-10 logarithm: the square root of the texels its texture coordinates cover
  per square unit of its area; NaN where it has no diffuse texture, no texture coordinates, or none of their area."""
  data = entry["data"]
  if data["uvs"] is None:
    return numpy.full(faceCount(entry), numpy.nan)
  pixels = [texturePixels(material) for material in data["materials"]] + [None]
  texelsPerRepeat = numpy.array([0.0 if size is None else float(size[0] * size[1]) for size in pixels])
  textured = data["uvs"][data["triangleLoops"]]
  firstUV, secondUV = textured[:, 1] - textured[:, 0], textured[:, 2] - textured[:, 0]
  texels = faceSums(entry, numpy.abs(firstUV[:, 0] * secondUV[:, 1] - firstUV[:, 1] * secondUV[:, 0]) / 2) * texelsPerRepeat[numpy.minimum(data["materialIndices"], len(data["materials"]))]
  areas = faceSums(entry, triangleAreas(triangleCorners(entry)))
  return numpy.where(texels > 0, numpy.log10(numpy.maximum(texels, tinyArea) / numpy.maximum(areas, tinyArea)) / 2, numpy.nan)


def significant(value, digits=3):
  return 0.0 if value == 0 else round(float(value), digits - 1 - math.floor(math.log10(abs(value))))


def rampOf(fractions, stops):
  """Colors (0 to 1) at fractions along stops [(position, (r, g, b) of 255)]."""
  positions = [position for position, _ in stops]
  return numpy.stack([numpy.interp(fractions, positions, [color[channel] for _, color in stops]) for channel in range(3)], axis=1) / 255


def rampStops():
  return [(index / (len(rampColors) - 1), color) for index, (_, color) in enumerate(rampColors)]


def rampScale(low, high):
  """Each ramp color with the value it stands for, ramped over base-10 logarithms from low to high."""
  steps = len(rampColors) - 1
  return [[significant(10 ** (low + (high - low) * index / steps)), name] for index, (name, _) in enumerate(rampColors)]


def hexColor(color):
  return "#" + "".join(f"{channel:02x}" for channel in color)


def logRange(logarithms):
  return None if not len(logarithms) else [significant(10 ** logarithms.min()), significant(10 ** logarithms.max())]


def paintObjects(copies, seen, passPath):
  """Each object a color of its own, the objects the view shows most of first; returns the legend of those it shows."""
  shown = [int(index) for index in numpy.argsort(-seen.counts, kind="stable") if seen.counts[index] > 0]
  colorOf = {index: objectColors[rank] if rank < len(objectColors) else otherColor for rank, index in enumerate(shown)}
  copies.paint(shadeAttributeName, attributeMaterial(shadeAttributeName, True), lambda entry: ("FACE", numpy.tile(numpy.array(colorOf.get(entry["owner"], otherColor)[1]) / 255, (faceCount(entry), 1))))
  return {"legend": [{"object": seen.owners[index], "color": colorOf[index][0], "rgb": hexColor(colorOf[index][1]), "share": seen.share(seen.counts[index])} for index in shown]}


def paintCurvature(copies, seen, passPath):
  def colors(entry):
    bent = curvatures(entry) * halfColorRadius
    strength = numpy.abs(bent) / (1 + numpy.abs(bent))
    toward = numpy.where((bent >= 0)[:, None], numpy.array(convexColor), numpy.array(concaveColor))
    return "POINT", (numpy.array(flatColor) + (toward - numpy.array(flatColor)) * strength[:, None]) / 255
  copies.paint(shadeAttributeName, attributeMaterial(shadeAttributeName, True), colors)
  return {"warm": "convex", "cool": "concave", "neutral": "flat", "halfColorRadius": halfColorRadius, "unit": "a ridge or trough curved to halfColorRadius draws at half color, sharper ones fuller"}


def paintTriangleDensity(copies, seen, passPath):
  logarithms, _ = copies.valuePass(triangleDensities, passPath)
  low, high = densityDecades
  copies.paint(shadeAttributeName, attributeMaterial(shadeAttributeName, True), lambda entry: ("FACE", rampOf(numpy.clip((triangleDensities(entry) - low) / (high - low), 0, 1), rampStops())))
  return {
    "unit": "triangles per 10,000 square units of surface", "colors": rampScale(low, high), "beyondEnds": "drawn in the end colors",
    "visibleRange": logRange(logarithms),
  }


def paintTexelDensity(copies, seen, passPath):
  logarithms, untexturedPixels = copies.valuePass(texelDensities, passPath)
  low, high = (float(logarithms.min()), float(logarithms.max())) if len(logarithms) else (0.0, 0.0)

  def colors(entry):
    densities = texelDensities(entry)
    fractions = numpy.full(len(densities), 0.5) if high == low else numpy.clip((numpy.nan_to_num(densities) - low) / (high - low), 0, 1)
    return "FACE", numpy.where(numpy.isnan(densities)[:, None], numpy.array(untexturedColor) / 255, rampOf(fractions, rampStops()))
  copies.paint(shadeAttributeName, attributeMaterial(shadeAttributeName, True), colors)
  return {
    "unit": "texture pixels per world unit", "range": logRange(logarithms), "colors": rampScale(low, high) if len(logarithms) else None,
    "untextured": "dark grey: no diffuse texture, no texture coordinates, or none of their area", "untexturedShare": seen.share(untexturedPixels),
  }


painters = {"objects": paintObjects, "curvature": paintCurvature, "triangleDensity": paintTriangleDensity, "texelDensity": paintTexelDensity}


def deepestPixels(mask):
  """The pixels of a mask farthest inside it, the edge of the picture counting as its edge."""
  current = mask.copy()
  current[[0, -1], :] = False
  current[:, [0, -1]] = False
  if not current.any():
    return mask
  while True:
    eroded = current.copy()
    eroded[1:, :] &= current[:-1, :]
    eroded[:-1, :] &= current[1:, :]
    eroded[:, 1:] &= current[:, :-1]
    eroded[:, :-1] &= current[:, 1:]
    if not eroded.any():
      return current
    current = eroded


def labelPlaces(preview, copies, seen, labels):
  """Where each named object shows in the view: the projection of its middle where the object shows there, else the point deepest inside
  what shows of it nearest that projection; and the named objects the view does not show."""
  if not labels or len(set(labels)) != len(labels):
    raise ValueError(f"labels names the objects to label, each once, got {labels!r}")
  for name in labels:
    if bpy.data.objects.get(name) is None:
      raise ValueError(f"No object named '{name}' to label")
    if name not in seen.owners:
      raise ValueError(f"'{name}' draws nothing in views: it is hidden from renders, a guide with guides off, or not a mesh or collection instance")
  height, width = seen.ownerPixels.shape
  shown, hidden = [], []
  for name in labels:
    index = seen.owners.index(name)
    if not seen.counts[index]:
      hidden.append(name)
      continue
    low, high = copies.ownerBounds(index)
    projected = bpy_extras.object_utils.world_to_camera_view(preview.scene, preview.camera, mathutils.Vector((low + high) / 2))
    x, y = projected.x * width, (1 - projected.y) * height
    mask = seen.ownerPixels == index
    column, row = int(x), int(y)
    if projected.z > 0 and 0 <= column < width and 0 <= row < height and mask[row, column]:
      at = [column, row]
    else:
      rows, columns = numpy.nonzero(deepestPixels(mask))
      nearest = int(numpy.argmin((columns - x) ** 2 + (rows - y) ** 2))
      at = [int(columns[nearest]), int(rows[nearest])]
    shown.append({"object": name, "at": at, "pixels": int(seen.counts[index])})
  return {"shown": shown, "notVisible": hidden}


def prepareView(preview, shading, labels, passPath):
  """Before a view renders: an object pass when the view has labels or a value shading, and the shading drawn; returns the labels'
  places and the shading's legend or scale."""
  copies = ShadedCopies(preview)
  seen = copies.objectPass(passPath)
  added = {}
  if labels is not None:
    added["labels"] = labelPlaces(preview, copies, seen, labels)
  if shading in bridgeViews.valueShadings:
    added[shading] = painters[shading](copies, seen, passPath)
  else:
    copies.restore()
  return added
