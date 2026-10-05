"""Water laid in as an artist lays it: named bodies, each rebuilt against the ground from what it was made from whenever it is edited,
so water is adjusted one body at a time and looked at after every change. A pool floods from a point up to a level; a river runs along a
path at levels falling with it, spreading over the ground below its level within reach of the path; a fall hangs as a sheet from a lip,
arcing out as it drops. Pools and rivers reach a little under their banks so no seam shows at the waterline, and the swim volumes the
client needs are derived from what they cover. A body's white water is the client's: spray emitters where the water strikes, kept with
the body and placed again whenever it is rebuilt. Runs under Blender's Python."""
import json
import math
import re

import bmesh
import bpy
import mathutils
import mathutils.bvhtree
import numpy

import bridgeEnvironment
import bridgeMeshAccess
import bridgeObjects
import bridgeShaping
import bridgeSurfacing

waterCollectionName = "water"
waterKinds = ("pool", "river", "fall")
strokeModes = ("add", "remove")
bodyLiquids = {"pool": ("water", "lava"), "river": ("water", "waterfall", "lava"), "fall": ("waterfall", "lava")}
sprayFields = ("at", "definition", "rings", "spacing", "count", "above")
# Most of the client's emitter lists give their emitters this lifespan.
sprayLifespan = 4000000
# Ripple rings lie this far over the water, as Crescent Reach lays its rings 0.1 to 1 over its pools.
ringLift = 0.5
# A pool or river's surface runs on past its waterline until it lies at least this far under the bank, for up to this many cells, so
# no sliver of it shows on a gentle shore.
shoreTuck = 2.0
shoreRounds = 2
# A flood that has measured this many grid points has leaked into open ground; it is refused rather than left to fill the zone.
maximumFloodPoints = 250000
# A fall's sheet starts this far back from its lip, level with it, so it turns over the edge rather than starting in the air.
lipLeadIn = 4.0
# How far in front of and behind its lip a fall looks for the ground, in its spacings, to tell its front from its back.
lipProbeSpacings = 2.0
up = mathutils.Vector((0.0, 0.0, 1.0))
down = mathutils.Vector((0.0, 0.0, -1.0))
reach = bridgeMeshAccess.waterReach
surfaceContact = 1e-3
edgeHalvings = 20
# A cut closer to a cell's corner than this share of its edge would leave a sliver of a face; it is taken as the corner.
cornerSnap = 1e-4
neighbourSteps = ((1, 0), (-1, 0), (0, 1), (0, -1))


# The ground water lies on

def groundTop(x, y):
  """The height of the highest ground at [x, y], or None."""
  ground = Ground()
  return ground.heightBelow(x, y, bridgeMeshAccess.sceneTopHeight() + 1)


class Ground:
  """What water lies on and against: what players stand on (bridgeMeshAccess.playerSolidTriangles)."""

  def __init__(self):
    positions, triangles = bridgeMeshAccess.playerSolidTriangles()
    self.tree = mathutils.bvhtree.BVHTree.FromPolygons(positions.tolist(), triangles.tolist())

  def depth(self, x, y, level):
    """How deep water at `level` stands over [x, y]: the drop to the ground below, 0 where the point lies under the ground (the first
    surface above it faces up), or None where no ground lies below at all."""
    point = mathutils.Vector((x, y, level))
    location, normal, _, _ = self.tree.ray_cast(point, up, reach)
    if location is not None and normal.z > 0:
      return 0.0
    location, _, _, distance = self.tree.ray_cast(point, down, reach)
    return None if location is None else distance

  def barelyCovers(self, x, y, level):
    """Whether the ground over [x, y] stands at `level` or above it by less than shoreTuck, so a surface ending there would barely hide."""
    location, normal, _, _ = self.tree.ray_cast(mathutils.Vector((x, y, level + shoreTuck - surfaceContact)), down, shoreTuck)
    return location is not None and normal.z > 0

  def castWithNormal(self, origin, direction, distance):
    location, normal, _, _ = self.tree.ray_cast(origin, direction, distance)
    return None if location is None else (location, normal)

  def heightBelow(self, x, y, z):
    location, _, _, _ = self.tree.ray_cast(mathutils.Vector((x, y, z)), down, reach)
    return None if location is None else location.z

  def insideRock(self, point):
    """Whether a point lies under the ground: the first surface above it faces up. A point on a surface is not inside it."""
    location, normal, _, distance = self.tree.ray_cast(mathutils.Vector(point), up, reach)
    return location is not None and normal.z > 0 and distance > surfaceContact

  def clearance(self, point):
    return self.tree.find_nearest(mathutils.Vector(point))[3]


# Definitions

def requireArea(area):
  if not isinstance(area, dict) or len(area) != 1 or next(iter(area)) not in ("circle", "polygon"):
    raise ValueError(f"An area is {{\"circle\": {{\"center\": [x, y], \"radius\": r}}}} or {{\"polygon\": [[x, y], ...]}}, got {area!r}")
  if "circle" in area:
    circle = area["circle"]
    if not isinstance(circle, dict) or set(circle) != {"center", "radius"} or len(circle["center"]) != 2 or circle["radius"] <= 0:
      raise ValueError(f"A circle area is {{\"center\": [x, y], \"radius\": r}} with a positive radius, got {circle!r}")
  else:
    polygon = numpy.asarray(area["polygon"], dtype=numpy.float64)
    if polygon.ndim != 2 or polygon.shape[1] != 2 or len(polygon) < 3:
      raise ValueError(f"A polygon area is at least three [x, y] points, got {area['polygon']!r}")
  return area


def insideArea(points, area):
  if "circle" in area:
    return numpy.linalg.norm(points - numpy.asarray(area["circle"]["center"], dtype=numpy.float64), axis=1) <= area["circle"]["radius"]
  return bridgeMeshAccess.insidePolygon(points, numpy.asarray(area["polygon"], dtype=numpy.float64))


def requirePlanPoint(label, point):
  if len(point) != 2:
    raise ValueError(f"{label} is [x, y], got {point!r}")
  return [float(component) for component in point]


def requirePath(path):
  pathArray = numpy.asarray(path, dtype=numpy.float64)
  if pathArray.ndim != 2 or pathArray.shape[1] != 3 or len(pathArray) < 2:
    raise ValueError(f"A river's path is at least two [x, y, level] points, got {path!r}")
  if (numpy.linalg.norm(numpy.diff(pathArray[:, :2], axis=0), axis=1) < 1e-6).any():
    raise ValueError("A river's path has two points at the same [x, y]; a drop is a fall, made with pourWaterfall")
  return pathArray.tolist()


def requireLip(lip):
  lipArray = numpy.asarray(lip, dtype=numpy.float64)
  if lipArray.ndim != 2 or lipArray.shape[1] != 3 or len(lipArray) < 2:
    raise ValueError(f"A fall's lip is at least two [x, y, z] points, got {lip!r}")
  if (numpy.linalg.norm(numpy.diff(lipArray[:, :2], axis=0), axis=1) < 1e-6).any():
    raise ValueError("A fall's lip has two points at the same [x, y]")
  return lipArray.tolist()


