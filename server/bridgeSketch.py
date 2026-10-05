"""Sketches: plan-view shapes an artist draws to think a layout through (areas, building footprints, paths, points, notes), kept in
the work file on named sheets and measured against the ground under them. They are aids, never a plan the zone is held to: nothing
checks the zone against a sketch, export leaves them out, and players do not stand on them. Runs under Blender's Python."""
import json
import math
import re

import bpy
import mathutils
import numpy

import bridgeAuthoring
import bridgeBoundaries
import bridgeCaveRuns
import bridgeCaves
import bridgeHousing
import bridgeMeshAccess
import bridgeObjects
import bridgeSurfacing
import bridgeSwim
import bridgeWater

sketchProperty = "zonewrightSketch"
sketchKinds = ("area", "footprint", "path", "point", "note")
namePattern = re.compile(r"^[A-Za-z0-9_-]+$")
shapeKeys = {"name", "kind", "outline", "rectangle", "points", "at", "width", "floor", "height", "facingDegrees", "label", "note"}
massingMaterialName = "zonewrightSketchMassing"
massingVersion = 2
massingColor = (0.82, 0.8, 0.76)
# Massing shades each face by the way it faces, the same from every view: tops lightest, then north and south (+X and -X), then east and
# west.
massingShade = {"base": 0.45, "up": 0.4, "northSouth": 0.25, "eastWest": 0.1}
# A boundary face whose plan covers less than this share of its own area stands upright: a wall, drawn in plan as a line.
uprightPlanShare = 0.01
planLayers = ("regions", "plots", "water", "swim", "boundaries", "zoneLines", "caves")
# Ground under a shape is sampled on a grid at least this fine, and no finer than this many samples.
groundSampleSpacing = 4.0
groundSampleLimit = 2500
pathSampleSpacing = 4.0
sketchLift = 0.25


def collectionName(sheet):
  return f"sketch {sheet}"


def objectName(sheet, name):
  return f"sketch.{sheet}.{name}"


def sketchObjects(sheet=None):
  found = [sceneObject for sceneObject in bpy.context.scene.objects if sketchProperty in sceneObject]
  if sheet is not None:
    found = [sceneObject for sceneObject in found if readSpec(sceneObject)["sheet"] == sheet]
  return sorted(found, key=lambda sceneObject: (readSpec(sceneObject)["sheet"], readSpec(sceneObject)["name"]))


def readSpec(sceneObject):
  return json.loads(sceneObject[sketchProperty])


def requireName(kind, value):
  if not isinstance(value, str) or not namePattern.match(value):
    raise ValueError(f"A sketch {kind} name is letters, digits, '_' and '-', got {value!r}")


def rectangleOutline(rectangle):
  if set(rectangle) != {"center", "size", "headingDegrees"} or len(rectangle["center"]) != 2 or len(rectangle["size"]) != 2 or min(rectangle["size"]) <= 0:
    raise ValueError(f"A rectangle is {{center: [x, y], size: [across, along], headingDegrees}} with a positive size, got {rectangle!r}")
  heading = math.radians(rectangle["headingDegrees"])
  front = numpy.array([math.sin(heading), math.cos(heading)])
  right = numpy.array([front[1], -front[0]])
  center = numpy.array(rectangle["center"], dtype=numpy.float64)
  halfAcross, halfAlong = rectangle["size"][0] / 2, rectangle["size"][1] / 2
  return [list(center + right * a * halfAcross + front * b * halfAlong) for a, b in ((1, 1), (-1, 1), (-1, -1), (1, -1))]


def polygonArea(outline):
  x, y = outline[:, 0], outline[:, 1]
  return 0.5 * float(numpy.dot(x, numpy.roll(y, -1)) - numpy.dot(y, numpy.roll(x, -1)))


def requireOutline(outline, label):
  points = numpy.array(outline, dtype=numpy.float64)
  if points.ndim != 2 or points.shape[1] != 2 or len(points) < 3:
    raise ValueError(f"{label}: an outline is at least three [x, y] points, got {outline!r}")
  edges = [(points[index], points[(index + 1) % len(points)]) for index in range(len(points))]
  for first in range(len(edges)):
    for second in range(first + 2, len(edges)):
      if first == 0 and second == len(edges) - 1:
        continue
      if segmentsCross(*edges[first], *edges[second]):
        raise ValueError(f"{label}: the outline crosses itself between points {first} and {second}")
  if abs(polygonArea(points)) < 1e-6:
    raise ValueError(f"{label}: the outline encloses no area")
  return points


def segmentsCross(a, b, c, d):
  def side(p, q, r):
    return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])
  return side(a, b, c) * side(a, b, d) < 0 and side(c, d, a) * side(c, d, b) < 0


def normalizedShape(sheet, shape):
  """A shape as given, checked, with its plan geometry: an outline for areas and footprints, points for paths, a spot for points and notes."""
  if not isinstance(shape, dict) or not shapeKeys >= set(shape):
    raise ValueError(f"A sketch shape takes {sorted(shapeKeys)}, got {shape!r}")
  requireName("shape", shape.get("name"))
  kind = shape.get("kind")
  if kind not in sketchKinds:
    raise ValueError(f"Shape '{shape['name']}': kind must be one of {list(sketchKinds)}, got {kind!r}")
  label = f"Shape '{shape['name']}' ({kind})"
  given = {key for key in ("outline", "rectangle", "points", "at") if key in shape}
  allowed = {"area": [{"outline"}, {"rectangle"}], "footprint": [{"outline"}, {"rectangle"}], "path": [{"points"}], "point": [{"at"}], "note": [{"at"}]}[kind]
  if given not in allowed:
    raise ValueError(f"{label} takes {' or '.join(sorted(next(iter(option))) for option in allowed)}, got {sorted(given)}")
  spec = {"sheet": sheet, "name": shape["name"], "kind": kind}
  for key in ("label", "note"):
    if key in shape:
      if not isinstance(shape[key], str):
        raise ValueError(f"{label}: {key} is text, got {shape[key]!r}")
      spec[key] = shape[key]
  if kind == "note" and not spec.get("label"):
    raise ValueError(f"{label}: a note needs a label, its text")
  for key in ("floor", "height", "width", "facingDegrees"):
    if key in shape:
      if not isinstance(shape[key], (int, float)) or isinstance(shape[key], bool):
        raise ValueError(f"{label}: {key} is a number, got {shape[key]!r}")
      spec[key] = float(shape[key])
  if "height" in spec and (kind != "footprint" or spec["height"] <= 0):
    raise ValueError(f"{label}: height is a footprint's positive height")
  if "width" in spec and (kind != "path" or spec["width"] <= 0):
    raise ValueError(f"{label}: width is a path's positive width")
  if "floor" in spec and kind not in ("area", "footprint"):
    raise ValueError(f"{label}: floor is the height an area or footprint is graded to")
  if "facingDegrees" in spec and kind not in ("area", "footprint", "point"):
    raise ValueError(f"{label}: facingDegrees is the way an area, footprint, or point faces")
  if kind in ("area", "footprint"):
    outline = rectangleOutline(shape["rectangle"]) if "rectangle" in shape else shape["outline"]
    if "rectangle" in shape and "facingDegrees" not in spec:
      spec["facingDegrees"] = float(shape["rectangle"]["headingDegrees"]) % 360
    geometry = [[float(x), float(y), None] for x, y in requireOutline(outline, label)]
  elif kind == "path":
    points = shape["points"]
    if len(points) < 2 or any(len(point) not in (2, 3) for point in points):
      raise ValueError(f"{label}: points are at least two [x, y] or [x, y, z]")
    geometry = [[float(point[0]), float(point[1]), float(point[2]) if len(point) == 3 else None] for point in points]
  else:
    if len(shape["at"]) not in (2, 3):
      raise ValueError(f"{label}: at is [x, y] or [x, y, z]")
    geometry = [[float(shape["at"][0]), float(shape["at"][1]), float(shape["at"][2]) if len(shape["at"]) == 3 else None]]
  return spec, geometry


