"""Swim volumes: the boxes where the client lets players swim (the .zon's AWT_ water and ALV_ lava regions), designed as objects an
artist places and adjusts and never derived at export. A generator gives a pool or river starting boxes from its surface and bed; hand
edits are kept, and a body changed since its boxes were accepted is reported. A box need not match any surface: a floating pool is
design. Runs under Blender's Python."""
import hashlib
import json
import math
import re

import bpy
import mathutils
import numpy

import bridgeMeshAccess
import bridgeObjects
import bridgeSurfacing
import bridgeWater

swimCollectionName = "swimVolumes"
volumePrefixes = {"water": "AWT_", "lava": "ALV_"}
namePattern = re.compile(r"^[A-Za-z0-9]+$")
# A box's top stays within this of the surface it was built under, and its bottom lies this far under the deepest bed below it.
volumeTolerance = 1.0
volumeFloorMargin = 4.0
# A box stops this far above the ceiling of open space found under a bed (a cave or tunnel below the water).
ceilingClearance = 1.0
editedTolerance = 1e-3
down = mathutils.Vector((0.0, 0.0, -1.0))
findingSamples = 8
dryShare = 0.5


def swimBoxes():
  return sorted((sceneObject for sceneObject in bpy.context.scene.objects if bridgeMeshAccess.swimProperty in sceneObject), key=lambda box: box.name)


def readBox(box):
  return json.loads(box[bridgeMeshAccess.swimProperty])


def boxBounds(box):
  """A box's center and half extents in world units, from its transform."""
  return [round(float(value), 4) for value in box.matrix_world.translation], [round(float(value), 4) for value in box.scale]


def isHandEdited(box):
  spec = readBox(box)
  if spec["built"] is None:
    return True
  center, halfExtents = boxBounds(box)
  return any(abs(a - b) > editedTolerance for a, b in zip(center + halfExtents, spec["built"]["center"] + spec["built"]["halfExtents"]))


def swimBodies():
  """Rendered pools and rivers: falls are not swum."""
  return [
    sceneObject for sceneObject in bpy.context.scene.objects
    if bridgeMeshAccess.waterProperty in sceneObject and not sceneObject.hide_render and bridgeWater.readDefinition(sceneObject)["kind"] != "fall"
  ]


def requireSwimBody(name):
  body = bridgeMeshAccess.requireWater(name)
  if bridgeWater.readDefinition(body)["kind"] == "fall":
    raise ValueError(f"'{name}' is a fall; falls are not swum")
  return body


def liquidOfBody(body):
  material = body.material_slots[0].material if body.material_slots else None
  liquid = bridgeSurfacing.liquidOf(material)
  if liquid is None or liquid["liquid"] not in volumePrefixes:
    raise ValueError(f"Water body '{body.name}' needs a water or lava material for swim volumes")
  return liquid["liquid"]


def bodyCells(body, ground, area=None):
  """The body's grid cells whose middles its surface covers with water over the bed: (i, j) -> (surface level, box floor, whether a
  ceiling held the floor up). A cell's floor lies volumeFloorMargin under the deepest bed at its middle and corners, but stays
  ceilingClearance above any open space found under that bed."""
  spacing = bridgeWater.readDefinition(body)["spacing"]
  surface = bridgeMeshAccess.worldTree([body])
  positions, _ = bridgeMeshAccess.readVertexArrays(body)
  low, high = numpy.floor(positions[:, :2].min(0) / spacing).astype(int), numpy.ceil(positions[:, :2].max(0) / spacing).astype(int)
  above = float(positions[:, 2].max()) + 1
  cells = {}
  for i in range(low[0], high[0]):
    for j in range(low[1], high[1]):
      x, y = (i + 0.5) * spacing, (j + 0.5) * spacing
      if area is not None and not bridgeWater.insideArea(numpy.array([[x, y]]), area)[0]:
        continue
      location, _, _, _ = surface.ray_cast(mathutils.Vector((x, y, above)), down, bridgeMeshAccess.waterReach)
      if location is None:
        continue
      depth = ground.depth(x, y, location.z)
      if not depth:
        continue
      corners = [ground.depth(x + dx * spacing / 2, y + dy * spacing / 2, location.z) for dx in (-1, 1) for dy in (-1, 1)]
      bed = location.z - max([depth] + [corner for corner in corners if corner])
      floor = bed - volumeFloorMargin
      below, normal, _, _ = ground.tree.ray_cast(mathutils.Vector((x, y, location.z - depth - 0.01)), down, bridgeMeshAccess.waterReach)
      held = below is not None and normal.z < 0 and below.z + ceilingClearance > floor
      if held:
        floor = below.z + ceilingClearance
      cells[(i, j)] = (location.z, min(floor, location.z - volumeTolerance), held)
  return cells, spacing