def validatedDefinition(definition):
  """A definition checked as a whole: every edit goes through here before the body is rebuilt."""
  kind = definition["kind"]
  if definition["spacing"] <= 0 or definition["worldUnitsPerRepeat"] <= 0:
    raise ValueError(f"spacing and worldUnitsPerRepeat must be positive, got {definition['spacing']} and {definition['worldUnitsPerRepeat']}")
  if kind == "pool":
    definition["seed"] = requirePlanPoint("seed", definition["seed"])
    if definition["within"] is not None:
      requireArea({"polygon": definition["within"]})
  elif kind == "river":
    definition["path"] = requirePath(definition["path"])
    if definition["reach"] <= 0:
      raise ValueError(f"reach must be positive, got {definition['reach']}")
  else:
    definition["lip"] = requireLip(definition["lip"])
    if min(point[2] for point in definition["lip"]) <= definition["bottom"]:
      raise ValueError(f"A fall's bottom {definition['bottom']} must lie below every point of its lip")
    if definition["throw"] < 0 or definition["spread"] <= 0:
      raise ValueError(f"throw must be at least 0 and spread positive, got {definition['throw']} and {definition['spread']}")
    if not isinstance(definition["acrossRepeats"], int) or isinstance(definition["acrossRepeats"], bool) or definition["acrossRepeats"] < 1:
      raise ValueError(f"acrossRepeats is a whole number of times the texture spans the lip, 1 or more, got {definition['acrossRepeats']!r}")
  if "swimmable" in definition and not isinstance(definition["swimmable"], bool):
    raise ValueError(f"swimmable is true or false, got {definition['swimmable']!r}")
  for stroke in definition.get("strokes", []):
    if stroke["mode"] not in strokeModes:
      raise ValueError(f"A stroke's mode is one of {list(strokeModes)}, got '{stroke['mode']}'")
    requireArea(stroke["area"])
  definition["sprays"] = [requireSpray(spray, kind) for spray in definition["sprays"]]
  return definition


def requireSpray(spray, kind):
  """A spray as the body keeps it: {at, definition, rings, spacing, count, above}, at "foot" or "lip" for a fall, {"points": [[x, y],
  ...]} on a pool or river, or {"along": [[x, y], [x, y]]} for a stretch of a river; a row takes its spacing or its count."""
  if not isinstance(spray, dict) or set(spray) != set(sprayFields):
    raise ValueError(f"A spray is {{{', '.join(sprayFields)}}}, got {spray!r}")
  at = spray["at"]
  if at in ("foot", "lip"):
    if kind != "fall":
      raise ValueError(f"A {kind}'s sprays go at points ({{\"points\": [[x, y], ...]}}) or, on a river, along a stretch ({{\"along\": [[x, y], [x, y]]}}); \"{at}\" is a fall's")
  elif isinstance(at, dict) and set(at) == {"points"}:
    if kind == "fall":
      raise ValueError("A fall's sprays go at its \"foot\" or its \"lip\"; spray points on the pool or river it lands in")
    if not at["points"]:
      raise ValueError("A spray's points are one or more [x, y]")
    at = {"points": [requirePlanPoint("A spray point", point) for point in at["points"]]}
  elif isinstance(at, dict) and set(at) == {"along"}:
    if kind != "river":
      raise ValueError(f"Only a river's sprays run along a stretch of it; a {kind}'s go at {'its foot or lip' if kind == 'fall' else 'points'}")
    if len(at["along"]) != 2:
      raise ValueError(f"along is the stretch's two ends [[x, y], [x, y]], got {at['along']!r}")
    at = {"along": [requirePlanPoint("A stretch's end", point) for point in at["along"]]}
  else:
    raise ValueError(f"A spray's at is \"foot\", \"lip\", {{\"points\": [[x, y], ...]}}, or {{\"along\": [[x, y], [x, y]]}}, got {at!r}")
  for key in ("definition", "rings"):
    value = spray[key]
    if (value is not None or key == "definition") and (not isinstance(value, int) or isinstance(value, bool) or value < 1):
      raise ValueError(f"A spray's {key} is a client emitter definition index, 1 or more{'' if key == 'definition' else ', or null'}, got {value!r}")
  if spray["rings"] is not None and at == "lip":
    raise ValueError("Rings lie on water; a lip has none under it")
  if isinstance(at, dict) and "points" in at:
    if spray["spacing"] is not None or spray["count"] is not None:
      raise ValueError("A spray at points puts one emitter at each; it takes no spacing or count")
  elif (spray["spacing"] is None) == (spray["count"] is None):
    raise ValueError(f"A row of sprays takes its spacing or its count, one of them, got spacing {spray['spacing']!r} and count {spray['count']!r}")
  elif spray["spacing"] is not None and not spray["spacing"] > 0:
    raise ValueError(f"A spray's spacing is positive, got {spray['spacing']!r}")
  elif spray["count"] is not None and (not isinstance(spray["count"], int) or isinstance(spray["count"], bool) or spray["count"] < 1):
    raise ValueError(f"A spray's count is a whole number, 1 or more, got {spray['count']!r}")
  if not isinstance(spray["above"], (int, float)) or isinstance(spray["above"], bool) or spray["above"] < 0:
    raise ValueError(f"A spray's above is how far over the water or ground it stands, 0 or more, got {spray['above']!r}")
  return spray | {"at": at}


def readDefinition(sceneObject):
  definition = json.loads(sceneObject[bridgeMeshAccess.waterProperty])
  # Bodies made before sprays and before falls spanned their lip once have neither; they had no sprays, and a rebuild maps them anew.
  definition.setdefault("sprays", [])
  if definition["kind"] == "fall":
    definition.setdefault("acrossRepeats", 1)
  return definition


def requireLiquidMaterial(kind, materialName):
  material = bpy.data.materials.get(materialName)
  if material is None:
    raise ValueError(f"No material named '{materialName}'")
  liquid = bridgeSurfacing.liquidOf(material)
  if liquid is None or liquid["liquid"] not in bodyLiquids[kind]:
    raise ValueError(f"A {kind} takes a liquid material (createLiquidMaterial) of {list(bodyLiquids[kind])}; '{materialName}' is {liquid['liquid'] if liquid else 'not a liquid material'}")
  return material


# Pools and rivers

def pathPositions(points, path):
  """For [x, y] points: the distance along a path to its nearest spot, the signed distance across it (right positive; past an end, square across the end's line), the level there, and whether it lies past an end."""
  pathArray = numpy.asarray(path, dtype=numpy.float64)
  starts, ends = pathArray[:-1], pathArray[1:]
  segments = ends[:, :2] - starts[:, :2]
  lengths = numpy.linalg.norm(segments, axis=1)
  arcStarts = numpy.concatenate([[0.0], numpy.cumsum(lengths)[:-1]])
  offsets = points[:, None, :] - starts[None, :, :2]
  unclipped = (offsets * segments[None]).sum(2) / (lengths * lengths)[None]
  along = numpy.clip(unclipped, 0, 1)
  nearest = starts[None, :, :2] + along[:, :, None] * segments[None]
  distances = numpy.linalg.norm(points[:, None, :] - nearest, axis=2)
  closest = distances.argmin(axis=1)
  rows = numpy.arange(len(points))
  fraction = along[rows, closest]
  away = points - nearest[rows, closest]
  leftward = segments[closest, 0] * away[:, 1] - segments[closest, 1] * away[:, 0] > 0
  beyond = ((closest == 0) & (unclipped[rows, closest] < 0)) | ((closest == len(segments) - 1) & (unclipped[rows, closest] > 1))
  square = (segments[closest, 1] * offsets[rows, closest, 0] - segments[closest, 0] * offsets[rows, closest, 1]) / lengths[closest]
  lateral = numpy.where(beyond, square, numpy.where(leftward, -1.0, 1.0) * distances[rows, closest])
  levels = starts[closest, 2] + fraction * (ends[closest, 2] - starts[closest, 2])
  return arcStarts[closest] + fraction * lengths[closest], lateral, levels, beyond