class GroundProbe:
  """The top of what players stand on, and water over it, sampled from above."""

  def __init__(self):
    self.surfaces = bridgeMeshAccess.PlayerSurfaces()
    self.water = bridgeMeshAccess.swimSurfaces()
    self.top = bridgeMeshAccess.sceneTopHeight() + 10
    self.reach = self.top + 100000

  def height(self, x, y):
    hit = self.surfaces.footingBelow(mathutils.Vector((x, y, self.top)), self.reach)
    return None if hit is None else hit.z

  def waterDepth(self, x, y, z):
    return bridgeMeshAccess.waterDepthAt(self.water, (x, y, z))


def insidePolygon(points, outline):
  """Which of points (n x 2) lie inside a polygon (m x 2), by crossings."""
  inside = numpy.zeros(len(points), dtype=bool)
  x, y = points[:, 0], points[:, 1]
  for index in range(len(outline)):
    (x1, y1), (x2, y2) = outline[index], outline[(index - 1) % len(outline)]
    crosses = ((y1 > y) != (y2 > y)) & (x < (x2 - x1) * (y - y1) / numpy.where(y2 == y1, 1e-12, y2 - y1) + x1)
    inside ^= crosses
  return inside


def groundUnder(probe, outline):
  """Heights of the ground on a grid inside an outline, with the grid's spacing: rows by y, columns by x, NaN outside or where nothing lies."""
  area = abs(polygonArea(outline))
  spacing = max(groundSampleSpacing, math.sqrt(area / groundSampleLimit))
  low, high = outline.min(axis=0), outline.max(axis=0)
  xs = numpy.arange(low[0] + spacing / 2, high[0], spacing)
  ys = numpy.arange(low[1] + spacing / 2, high[1], spacing)
  if not len(xs) or not len(ys):
    xs, ys = numpy.array([outline[:, 0].mean()]), numpy.array([outline[:, 1].mean()])
  grid = numpy.stack(numpy.meshgrid(xs, ys), axis=-1)
  inside = insidePolygon(grid.reshape(-1, 2), outline).reshape(grid.shape[:2])
  heights = numpy.full(grid.shape[:2], numpy.nan)
  depths = numpy.full(grid.shape[:2], numpy.nan)
  for row, column in zip(*numpy.nonzero(inside)):
    x, y = grid[row, column]
    z = probe.height(x, y)
    if z is not None:
      heights[row, column] = z
      depth = probe.waterDepth(x, y, z)
      depths[row, column] = 0.0 if depth is None else depth
  return heights, depths, spacing


def steepestDegrees(heights, spacing):
  rises = [numpy.abs(numpy.diff(heights, axis=axis)) for axis in (0, 1)]
  steepest = max((float(numpy.nanmax(rise)) for rise in rises if rise.size and not numpy.all(numpy.isnan(rise))), default=0.0)
  return round(math.degrees(math.atan2(steepest, spacing)), 1)


def rounded(value, digits=1):
  """A measure rounded to digits, never negative zero."""
  return round(float(value), digits) + 0.0


def roundPoint(point):
  return [rounded(component) for component in point]


def worldGeometry(sceneObject):
  """The shape's plan points in world space: an outline's corners, a path's points, or a point's spot, with their heights."""
  spec = readSpec(sceneObject)
  vertices = [sceneObject.matrix_world @ vertex.co for vertex in sceneObject.data.vertices]
  return [list(vertex) for vertex in vertices[:spec["count"]]]


def measureShape(sceneObject, probe, others):
  spec = readSpec(sceneObject)
  geometry = numpy.array(worldGeometry(sceneObject))
  measured = {}
  if spec["kind"] in ("area", "footprint"):
    outline = geometry[:, :2]
    heights, depths, spacing = groundUnder(probe, outline)
    found = heights[~numpy.isnan(heights)]
    measured["area"] = rounded(abs(polygonArea(outline)))
    measured["centroid"] = roundPoint(outline.mean(axis=0))
    measured["bounds"] = [roundPoint(outline.min(axis=0)), roundPoint(outline.max(axis=0))]
    if len(found):
      measured["ground"] = {
        "lowest": rounded(found.min()), "mean": rounded(found.mean()), "highest": rounded(found.max()),
        "steepestDegrees": steepestDegrees(heights, spacing), "sampleSpacing": rounded(spacing),
        "underWater": rounded((depths[~numpy.isnan(depths)] > 0).mean(), 3),
      }
      if "floor" in spec:
        measured["ground"]["cut"] = rounded(max(0.0, float(found.max()) - spec["floor"]))
        measured["ground"]["fill"] = rounded(max(0.0, spec["floor"] - float(found.min())))
    else:
      measured["ground"] = None
    measured["overlaps"] = sorted(name for name, otherOutline in others if name != spec["name"] and outlinesOverlap(outline, otherOutline))
    distances = [(polygonDistance(outline, otherOutline), name) for name, otherOutline in others if name != spec["name"]]
    nearest = min(distances, default=None)
    measured["nearest"] = None if nearest is None else {"shape": nearest[1], "gap": rounded(nearest[0])}
  elif spec["kind"] == "path":
    samples, along = [], 0.0
    for start, end in zip(geometry[:-1], geometry[1:]):
      run = float(numpy.hypot(*(end[:2] - start[:2])))
      steps = max(1, math.ceil(run / pathSampleSpacing))
      for step in range(steps):
        point = start[:2] + (end[:2] - start[:2]) * step / steps
        samples.append((along + run * step / steps, probe.height(*point)))
      along += run
    samples.append((along, probe.height(*geometry[-1][:2])))
    found = [(distance, z) for distance, z in samples if z is not None]
    grades = [math.degrees(math.atan2(abs(z2 - z1), d2 - d1)) for (d1, z1), (d2, z2) in zip(found[:-1], found[1:]) if d2 > d1]
    measured["length"] = rounded(along)
    measured["ground"] = None if not found else {
      "start": rounded(found[0][1]), "end": rounded(found[-1][1]), "lowest": rounded(min(z for _, z in found)), "highest": rounded(max(z for _, z in found)),
      "steepestDegrees": rounded(max(grades, default=0.0)),
    }
    width = spec.get("width", 0.0)
    measured["crosses"] = sorted(name for name, otherOutline in others if pathMeetsOutline(geometry[:, :2], width / 2, otherOutline))
  else:
    x, y = geometry[0][:2]
    z = probe.height(x, y)
    measured["ground"] = None if z is None else rounded(z)
  return measured


def outlinesOverlap(first, second):
  edgesCross = any(
    segmentsCross(first[i], first[(i + 1) % len(first)], second[j], second[(j + 1) % len(second)])
    for i in range(len(first)) for j in range(len(second))
  )
  return edgesCross or bool(insidePolygon(first[:1], second)[0]) or bool(insidePolygon(second[:1], first)[0])


def pointSegmentDistance(points, start, end):
  run = end - start
  share = numpy.clip(((points - start) @ run) / max(float(run @ run), 1e-12), 0, 1)
  return numpy.linalg.norm(points - (start + share[:, None] * run), axis=1)