def bandRectangles(cells):
  """Cover cells keyed (i, j) with rectangles of whole cells, greedily: across each row as far as it runs, then down while whole rows
  below match."""
  remaining = set(cells)
  for i, j in sorted(cells, key=lambda cell: (cell[1], cell[0])):
    if (i, j) not in remaining:
      continue
    last = i
    while (last + 1, j) in remaining:
      last += 1
    bottomRow = j
    while all((column, bottomRow + 1) in remaining for column in range(i, last + 1)):
      bottomRow += 1
    for column in range(i, last + 1):
      for row in range(j, bottomRow + 1):
        remaining.discard((column, row))
    yield i, last, j, bottomRow


def cellBoxes(cells, spacing):
  """Boxes over the cells, grouped by surface level within volumeTolerance (and, where a ceiling held a floor up, by that floor, so no
  box reaches into the space below): tops at the lowest surface, bottoms at the lowest floor."""
  bands = {}
  for cell, (level, floor, held) in cells.items():
    bands.setdefault((math.floor(level / volumeTolerance), math.floor(floor / volumeTolerance) if held else None), []).append(cell)
  boxes = []
  for band in sorted(bands, key=lambda key: (key[0], key[1] is not None, key[1] or 0)):
    for first, last, firstRow, lastRow in bandRectangles(bands[band]):
      covered = [cells[(i, j)] for i in range(first, last + 1) for j in range(firstRow, lastRow + 1)]
      top = min(level for level, _, _ in covered)
      bottom = min(floor for _, floor, _ in covered)
      boxes.append({
        "center": [(first + last + 1) / 2 * spacing, (firstRow + lastRow + 1) / 2 * spacing, (top + bottom) / 2],
        "halfExtents": [(last - first + 1) * spacing / 2, (lastRow - firstRow + 1) * spacing / 2, (top - bottom) / 2],
      })
  return boxes


def bodyFingerprint(body, cells):
  """What a body's boxes were made from: its definition, its surface, and the bed under it."""
  digest = hashlib.sha256(json.dumps(bridgeWater.readDefinition(body), sort_keys=True).encode())
  positions, _ = bridgeMeshAccess.readVertexArrays(body)
  digest.update(numpy.round(positions, 2).tobytes())
  digest.update(json.dumps(sorted([[cell[0], cell[1], round(level, 2), round(floor, 2)] for cell, (level, floor, _) in cells.items()])).encode())
  return digest.hexdigest()[:16]


def boxObject(name, liquid, body, center, halfExtents, built, fingerprint):
  if min(halfExtents) <= 0:
    raise ValueError(f"Swim volume '{name}' needs positive half extents, got {halfExtents}")
  box = bpy.data.objects.new(name, None)
  box.empty_display_type = "CUBE"
  box.empty_display_size = 1.0
  box.location = center
  box.scale = halfExtents
  box[bridgeMeshAccess.swimProperty] = json.dumps({"liquid": liquid, "body": body, "built": built, "fingerprint": fingerprint})
  bridgeObjects.targetCollection(swimCollectionName).objects.link(box)
  return box