def levelsAt(definition, points):
  if definition["kind"] == "pool":
    return numpy.full(len(points), float(definition["level"]))
  return pathPositions(points, definition["path"])[2]


def areaAllowance(points, area):
  """How far inside an area each [x, y] point lies, negative outside it."""
  if "circle" in area:
    return area["circle"]["radius"] - numpy.linalg.norm(points - numpy.asarray(area["circle"]["center"], dtype=numpy.float64), axis=1)
  return bridgeShaping.signedDistanceToOutline(points, numpy.asarray(area["polygon"], dtype=numpy.float64))[0]


def allowance(definition, points, throughEnds=False):
  """How far inside where the body may spread each [x, y] point lies, negative outside: within for a pool, reach between its ends for a river (throughEnds, on past them), then each stroke adding or taking away its area."""
  if definition["kind"] == "pool":
    bounds = numpy.full(len(points), numpy.inf) if definition["within"] is None else areaAllowance(points, {"polygon": definition["within"]})
  else:
    _, lateral, _, beyond = pathPositions(points, definition["path"])
    bounds = definition["reach"] - numpy.abs(lateral)
    if not throughEnds:
      bounds = numpy.where(beyond, -numpy.inf, bounds)
  for stroke in definition["strokes"]:
    inside = areaAllowance(points, stroke["area"])
    bounds = numpy.maximum(bounds, inside) if stroke["mode"] == "add" else numpy.minimum(bounds, -inside)
  return bounds


def allowedAt(definition, points):
  return allowance(definition, points) >= 0


def floodSeeds(definition):
  spacing = definition["spacing"]
  if definition["kind"] == "pool":
    return [tuple(definition["seed"])]
  path = numpy.asarray(definition["path"], dtype=numpy.float64)
  seeds = []
  for start, end in zip(path[:-1], path[1:]):
    pieces = max(1, math.ceil(numpy.linalg.norm(end[:2] - start[:2]) / spacing))
    seeds.extend(tuple(start[:2] + (end[:2] - start[:2]) * step / pieces) for step in range(pieces))
  return seeds + [tuple(path[-1, :2])]


def flood(definition, ground):
  """The grid points the body covers: those with ground below its level, where it may spread, reached from its seeds point to
  neighbouring point; and the points where it reaches the end of the ground."""
  spacing = definition["spacing"]
  depths = {}

  def measure(keys):
    keys = [key for key in keys if key not in depths]
    if not keys:
      return
    points = numpy.array(keys, dtype=numpy.float64) * spacing
    allowed = allowedAt(definition, points)
    levels = levelsAt(definition, points)
    for key, (x, y), isAllowed, level in zip(keys, points, allowed, levels):
      depths[key] = ground.depth(x, y, level) if isAllowed else 0.0
    if len(depths) > maximumFloodPoints:
      raise ValueError(f"The water has spread over {len(depths)} grid points without filling: it leaks into open ground. Bound it (within, a lower level, or a removed stroke across the gap) and look at it again")

  seedPoints = floodSeeds(definition)
  seeds = {(round(x / spacing), round(y / spacing)) for x, y in seedPoints}
  measure(seeds)
  frontier = {key for key in seeds if depths[key]}
  if not frontier:
    x, y = seedPoints[0]
    level = float(levelsAt(definition, numpy.array([[x, y]]))[0])
    ground = groundTop(x, y)
    raise ValueError(
      f"No water stands at its {'seed' if definition['kind'] == 'pool' else 'path'}: at {[round(x, 1), round(y, 1)]} the ground is at"
      f" {'nothing' if ground is None else round(ground, 2)} and the level {round(level, 2)}; the level must stand above the ground, where the water may spread")
  wet = set(frontier)
  while frontier:
    neighbours = {(i + di, j + dj) for i, j in frontier for di, dj in neighbourSteps} - set(depths)
    measure(neighbours)
    frontier = {key for key in neighbours if depths[key]}
    wet |= frontier
  edges = sorted({(i + di, j + dj) for i, j in wet for di, dj in neighbourSteps if depths.get((i + di, j + dj), 0.0) is None})
  return wet, depths, edges


def cellCorners(cell):
  i, j = cell
  return ((i, j), (i + 1, j), (i + 1, j + 1), (i, j + 1))


def cellsAround(point):
  i, j = point
  return ((i - 1, j - 1), (i, j - 1), (i - 1, j), (i, j))


def floodCells(definition, ground, wet):
  """Every grid cell with a covered corner, then the cells past any corner where the bank barely covers the surface's edge."""
  spacing = definition["spacing"]
  cells = {cell for point in wet for cell in cellsAround(point)}
  for _ in range(shoreRounds):
    corners = sorted({corner for cell in cells for corner in cellCorners(cell)} - wet)
    points = numpy.array(corners, dtype=numpy.float64) * spacing
    allowed = allowedAt(definition, points)
    levels = levelsAt(definition, points)
    exposed = [corner for corner, (x, y), isAllowed, level in zip(corners, points, allowed, levels) if isAllowed and ground.barelyCovers(x, y, level)]
    added = {cell for corner in exposed for cell in cellsAround(corner)} - cells
    if not added:
      break
    cells |= added
  return cells | riverEndCells(definition) if definition["kind"] == "river" else cells


def riverEnds(definition):
  """A river's two ends, each with the unit direction pointing out of the river there."""
  path = numpy.asarray(definition["path"], dtype=numpy.float64)[:, :2]
  return [(end, (end - before) / numpy.linalg.norm(end - before)) for end, before in ((path[0], path[1]), (path[-1], path[-2]))]


def riverEndCells(definition):
  """The cells each end of a river crosses within its reach, so its surface spans its whole width where it is cut square there."""
  spacing, reach = definition["spacing"], definition["reach"]
  cells = set()
  for end, outward in riverEnds(definition):
    across = numpy.array([-outward[1], outward[0]])
    first, last = (end - across * reach) / spacing, (end + across * reach) / spacing
    crossings = [0.0, 1.0]
    for axis in range(2):
      if last[axis] != first[axis]:
        lines = numpy.arange(math.ceil(min(first[axis], last[axis])), math.floor(max(first[axis], last[axis])) + 1)
        crossings.extend(((lines - first[axis]) / (last[axis] - first[axis])).tolist())
    crossings.sort()
    for low, high in zip(crossings[:-1], crossings[1:]):
      x, y = first + (low + high) / 2 * (last - first)
      cells.add((math.floor(x), math.floor(y)))
  return cells


def cutRiverEnds(meshEditor, definition):
  """Cut a river's surface square across its path at each end and take away what lies past it, but for water a stroke adds there."""
  near = definition["reach"] + 2 * definition["spacing"]
  for end, outward in riverEnds(definition):

    def nearFaces():
      return [face for face in meshEditor.faces if any(math.dist(vertex.co[:2], end) <= near for vertex in face.verts)]

    faces = nearFaces()
    geometry = list({vertex for face in faces for vertex in face.verts}) + list({edge for face in faces for edge in face.edges}) + faces
    bmesh.ops.bisect_plane(meshEditor, geom=geometry, dist=1e-4, plane_co=(*end, 0.0), plane_no=(*outward, 0.0))
    past = [face for face in nearFaces() if numpy.dot(numpy.array(face.calc_center_median()[:2]) - end, outward) > 0]
    if past:
      added = allowedAt(definition, numpy.array([face.calc_center_median()[:2] for face in past]))
      bmesh.ops.delete(meshEditor, geom=[face for face, isAdded in zip(past, added) if not isAdded], context="FACES")


def cellEdges(cell):
  corners = cellCorners(cell)
  return [(corner, corners[(index + 1) % 4]) for index, corner in enumerate(corners)]