def polygonDistance(first, second):
  if outlinesOverlap(first, second):
    return 0.0
  gaps = [pointSegmentDistance(first, second[j], second[(j + 1) % len(second)]).min() for j in range(len(second))]
  gaps += [pointSegmentDistance(second, first[i], first[(i + 1) % len(first)]).min() for i in range(len(first))]
  return float(min(gaps))


def pathMeetsOutline(points, halfWidth, outline):
  for start, end in zip(points[:-1], points[1:]):
    if pointSegmentDistance(outline, start, end).min() <= halfWidth or bool(insidePolygon(numpy.array([start]), outline)[0]):
      return True
    if any(segmentsCross(start, end, outline[j], outline[(j + 1) % len(outline)]) for j in range(len(outline))):
      return True
  return False


def outlinesOn(sheet):
  return [(readSpec(sceneObject)["name"], numpy.array(worldGeometry(sceneObject))[:, :2]) for sceneObject in sketchObjects(sheet) if readSpec(sceneObject)["kind"] in ("area", "footprint")]


def massingMaterial():
  """The massing blocks' material, rebuilt when a file holds one from an earlier version of its shading."""
  material = bpy.data.materials.get(massingMaterialName)
  if material is None or material.get("zonewrightVersion") != massingVersion:
    if material is None:
      material = bpy.data.materials.new(massingMaterialName)
    material["zonewrightVersion"] = massingVersion
    material.use_nodes = True
    nodes, links = material.node_tree.nodes, material.node_tree.links
    nodes.clear()
    geometry = nodes.new("ShaderNodeNewGeometry")
    normal = nodes.new("ShaderNodeSeparateXYZ")
    links.new(geometry.outputs["Normal"], normal.inputs["Vector"])
    shade = nodes.new("ShaderNodeValue")
    shade.outputs["Value"].default_value = massingShade["base"]
    total = shade.outputs["Value"]
    for axis, weight, operation in (("Z", massingShade["up"], "MAXIMUM"), ("X", massingShade["northSouth"], "ABSOLUTE"), ("Y", massingShade["eastWest"], "ABSOLUTE")):
      part = nodes.new("ShaderNodeMath")
      part.operation = operation
      links.new(normal.outputs[axis], part.inputs[0])
      part.inputs[1].default_value = 0.0
      added = nodes.new("ShaderNodeMath")
      added.operation = "MULTIPLY_ADD"
      links.new(part.outputs["Value"], added.inputs[0])
      added.inputs[1].default_value = weight
      links.new(total, added.inputs[2])
      total = added.outputs["Value"]
    shaded = nodes.new("ShaderNodeVectorMath")
    shaded.operation = "SCALE"
    shaded.inputs[0].default_value = massingColor
    links.new(total, shaded.inputs["Scale"])
    emission = nodes.new("ShaderNodeEmission")
    links.new(shaded.outputs["Vector"], emission.inputs["Color"])
    output = nodes.new("ShaderNodeOutputMaterial")
    links.new(emission.outputs["Emission"], output.inputs["Surface"])
  return material


def shapeMesh(name, spec, geometry, probe):
  """The shape in the scene: a footprint with a height as a block from the lowest ground under it to its height above its floor (or
  that ground), so it stands on a plinth where the ground falls away, drawn in views with the guides; anything else as its outline,
  path, or spot, which views do not draw."""
  kind = spec["kind"]
  mesh = bpy.data.meshes.new(name)
  if kind in ("area", "footprint"):
    outline = numpy.array(geometry)[:, :2]
    heights, _, _ = groundUnder(probe, outline)
    found = heights[~numpy.isnan(heights)]
    lowest = float(found.min()) if len(found) else spec.get("floor", 0.0)
    base = spec.get("floor", lowest)
    if polygonArea(outline) < 0:
      outline = outline[::-1]
    count = len(outline)
    bottom = [(x, y, min(base, lowest) + sketchLift) for x, y in outline]
    if kind == "footprint" and "height" in spec:
      top = [(x, y, base + spec["height"]) for x, y in outline]
      faces = [tuple(range(count))[::-1], tuple(range(count, 2 * count))]
      faces += [(index, (index + 1) % count, count + (index + 1) % count, count + index) for index in range(count)]
      mesh.from_pydata(bottom + top, [], faces)
      mesh.materials.append(massingMaterial())
    else:
      mesh.from_pydata(bottom, [(index, (index + 1) % count) for index in range(count)], [])
  elif kind == "path":
    points = []
    for x, y, z in geometry:
      ground = probe.height(x, y) if z is None else z
      points.append((x, y, (ground if ground is not None else 0.0) + sketchLift))
    count = len(points)
    mesh.from_pydata(points, [(index, index + 1) for index in range(count - 1)], [])
  else:
    x, y, z = geometry[0]
    ground = probe.height(x, y) if z is None else z
    count = 1
    mesh.from_pydata([(x, y, (ground if ground is not None else 0.0) + sketchLift)], [], [])
  mesh.validate()
  return mesh, count


def sketchShapes(sheet, shapes):
  """Add shapes to a sheet, or redraw those whose names it already has; returns each with its measures."""
  requireName("sheet", sheet)
  if not shapes:
    raise ValueError("sketch needs at least one shape")
  normalized = [normalizedShape(sheet, shape) for shape in shapes]
  names = [spec["name"] for spec, _ in normalized]
  repeated = sorted({name for name in names if names.count(name) > 1})
  if repeated:
    raise ValueError(f"Shape names repeat within the call: {repeated}")
  probe = GroundProbe()
  collection = bridgeObjects.targetCollection(collectionName(sheet))
  placed = []
  for spec, geometry in normalized:
    name = objectName(sheet, spec["name"])
    existing = bpy.data.objects.get(name)
    if existing is not None:
      if sketchProperty not in existing:
        raise ValueError(f"An object named '{name}' already exists and is not a sketch shape")
      oldMesh = existing.data
      bpy.data.objects.remove(existing)
      bpy.data.meshes.remove(oldMesh)
    mesh, count = shapeMesh(name, spec, geometry, probe)
    shapeObject = bpy.data.objects.new(name, mesh)
    shapeObject[sketchProperty] = json.dumps(spec | {"count": count})
    shapeObject[bridgeMeshAccess.guideProperty] = "sketch"
    collection.objects.link(shapeObject)
    placed.append(shapeObject)
  bpy.context.view_layer.update()
  others = outlinesOn(sheet)
  return {"sheet": sheet, "shapes": [describeShape(shapeObject, probe, others) for shapeObject in placed]}


def describeShape(sceneObject, probe, others):
  spec = readSpec(sceneObject)
  geometry = worldGeometry(sceneObject)
  key = {"area": "outline", "footprint": "outline", "path": "points", "point": "at", "note": "at"}[spec["kind"]]
  plan = [roundPoint(point[:2]) for point in geometry] if key != "at" else roundPoint(geometry[0][:2])
  return {key: value for key, value in spec.items() if key not in ("sheet", "count")} | {"object": sceneObject.name, key: plan} | measureShape(sceneObject, probe, others)