def freeName(prefix, stem, taken):
  index = 1
  while f"{prefix}{stem}{index:02d}".lower() in taken:
    index += 1
  name = f"{prefix}{stem}{index:02d}"
  taken.add(name.lower())
  return name


def takenNames():
  return {sceneObject.name.lower() for sceneObject in bpy.data.objects}


def buildSwimVolumes(body, area, replaceEdited):
  """Starting boxes for a pool or river, or the part of it inside an area: its generated boxes there are replaced; boxes edited or placed
  by hand are kept, and refuse the rebuild unless replaceEdited."""
  bodyObject = requireSwimBody(body)
  definition = bridgeWater.readDefinition(bodyObject)
  if definition.get("swimmable") is False:
    raise ValueError(f"'{body}' is marked not swimmable; mark it swimmable (editWater swimmable true) before building its swim volumes")
  liquid = liquidOfBody(bodyObject)
  area = bridgeWater.requireArea(area) if area is not None else None
  bpy.context.view_layer.update()
  inScope = [box for box in swimBoxes() if readBox(box)["body"] == body and (area is None or bridgeWater.insideArea(numpy.array([boxBounds(box)[0][:2]]), area)[0])]
  edited = [box.name for box in inScope if isHandEdited(box)]
  if edited and not replaceEdited:
    raise ValueError(f"Swim volumes {edited} of '{body}' were edited or placed by hand; rebuilding would replace them (give replaceEdited to)")
  for box in inScope:
    bpy.data.objects.remove(box)
  ground = bridgeWater.Ground()
  cells, spacing = bodyCells(bodyObject, ground, area)
  if not cells:
    raise ValueError(f"'{body}' has no water over its bed" + (" inside the area" if area is not None else ""))
  fingerprint = bodyFingerprint(bodyObject, bodyCells(bodyObject, ground)[0])
  taken = takenNames()
  stem = bridgeWater.fileStem(body)
  built = []
  for box in cellBoxes(cells, spacing):
    name = freeName(volumePrefixes[liquid], stem, taken)
    boxObject(name, liquid, body, box["center"], box["halfExtents"], box, fingerprint)
    built.append(name)
  bpy.context.view_layer.update()
  return {"body": body, "replaced": len(inScope), "built": built} | describeBody(bodyObject, ground)


def placeSwimVolume(name, liquid, minimum, maximum, body):
  """A box placed by hand from its corners: a floating pool, a cove, part of a lake; tied to a body or standing alone."""
  if not isinstance(name, str) or not namePattern.match(name):
    raise ValueError(f"A swim volume's name is letters and digits (its prefix is added), got {name!r}")
  if liquid not in volumePrefixes:
    raise ValueError(f"liquid is one of {list(volumePrefixes)}, got {liquid!r}")
  if len(minimum) != 3 or len(maximum) != 3 or any(high <= low for low, high in zip(minimum, maximum)):
    raise ValueError(f"minimum and maximum are [x, y, z] corners with every maximum above its minimum, got {minimum} and {maximum}")
  if body is not None:
    bodyObject = requireSwimBody(body)
    if bridgeWater.readDefinition(bodyObject).get("swimmable") is False:
      raise ValueError(f"'{body}' is marked not swimmable; mark it swimmable (editWater swimmable true) before giving it swim volumes")
    if liquidOfBody(bodyObject) != liquid:
      raise ValueError(f"'{body}' is {liquidOfBody(bodyObject)}, not {liquid}")
  fullName = volumePrefixes[liquid] + name
  if fullName.lower() in takenNames():
    raise ValueError(f"An object named '{fullName}' already exists (names count the same whatever their case)")
  center = [(low + high) / 2 for low, high in zip(minimum, maximum)]
  halfExtents = [(high - low) / 2 for low, high in zip(minimum, maximum)]
  box = boxObject(fullName, liquid, body, center, halfExtents, None, None)
  bpy.context.view_layer.update()
  return describeBox(box)