def edgeCuts(cells, inside, step, isInside):
  """Where each cell edge from an inside corner to an outside one leaves the inside, found by halving: {(inside corner, outside corner): fraction from the inside one}."""
  edges = sorted({(a, b) if inside[a] else (b, a) for cell in cells for a, b in cellEdges(cell) if inside[a] != inside[b]})
  if not edges:
    return {}
  starts = numpy.array([a for a, _ in edges], dtype=numpy.float64) * step
  ends = numpy.array([b for _, b in edges], dtype=numpy.float64) * step
  low, high = numpy.zeros(len(edges)), numpy.ones(len(edges))
  for _ in range(edgeHalvings):
    middle = (low + high) / 2
    within = isInside(starts + middle[:, None] * (ends - starts))
    low, high = numpy.where(within, middle, low), numpy.where(within, high, middle)
  return dict(zip(edges, low.tolist()))


def clippedLoops(cells, inside, cuts):
  """Each cell's inside part as a loop of keys: its inside corners, and the edge cuts between them and its outside ones, which neighbouring cells share."""
  loops = []
  for cell in sorted(cells):
    corners = cellCorners(cell)
    loop = []
    for index, corner in enumerate(corners):
      following = corners[(index + 1) % 4]
      if inside[corner]:
        loop.append(corner)
      if inside[corner] != inside[following]:
        edge = (corner, following) if inside[corner] else (following, corner)
        loop.append(edge[0] if cuts[edge] < cornerSnap else edge)
    loop = [key for index, key in enumerate(loop) if key != loop[index - 1]]
    if len(loop) >= 3:
      loops.append(loop)
  return loops


def loopPoint(key, cuts, step):
  """A clippedLoops key in plan: a corner of the grid, or the cut along an edge."""
  if isinstance(key[0], tuple):
    start, end = numpy.array(key[0], dtype=numpy.float64), numpy.array(key[1], dtype=numpy.float64)
    return (start + cuts[key] * (end - start)) * step
  return numpy.array(key, dtype=numpy.float64) * step


def surfaceMesh(name, definition, cells):
  """The surface over the cells at the body's levels, cut along the bounds of where it may spread; a pool's flat cells merged and mapped
  across the plan, a river's mapped along its path: u across (rightward looking downstream) and the client's v downstream, as the client's
  rivers run it (Brell's Rest, Beasts' Domain), so its liquid's slides flow as theirs do. Export counts v down from the texture's top
  (client v = 1 - Blender v), so Blender's v falls downstream."""
  spacing = definition["spacing"]

  def allowed(points):
    # A river's reach runs on past its ends until cutRiverEnds cuts them square, so one ending at a fall's lip ends along all of it.
    return allowance(definition, points, throughEnds=True) >= 0

  corners = sorted({corner for cell in cells for corner in cellCorners(cell)})
  inside = dict(zip(corners, allowed(numpy.array(corners, dtype=numpy.float64) * spacing).tolist()))
  cuts = edgeCuts(cells, inside, spacing, allowed)
  loops = clippedLoops(cells, inside, cuts)
  index = {}
  for loop in loops:
    for key in loop:
      index.setdefault(key, len(index))
  plan = numpy.array([loopPoint(key, cuts, spacing) for key in index])
  meshEditor = bmesh.new()
  vertices = [meshEditor.verts.new((x, y, 0.0)) for x, y in plan]
  for loop in loops:
    meshEditor.faces.new([vertices[index[key]] for key in loop])
  if definition["kind"] == "river":
    cutRiverEnds(meshEditor, definition)
  for vertex, level in zip(meshEditor.verts, levelsAt(definition, numpy.array([list(vertex.co[:2]) for vertex in meshEditor.verts]))):
    vertex.co.z = level
  if definition["kind"] == "pool":
    bmesh.ops.dissolve_limit(meshEditor, angle_limit=math.radians(0.01), use_dissolve_boundaries=False, verts=list(meshEditor.verts), edges=list(meshEditor.edges))
  uvLayer = meshEditor.loops.layers.uv.new(bridgeSurfacing.uvLayerName)
  repeat = definition["worldUnitsPerRepeat"]
  for face in meshEditor.faces:
    points = numpy.array([list(loop.vert.co[:2]) for loop in face.loops])
    if definition["kind"] == "pool":
      coordinates = points / repeat
    else:
      along, across, _, _ = pathPositions(points, definition["path"])
      coordinates = numpy.column_stack([across, -along]) / repeat
    for loop, coordinate in zip(face.loops, coordinates):
      loop[uvLayer].uv = coordinate
  mesh = bpy.data.meshes.new(name)
  meshEditor.normal_update()
  meshEditor.to_mesh(mesh)
  meshEditor.free()
  return mesh


def visibleSurface(body, ground):
  """Where a pool or river's surface shows in plan, not tucked under the ground, on the body's grid with its edges placed by halving: row runs [x0, y0, x1, y1] of whole cells and loops of [x, y] where its edge crosses a cell."""
  step = readDefinition(body)["spacing"]
  positions, _ = bridgeMeshAccess.readVertexArrays(body)
  low, high = positions[:, :2].min(0), positions[:, :2].max(0)
  surface = bridgeMeshAccess.worldTree([body])
  above = float(positions[:, 2].max()) + 1

  def shows(points):
    hits = [surface.ray_cast(mathutils.Vector((x, y, above)), down, reach)[0] for x, y in points]
    return numpy.array([hit is not None and not ground.insideRock(hit) for hit in hits], dtype=bool)

  first, last = numpy.floor(low / step).astype(int), numpy.ceil(high / step).astype(int)
  corners = [(i, j) for i in range(first[0], last[0] + 1) for j in range(first[1], last[1] + 1)]
  inside = dict(zip(corners, shows(numpy.array(corners, dtype=numpy.float64) * step).tolist()))
  cells = [(i, j) for i in range(first[0], last[0]) for j in range(first[1], last[1]) if any(inside[corner] for corner in cellCorners((i, j)))]
  whole = {cell for cell in cells if all(inside[corner] for corner in cellCorners(cell))}
  cuts = edgeCuts(cells, inside, step, shows)
  runs = []
  for i, j in sorted(whole, key=lambda cell: (cell[1], cell[0])):
    if (i - 1, j) not in whole:
      end = i
      while (end + 1, j) in whole:
        end += 1
      runs.append([round(i * step, 2), round(j * step, 2), round((end + 1) * step, 2), round((j + 1) * step, 2)])
  edgeCells = set(cells) - whole
  loops = [[[round(float(value), 2) for value in loopPoint(key, cuts, step)] for key in loop] for loop in clippedLoops(edgeCells, inside, cuts)]
  return {"runs": runs, "loops": loops}


def visibleExtent(body, ground):
  """The plan bounds of the water players see (a surface not tucked under its banks, a fall's sheet out of the rock), or None."""
  if readDefinition(body)["kind"] == "fall":
    positions, _ = bridgeMeshAccess.readVertexArrays(body)
    shown = [point[:2] for point in positions if not ground.insideRock(point)]
  else:
    surface = visibleSurface(body, ground)
    shown = [corner for x0, y0, x1, y1 in surface["runs"] for corner in ((x0, y0), (x1, y1))] + [point for loop in surface["loops"] for point in loop]
  if not shown:
    return None
  shown = numpy.array(shown, dtype=numpy.float64)
  return {"minimum": bridgeObjects.roundVector(shown.min(0), 1), "maximum": bridgeObjects.roundVector(shown.max(0), 1)}