def eraseSketch(sheet, names, wholeSheet):
  requireName("sheet", sheet)
  shapes = sketchObjects(sheet)
  if not shapes:
    raise ValueError(f"No sketch sheet '{sheet}'; sheets: {sorted({readSpec(shape)['sheet'] for shape in sketchObjects()})}")
  if wholeSheet == (names is not None):
    raise ValueError("Give names, the shapes to erase, or wholeSheet true")
  if names is not None:
    byName = {readSpec(shape)["name"]: shape for shape in shapes}
    missing = sorted(set(names) - set(byName))
    if missing:
      raise ValueError(f"Sheet '{sheet}' has no shapes {missing}; it has {sorted(byName)}")
    shapes = [byName[name] for name in names]
  for shape in shapes:
    mesh = shape.data
    bpy.data.objects.remove(shape)
    bpy.data.meshes.remove(mesh)
  collection = bpy.data.collections.get(collectionName(sheet))
  if collection is not None and not collection.all_objects:
    bpy.data.collections.remove(collection)
  return {"sheet": sheet, "erased": len(shapes), "remaining": len(sketchObjects(sheet))}


def getSketch(sheet):
  if sheet is not None:
    requireName("sheet", sheet)
  shapes = sketchObjects(sheet)
  if sheet is not None and not shapes:
    raise ValueError(f"No sketch sheet '{sheet}'; sheets: {sorted({readSpec(shape)['sheet'] for shape in sketchObjects()})}")
  bpy.context.view_layer.update()
  probe = GroundProbe()
  sheets = {}
  for shape in shapes:
    sheets.setdefault(readSpec(shape)["sheet"], []).append(shape)
  return {"sheets": [{"sheet": name, "shapes": [describeShape(shape, probe, outlinesOn(name)) for shape in members]} for name, members in sorted(sheets.items())]}


def planOverlays(sheets, layers, spots):
  """What a plan drawing lays over the base: the sheets' shapes, the plan's own regions, plots, water, swim volumes, boundaries, and
  zone lines, and the ground's height at each of spots ([x, y]; those over no ground are left out), in plan coordinates."""
  known = {readSpec(shape)["sheet"] for shape in sketchObjects()}
  if sheets is not None:
    missing = sorted(set(sheets) - known)
    if missing:
      raise ValueError(f"No sketch sheets {missing}; sheets: {sorted(known)}")
  chosen = sorted(known) if sheets is None else sheets
  unknownLayers = sorted(set(layers) - set(planLayers))
  if unknownLayers:
    raise ValueError(f"layers are {list(planLayers)}; got {unknownLayers}")
  bpy.context.view_layer.update()
  overlays = {"sheets": [], "regions": [], "plots": [], "water": [], "swim": [], "boundaries": [], "zoneLines": [], "caves": [], "spots": []}
  if spots:
    probe = GroundProbe()
    for x, y in spots:
      height = probe.height(x, y)
      if height is not None:
        overlays["spots"].append({"at": [x, y], "height": rounded(height)})
  for sheet in chosen:
    shapes = []
    for shape in sketchObjects(sheet):
      spec = readSpec(shape)
      shapes.append({key: value for key, value in spec.items() if key not in ("sheet", "count")} | {"plan": [roundPoint(point[:2]) for point in worldGeometry(shape)]})
    overlays["sheets"].append({"sheet": sheet, "shapes": shapes})
  if "caves" in layers:
    overlays["caves"] = planCaves()
  if "regions" in layers:
    overlays["regions"] = [{"name": region["name"], "outline": region["outline"]} for region in bridgeAuthoring.getRegions()["regions"]]
  if "plots" in layers:
    overlays["plots"] = [
      {"address": plot.name, "corners": [roundPoint(corner) for corner in bridgeHousing.footprint(plot)], "facingDegrees": round(bridgeHousing.facingOf(plot), 1)}
      for plot in bridgeHousing.plotObjects()
    ]
  if "swim" in layers:
    overlays["swim"] = [{"name": box.name, "liquid": bridgeSwim.readBox(box)["liquid"], "corners": bridgeSwim.boxCorners(box)} for box in bridgeSwim.swimBoxes()]
  if "boundaries" in layers:
    overlays["boundaries"] = [boundaryPlan(boundary) for boundary in bridgeBoundaries.boundaryObjects() if boundary.type == "MESH"]
  if "zoneLines" in layers:
    overlays["zoneLines"] = [{"name": line.name, "corners": [bridgeBoundaries.boxCorners(line)[corner][:2] for corner in (0, 1, 3, 2)]} for line in bridgeBoundaries.zoneLineObjects()]
  if "water" in layers:
    bodies = renderedWater()
    ground = bridgeWater.Ground() if bodies else None
    for body in bodies:
      liquid = bridgeSurfacing.liquidOf(body.material_slots[0].material)["liquid"]
      if bridgeWater.readDefinition(body)["kind"] == "fall":
        positions, triangles = bridgeMeshAccess.worldTriangles([body])
        shown = {"runs": [], "loops": numpy.round(positions[triangles][:, :, :2], 1).tolist()}
      else:
        shown = bridgeWater.visibleSurface(body, ground)
      overlays["water"].append({"name": body.name, "liquid": liquid} | shown)
  return overlays


def offsetLine(samples, offsets):
  """A run's centerline samples ([x, y, floor, width, height, along] rows) moved across it by offsets (one per sample, negative to the
  left looking along it), in plan."""
  points = samples[:, :2]
  directions = numpy.gradient(points, axis=0)
  directions /= numpy.maximum(numpy.linalg.norm(directions, axis=1, keepdims=True), 1e-9)
  right = numpy.column_stack([directions[:, 1], -directions[:, 0]])
  return points + right * numpy.asarray(offsets)[:, None]


def planCaves():
  """The caves' runs in plan (bridgeCaves.caveGuides): each run's walls at floor height, its floor height at each path point, its
  junctions, the outlines of its floor strokes, its name, and its mean floor height (runs are drawn lowest first)."""
  drawn = []
  for guide in bridgeCaves.caveGuides():
    for run in guide["runs"]:
      samples = numpy.array(run["samples"])
      halves = samples[:, 3] / 2
      stations = numpy.array(run["stations"])
      spots = samples[numpy.abs(samples[:, 5][None, :] - stations[:, None]).argmin(axis=1)]
      strokes = []
      for stroke in run["strokes"]:
        if stroke["kind"] == "rough":
          strokes.append({"name": stroke["name"], "kind": "rough", "outline": stroke["outline"]})
          continue
        inside = samples[(samples[:, 5] >= stroke["from"] - 1e-6) & (samples[:, 5] <= stroke["to"] + 1e-6)]
        if len(inside) < 2:
          continue
        left = offsetLine(inside, numpy.full(len(inside), stroke["across"][0]))
        right = offsetLine(inside, numpy.full(len(inside), stroke["across"][1]))
        strokes.append({"name": stroke["name"], "kind": stroke["kind"], "outline": numpy.round(numpy.vstack([left, right[::-1]]), 2).tolist()})
      drawn.append({
        "cave": guide["cave"], "run": run["run"], "label": guide["cave"] + ("" if run["run"] == bridgeCaveRuns.mainRun else f" {run['run']}"),
        "left": numpy.round(offsetLine(samples, -halves), 2).tolist(), "right": numpy.round(offsetLine(samples, halves), 2).tolist(),
        "middle": numpy.round(samples[:, :2], 2).tolist(), "floors": [{"at": roundPoint(spot[:2]), "floor": rounded(spot[2], 1)} for spot in spots],
        "junction": roundPoint(samples[0, :2]) if run["from"] is not None else None, "opening": roundPoint(samples[-1, :2]) if run["into"] is not None else None, "strokes": strokes, "meanFloor": rounded(float(samples[:, 2].mean()), 2),
      })
  return drawn