def acceptSwimVolumes(body):
  """Stamp a body's boxes as made for the body as it now is, after its water or bed changed and the boxes were looked at again."""
  bodyObject = requireSwimBody(body)
  boxes = [box for box in swimBoxes() if readBox(box)["body"] == body]
  if not boxes:
    raise ValueError(f"'{body}' has no swim volumes to accept")
  ground = bridgeWater.Ground()
  fingerprint = bodyFingerprint(bodyObject, bodyCells(bodyObject, ground)[0])
  for box in boxes:
    box[bridgeMeshAccess.swimProperty] = json.dumps(readBox(box) | {"fingerprint": fingerprint})
  return {"body": body, "accepted": [box.name for box in boxes]} | describeBody(bodyObject, ground)


def describeBox(box):
  spec = readBox(box)
  center, halfExtents = boxBounds(box)
  return {
    "name": box.name, "liquid": spec["liquid"], "body": spec["body"], "handEdited": isHandEdited(box),
    "minimum": [round(c - h, 2) for c, h in zip(center, halfExtents)], "maximum": [round(c + h, 2) for c, h in zip(center, halfExtents)],
  }


def describeBody(body, ground):
  """A body's swim state (boxed, changed since accepted, not swimmable, undecided) and findings to look at, never errors: cells left uncovered, tops away from the surface, boxes mostly over dry ground or without water over them."""
  definition = bridgeWater.readDefinition(body)
  boxes = [box for box in swimBoxes() if readBox(box)["body"] == body.name]
  if definition.get("swimmable") is False:
    return {"state": "notSwimmable", "boxes": [box.name for box in boxes], "findings": []}
  if not boxes:
    return {"state": "undecided", "boxes": [], "findings": []}
  cells, spacing = bodyCells(body, ground)
  fingerprint = bodyFingerprint(body, cells)
  state = "boxed" if all(readBox(box)["fingerprint"] == fingerprint for box in boxes) else "changed"
  bounds = [boxBounds(box) for box in boxes]
  findings = []
  uncovered = [cell for cell, (level, _, _) in cells.items() if not any(insideBox((cell[0] + 0.5) * spacing, (cell[1] + 0.5) * spacing, level - volumeTolerance / 2, center, half) for center, half in bounds)]
  if uncovered:
    findings.append({"finding": "surface uncovered", "cells": len(uncovered), "at": [[round((i + 0.5) * spacing, 1), round((j + 0.5) * spacing, 1)] for i, j in uncovered[:findingSamples]]})
  surface = bridgeMeshAccess.worldTree([body])
  above = float(bridgeMeshAccess.readVertexArrays(body)[0][:, 2].max()) + 1
  for box, (center, half) in zip(boxes, bounds):
    top = center[2] + half[2]
    levels = [level for cell, (level, _, _) in cells.items() if insideBox((cell[0] + 0.5) * spacing, (cell[1] + 0.5) * spacing, center[2], center, half)]
    if levels and max(abs(top - level) for level in levels) > volumeTolerance:
      findings.append({"finding": "top away from the surface", "box": box.name, "top": round(top, 2), "surface": [round(min(levels), 2), round(max(levels), 2)]})
    samples = [(x, y) for x in numpy.linspace(center[0] - half[0], center[0] + half[0], 5)[1:-1] for y in numpy.linspace(center[1] - half[1], center[1] + half[1], 5)[1:-1]]
    dry = [[round(float(x), 1), round(float(y), 1)] for x, y in samples if (height := ground.heightBelow(x, y, top + 1)) is not None and height >= top]
    # A box at a shore always meets some bank; one mostly over ground holds dry land.
    if len(dry) >= dryShare * len(samples):
      findings.append({"finding": "mostly over dry ground", "box": box.name, "at": dry[:findingSamples]})
    unwatered = [[round(float(x), 1), round(float(y), 1)] for x, y in samples if not waterOver(surface, ground, x, y, above)]
    if len(unwatered) >= dryShare * len(samples):
      findings.append({"finding": "mostly without water over it", "box": box.name, "at": unwatered[:findingSamples]})
  return {"state": state, "boxes": [box.name for box in boxes], "findings": findings}