def spreadBody(name, definition, ground):
  wet, depths, edges = flood(definition, ground)
  cells = floodCells(definition, ground, wet)
  spacing = definition["spacing"]
  wetDepths = [depths[point] for point in wet]
  report = {
    "coveredPoints": len(wet), "deepest": round(max(wetDepths), 2),
    "reachesGroundEnd": [[round(i * spacing, 1), round(j * spacing, 1)] for i, j in edges[:8]], "groundEndPoints": len(edges),
  }
  return surfaceMesh(name, definition, cells), report


# Falls

def resampledLip(lip, spacing):
  """Points along the lip at most `spacing` apart, its own points among them, with the lip's direction at each."""
  lipArray = numpy.asarray(lip, dtype=numpy.float64)
  points, directions = [], []
  segmentDirections = [(end[:2] - start[:2]) / numpy.linalg.norm(end[:2] - start[:2]) for start, end in zip(lipArray[:-1], lipArray[1:])]
  for index, (start, end) in enumerate(zip(lipArray[:-1], lipArray[1:])):
    pieces = max(1, math.ceil(numpy.linalg.norm(end[:2] - start[:2]) / spacing - 1e-9))
    for step in range(pieces):
      points.append(start + (end - start) * step / pieces)
      direction = segmentDirections[index] if step or index == 0 else segmentDirections[index - 1] + segmentDirections[index]
      directions.append(direction / numpy.linalg.norm(direction))
  points.append(lipArray[-1])
  directions.append(segmentDirections[-1])
  return numpy.array(points), numpy.array(directions)


def fallGrid(definition, ground):
  """The sheet's vertices, [column along the lip, row], row 0 the lead-in behind the lip and row 1 the lip; the lip's points and its
  length along them at each."""
  spacing, bottom, throw, spread = definition["spacing"], definition["bottom"], definition["throw"], definition["spread"]
  points, directions = resampledLip(definition["lip"], spacing)
  outward = numpy.column_stack([directions[:, 1], -directions[:, 0]])
  arc = numpy.concatenate([[0.0], numpy.cumsum(numpy.linalg.norm(numpy.diff(points[:, :2], axis=0), axis=1))])
  middle = int(numpy.searchsorted(arc, arc[-1] / 2))
  probe = lipProbeSpacings * spacing
  aboveLip = points[middle, 2] + spacing
  front = ground.heightBelow(*(points[middle, :2] + outward[middle] * probe), aboveLip)
  behind = ground.heightBelow(*(points[middle, :2] - outward[middle] * probe), aboveLip)
  if behind is None or (front is not None and front >= behind):
    raise ValueError(
      f"The ground in front of the lip (at {[round(value, 1) for value in points[middle, :2] + outward[middle] * probe]}) does not lie below the ground behind it: a lip"
      " runs from the fall's left edge to its right as seen from in front of it; reverse it if it runs the other way")
  center = numpy.interp(arc[-1] / 2, arc, points[:, 0]), numpy.interp(arc[-1] / 2, arc, points[:, 1])
  center = numpy.array(center)
  rows = max(1, math.ceil((points[:, 2].max() - bottom) / spacing))
  fractions = numpy.arange(rows + 1) / rows
  grid = numpy.empty((len(points), rows + 2, 3))
  grid[:, 0, :2] = points[:, :2] - outward * lipLeadIn
  grid[:, 0, 2] = points[:, 2]
  for row, fraction in enumerate(fractions, 1):
    grid[:, row, :2] = center + (points[:, :2] - center) * (1 + (spread - 1) * fraction) + outward * throw * math.sqrt(fraction)
    grid[:, row, 2] = points[:, 2] - fraction * (points[:, 2] - bottom)
  return grid, points, arc


def fallMesh(name, definition, ground):
  """A sheet hung from the lip: it starts a little back from the lip, turns over it, and drops to the bottom, carried out from the
  face by `throw` at the bottom as falling water is (out as the square root of the drop) and spreading `spread` times as wide. Mapped as
  the client maps its falls: u spans the lip `acrossRepeats` times (once, 0 to 1, so a fall texture's faded side edges frame the fall)
  and the client's v runs down the drop, a repeat every worldUnitsPerRepeat, so its liquid's slides flow down as theirs do (Blender's v
  falls down the drop; export counts v down from the texture's top)."""
  grid, points, arc = fallGrid(definition, ground)
  rows = grid.shape[1] - 2
  bottom = definition["bottom"]
  meshEditor = bmesh.new()
  vertices = [[meshEditor.verts.new(grid[column, row]) for row in range(rows + 2)] for column in range(len(points))]
  uvLayer = meshEditor.loops.layers.uv.new(bridgeSurfacing.uvLayerName)
  fallen = numpy.concatenate([numpy.zeros((len(points), 1)), numpy.cumsum(numpy.linalg.norm(numpy.diff(grid, axis=1), axis=2), axis=1)], axis=1)
  repeat = definition["worldUnitsPerRepeat"]
  across = definition["acrossRepeats"] * arc / arc[-1]
  for column in range(len(points) - 1):
    for row in range(rows + 1):
      corners = ((column, row), (column, row + 1), (column + 1, row + 1), (column + 1, row))
      face = meshEditor.faces.new([vertices[c][r] for c, r in corners])
      for loop, (c, r) in zip(face.loops, corners):
        loop[uvLayer].uv = (across[c], -fallen[c, r] / repeat)
  mesh = bpy.data.meshes.new(name)
  meshEditor.normal_update()
  meshEditor.to_mesh(mesh)
  meshEditor.free()
  sheet = grid[:, 1:].reshape(-1, 3)
  inside = numpy.array([ground.insideRock(point) for point in sheet]).reshape(len(points), rows + 1)
  insideRows = sorted({int(row) for row in numpy.flatnonzero(inside.any(axis=0))})
  report = {
    "drop": round(float(points[:, 2].max() - bottom), 2), "columns": len(points), "rows": rows,
    "closestToRock": round(min(ground.clearance(point) for point in sheet), 2),
    "insideRock": {"vertices": int(inside.sum()), "rowsFromTop": insideRows, "heights": [round(float(grid[:, row + 1, 2].mean()), 1) for row in insideRows[:8]]},
  }
  return mesh, report


# Sprays

class BodySurface:
  """A newly built pool or river's surface, which stands in world space, for its sprays to stand on."""

  def __init__(self, name, mesh):
    self.name = name
    self.tree = mathutils.bvhtree.BVHTree.FromPolygons([vertex.co for vertex in mesh.vertices], [polygon.vertices for polygon in mesh.polygons])
    self.top = max(vertex.co.z for vertex in mesh.vertices) + 1

  def height(self, point):
    location = self.tree.ray_cast(mathutils.Vector((point[0], point[1], self.top)), down, reach)[0]
    if location is None:
      raise ValueError(f"The spray point {[round(float(value), 1) for value in point]} is not over '{self.name}''s surface; move it onto the water, or take the spray off (editWater sprays)")
    return location.z


def heightAt(column, level):
  """The point where a column of a fall's sheet, its lip first and falling row by row, passes a height between its ends."""
  rising = column[::-1]
  return numpy.array([numpy.interp(level, rising[:, 2], rising[:, axis]) for axis in range(3)])