def boundaryPlan(boundary):
  """A boundary in plan: the faces seen from above as areas (a lid, a floor) and the upright ones as lines along their foot (a wall)."""
  positions, triangles = bridgeMeshAccess.worldTriangles([boundary])
  corners = positions[triangles]
  firstEdge, secondEdge = corners[:, 1] - corners[:, 0], corners[:, 2] - corners[:, 0]
  planArea = numpy.abs(firstEdge[:, 0] * secondEdge[:, 1] - firstEdge[:, 1] * secondEdge[:, 0]) / 2
  upright = planArea <= uprightPlanShare * numpy.linalg.norm(numpy.cross(firstEdge, secondEdge), axis=1) / 2
  lines = []
  for triangle in corners[upright][:, :, :2]:
    pairs = [(triangle[a], triangle[b]) for a, b in ((0, 1), (1, 2), (2, 0))]
    start, end = max(pairs, key=lambda pair: numpy.linalg.norm(pair[1] - pair[0]))
    lines.append([roundPoint(start), roundPoint(end)])
  areas = [[roundPoint(point) for point in triangle] for triangle in corners[~upright][:, :, :2]]
  return {"name": boundary.name, "kind": bridgeBoundaries.readSpec(boundary, bridgeMeshAccess.boundaryProperty)["kind"], "lines": lines, "areas": areas}


def renderedWater():
  return [body for body in bpy.context.scene.objects if bridgeMeshAccess.waterProperty in body and not body.hide_render]


sectionLayers = ("ground", "water", "swim", "massing", "sketch", "plots", "boundaries", "zoneLines", "caves")
# A cave run's section runs on past each end by at least this, so the ground in front of a mouth shows.
caveSectionLead = 30.0
# A run goes along a section where its middle stays within this, or a quarter of its width, of the section's line.
sectionAlongLateral = 2.0
# A section fitted to the ground it cuts reaches past it by at least this above and below.
sectionFitMargin = 10.0
# Where a run crosses a section's line at a bend of the line, both legs meeting there find the crossing; closer than this it is one.
crossingSeparation = 0.5
# Cuts reaching past the drawing by this share of its size are dropped; the drawing clips the rest.
sectionMargin = 0.1
waterSectionStep = 0.5
# A path's stretch within this angle of a section's line runs along it, and the section shows its rise and fall; one crossing it at a
# steeper angle shows level across its width. A path without a width runs along the line when it lies this close to it.
alongDegrees = 15.0
alongTolerance = 1.0


def insideStretches(outline, start, along, normal, length):
  """The stretches [s0, s1] of a section's line, from 0 to length, that lie inside a closed outline."""
  offsets = numpy.asarray(outline, dtype=numpy.float64)[:, :2] - start
  sides, distances = offsets @ normal, offsets @ along
  crossings = []
  for index in range(len(offsets)):
    following = (index + 1) % len(offsets)
    if (sides[index] > 0) != (sides[following] > 0):
      share = sides[index] / (sides[index] - sides[following])
      crossings.append(float(distances[index] + share * (distances[following] - distances[index])))
  crossings.sort()
  return [[max(0.0, s0), min(length, s1)] for s0, s1 in zip(crossings[0::2], crossings[1::2]) if min(length, s1) > max(0.0, s0)]


def plotCut(plot, start, along, normal, length):
  """Where a section's line crosses a plot: its pad, level at the plot's height, and where its entrance side is crossed and which way
  it faces along the line."""
  corners = bridgeHousing.footprint(plot)
  stretches = insideStretches(corners, start, along, normal, length)
  if not stretches:
    return None
  sides = (corners[:2] - start) @ normal
  entrance = None
  if (sides[0] > 0) != (sides[1] > 0):
    share = sides[0] / (sides[0] - sides[1])
    entrance = rounded((corners[0] + share * (corners[1] - corners[0]) - start) @ along, 2)
  outward = 1 if bridgeHousing.frontDirection(bridgeHousing.facingOf(plot)) @ along > 0 else -1
  return {"name": plot.name, "s": [rounded(value, 2) for value in stretches[0]], "z": rounded(plot.matrix_world.translation.z, 2), "entrance": entrance, "outward": outward}


def pathPieces(points, halfWidth, start, along, normal, length):
  """Where a path meets a section: [s0, z0, s1, z1] pieces, its run where it goes along the line and a level span across its width
  where it crosses it."""
  heights = points[:, 2] - sketchLift
  offsets = points[:, :2] - start
  sides, distances = offsets @ normal, offsets @ along
  pieces = []
  for index in range(len(points) - 1):
    run = float(numpy.hypot(*(points[index + 1, :2] - points[index, :2])))
    if run < 1e-9:
      continue
    sine = abs(sides[index + 1] - sides[index]) / run
    if sine < math.sin(math.radians(alongDegrees)):
      if max(abs(sides[index]), abs(sides[index + 1])) <= max(halfWidth, alongTolerance):
        pieces.append([distances[index], heights[index], distances[index + 1], heights[index + 1]])
    elif (sides[index] > 0) != (sides[index + 1] > 0):
      share = sides[index] / (sides[index] - sides[index + 1])
      middle = distances[index] + share * (distances[index + 1] - distances[index])
      height = heights[index] + share * (heights[index + 1] - heights[index])
      pieces.append([middle - halfWidth / sine, height, middle + halfWidth / sine, height])
  return [[rounded(value, 2) for value in piece] for piece in pieces if max(piece[0], piece[2]) >= 0 and min(piece[0], piece[2]) <= length]


def sketchCuts(start, along, normal, length):
  """Sketch areas with a floor, at that floor where the line lies inside them, and paths (pathPieces), by sheet."""
  found = []
  for shape in sketchObjects():
    spec = readSpec(shape)
    geometry = numpy.array(worldGeometry(shape))
    if spec["kind"] == "area" and "floor" in spec:
      pieces = [[rounded(value, 2) for value in (s0, spec["floor"], s1, spec["floor"])] for s0, s1 in insideStretches(geometry, start, along, normal, length)]
    elif spec["kind"] == "path":
      pieces = pathPieces(geometry, spec.get("width", 0.0) / 2, start, along, normal, length)
    else:
      continue
    if pieces:
      found.append({"sheet": spec["sheet"], "shape": spec["name"], "kind": spec["kind"], "label": spec.get("label") or spec["name"], "pieces": pieces})
  return found


def planeSegments(positions, triangles, start, along, normal):
  """Where triangles cross the vertical plane through start along `along`: segments as [s0, z0, s1, z1], s the distance along."""
  if len(triangles) == 0:
    return numpy.zeros((0, 4))
  offsets = positions[:, :2] - start
  sides = offsets @ normal
  sides[sides == 0] = 1e-9
  distances = offsets @ along
  heights = positions[:, 2]
  triangleSides = sides[triangles]
  crossing = triangles[(triangleSides.min(axis=1) < 0) & (triangleSides.max(axis=1) > 0)]
  ends = []
  for first, second in ((0, 1), (1, 2), (2, 0)):
    a, b = crossing[:, first], crossing[:, second]
    cut = sides[a] * sides[b] < 0
    share = numpy.where(cut, sides[a] / numpy.where(cut, sides[a] - sides[b], 1.0), 0.0)
    ends.append((cut, distances[a] + share * (distances[b] - distances[a]), heights[a] + share * (heights[b] - heights[a])))
  segments = []
  for row in range(len(crossing)):
    points = [(s[row], z[row]) for cut, s, z in ends if cut[row]]
    if len(points) == 2:
      segments.append([points[0][0], points[0][1], points[1][0], points[1][1]])
  return numpy.array(segments).reshape(-1, 4)


