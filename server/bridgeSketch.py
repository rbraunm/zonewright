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
import bridgeHousing
import bridgeMeshAccess
import bridgeObjects
import bridgeSwim

sketchProperty = "zonewrightSketch"
sketchKinds = ("area", "footprint", "path", "point", "note")
namePattern = re.compile(r"^[A-Za-z0-9_-]+$")
shapeKeys = {"name", "kind", "outline", "rectangle", "points", "at", "width", "floor", "height", "facingDegrees", "label", "note"}
massingMaterialName = "zonewrightSketchMassing"
massingVersion = 2
massingColor = (0.82, 0.8, 0.76)
# Massing shades each face by the way it faces, the same from every view: tops lightest, then east and west, then north and south.
massingShade = {"base": 0.45, "up": 0.4, "eastWest": 0.25, "northSouth": 0.1}
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


def roundPoint(point):
  return [round(float(component), 1) for component in point]


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
    measured["area"] = round(abs(polygonArea(outline)), 1)
    measured["centroid"] = roundPoint(outline.mean(axis=0))
    measured["bounds"] = [roundPoint(outline.min(axis=0)), roundPoint(outline.max(axis=0))]
    if len(found):
      measured["ground"] = {
        "lowest": round(float(found.min()), 1), "mean": round(float(found.mean()), 1), "highest": round(float(found.max()), 1),
        "steepestDegrees": steepestDegrees(heights, spacing), "sampleSpacing": round(spacing, 1),
        "underWater": round(float((depths[~numpy.isnan(depths)] > 0).mean()), 3),
      }
      if "floor" in spec:
        measured["ground"]["cut"] = round(max(0.0, float(found.max()) - spec["floor"]), 1)
        measured["ground"]["fill"] = round(max(0.0, spec["floor"] - float(found.min())), 1)
    else:
      measured["ground"] = None
    measured["overlaps"] = sorted(name for name, otherOutline in others if name != spec["name"] and outlinesOverlap(outline, otherOutline))
    distances = [(polygonDistance(outline, otherOutline), name) for name, otherOutline in others if name != spec["name"]]
    nearest = min(distances, default=None)
    measured["nearest"] = None if nearest is None else {"shape": nearest[1], "gap": round(nearest[0], 1)}
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
    measured["length"] = round(along, 1)
    measured["ground"] = None if not found else {
      "start": round(found[0][1], 1), "end": round(found[-1][1], 1), "lowest": round(min(z for _, z in found), 1), "highest": round(max(z for _, z in found), 1),
      "steepestDegrees": round(max(grades, default=0.0), 1),
    }
    width = spec.get("width", 0.0)
    measured["crosses"] = sorted(name for name, otherOutline in others if pathMeetsOutline(geometry[:, :2], width / 2, otherOutline))
  else:
    x, y = geometry[0][:2]
    z = probe.height(x, y)
    measured["ground"] = None if z is None else round(z, 1)
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
    for axis, weight, operation in (("Z", massingShade["up"], "MAXIMUM"), ("X", massingShade["eastWest"], "ABSOLUTE"), ("Y", massingShade["northSouth"], "ABSOLUTE")):
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
  """What a plan drawing lays over the base: the sheets' shapes, the plan's own regions, plots, and water, and the ground's height at
  each of spots ([x, y]; those over no ground are left out), in plan coordinates."""
  known = {readSpec(shape)["sheet"] for shape in sketchObjects()}
  if sheets is not None:
    missing = sorted(set(sheets) - known)
    if missing:
      raise ValueError(f"No sketch sheets {missing}; sheets: {sorted(known)}")
  chosen = sorted(known) if sheets is None else sheets
  unknownLayers = sorted(set(layers) - {"regions", "plots", "water", "swim"})
  if unknownLayers:
    raise ValueError(f"layers are regions, plots, water, and swim; got {unknownLayers}")
  bpy.context.view_layer.update()
  overlays = {"sheets": [], "regions": [], "plots": [], "water": [], "swim": [], "spots": []}
  if spots:
    probe = GroundProbe()
    for x, y in spots:
      height = probe.height(x, y)
      if height is not None:
        overlays["spots"].append({"at": [x, y], "height": round(height, 1)})
  for sheet in chosen:
    shapes = []
    for shape in sketchObjects(sheet):
      spec = readSpec(shape)
      shapes.append({key: value for key, value in spec.items() if key not in ("sheet", "count")} | {"plan": [roundPoint(point[:2]) for point in worldGeometry(shape)]})
    overlays["sheets"].append({"sheet": sheet, "shapes": shapes})
  if "regions" in layers:
    overlays["regions"] = [{"name": region["name"], "outline": region["outline"]} for region in bridgeAuthoring.getRegions()["regions"]]
  if "plots" in layers:
    overlays["plots"] = [
      {"address": plot.name, "corners": [roundPoint(corner) for corner in bridgeHousing.footprint(plot)], "facingDegrees": round(bridgeHousing.facingOf(plot), 1)}
      for plot in bridgeHousing.plotObjects()
    ]
  if "swim" in layers:
    overlays["swim"] = [{"name": box.name, "liquid": bridgeSwim.readBox(box)["liquid"], "corners": bridgeSwim.boxCorners(box)} for box in bridgeSwim.swimBoxes()]
  if "water" in layers:
    for body in bpy.context.scene.objects:
      if bridgeMeshAccess.waterProperty in body and not body.hide_render:
        positions, triangles = bridgeMeshAccess.worldTriangles([body])
        overlays["water"].append({"name": body.name, "positions": numpy.round(positions[:, :2], 1).tolist(), "triangles": triangles.tolist()})
  return overlays