def footOfColumn(column, ground, water):
  """Where one column of a fall's sheet, its lip first, strikes what lies below: the water players swim under where its foot lies
  under a surface, else where it enters the ground, else what lies straight under its end; with whether that is water."""
  end = mathutils.Vector(column[-1])
  drop = column[0][2] - column[-1][2]
  if water is not None:
    location = water.ray_cast(end - up * bridgeMeshAccess.castNudge, up, drop + bridgeMeshAccess.castNudge)[0]
    if location is not None:
      return heightAt(column, location.z), True
  hidden = [ground.insideRock(point) for point in column]
  if hidden[-1]:
    lowest = len(column) - 1
    while lowest >= 0 and hidden[lowest]:
      lowest -= 1
    if lowest < 0:
      raise ValueError("A column of the fall's sheet lies inside the rock from its lip down; move the lip or raise its throw")
    inside, outside = column[lowest + 1], column[lowest]
    for _ in range(edgeHalvings):
      middle = (inside + outside) / 2
      inside, outside = (middle, outside) if ground.insideRock(middle) else (inside, middle)
    return outside, False
  waterBelow = water.ray_cast(end, down, reach)[0] if water is not None else None
  groundBelow = ground.heightBelow(end.x, end.y, end.z)
  if waterBelow is None and groundBelow is None:
    raise ValueError(f"Nothing lies under the foot of the fall at {[round(value, 1) for value in column[-1]]} for its spray to stand on")
  if groundBelow is None or (waterBelow is not None and waterBelow.z >= groundBelow):
    return numpy.array(waterBelow), True
  return numpy.array([end.x, end.y, groundBelow]), False


def rowPositions(line, onWater, spray):
  """A row of emitters along a line of points: `count` of them, or one per `spacing` of its length (at least one), each in the middle
  of its equal share of the line, with whether it stands on water."""
  line = numpy.asarray(line, dtype=numpy.float64)
  lengths = numpy.concatenate([[0.0], numpy.cumsum(numpy.linalg.norm(numpy.diff(line[:, :2], axis=0), axis=1))])
  count = spray["count"] or max(1, round(lengths[-1] / spray["spacing"]))
  marks = (numpy.arange(count) + 0.5) / count * lengths[-1]
  positions = numpy.column_stack([numpy.interp(marks, lengths, line[:, axis]) for axis in range(3)])
  return [(position, onWater[int(numpy.abs(lengths - mark).argmin())]) for position, mark in zip(positions, marks)]


def pathPointAt(path, distance):
  pathArray = numpy.asarray(path, dtype=numpy.float64)
  lengths = numpy.concatenate([[0.0], numpy.cumsum(numpy.linalg.norm(numpy.diff(pathArray[:, :2], axis=0), axis=1))])
  return numpy.array([numpy.interp(distance, lengths, pathArray[:, axis]) for axis in range(2)])


def sprayPlaces(definition, spray, surface, ground, water):
  """Where one spray's emitters stand, on what they strike: [(position on the surface, whether it is water)]. A fall's foot is where
  each column of its sheet strikes the water or ground below, its lip the lip; points and stretches lie on the body's own surface."""
  at = spray["at"]
  if at in ("foot", "lip"):
    grid, points, _ = fallGrid(definition, ground)
    if at == "lip":
      return rowPositions(grid[:, 1], [False] * len(points), spray)
    feet = [footOfColumn(grid[column, 1:], ground, water) for column in range(len(points))]
    return rowPositions([point for point, _ in feet], [isWater for _, isWater in feet], spray)
  if "points" in at:
    return [(numpy.array([*point, surface.height(point)]), True) for point in at["points"]]
  ends = pathPositions(numpy.asarray(at["along"], dtype=numpy.float64), definition["path"])[0]
  plan = [pathPointAt(definition["path"], distance) for distance in numpy.linspace(ends[0], ends[1], max(2, math.ceil(abs(ends[1] - ends[0]) / definition["spacing"]) + 1))]
  return rowPositions([[*point, surface.height(point)] for point in plan], [True] * len(plan), spray)


def sprayRecords(name, definition, surface, ground):
  """Every emitter the body's sprays place, named for the body and spray, with its definition and what it stands on."""
  water = bridgeMeshAccess.swimSurfaces() if any(spray["at"] == "foot" for spray in definition["sprays"]) else None
  records = []
  for index, spray in enumerate(definition["sprays"], 1):
    for number, (position, onWater) in enumerate(sprayPlaces(definition, spray, surface, ground, water), 1):
      on = "water" if onWater else ("lip" if spray["at"] == "lip" else "ground")
      records.append({"name": f"{name}Spray{index}_{number}", "definition": spray["definition"], "position": [float(position[0]), float(position[1]), float(position[2] + spray["above"])], "spray": index, "role": "spray", "on": on})
      if spray["rings"] is not None:
        records.append({"name": f"{name}Rings{index}_{number}", "definition": spray["rings"], "position": [float(position[0]), float(position[1]), float(position[2] + ringLift)], "spray": index, "role": "rings", "on": on})
  return records


def placedSprays(name):
  return [sceneObject for sceneObject in bpy.data.objects if bridgeMeshAccess.sprayProperty in sceneObject and json.loads(sceneObject[bridgeMeshAccess.sprayProperty])["body"] == name]


def placeSprays(sceneObject, records):
  """Take away the body's spray emitters and place them as the records say, in the body's collection."""
  old = placedSprays(sceneObject.name)
  oldNames = {emitter.name for emitter in old}
  for record in records:
    if record["name"] not in oldNames:
      bridgeObjects.requireNewName(record["name"])
  for emitter in old:
    bpy.data.objects.remove(emitter)
  if not records:
    return
  bridgeEnvironment.placeEmitters(
    [{"name": record["name"], "position": record["position"], "definition": record["definition"], "lifespan": sprayLifespan} for record in records],
    sceneObject.users_collection[0].name, None,
  )
  for record in records:
    bpy.data.objects[record["name"]][bridgeMeshAccess.sprayProperty] = json.dumps({"body": sceneObject.name, "spray": record["spray"], "role": record["role"], "on": record["on"]})
  bpy.context.view_layer.update()


def replaceFallFeet(ground):
  """Place again the sprays of every fall that sprays its foot, after the water it lands in changed; the falls' names."""
  bodies = [(sceneObject, readDefinition(sceneObject)) for sceneObject in bpy.context.scene.objects if bridgeMeshAccess.waterProperty in sceneObject]
  falls = [(fall, definition) for fall, definition in bodies if definition["kind"] == "fall" and any(spray["at"] == "foot" for spray in definition["sprays"])]
  for fall, definition in falls:
    placeSprays(fall, sprayRecords(fall.name, definition, None, ground))
  return sorted(fall.name for fall, _ in falls)


def describeSprays(sceneObject, definition):
  placed = {}
  for emitter in placedSprays(sceneObject.name):
    spec = json.loads(emitter[bridgeMeshAccess.sprayProperty])
    placed.setdefault(spec["spray"], []).append({
      "name": emitter.name, "role": spec["role"], "definition": int(emitter[bridgeEnvironment.definitionProperty]),
      "position": bridgeObjects.roundVector(emitter.matrix_world.translation, 2), "on": spec["on"],
    })
  return [spray | {"emitters": sorted(placed.get(index, []), key=lambda entry: entry["name"])} for index, spray in enumerate(definition["sprays"], 1)]


# Flow

def layerMotions(liquid, slides):
  """How each texture layer moves in the client's texture coordinates, [u, v] repeats per unit of effect time: water's first layer
  by its first slide and its second, at twice the coordinates, by minus half its second (RegionWater.fxo); a waterfall's color and alpha
  and lava's two diffuses each by minus their slide (RegionWaterFall.fxo, RegionLava.fxo)."""
  first, second = numpy.array(slides[:2], dtype=numpy.float64), numpy.array(slides[2:], dtype=numpy.float64)
  if liquid == "water":
    return {"first": first, "second": -second / 2}
  names = ("color", "alpha") if liquid == "waterfall" else ("first", "second")
  return {names[0]: -first, names[1]: -second}