def shownWater(segments, start, along, ground):
  """The parts of a water body's section segments players see, not tucked under the ground: sampled every waterSectionStep, each change placed by halving."""
  pieces = []
  for s0, z0, s1, z1 in segments:
    count = max(1, math.ceil(math.hypot(s1 - s0, z1 - z0) / waterSectionStep))

    def shows(share):
      s, z = s0 + share * (s1 - s0), z0 + share * (z1 - z0)
      return not ground.insideRock((*(start + s * along), z))

    shares = [index / count for index in range(count + 1)]
    showing = [shows(share) for share in shares]
    changes = [0.0]
    for index in range(count):
      if showing[index] != showing[index + 1]:
        low, high = shares[index], shares[index + 1]
        for _ in range(bridgeWater.edgeHalvings):
          middle = (low + high) / 2
          low, high = (middle, high) if shows(middle) == showing[index] else (low, middle)
        changes.append((low + high) / 2)
    changes.append(1.0)
    for first, last in zip(changes[:-1], changes[1:]):
      if last > first and shows((first + last) / 2):
        pieces.append([s0 + first * (s1 - s0), z0 + first * (z1 - z0), s0 + last * (s1 - s0), z0 + last * (z1 - z0)])
  return numpy.array(pieces).reshape(-1, 4)


def keptSegments(segments, length, bottom, top):
  margin = sectionMargin * max(length, top - bottom)
  inside = (numpy.maximum(segments[:, 0], segments[:, 2]) >= -margin) & (numpy.minimum(segments[:, 0], segments[:, 2]) <= length + margin)
  inside &= (numpy.maximum(segments[:, 1], segments[:, 3]) >= bottom - margin) & (numpy.minimum(segments[:, 1], segments[:, 3]) <= top + margin)
  return numpy.round(segments[inside], 2).tolist()


def boxCrossing(low, high, start, along):
  """Where a section's line runs through an axis-aligned box in plan, [enter, leave] as distances along it, or None."""
  enter, leave = -math.inf, math.inf
  for axis in (0, 1):
    if abs(along[axis]) < 1e-12:
      if not low[axis] <= start[axis] <= high[axis]:
        return None
      continue
    first, second = (low[axis] - start[axis]) / along[axis], (high[axis] - start[axis]) / along[axis]
    enter, leave = max(enter, min(first, second)), min(leave, max(first, second))
  return [round(enter, 2), round(leave, 2)] if leave > enter else None


def planTurned(vector, angle):
  """A plan vector turned counter-clockwise by angle radians."""
  return numpy.array([vector[0] * math.cos(angle) - vector[1] * math.sin(angle), vector[0] * math.sin(angle) + vector[1] * math.cos(angle)])


def sectionLine(start, end, path, cave):
  """A section's line in plan as a polyline, its bends' marks (each with its distance along and a label), and what it follows: start to
  end, a path [[x, y], ...] (its points marked by index), or a cave run's centerline (cave {objectName, name, run}, arcs in short
  chords), run on straight past each end by a lead so the ground in front of a mouth shows, its stations marked by point index."""
  given = [start is not None or end is not None, path is not None, cave is not None]
  if sum(given) != 1:
    raise ValueError("A section runs from start to end, along a path [[x, y], ...], or along a cave's run (cave {objectName, name, run}): give one of them")
  if cave is not None:
    if not isinstance(cave, dict) or set(cave) - {"objectName", "name", "run"} or not {"objectName", "name"} <= set(cave):
      raise ValueError(f"cave is {{objectName, name, run}} (run 'main' unless a branch is named), got {cave!r}")
    run = cave.get("run", bridgeCaveRuns.mainRun)
    guide = bridgeCaves.guideRun(cave["objectName"], cave["name"], run)
    polyline = numpy.array(guide["polyline"])[:, :2]
    starting, ending = polyline[1] - polyline[0], polyline[-1] - polyline[-2]
    leads = [max(caveSectionLead, guide["samples"][row][3]) for row in (0, -1)]
    points = numpy.vstack([polyline[0] - leads[0] * starting / numpy.linalg.norm(starting), polyline, polyline[-1] + leads[1] * ending / numpy.linalg.norm(ending)])
    marks = [(1 + guide["polylineAlongs"].index(station), str(index)) for index, station in enumerate(guide["stations"])]
    samples = numpy.array(guide["samples"])
    # Fitted, a run's own section reaches from under its lowest floor to a run's height over its highest vault, so the rock over it
    # shows without a mountain above shrinking it.
    reach = (float(samples[:, 2].min()) - 2 * sectionFitMargin, float((samples[:, 2] + samples[:, 4]).max() + samples[:, 4].max()))
    return points, marks, {"cave": cave["name"], "run": run, "object": cave["objectName"], "reach": reach}
  if path is not None:
    if not isinstance(path, list) or len(path) < 2 or any(not isinstance(point, list) or len(point) != 2 for point in path):
      raise ValueError(f"A section's path is at least two [x, y] points, got {path!r}")
    points = numpy.array(path, dtype=numpy.float64)
    return points, [(index, str(index)) for index in range(len(points))], None
  if start is None or end is None or len(start) != 2 or len(end) != 2:
    raise ValueError(f"A section runs from start [x, y] to end [x, y], got {start} and {end}")
  return numpy.array([start, end], dtype=numpy.float64), [], None


def clippedToLeg(segments, length):
  """Segments [s0, z0, s1, z1] cut to the part between 0 and length along a leg."""
  kept = []
  for s0, z0, s1, z1 in numpy.asarray(segments).reshape(-1, 4).tolist():
    if s0 == s1:
      if -1e-9 <= s0 <= length + 1e-9:
        kept.append([s0, z0, s1, z1])
      continue
    low, high = sorted(((0.0 - s0) / (s1 - s0), (length - s0) / (s1 - s0)))
    low, high = max(low, 0.0), min(high, 1.0)
    if high > low:
      kept.append([s0 + low * (s1 - s0), z0 + low * (z1 - z0), s0 + high * (s1 - s0), z0 + high * (z1 - z0)])
  return numpy.array(kept).reshape(-1, 4)


def clippedSpan(span, length):
  """A span [enter, leave] cut to 0 to length, or None."""
  low, high = max(span[0], 0.0), min(span[1], length)
  return [low, high] if high > low else None


class SectionLeg:
  """One straight leg of a section's line: where it starts, its direction and the normal to its left, its length, and how far along the
  whole line it starts."""

  def __init__(self, start, end, offset):
    self.start = start
    self.length = float(numpy.linalg.norm(end - start))
    self.along = (end - start) / self.length
    self.normal = numpy.array([-self.along[1], self.along[0]])
    self.offset = offset
    self.low, self.high = numpy.minimum(start, end), numpy.maximum(start, end)

  def nearTriangles(self, planLow, planHigh):
    """Which triangles (their plan boxes) the leg's plane can cut."""
    return ((planLow <= self.high + 1e-6) & (planHigh >= self.low - 1e-6)).all(axis=1)

  def shifted(self, segments):
    segments = clippedToLeg(segments, self.length)
    if len(segments):
      segments[:, [0, 2]] += self.offset
    return segments