def waterOver(surface, ground, x, y, above):
  """Whether the body's surface (a BVH over it) stands over [x, y] with water under it down to the bed."""
  location, _, _, _ = surface.ray_cast(mathutils.Vector((x, y, above)), down, bridgeMeshAccess.waterReach)
  return location is not None and bool(ground.depth(x, y, location.z))


def insideBox(x, y, z, center, half):
  return all(abs(value - c) <= h for value, c, h in zip((x, y, z), center, half))


def getSwimVolumes(name):
  """Every swim volume (or one) and every pool and river's swim state with its findings."""
  bpy.context.view_layer.update()
  boxes = swimBoxes()
  if name is not None:
    boxes = [box for box in boxes if box.name == name]
    if not boxes:
      raise ValueError(f"No swim volume '{name}'; swim volumes: {[box.name for box in swimBoxes()]}")
  bodies = swimBodies()
  ground = bridgeWater.Ground() if bodies else None
  return {
    "volumes": [describeBox(box) for box in boxes],
    "bodies": [{"body": body.name} | describeBody(body, ground) for body in bodies] if name is None else [],
    "errors": structuralErrors(),
  }


def structuralErrors():
  """What no zone file can hold: boxes turned or without size, names the client mixes up, prefixes against their liquid, bodies gone, boxes of a body no one swims in."""
  errors = []
  swimmable = {body.name: bridgeWater.readDefinition(body).get("swimmable") is not False for body in swimBodies()}
  bodies = set(swimmable)
  seen = {}
  for box in swimBoxes():
    spec = readBox(box)
    if any(abs(angle) > 1e-9 for angle in box.matrix_world.to_euler()):
      errors.append(f"'{box.name}' is turned; swim volumes stay square to the axes")
    if min(box.scale) <= 0:
      errors.append(f"'{box.name}' has a half extent of 0 or less: {[round(value, 3) for value in box.scale]}")
    if not box.name.startswith(volumePrefixes[spec["liquid"]]):
      errors.append(f"'{box.name}' holds {spec['liquid']} but its name does not start with {volumePrefixes[spec['liquid']]}")
    if spec["body"] is not None and spec["body"] not in bodies:
      errors.append(f"'{box.name}' belongs to '{spec['body']}', which is not a rendered pool or river")
    elif spec["body"] is not None and not swimmable[spec["body"]]:
      errors.append(f"'{box.name}' belongs to '{spec['body']}', which is marked not swimmable; delete the box or mark the body swimmable (editWater swimmable true)")
    seen.setdefault(box.name.lower(), []).append(box.name)
  errors += [f"Swim volumes {names} share a name once lowercased" for names in seen.values() if len(names) > 1]
  return errors


def swimRegions():
  """The .zon regions export writes, from the boxes as they stand."""
  bpy.context.view_layer.update()
  errors = structuralErrors()
  if errors:
    raise ValueError("Swim volumes a zone file cannot hold: " + "; ".join(errors))
  regions = []
  for box in swimBoxes():
    center, halfExtents = boxBounds(box)
    regions.append({"name": box.name, "center": center, "halfExtents": halfExtents})
  return regions


def swimDecisions():
  """The pools and rivers whose swimming is undecided (no boxes and not marked not swimmable) or changed since their boxes were accepted."""
  bodies = swimBodies()
  decisions = {"undecided": [], "changed": []}
  ground = bridgeWater.Ground() if bodies else None
  for body in bodies:
    state = describeBody(body, ground)["state"]
    if state in decisions:
      decisions[state].append(body)
  return decisions


def boxCorners(box):
  center, half = boxBounds(box)
  return [[round(c - h, 2) for c, h in zip(center, half)], [round(c + h, 2) for c, h in zip(center, half)]]


commands = {
  "buildSwimVolumes": (buildSwimVolumes, True),
  "placeSwimVolume": (placeSwimVolume, True),
  "acceptSwimVolumes": (acceptSwimVolumes, True),
  "getSwimVolumes": (getSwimVolumes, False),
}
