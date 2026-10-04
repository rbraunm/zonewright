"""Views drawn from a value on each face instead of the client's light, and which objects a view shows. Every drawn mesh (each part of a
collection instance as placed, and the scale figure) is replaced by a copy of its evaluated mesh carrying its values. An object pass
draws each pixel's object, which tells what the view shows; then each object draws in a color of its own, lit softly from the
northwest so the form still reads. Runs under Blender's Python."""
import os

import bpy
import bpy_extras.object_utils
import mathutils
import numpy

import bridgeExportChecks
import bridgeMeshAccess
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
# A pass draws through half floats and a slight scale, so a number goes as whole digits in base passBase, each a fraction of
# passBase - 1, read back within passTolerance of a whole digit; further off means samples blended. An object's number takes two digits.
passBase = 128
passTolerance = 0.25


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


def faceCount(entry):
  return len(entry["data"]["loopStarts"])


def hexColor(color):
  return "#" + "".join(f"{channel:02x}" for channel in color)


def paintObjects(copies, seen, passPath):
  """Each object a color of its own, the objects the view shows most of first; returns the legend of those it shows."""
  shown = [int(index) for index in numpy.argsort(-seen.counts, kind="stable") if seen.counts[index] > 0]
  colorOf = {index: objectColors[rank] if rank < len(objectColors) else otherColor for rank, index in enumerate(shown)}
  copies.paint(shadeAttributeName, attributeMaterial(shadeAttributeName, True), lambda entry: ("FACE", numpy.tile(numpy.array(colorOf.get(entry["owner"], otherColor)[1]) / 255, (faceCount(entry), 1))))
  return {"legend": [{"object": seen.owners[index], "color": colorOf[index][0], "rgb": hexColor(colorOf[index][1]), "share": seen.share(seen.counts[index])} for index in shown]}


painters = {"objects": paintObjects}


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