def legsOf(points):
  lengths = numpy.linalg.norm(numpy.diff(points, axis=0), axis=1)
  for index in numpy.flatnonzero(lengths < 1e-6):
    raise ValueError(f"The section's points {index} and {index + 1} stand at one place")
  offsets = numpy.concatenate([[0.0], numpy.cumsum(lengths)])
  return [SectionLeg(points[index], points[index + 1], float(offsets[index])) for index in range(len(points) - 1)], float(offsets[-1]), offsets


def legCuts(legs, positions, triangles):
  """Where each of a section's legs cuts triangles, as segments [s, z, s, z] along the whole line, leg by leg."""
  if not len(triangles):
    return [numpy.zeros((0, 4)) for _ in legs]
  corners = positions[triangles][:, :, :2]
  planLow, planHigh = corners.min(axis=1), corners.max(axis=1)
  return [leg.shifted(planeSegments(positions, triangles[leg.nearTriangles(planLow, planHigh)], leg.start, leg.along, leg.normal)) for leg in legs]


def triangleCuts(legs, positions, triangles):
  return numpy.vstack(legCuts(legs, positions, triangles))


def heightsAt(segments, s):
  """The heights of segments' ends standing at s along the line."""
  ends = numpy.vstack([segments[:, :2], segments[:, 2:]]) if len(segments) else numpy.zeros((0, 2))
  return sorted(round(float(z), 3) for z in ends[numpy.abs(ends[:, 0] - s) <= 1e-6, 1])


def fittedRange(ground, bottom, top):
  """The section's bottom and top: as given, or fitted round the ground the section cuts, with a margin."""
  if bottom is not None and top is not None:
    return bottom, top
  if not len(ground):
    raise ValueError("The section cuts no ground, so there is nothing to fit its bottom and top to; give bottom and top")
  low, high = float(ground[:, [1, 3]].min()), float(ground[:, [1, 3]].max())
  margin = max(sectionFitMargin, 0.08 * (high - low))
  return (low - margin if bottom is None else bottom), (high + margin if top is None else top)


def caveCuts(legs, followed):
  """Where a section meets every cave's runs (bridgeCaves.caveGuides): a run going along it (within alongDegrees, its middle within a
  quarter of its width of the line) as its floor and vault at their heights, its floor strokes (level ways at the floor, pads at their
  tops, rough ground at its rise), landings, and junctions; a run crossing it as a box from its floor to its vault, as long as its width
  crosses the line."""
  entries = []
  for guide in bridgeCaves.caveGuides():
    junctions = {}
    for run in guide["runs"]:
      if run["from"] is not None:
        junctions.setdefault(run["from"], []).append((run["run"], run["samples"][0]))
    for run in guide["runs"]:
      samples = numpy.array(run["samples"])
      isFollowed = followed is not None and followed["object"] == guide["object"] and followed["cave"] == guide["cave"] and followed["run"] == run["run"]
      label = f"{guide['cave']}" + ("" if run["run"] == bridgeCaveRuns.mainRun else f" {run['run']}")
      floor, vault, strokes, marks, boxes = [], [], [], [], []
      for leg in legs:
        offsets = samples[:, :2] - leg.start
        sides, distances = offsets @ leg.normal, offsets @ leg.along
        steps = numpy.linalg.norm(numpy.diff(samples[:, :2], axis=0), axis=1)
        sines = numpy.abs(numpy.diff(sides)) / numpy.maximum(steps, 1e-9)
        lateral = numpy.maximum(sectionAlongLateral, samples[:, 3] / 4)
        alongLeg = (numpy.abs(sides[:-1]) <= lateral[:-1]) & (numpy.abs(sides[1:]) <= lateral[1:]) & (sines < math.sin(math.radians(alongDegrees)))
        alongLeg &= (numpy.minimum(distances[:-1], distances[1:]) >= -1e-6) & (numpy.maximum(distances[:-1], distances[1:]) <= leg.length + 1e-6)
        for index in numpy.flatnonzero(alongLeg):
          s0, s1 = distances[index] + leg.offset, distances[index + 1] + leg.offset
          z0, z1 = samples[index, 2], samples[index + 1, 2]
          floor.append([s0, z0, s1, z1])
          vault.append([s0, z0 + samples[index, 4], s1, z1 + samples[index + 1, 4]])
          along0, along1 = samples[index, 5], samples[index + 1, 5]
          # Where the section's line passes across the run here: its point beside the run's middle, and how far across the run that is.
          linePoints = samples[[index, index + 1], :2] - sides[[index, index + 1], None] * leg.normal
          runDirection = (samples[index + 1, :2] - samples[index, :2]) / max(steps[index], 1e-9)
          lineAcross = float(-sides[index] * (leg.normal @ numpy.array([runDirection[1], -runDirection[0]])))
          for stroke in run["strokes"]:
            if stroke["kind"] in ("level", "pad"):
              if along0 >= stroke["from"] - 1e-6 and along1 <= stroke["to"] + 1e-6 and stroke["across"][0] - 1e-6 <= lineAcross <= stroke["across"][1] + 1e-6:
                height = (z0, z1) if stroke["kind"] == "level" else (stroke["top"], stroke["top"])
                strokes.append({"name": stroke["name"], "kind": stroke["kind"], "segment": [s0, height[0], s1, height[1]]})
            elif bridgeCaveRuns.pointsInPolygon(linePoints, numpy.array(stroke["outline"])).all():
              strokes.append({"name": stroke["name"], "kind": "rough", "segment": [s0, z0 + stroke["rise"], s1, z1 + stroke["rise"]]})
          for landing in run["landings"]:
            middle = sum(landing["arc"]) / 2
            if along0 <= middle < along1:
              marks.append({"s": s0, "z": z0, "label": f"landing {landing['point']}"})
          for branch, start in junctions.get(run["run"], []):
            nearest = int(numpy.argmin(numpy.linalg.norm(samples[:, :2] - numpy.array(start[:2]), axis=1)))
            if nearest == index:
              marks.append({"s": s0, "z": start[2], "label": f"{branch} leaves"})
        crossing = ~alongLeg & ((sides[:-1] > 0) != (sides[1:] > 0)) & (sines >= math.sin(math.radians(alongDegrees)))
        for index in numpy.flatnonzero(crossing):
          share = sides[index] / (sides[index] - sides[index + 1])
          s = distances[index] + share * (distances[index + 1] - distances[index])
          if not -1e-6 <= s <= leg.length + 1e-6:
            continue
          width, height = samples[index, 3], samples[index, 4]
          base = samples[index, 2] + share * (samples[index + 1, 2] - samples[index, 2])
          half = min(width / 2 / sines[index], 3 * width)
          if not any(abs(box["s"][0] + half - (s + leg.offset)) < crossingSeparation for box in boxes):
            boxes.append({"s": [s + leg.offset - half, s + leg.offset + half], "z": [base, base + height]})
      if isFollowed and floor:
        marks += followedGrades(run, legs)
      if floor or boxes:
        entries.append({
          "object": guide["object"], "cave": guide["cave"], "run": run["run"], "label": label, "followed": isFollowed,
          "floor": numpy.round(floor, 2).tolist(), "vault": numpy.round(vault, 2).tolist(),
          "strokes": [stroke | {"segment": [round(value, 2) for value in stroke["segment"]]} for stroke in strokes],
          "marks": [{key: round(value, 2) if isinstance(value, float) else value for key, value in mark.items()} for mark in marks],
          "crossings": [{key: [round(float(value), 2) for value in values] for key, values in box.items()} for box in boxes],
        })
  return entries