def bodyFlow(sceneObject, definition):
  """Which way and how fast the body's liquid moves, derived from its material's slides and the body's mapping: per layer, along
  the body (a river downstream, a fall down) and across it in world units per unit of effect time, or a pool's velocity [x, y]; and the
  body's flow: downstream or down, upstream or up, still where its two layers move against each other (the client's still water),
  across, or none."""
  liquid = bridgeSurfacing.liquidOf(sceneObject.material_slots[0].material)
  motions = layerMotions(liquid["liquid"], liquid["values"]["slides"])
  repeat = definition["worldUnitsPerRepeat"]
  # Adding 0 makes a negative zero (a layer moving straight one way has one across it) read 0.
  if definition["kind"] == "pool":
    velocities = {name: numpy.array([u * repeat, -v * repeat]) for name, (u, v) in motions.items()}
    first, second = velocities.values()
    flow = "none" if not (first.any() or second.any()) else "still" if first @ second < 0 else "drifting"
    return {"flow": flow, "layers": {name: {"velocity": [value + 0.0 for value in bridgeObjects.roundVector(velocity, 2)]} for name, velocity in velocities.items()}}
  if definition["kind"] == "river":
    acrossUnits, forward, backward = repeat, "downstream", "upstream"
  else:
    lip = numpy.asarray(definition["lip"], dtype=numpy.float64)
    acrossUnits, forward, backward = numpy.linalg.norm(numpy.diff(lip[:, :2], axis=0), axis=1).sum() / definition["acrossRepeats"], "down", "up"
  alongs = [v for _, v in motions.values()]
  if not any(motion.any() for motion in motions.values()):
    flow = "none"
  elif min(alongs) < 0 < max(alongs):
    flow = "still"
  elif max(alongs) > 0:
    flow = forward
  elif min(alongs) < 0:
    flow = backward
  else:
    flow = "across"
  return {"flow": flow, "layers": {name: {"along": round(float(v * repeat), 2) + 0.0, "across": round(float(u * acrossUnits), 2) + 0.0} for name, (u, v) in motions.items()}}


# Bodies

def buildBody(name, definition, ground):
  """The body's mesh, what the build found, and the emitters its sprays place on it as built; refused before anything changes when a
  spray cannot stand where it is set."""
  if definition["kind"] == "fall":
    mesh, report = fallMesh(name, definition, ground)
    surface = None
  else:
    mesh, report = spreadBody(name, definition, ground)
    surface = BodySurface(name, mesh)
  try:
    records = sprayRecords(name, definition, surface, ground)
  except ValueError:
    bpy.data.meshes.remove(mesh)
    raise
  return mesh, report, records


def describeBody(sceneObject, ground, report=None):
  definition = readDefinition(sceneObject)
  material = sceneObject.material_slots[0].material if sceneObject.material_slots else None
  positions, _ = bridgeMeshAccess.readVertexArrays(sceneObject)
  description = {
    "name": sceneObject.name, "kind": definition["kind"], "material": material.name if material else None,
    "definition": definition, "levels": [round(float(positions[:, 2].min()), 2), round(float(positions[:, 2].max()), 2)],
    "visibleExtent": visibleExtent(sceneObject, ground), "flow": bodyFlow(sceneObject, definition), "sprays": describeSprays(sceneObject, definition),
  } | bridgeMeshAccess.meshCounts(sceneObject)
  return description | ({"built": report} if report is not None else {})


def settleSprays(sceneObject, definition, records, ground, report):
  """Place the body's sprays as built and, after a pool or river changed, those at the foot of every fall that sprays it."""
  placeSprays(sceneObject, records)
  if definition["kind"] != "fall":
    sprayedAgain = replaceFallFeet(ground)
    if sprayedAgain:
      report = report | {"fallFeetSprayedAgain": sprayedAgain}
  return report


def placeBody(name, definition, materialName, collection):
  bridgeObjects.requireNewName(name)
  material = requireLiquidMaterial(definition["kind"], materialName)
  definition = validatedDefinition(definition)
  ground = Ground()
  mesh, report, records = buildBody(name, definition, ground)
  mesh.materials.append(material)
  sceneObject = bpy.data.objects.new(name, mesh)
  sceneObject[bridgeMeshAccess.waterProperty] = json.dumps(definition)
  bridgeObjects.targetCollection(collection or waterCollectionName).objects.link(sceneObject)
  bpy.context.view_layer.update()
  report = settleSprays(sceneObject, definition, records, ground, report)
  return describeBody(sceneObject, ground, report)


def installBody(sceneObject, definition, mesh, material, ground, report, records):
  """Put a body's newly built mesh in place of its old one, with what it was made from, and its sprays where they now stand."""
  mesh.materials.append(material)
  oldMesh = sceneObject.data
  sceneObject.matrix_world = mathutils.Matrix.Identity(4)
  sceneObject.data = mesh
  bpy.data.meshes.remove(oldMesh)
  mesh.name = sceneObject.name
  sceneObject[bridgeMeshAccess.waterProperty] = json.dumps(definition)
  bpy.context.view_layer.update()
  report = settleSprays(sceneObject, definition, records, ground, report)
  return describeBody(sceneObject, ground, report)


def rebuildBody(sceneObject, definition, materialName):
  material = sceneObject.material_slots[0].material if materialName is None else requireLiquidMaterial(definition["kind"], materialName)
  definition = validatedDefinition(definition)
  ground = Ground()
  mesh, report, records = buildBody(sceneObject.name, definition, ground)
  return installBody(sceneObject, definition, mesh, material, ground, report, records)


def floodWater(name, seed, level, within, spacing, worldUnitsPerRepeat, material, collection):
  definition = {
    "kind": "pool", "seed": seed, "level": level, "within": within, "spacing": spacing, "worldUnitsPerRepeat": worldUnitsPerRepeat, "strokes": [],
    "sprays": [],
  }
  return placeBody(name, definition, material, collection)


def runWater(name, path, reach, spacing, worldUnitsPerRepeat, material, collection):
  definition = {"kind": "river", "path": path, "reach": reach, "spacing": spacing, "worldUnitsPerRepeat": worldUnitsPerRepeat, "strokes": [], "sprays": []}
  return placeBody(name, definition, material, collection)


def pourWaterfall(name, lip, bottom, throw, spread, spacing, worldUnitsPerRepeat, acrossRepeats, material, collection):
  definition = {
    "kind": "fall", "lip": lip, "bottom": bottom, "throw": throw, "spread": spread, "spacing": spacing, "worldUnitsPerRepeat": worldUnitsPerRepeat,
    "acrossRepeats": acrossRepeats, "sprays": [],
  }
  return placeBody(name, definition, material, collection)


def sprayWater(name, spray):
  """Add a spray to a body and place its emitters where the water strikes; the body's sprays go with it from then on."""
  sceneObject = bridgeMeshAccess.requireWater(name)
  definition = readDefinition(sceneObject)
  return rebuildBody(sceneObject, definition | {"sprays": definition["sprays"] + [spray]}, None)


editableFields = {
  "pool": ("seed", "level", "within", "spacing", "worldUnitsPerRepeat", "strokes", "swimmable", "sprays"),
  "river": ("path", "reach", "spacing", "worldUnitsPerRepeat", "strokes", "swimmable", "sprays"),
  "fall": ("lip", "bottom", "throw", "spread", "spacing", "worldUnitsPerRepeat", "acrossRepeats", "sprays"),
}