sectionLayers = ("ground", "water", "swim", "massing", "plots")
# Cuts reaching past the drawing by this share of its size are dropped; the drawing clips the rest.
sectionMargin = 0.1


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


def keptSegments(segments, length, bottom, top):
  margin = sectionMargin * max(length, top - bottom)
  inside = (numpy.maximum(segments[:, 0], segments[:, 2]) >= -margin) & (numpy.minimum(segments[:, 0], segments[:, 2]) <= length + margin)
  inside &= (numpy.maximum(segments[:, 1], segments[:, 3]) >= bottom - margin) & (numpy.minimum(segments[:, 1], segments[:, 3]) <= top + margin)
  return numpy.round(segments[inside], 2).tolist()


def sectionCuts(start, end, bottom, top, layers):
  """What the zone holds where the vertical plane through the line from start to end cuts it, in the plane's own terms (s along the line
  from start, z height): the ground players stand on, water surfaces, swim volumes, sketch massing, and plot pads."""
  unknown = sorted(set(layers) - set(sectionLayers))
  if unknown:
    raise ValueError(f"Section layers are {list(sectionLayers)}; got {unknown}")
  if len(start) != 2 or len(end) != 2 or top <= bottom:
    raise ValueError(f"A section runs from [x, y] to [x, y] between a bottom and a higher top, got {start}, {end}, {bottom}, {top}")
  start, end = numpy.array(start, dtype=numpy.float64), numpy.array(end, dtype=numpy.float64)
  length = float(numpy.linalg.norm(end - start))
  if length < 1e-6:
    raise ValueError("A section's two points must differ")
  along = (end - start) / length
  normal = numpy.array([-along[1], along[0]])
  bpy.context.view_layer.update()
  cuts = {"length": round(length, 2), "ground": [], "water": [], "swim": [], "massing": [], "plots": []}

  def cutObjects(sceneObjects):
    found = []
    for sceneObject in sceneObjects:
      positions, triangles = bridgeMeshAccess.worldTriangles([sceneObject])
      segments = keptSegments(planeSegments(positions, triangles, start, along, normal), length, bottom, top)
      if segments:
        found.append({"name": sceneObject.name, "segments": segments})
    return found

  if "ground" in layers:
    positions, triangles = bridgeMeshAccess.partTriangles(bridgeMeshAccess.playerSolidParts())
    cuts["ground"] = keptSegments(planeSegments(positions, triangles, start, along, normal), length, bottom, top)
  if "water" in layers:
    cuts["water"] = cutObjects([body for body in bpy.context.scene.objects if bridgeMeshAccess.waterProperty in body and not body.hide_render])
  if "massing" in layers:
    cuts["massing"] = [entry | {"label": readSpec(bpy.data.objects[entry["name"]]).get("label") or readSpec(bpy.data.objects[entry["name"]])["name"]} for entry in cutObjects([shape for shape in sketchObjects() if len(shape.data.polygons)])]
  if "plots" in layers:
    cuts["plots"] = cutObjects(bridgeHousing.plotObjects())
  if "swim" in layers:
    for box in bridgeSwim.swimBoxes():
      (low, high) = bridgeSwim.boxCorners(box)
      enter, leave = -math.inf, math.inf
      for axis in (0, 1):
        if abs(along[axis]) < 1e-12:
          if not low[axis] <= start[axis] <= high[axis]:
            enter, leave = 1.0, 0.0
          continue
        first, second = (low[axis] - start[axis]) / along[axis], (high[axis] - start[axis]) / along[axis]
        enter, leave = max(enter, min(first, second)), min(leave, max(first, second))
      if leave > enter:
        cuts["swim"].append({"name": box.name, "liquid": bridgeSwim.readBox(box)["liquid"], "s": [round(enter, 2), round(leave, 2)], "z": [low[2], high[2]]})
  return cuts


commands = {
  "sketchShapes": (sketchShapes, True),
  "eraseSketch": (eraseSketch, True),
  "getSketch": (getSketch, False),
  "planOverlays": (planOverlays, False),
  "sectionCuts": (sectionCuts, False),
}