def followedGrades(run, legs):
  """The grade of each segment of the run a section follows, written over the middle of the segment's floor (none on a level one)."""
  samples = numpy.array(run["samples"])
  polyline, polylineAlongs = numpy.array(run["polyline"]), numpy.array(run["polylineAlongs"])
  lengths = numpy.concatenate([[0.0], numpy.cumsum(numpy.linalg.norm(numpy.diff(polyline[:, :2], axis=0), axis=1))])
  lead = legs[0].length
  marks = []
  for segment in run["segments"]:
    if abs(segment["degrees"]) < 0.05:
      continue
    middle = segment["middle"]
    marks.append({"s": lead + float(numpy.interp(middle, polylineAlongs, lengths)), "z": float(numpy.interp(middle, samples[:, 5], samples[:, 2])), "label": f"{segment['degrees']:.1f}\N{DEGREE SIGN}"})
  return marks


def sectionCuts(start, end, path, cave, bottom, top, layers):
  """What the zone holds where the vertical planes along a section's line (sectionLine) cut it, its legs laid out end to end (s along
  the line from its start, z height): the ground players stand on, water surfaces, swim volumes, sketch massing, sketch area floors and
  paths, plot pads, boundaries, zone lines, and the caves' runs (caveCuts); with the line's bends marked and its bottom and top (fitted
  round the ground when not given)."""
  unknown = sorted(set(layers) - set(sectionLayers))
  if unknown:
    raise ValueError(f"Section layers are {list(sectionLayers)}; got {unknown}")
  if bottom is not None and top is not None and top <= bottom:
    raise ValueError(f"A section's top is above its bottom, got {bottom} and {top}")
  points, bends, followed = sectionLine(start, end, path, cave)
  legs, length, offsets = legsOf(points)
  bpy.context.view_layer.update()
  positions, triangles = bridgeMeshAccess.playerSolidTriangles()
  groundByLeg = legCuts(legs, positions, triangles)
  ground = numpy.vstack(groundByLeg)
  caveEntries = caveCuts(legs, followed) if "caves" in layers or followed is not None else []
  if followed is not None:
    # A passage crossing the run shows whole, as a closed shape over or under it.
    crossingTop = max((box["z"][1] for entry in caveEntries for box in entry["crossings"]), default=-math.inf) + sectionFitMargin
    crossingBottom = min((box["z"][0] for entry in caveEntries for box in entry["crossings"]), default=math.inf) - sectionFitMargin
    bottom = min(followed["reach"][0], crossingBottom) if bottom is None else bottom
    top = max(followed["reach"][1], crossingTop) if top is None else top
  bottom, top = fittedRange(ground, bottom, top)
  # Where two legs meet, the ground each cuts ends at the heights the other's begins at.
  joins = [
    {"s": round(float(offsets[index]), 2), "before": heightsAt(groundByLeg[index - 1], offsets[index]), "after": heightsAt(groundByLeg[index], offsets[index])}
    for index in range(1, len(legs))
  ]
  cuts = {
    "length": round(length, 2), "bottom": round(bottom, 2), "top": round(top, 2), "ground": [], "water": [], "swim": [], "massing": [], "sketch": [],
    "plots": [], "boundaries": [], "zoneLines": [], "caves": [], "sheets": sorted({readSpec(shape)["sheet"] for shape in sketchObjects()}),
    "points": numpy.round(points, 2).tolist(), "bends": [{"s": round(float(offsets[index]), 2), "label": label} for index, label in bends], "followed": followed,
    "groundAtJoins": joins,
  }

  def cutObjects(sceneObjects):
    found = []
    for sceneObject in sceneObjects:
      objectPositions, objectTriangles = bridgeMeshAccess.worldTriangles([sceneObject])
      segments = keptSegments(triangleCuts(legs, objectPositions, objectTriangles), length, bottom, top)
      if segments:
        found.append({"name": sceneObject.name, "segments": segments})
    return found

  if "ground" in layers:
    cuts["ground"] = keptSegments(ground, length, bottom, top)
  if "water" in layers:
    bodies = renderedWater()
    waterGround = bridgeWater.Ground() if bodies else None
    for body in bodies:
      bodyPositions, bodyTriangles = bridgeMeshAccess.worldTriangles([body])
      shown = [leg.shifted(shownWater(planeSegments(bodyPositions, bodyTriangles, leg.start, leg.along, leg.normal), leg.start, leg.along, waterGround)) for leg in legs]
      segments = keptSegments(numpy.vstack(shown), length, bottom, top)
      if segments:
        cuts["water"].append({"name": body.name, "segments": segments})
  if "massing" in layers:
    cuts["massing"] = [entry | {"label": readSpec(bpy.data.objects[entry["name"]]).get("label") or readSpec(bpy.data.objects[entry["name"]])["name"]} for entry in cutObjects([shape for shape in sketchObjects() if len(shape.data.polygons)])]
  if "sketch" in layers:
    merged = {}
    for leg in legs:
      for entry in sketchCuts(leg.start, leg.along, leg.normal, leg.length):
        pieces = leg.shifted(entry["pieces"]).round(2).tolist()
        if pieces:
          merged.setdefault((entry["sheet"], entry["shape"]), entry | {"pieces": []})["pieces"] += pieces
    cuts["sketch"] = list(merged.values())
  if "plots" in layers:
    for plot in bridgeHousing.plotObjects():
      for leg in legs:
        cut = plotCut(plot, leg.start, leg.along, leg.normal, leg.length)
        if cut is not None:
          entrance = None if cut["entrance"] is None or not 0 <= cut["entrance"] <= leg.length else rounded(cut["entrance"] + leg.offset, 2)
          cuts["plots"].append(cut | {"s": [rounded(value + leg.offset, 2) for value in cut["s"]], "entrance": entrance})
  if "swim" in layers:
    for box in bridgeSwim.swimBoxes():
      (low, high) = bridgeSwim.boxCorners(box)
      for leg in legs:
        crossing = boxCrossing(low, high, leg.start, leg.along)
        span = None if crossing is None else clippedSpan(crossing, leg.length)
        if span is not None:
          cuts["swim"].append({"name": box.name, "liquid": bridgeSwim.readBox(box)["liquid"], "s": [rounded(value + leg.offset, 2) for value in span], "z": [low[2], high[2]]})
  if "boundaries" in layers:
    boundaries = [boundary for boundary in bridgeBoundaries.boundaryObjects() if boundary.type == "MESH"]
    cuts["boundaries"] = [entry | {"kind": bridgeBoundaries.readSpec(bpy.data.objects[entry["name"]], bridgeMeshAccess.boundaryProperty)["kind"]} for entry in cutObjects(boundaries)]
  if "zoneLines" in layers:
    for line in bridgeBoundaries.zoneLineObjects():
      center, half = bridgeBoundaries.boxBounds(line)
      heading = bridgeBoundaries.boxHeading(line)
      for leg in legs:
        crossing = boxCrossing([-extent for extent in half], half, planTurned(leg.start - center[:2], -heading), planTurned(leg.along, -heading))
        span = None if crossing is None else clippedSpan(crossing, leg.length)
        if span is not None:
          cuts["zoneLines"].append({"name": line.name, "s": [rounded(value + leg.offset, 2) for value in span], "z": [round(center[2] - half[2], 2), round(center[2] + half[2], 2)]})
  if "caves" in layers:
    cuts["caves"] = caveEntries
  return cuts


commands = {
  "sketchShapes": (sketchShapes, True),
  "eraseSketch": (eraseSketch, True),
  "getSketch": (getSketch, False),
  "planOverlays": (planOverlays, False),
  "sectionCuts": (sectionCuts, False),
}