def editWater(name, changes, material):
  """Change what a body is made from and build it again against the ground as it now is; with no changes, only rebuild it."""
  sceneObject = bridgeMeshAccess.requireWater(name)
  definition = readDefinition(sceneObject)
  stray = sorted(set(changes) - set(editableFields[definition["kind"]]))
  if stray:
    raise ValueError(f"A {definition['kind']} takes {list(editableFields[definition['kind']])}; not {stray}")
  if "within" in changes and changes["within"] == []:
    changes["within"] = None
  if changes.get("swimmable") is False:
    boxes = sorted(
      box.name for box in bpy.context.scene.objects
      if bridgeMeshAccess.swimProperty in box and json.loads(box[bridgeMeshAccess.swimProperty])["body"] == name
    )
    if boxes:
      raise ValueError(f"'{name}' has swim volumes {boxes}; a body no one swims in has none. Delete them (deleteObjects) first, or keep it swimmable")
  return rebuildBody(sceneObject, definition | changes, material)


def meshShape(mesh, matrix):
  """A mesh's face count and its vertices in world space, sorted, to tell whether a rebuild changed anything."""
  coordinates = numpy.empty(len(mesh.vertices) * 3)
  mesh.vertices.foreach_get("co", coordinates)
  transform = bridgeMeshAccess.matrixArray(matrix)
  world = coordinates.reshape(-1, 3) @ transform[:3, :3].T + transform[:3, 3]
  return len(mesh.polygons), sorted(map(tuple, numpy.round(world, 3).tolist()))


def unchangedStrokeReason(definition, mode, area, ground):
  """Why a stroke left a body as it was, from the ground at the body's grid points inside the stroke's area."""
  if mode == "remove":
    return "none of the water lies inside the area yet; it stops the water there once an edit spreads it so far"
  spacing = definition["spacing"]
  if "circle" in area:
    center, radius = numpy.asarray(area["circle"]["center"], dtype=numpy.float64), area["circle"]["radius"]
    low, high = center - radius, center + radius
  else:
    polygon = numpy.asarray(area["polygon"], dtype=numpy.float64)
    low, high = polygon.min(0), polygon.max(0)
  keys = [
    (i, j) for i in range(math.ceil(low[0] / spacing), math.floor(high[0] / spacing) + 1)
    for j in range(math.ceil(low[1] / spacing), math.floor(high[1] / spacing) + 1)
  ]
  points = numpy.array(keys, dtype=numpy.float64).reshape(-1, 2) * spacing
  keys = [key for key, inside in zip(keys, insideArea(points, area)) if inside]
  if not keys:
    return f"the area holds no point of the body's {spacing}-unit grid; make it larger"
  points = numpy.array(keys, dtype=numpy.float64) * spacing
  low = [key for key, (x, y), level in zip(keys, points, levelsAt(definition, points)) if ground.depth(x, y, level)]
  if not low:
    return "the ground inside the area stands at or above the water's level there, so no water would stand in it"
  wet, _, _ = flood(definition, ground)
  cutOff = [key for key in low if key not in wet]
  if not cutOff:
    return "the water already covers the low ground inside the area"
  return (
    f"the ground inside the area dips below the water's level at {len(cutOff)} grid points (such as"
    f" {[round(value * spacing, 1) for value in cutOff[0]]}), but nothing joins them to the water: higher ground, or the body's own"
    " bounds (within, reach, other strokes), lie between. Open a way through, stroke the gap in too, or flood the low ground as its own body"
  )


def shapeWaterExtent(name, mode, area):
  """Stroke where a pool or river may spread: add lets it flood an area its bounds left out, remove stops it where it leaks; a stroke that changes nothing yet is kept and says why."""
  sceneObject = bridgeMeshAccess.requireWater(name)
  definition = readDefinition(sceneObject)
  if definition["kind"] == "fall":
    raise ValueError(f"'{name}' is a fall; its extent is its lip, edited with editWater")
  if mode not in strokeModes:
    raise ValueError(f"mode is one of {list(strokeModes)}, got '{mode}'")
  definition = validatedDefinition(definition | {"strokes": definition["strokes"] + [{"mode": mode, "area": requireArea(area)}]})
  ground = Ground()
  mesh, report, records = buildBody(name, definition, ground)
  changed = meshShape(mesh, mathutils.Matrix.Identity(4)) != meshShape(sceneObject.data, sceneObject.matrix_world)
  description = installBody(sceneObject, definition, mesh, sceneObject.material_slots[0].material, ground, report, records)
  if changed:
    return description | {"strokeChanged": True}
  return description | {"strokeChanged": False, "whyUnchanged": unchangedStrokeReason(definition, mode, area, ground)}


def getWater():
  bpy.context.view_layer.update()
  bodies = [sceneObject for sceneObject in bpy.context.scene.objects if bridgeMeshAccess.waterProperty in sceneObject]
  ground = Ground() if bodies else None
  return {"bodies": [describeBody(sceneObject, ground) for sceneObject in bodies]}


def smoothStep(values):
  return values * values * (3 - 2 * values)


def carveWaterBed(name, objectName, depth, shoreWidth):
  """Lower the ground under a pool or river, `depth` under its surface from `shoreWidth` out from its waterline, after cutting the ground along the waterline so the shore stays put."""
  waterObject = bridgeMeshAccess.requireWater(name)
  if readDefinition(waterObject)["kind"] == "fall":
    raise ValueError(f"'{name}' is a fall; a fall has no bed")
  if depth <= 0 or shoreWidth <= 0:
    raise ValueError(f"depth and shoreWidth must be positive, got {depth} and {shoreWidth}")
  sceneObject = bridgeMeshAccess.requireMeshObject(objectName)
  surface = bridgeMeshAccess.WaterSurface(waterObject)
  positions, _ = bridgeMeshAccess.readVertexArrays(sceneObject)
  if not (surface.rise(positions) < -bridgeMeshAccess.waterlineTolerance).any():
    raise ValueError(f"No vertex of '{objectName}' lies under '{name}'")
  measure = bridgeShaping.WaterlineMeasure(sceneObject, positions, surface, [0])
  waterlineCut = bridgeShaping.cutAlongLevels(sceneObject, positions, measure, [0], numpy.ones(len(sceneObject.data.polygons), dtype=bool))
  positions, _ = bridgeMeshAccess.readVertexArrays(sceneObject)
  rise = surface.rise(positions)
  under = rise < -bridgeMeshAccess.waterlineTolerance
  waterline = surface.waterline(positions, bridgeMeshAccess.meshTriangles(sceneObject))
  fromShore = bridgeMeshAccess.waterlineDistances(waterline, positions[under])
  targets = positions[under, 2] - rise[under] - depth * smoothStep(numpy.clip(fromShore / shoreWidth, 0, 1))
  updated = positions.copy()
  updated[under, 2] = numpy.where(targets < positions[under, 2] - bridgeMeshAccess.waterlineTolerance, targets, positions[under, 2])
  left = bridgeShaping.writeWorldPositions(sceneObject, updated)
  return bridgeShaping.moveSummary(sceneObject, positions, updated) | {
    "underWater": int(under.sum()), "deepestBed": round(float(updated[under, 2].min()), 2), "waterlineCut": waterlineCut,
  } | left


# Names

def fileStem(text):
  return re.sub(r"[^a-z0-9]+", "", text.lower())


commands = {
  "floodWater": (floodWater, True),
  "runWater": (runWater, True),
  "pourWaterfall": (pourWaterfall, True),
  "sprayWater": (sprayWater, True),
  "editWater": (editWater, True),
  "shapeWaterExtent": (shapeWaterExtent, True),
  "carveWaterBed": (carveWaterBed, True),
  "getWater": (getWater, False),
}
