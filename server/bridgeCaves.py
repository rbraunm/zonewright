"""Caves cut into a terrain mesh as the client's own are built: a tube swept along a floor path is subtracted with the exact boolean
from the terrain around it and spliced back into the same mesh, so one terrain holds the hill and the room under it. The terrain faces
the cut changed (the plug) are recorded and deleted as faces only: their vertices stay and take every shaping pass, so taking the cave
back puts the ground back exactly. The new vertices where the tube meets the ground (the ring) follow the plug's triangles in every
pass, so the mouth stays sealed whatever shapes the ground, and the tube's own surface (the lining) holds no offset in any pass. Every
tool that shapes, cuts, or paints the terrain keeps to that through the guards in bridgeCaveData. A cave's definition is kept on the
terrain, so it is cut again whole (editCave) once the ground under it moves. Runs under Blender's Python."""
import contextlib
import json
import math

import bmesh
import bpy
import mathutils
import mathutils.bvhtree
import mathutils.kdtree
import numpy

import bridgeAuthoring
import bridgeCaveData
import bridgeMeshAccess
import bridgeNoise
import bridgePasses
import bridgeSurfacing
import playerScale

# A pass holds single precision: a ring vertex reads back this close to its plug triangle, and ground counts as moved past this.
ringTolerance = 1e-4
groundTolerance = 1e-3
floorNormalZ = 0.7
# The terrain patch around the tube reaches past its walls by three breakup amplitudes (noise's farthest reach) and two edges.
breakupReach = 3.0
patchEdges = 2.0
solidDepth = 20.0
matchDistance = 1e-4
mouthEdgeShare = 1.0 / 3.0
weldRounds = 8
# Candidate rows lie this share of edgeLength apart; rows are kept so no point of the section moves more than edgeLength between two.
rowSampleShare = 0.25
breakupOctaves = 3
breakupRoughness = 0.5
levelTolerance = 0.01
patchChunk = 2048
patchRounds = 8
# A blind end's rows run round its quarter ellipse down to this share of the section, then close on an apex.
endShrink = 0.3
endRows = 16
# traceLedge looks for a cliff's face, steeper than 45 degrees, this many widths to the side, settles each point's floor in this many
# rounds, and measures the share of the width inside the rock at this many points across it.
faceReachWidths = 3.0
faceNormalZ = math.cos(math.radians(45.0))
traceRounds = 3
shareSamples = 81
definitionDefaults = {"edgeLength": 16.0, "wallShare": 0.35, "breakup": None, "mouthFade": None, "maximumFloorDegrees": 30.0, "trimBands": None}
definitionKeys = ("path", "widths", "heights", "wallMaterial", "floorMaterial", "worldUnitsPerRepeat") + tuple(definitionDefaults)
bandKeys = {"fromFloor", "height", "material", "worldUnitsPerRepeat"}
# Two section stops closer than this in every row are one stop.
stopTolerance = 1e-6
# Which tube face each piece of lining came from, while a cut is spliced; never written to the mesh.
tubeFaceLayerName = "zonewrightCaveTubeFace"
cornerOutward = math.sqrt(0.5)
up = mathutils.Vector((0.0, 0.0, 1.0))
down = mathutils.Vector((0.0, 0.0, -1.0))


def replayStrokes(sceneObject, name, strokes):
  """Paint a newly cut lining with the strokes kept with its cave, each confined to that lining."""
  tools = {
    "paintSurface": bridgeAuthoring.paintSurface, "eraseSurface": bridgeAuthoring.eraseSurface, "editSurface": bridgeAuthoring.editSurface,
    "paintTransition": bridgeAuthoring.paintTransition,
  }
  for stroke in strokes:
    arguments = dict(stroke["arguments"])
    arguments["selector"] = {"and": [arguments["selector"], {"cave": name}]}
    tools[stroke["tool"]](sceneObject.name, **arguments, keepCaveStroke=False)


# Definitions

def caveDefinition(path, widths, heights, wallMaterial, floorMaterial, worldUnitsPerRepeat, edgeLength, wallShare, breakup, mouthFade, maximumFloorDegrees, trimBands=None):
  if not isinstance(path, list) or len(path) < 2 or any(len(point) != 3 for point in path):
    raise ValueError(f"A cave's path is at least two [x, y, z] floor points, got {path!r}")
  if len(widths) != len(path) or len(heights) != len(path) or min(widths) <= 0 or min(heights) <= 0:
    raise ValueError(f"widths and heights are one positive value per path point ({len(path)}), got {widths!r} and {heights!r}")
  if edgeLength <= 0 or worldUnitsPerRepeat <= 0:
    raise ValueError(f"edgeLength and worldUnitsPerRepeat are positive, got {edgeLength} and {worldUnitsPerRepeat}")
  if not 0 < wallShare <= 1:
    raise ValueError(
      f"wallShare is the share of the height the walls rise straight before the vault, above 0 and at most 1 (a hall: straight walls under"
      f" a flat ceiling), got {wallShare}"
    )
  if breakup is not None and (not isinstance(breakup, dict) or set(breakup) != {"featureSize", "amplitude", "seed"} or breakup["featureSize"] <= 0 or breakup["amplitude"] <= 0):
    raise ValueError(f"breakup is {{featureSize, amplitude, seed}} with a positive featureSize and amplitude, got {breakup!r}")
  if mouthFade is not None and mouthFade < 0:
    raise ValueError(f"mouthFade is a distance, at least 0, got {mouthFade}")
  if not 0 < maximumFloorDegrees < 60:
    raise ValueError(f"maximumFloorDegrees is above 0 and under 60 (steeper is not walked), got {maximumFloorDegrees}")
  for material in (wallMaterial, floorMaterial):
    if bpy.data.materials.get(material) is None:
      raise ValueError(f"No material named '{material}'")
  definition = {
    "path": [[float(value) for value in point] for point in path], "widths": [float(value) for value in widths], "heights": [float(value) for value in heights],
    "wallMaterial": wallMaterial, "floorMaterial": floorMaterial, "worldUnitsPerRepeat": float(worldUnitsPerRepeat), "edgeLength": float(edgeLength),
    "wallShare": float(wallShare), "breakup": None if breakup is None else {"featureSize": float(breakup["featureSize"]), "amplitude": float(breakup["amplitude"]), "seed": int(breakup["seed"])},
    "mouthFade": None if mouthFade is None else float(mouthFade), "maximumFloorDegrees": float(maximumFloorDegrees),
    "trimBands": bandDefinitions(trimBands),
  }
  wallGaps(definition)
  return definition


def bandDefinitions(trimBands):
  """Trim bands as a definition keeps them, refusing any that is not a band of the walls with a material of its own."""
  if trimBands is None:
    return []
  if not isinstance(trimBands, list):
    raise ValueError(f"trimBands is a list of {{fromFloor, height, material, worldUnitsPerRepeat}}, got {trimBands!r}")
  bands = []
  for index, band in enumerate(trimBands):
    if not isinstance(band, dict) or set(band) != bandKeys:
      raise ValueError(f"Trim band {index} is {{fromFloor, height, material, worldUnitsPerRepeat}}, got {band!r}")
    if band["fromFloor"] < 0 or band["height"] <= 0:
      raise ValueError(f"Trim band {index} starts at fromFloor, at least 0 over the floor, and is a positive height tall, got fromFloor {band['fromFloor']} and height {band['height']}")
    if band["worldUnitsPerRepeat"] <= 0:
      raise ValueError(f"Trim band {index}'s worldUnitsPerRepeat is positive, got {band['worldUnitsPerRepeat']}")
    material = bpy.data.materials.get(band["material"])
    if material is None or bridgeSurfacing.cutoutPropertyName not in material:
      raise ValueError(f"Trim band {index}'s material '{band['material']}' is not a material createMaterial made; make it with createMaterial first")
    bands.append({"fromFloor": float(band["fromFloor"]), "height": float(band["height"]), "material": band["material"], "worldUnitsPerRepeat": float(band["worldUnitsPerRepeat"])})
  ordered = sorted(range(len(bands)), key=lambda index: bands[index]["fromFloor"])
  for lower, upper in zip(ordered, ordered[1:]):
    top = bands[lower]["fromFloor"] + bands[lower]["height"]
    if bands[upper]["fromFloor"] < top - stopTolerance:
      raise ValueError(
        f"Trim bands {lower} ({bands[lower]['fromFloor']:g} to {top:g} over the floor) and {upper} ({bands[upper]['fromFloor']:g} to"
        f" {bands[upper]['fromFloor'] + bands[upper]['height']:g}) overlap; bands may meet but not overlap"
      )
  return bands


def wallGaps(definition):
  """The stretches the walls are cut into from the floor up, between the floor, each trim band's edges, and the top of the straight
  walls: each stretch's ends as (share of the row's height, units over the floor), its trim band (or -1), and how many points it takes
  to keep its edges within edgeLength in the tallest row. Stops that meet in every row are one stop; refuses a band reaching above the
  straight walls, or meeting their top in some rows but not others."""
  heights = numpy.array(definition["heights"])
  wallShare = definition["wallShare"]
  for index, band in enumerate(definition["trimBands"]):
    top = band["fromFloor"] + band["height"]
    over = numpy.flatnonzero(top > wallShare * heights + stopTolerance)
    if len(over):
      point = int(over[0])
      raise ValueError(
        f"Trim band {index} ({band['fromFloor']:g} to {top:g} over the floor) reaches above the walls' straight part at path point {point}"
        f" {roundedPoint(definition['path'][point])}, where the walls rise straight {wallShare * heights[point]:g} (wallShare {wallShare:g} of the"
        f" height {heights[point]:g}); lower the band, or raise the height or wallShare there"
      )
  stops = [((0.0, 0.0), -1)]
  for index, band in sorted(enumerate(definition["trimBands"]), key=lambda item: item[1]["fromFloor"]):
    stops += [((0.0, band["fromFloor"]), index), ((0.0, band["fromFloor"] + band["height"]), -1)]
  stops.append(((wallShare, 0.0), -1))
  gaps = []
  for (low, band), (high, _) in zip(stops, stops[1:]):
    lengths = (high[0] - low[0]) * heights + high[1] - low[1]
    closed = lengths <= stopTolerance
    if closed.all():
      continue
    if closed.any():
      point = int(numpy.flatnonzero(closed)[0])
      raise ValueError(
        f"A trim band's top meets the top of the straight walls at path point {point} {roundedPoint(definition['path'][point])} but runs below it"
        " elsewhere; run it below the walls' top everywhere, or along it everywhere"
      )
    gaps.append({"low": low, "high": high, "band": band, "count": max(1, math.ceil(lengths.max() / definition["edgeLength"])), "shortest": float(lengths.min())})
  return gaps


def smoothstep(share):
  return share * share * (3 - 2 * share)


class CaveLine:
  """A cave's floor centerline in plan: its path's straight runs joined by an arc at each bend, a station at each path point (an arc's
  middle at a bend), the floor graded evenly by plan length between stations, and the widths and heights eased between them."""

  def __init__(self, definition):
    path = numpy.array(definition["path"], dtype=numpy.float64)
    plan = path[:, :2]
    self.floors = path[:, 2]
    self.widths = numpy.array(definition["widths"], dtype=numpy.float64)
    self.heights = numpy.array(definition["heights"], dtype=numpy.float64)
    runs = numpy.diff(plan, axis=0)
    lengths = numpy.linalg.norm(runs, axis=1)
    for index in numpy.flatnonzero(lengths < 1e-6):
      raise ValueError(f"Points {index} and {index + 1} of the cave's path stand at one place in plan")
    directions = runs / lengths[:, None]
    self.lines, self.arcs = [], []
    stations, along, start = [0.0], 0.0, plan[0]
    for index in range(1, len(path) - 1):
      incoming, outgoing = directions[index - 1], directions[index]
      cross = float(incoming[0] * outgoing[1] - incoming[1] * outgoing[0])
      turn = math.atan2(abs(cross), float(incoming @ outgoing))
      if turn > math.pi - 1e-6:
        raise ValueError(f"The cave's path turns straight back on itself at point {index}")
      if turn < 1e-9:
        along = self.addLine(start, plan[index], along)
        stations.append(along)
        start = plan[index]
        continue
      room = min(lengths[index - 1], lengths[index]) / 2
      radius = min(self.widths[index], room / math.tan(turn / 2))
      if radius <= self.widths[index] / 2:
        raise ValueError(
          f"The cave's path bends at point {index} tighter than half its width there: it turns {math.degrees(turn):.1f} degrees with"
          f" {room:.1f} of the path on each side to turn in, so its floor's middle would turn on a radius of {radius:.1f}, no more than"
          f" half its width ({self.widths[index] / 2:.1f}), and its inner wall would fold. Move the points around the bend apart or turn less sharply"
        )
      tangent = radius * math.tan(turn / 2)
      turnStart = plan[index] - incoming * tangent
      along = self.addLine(start, turnStart, along)
      sign = 1.0 if cross >= 0 else -1.0
      center = turnStart + numpy.array([-incoming[1], incoming[0]]) * sign * radius
      self.arcs.append((center, radius, math.atan2(turnStart[1] - center[1], turnStart[0] - center[0]), sign * turn, along))
      stations.append(along + radius * turn / 2)
      along += radius * turn
      start = plan[index] + outgoing * tangent
    self.length = self.addLine(start, plan[-1], along)
    stations.append(self.length)
    self.stations = numpy.array(stations)

  def addLine(self, start, end, along):
    length = float(numpy.linalg.norm(end - start))
    if length > 1e-9:
      self.lines.append((start, end, along))
    return along + length

  def requireGrades(self, maximumDegrees):
    for index, (run, rise) in enumerate(zip(numpy.diff(self.stations), numpy.diff(self.floors))):
      degrees = math.degrees(math.atan2(abs(rise), run))
      if degrees > maximumDegrees + 1e-9:
        raise ValueError(
          f"The cave's floor {'rises' if rise > 0 else 'falls'} {abs(rise):.1f} from point {index} to point {index + 1} over a run of"
          f" {run:.1f}, {degrees:.1f} degrees, steeper than {maximumDegrees:g}; at {maximumDegrees:g} degrees it needs a run of"
          f" {abs(rise) / math.tan(math.radians(maximumDegrees)):.1f}"
        )

  def at(self, alongs):
    """Floor points, plan directions, widths, and heights at distances along the line."""
    alongs = numpy.clip(numpy.asarray(alongs, dtype=numpy.float64), 0.0, self.length)
    points, directions = numpy.zeros((len(alongs), 2)), numpy.zeros((len(alongs), 2))
    for start, end, lineAlong in self.lines:
      length = float(numpy.linalg.norm(end - start))
      inside = (alongs >= lineAlong - 1e-9) & (alongs <= lineAlong + length + 1e-9)
      points[inside] = start + numpy.clip((alongs[inside] - lineAlong) / length, 0, 1)[:, None] * (end - start)
      directions[inside] = (end - start) / length
    for center, radius, startAngle, sweep, arcAlong in self.arcs:
      inside = (alongs >= arcAlong - 1e-9) & (alongs <= arcAlong + radius * abs(sweep) + 1e-9)
      angles = startAngle + numpy.clip((alongs[inside] - arcAlong) / radius, 0, abs(sweep)) * math.copysign(1.0, sweep)
      points[inside] = center + radius * numpy.column_stack([numpy.cos(angles), numpy.sin(angles)])
      directions[inside] = math.copysign(1.0, sweep) * numpy.column_stack([-numpy.sin(angles), numpy.cos(angles)])
    span = numpy.clip(numpy.searchsorted(self.stations, alongs, side="right") - 1, 0, len(self.stations) - 2)
    share = (alongs - self.stations[span]) / (self.stations[span + 1] - self.stations[span])
    floors = self.floors[span] + share * (self.floors[span + 1] - self.floors[span])
    eased = smoothstep(share)
    widths = self.widths[span] + eased * (self.widths[span + 1] - self.widths[span])
    heights = self.heights[span] + eased * (self.heights[span + 1] - self.heights[span])
    return numpy.column_stack([points, floors]), directions, widths, heights

  def levelStretches(self):
    """Each run of the path whose floor stays level and whose width stays the same (a flare between two widths its own run): where it
    starts and ends, its length, floor height, and narrowest width."""
    runs = []
    for index in range(len(self.floors) - 1):
      if abs(self.floors[index + 1] - self.floors[index]) > levelTolerance:
        continue
      constant = self.widths[index + 1] == self.widths[index]
      previous = runs[-1] if runs else None
      if previous is not None and previous[1] == index and previous[2] and constant and self.widths[index] == self.widths[previous[0]]:
        previous[1] = index + 1
      else:
        runs.append([index, index + 1, constant])
    stretches = []
    for first, last, _ in runs:
      ends, _, _, _ = self.at(numpy.array([self.stations[first], self.stations[last]]))
      stretches.append({
        "points": [first, last], "start": [round(float(value), 1) for value in ends[0, :2]], "end": [round(float(value), 1) for value in ends[1, :2]],
        "length": round(float(self.stations[last] - self.stations[first]), 1), "floor": round(float(self.floors[first]), 2),
        "width": round(float(self.widths[first:last + 1].min()), 1),
      })
    return stretches


def sectionShape(definition):
  """The tube's section counterclockwise looking along it, from the floor's left corner: each point across (a share of the width) and up
  (a share of the height plus units over the floor, both shrinking with the rows a rounded end adds), its outward direction, and the
  trim band (or -1) of the stretch from it to the next point. Enough points for the widest and tallest stretch at edgeLength: the floor
  evenly across, each wall in its stretches between the floor, the band edges, and its top (wallGaps), then a vault, or for a hall
  (wallShare 1) a flat ceiling evenly across with square corners."""
  edgeLength, wallShare = definition["edgeLength"], definition["wallShare"]
  widest, tallest = max(definition["widths"]), max(definition["heights"])
  floorCount = max(2, math.ceil(widest / edgeLength))
  gaps = wallGaps(definition)
  points = [(-0.5 + index / floorCount, 0.0, 0.0, (0.0, -1.0), -1) for index in range(floorCount)]
  for gap in gaps:
    for step in range(gap["count"]):
      points.append((0.5, *gapStop(gap, step / gap["count"]), (1.0, 0.0), gap["band"]))
  if wallShare < 1:
    vault = math.pi * math.sqrt(((widest / 2) ** 2 + ((1 - wallShare) * tallest) ** 2) / 2)
    arcCount = max(4, math.ceil(vault / edgeLength))
    for index in range(arcCount):
      angle = math.pi * index / arcCount
      points.append((0.5 * math.cos(angle), wallShare + (1 - wallShare) * math.sin(angle), 0.0, (math.cos(angle), math.sin(angle)), -1))
  else:
    points += [(0.5 - index / floorCount, 1.0, 0.0, (cornerOutward, cornerOutward) if index == 0 else (0.0, 1.0), -1) for index in range(floorCount)]
  for gap in reversed(gaps):
    for step in range(gap["count"]):
      corner = wallShare == 1 and gap is gaps[-1] and step == 0
      points.append((-0.5, *gapStop(gap, 1 - step / gap["count"]), (-cornerOutward, cornerOutward) if corner else (-1.0, 0.0), gap["band"]))
  return {
    "across": numpy.array([point[0] for point in points]), "upShare": numpy.array([point[1] for point in points]),
    "upUnits": numpy.array([point[2] for point in points]), "outward": numpy.array([point[3] for point in points]),
    "bands": numpy.array([point[4] for point in points]), "floorCount": floorCount, "wallCount": sum(gap["count"] for gap in gaps),
    "onFloor": numpy.array([point[1] == 0.0 and point[2] == 0.0 for point in points]),
    "shortestStretch": min(gap["shortest"] / gap["count"] for gap in gaps),
  }


def gapStop(gap, share):
  """The point share of the way up a wall stretch, as (share of the row's height, units over the floor)."""
  return tuple((1 - share) * low + share * high for low, high in zip(gap["low"], gap["high"]))


def sectionPoints(shape, floors, directions, widths, heights, scales):
  """World points of the section at each row (scales: how far a rounded end's row has shrunk, 1 elsewhere): rows x points x 3."""
  right = numpy.column_stack([directions[:, 1], -directions[:, 0], numpy.zeros(len(directions))])
  rise = shape["upShare"][None, :] * heights[:, None] + shape["upUnits"][None, :] * scales[:, None]
  return floors[:, None, :] + (shape["across"][None, :, None] * widths[:, None, None]) * right[:, None, :] + rise[..., None] * numpy.array([0.0, 0.0, 1.0])


class TerrainSurface:
  """The terrain (its triangles over shown positions) as it is seen near a line in plan, for casts. Only its triangles with a corner
  within `margin` of the line's plan box are taken, so a large terrain costs no more than the ground in reach."""

  def __init__(self, shown, triangles, plan, margin):
    plan = numpy.asarray(plan, dtype=numpy.float64)[:, :2]
    near = ((shown[:, :2] >= plan.min(axis=0) - margin) & (shown[:, :2] <= plan.max(axis=0) + margin)).all(axis=1)
    triangles = triangles[near[triangles].any(axis=1)]
    used, local = numpy.unique(triangles, return_inverse=True)
    self.tree = mathutils.bvhtree.BVHTree.FromPolygons(shown[used].tolist(), local.reshape(-1, 3).tolist())

  def depths(self, points):
    """How far each point lies under the surface (the first face above it faces up, met from behind): 0 for a point in the open."""
    depths = numpy.zeros(len(points))
    for index, point in enumerate(points.tolist()):
      location, normal, _, distance = self.tree.ray_cast(mathutils.Vector(point), up)
      if location is not None and normal.z > 0:
        depths[index] = distance
    return depths

  def drops(self, points):
    """How far below each point the first surface lies: infinity where none does."""
    drops = numpy.full(len(points), numpy.inf)
    for index, point in enumerate(points.tolist()):
      location, _, _, distance = self.tree.ray_cast(mathutils.Vector(point), down)
      if location is not None:
        drops[index] = distance
    return drops

  def onGround(self, points):
    """Whether each point stands within a step of walkable ground: over it in the open by a step or less, or under it by a step or less
    where the ground over it is walkable rather than a cliff's face."""
    standing = numpy.zeros(len(points), dtype=bool)
    for index, point in enumerate(points.tolist()):
      origin = mathutils.Vector(point)
      location, normal, _, distance = self.tree.ray_cast(origin, up)
      if location is not None and normal.z > 0:
        standing[index] = distance <= playerScale.stepHeight and normal.z >= playerScale.walkableNormalZ
      else:
        location, _, _, distance = self.tree.ray_cast(origin, down)
        standing[index] = location is not None and distance <= playerScale.stepHeight
    return standing

  def faceBeside(self, point, toSide, reach):
    """Where a cliff's face stands beside a point at its height, looking toward toSide: the point moved toward it onto the surface, or
    back out onto the surface when it lies inside the rock; None when no face steeper than half upright lies within reach (a bump of
    ground past a cliff's top is no face)."""
    direction = -toSide if self.depths(point[None])[0] > 0 else toSide
    location, normal, _, distance = self.tree.ray_cast(mathutils.Vector(point.tolist()), mathutils.Vector(direction.tolist()), reach)
    return None if location is None or abs(normal.z) > faceNormalZ else point + direction * distance


def caveSurface(shown, triangles, definition):
  """The terrain around a cave's path out to everything its tube and breakup can reach."""
  amplitude = definition["breakup"]["amplitude"] if definition["breakup"] is not None else 0.0
  margin = max(definition["widths"]) + max(definition["heights"]) + breakupReach * amplitude + patchEdges * definition["edgeLength"]
  return TerrainSurface(shown, triangles, definition["path"], margin)


def roundedPoint(point):
  return [round(float(value), 1) for value in point]


def requireFloorOnRock(surface, floors):
  """Refuse a floor hanging in the air: a sample of its middle neither inside the rock nor within a step above the ground."""
  inOpen = numpy.flatnonzero(surface.depths(floors) <= 0)
  gaps = numpy.zeros(len(floors))
  gaps[inOpen] = surface.drops(floors[inOpen])
  hanging = gaps > playerScale.stepHeight
  if not hanging.any():
    return
  first = int(numpy.argmax(hanging))
  last = first + int(numpy.argmin(numpy.append(hanging[first:], False))) - 1
  stretches = int((numpy.diff(hanging.astype(numpy.int8)) == 1).sum()) + int(hanging[0])
  gap = float(gaps[first:last + 1].max())
  raise ValueError(
    f"The cave's floor hangs in the air from {roundedPoint(floors[first])} to {roundedPoint(floors[last])}"
    + (f" and in {stretches - 1} more stretches" if stretches > 1 else "")
    + f": no rock lies under its middle within a step ({playerScale.stepHeight:g}), the ground {'nowhere' if math.isinf(gap) else f'up to {gap:.1f}'} below it."
    " Keep the floor inside the rock or on the ground; a gallery along a cliff wants more of its width inside the rock (traceLedge insideShare)"
  )


def endKind(definition, shape, surface, floor, direction, width, height, end):
  """How one end of a tube meets the ground, and whether it is rounded off: open (some of its floor on the ground: a mouth wholly in
  the open but for a sill a step deep, or a gallery's end beside a cliff, part in the rock), ledge (part in the rock with its floor
  running out over a drop beside it), or blind (wholly inside the rock). A hall's ends are never rounded, and stand wholly in the open
  or wholly in the rock."""
  cap = sectionPoints(shape, floor[None], direction[None], numpy.array([width]), numpy.array([height]), numpy.ones(1))[0]
  depths = surface.depths(cap)
  hall = definition["wallShare"] == 1
  if (depths > 0).all():
    return "blind", not hall
  if depths.max() <= playerScale.stepHeight:
    return "open", False
  if hall:
    raise ValueError(
      f"The hall's {end} at {roundedPoint(floor)} is part in the rock (up to {depths.max():.1f} into it) and part in the open: a hall's mouth"
      " stands in front of the cliff's face, which dressFacade dresses, and its far end inside the rock"
    )
  if surface.onGround(cap[shape["onFloor"]]).any():
    return "open", True
  if (depths[shape["onFloor"]] <= 0).any():
    return "ledge", True
  raise ValueError(
    f"The cave's {end} at {roundedPoint(floor)} is part in the rock (up to {depths.max():.1f} into it) and part in the open, its"
    " floor buried more than a step under the ground; end it with its floor on the ground in front of it or beside it, or wholly inside the rock"
  )


def tubeRows(definition, line, surface):
  """The tube's rows (floor points, plan directions, widths, heights, and scales: how far a rounded end's row has shrunk, which its
  breakup and band edges shrink with) and each end's kind (endKind). An end with rock in its section is rounded off, closing over half
  its width beyond its last point on an apex at floor height, as a dome closes; a hall's blind end closes as a flat wall."""
  edgeLength = definition["edgeLength"]
  shape = sectionShape(definition)
  candidates = numpy.unique(numpy.concatenate([numpy.arange(0.0, line.length, edgeLength * rowSampleShare), line.stations]))
  floors, directions, widths, heights = line.at(candidates)
  requireFloorOnRock(surface, floors)
  ends, rounded = {}, {}
  for end, row in (("start", 0), ("end", -1)):
    ends[end], rounded[end] = endKind(definition, shape, surface, floors[row], directions[row], widths[row], heights[row], end)
  if set(ends.values()) == {"blind"}:
    raise ValueError("Both ends of the cave lie wholly inside the rock, so nothing would open into it; start or end it on open ground in front of its mouth")
  rows = [(floors, directions, widths, heights, numpy.ones(len(candidates)), numpy.isin(candidates, line.stations))]
  apexes = {}
  for end, row, sign in (("start", 0, -1.0), ("end", -1, 1.0)):
    if not rounded[end]:
      continue
    reach = widths[row] / 2
    # Rows by angle round a quarter ellipse bunch toward its tip; below endShrink an apex closes it, and the breakup shrinks with the
    # section, so the end rounds off as a dome rather than standing as a wall on a flat cap.
    angles = numpy.linspace(0.0, math.acos(endShrink), endRows)[1:]
    beyond, shrink = reach * numpy.sin(angles), numpy.cos(angles)
    extended = (
      numpy.column_stack([floors[row, :2] + sign * beyond[:, None] * directions[row], numpy.full(len(beyond), floors[row, 2])]),
      numpy.repeat(directions[[row]], len(beyond), axis=0), widths[row] * shrink, heights[row] * shrink, shrink, numpy.zeros(len(beyond), dtype=bool),
    )
    apexes[end] = numpy.array([*(floors[row, :2] + sign * reach * directions[row]), floors[row, 2]])
    depths = surface.depths(numpy.vstack([sectionPoints(shape, *extended[:5]).reshape(-1, 3), apexes[end] + [0.0, 0.0, playerScale.stepHeight]]))
    if ends[end] == "blind" and not (depths > 0).all():
      raise ValueError(f"The cave's blind {end} at {[round(float(value), 1) for value in floors[row]]} is rounded off over {reach:.1f} beyond it, which reaches out of the rock; end it deeper inside")
    if end == "start":
      rows.insert(0, tuple(numpy.flip(part, axis=0) for part in extended))
    else:
      rows.append(extended)
  floors, directions, widths, heights, scales, stations = (numpy.concatenate([part[index] for part in rows]) for index in range(6))
  sections = sectionPoints(shape, floors, directions, widths, heights, scales)
  kept = [0]
  for index in range(1, len(floors)):
    if stations[index] or index == len(floors) - 1 or numpy.linalg.norm(sections[index + 1] - sections[kept[-1]], axis=1).max() > edgeLength:
      kept.append(index)
  return {
    "shape": shape, "floors": floors[kept], "directions": directions[kept], "widths": widths[kept], "heights": heights[kept],
    "scales": scales[kept], "ends": ends, "rounded": rounded, "apexes": apexes,
  }


def tubeMesh(definition, rows, surface):
  """The tube's world vertices, outward faces, and for each face the rows it spans (the end's row twice for a cap) and its trim band (or
  -1): the rows' sections with the walls and vault broken up along their outward directions, the breakup fading out within mouthFade of
  wherever the tube lies in the open. A face is in a band only between two rows of the tube itself, not those a rounded end adds."""
  shape = rows["shape"]
  sections = sectionPoints(shape, rows["floors"], rows["directions"], rows["widths"], rows["heights"], rows["scales"])
  count, size = sections.shape[:2]
  vertices = sections.reshape(-1, 3)
  breakup = definition["breakup"]
  if breakup is not None:
    right = numpy.column_stack([rows["directions"][:, 1], -rows["directions"][:, 0], numpy.zeros(count)])
    outward = (shape["outward"][None, :, 0, None] * right[:, None, :] + shape["outward"][None, :, 1, None] * numpy.array([0.0, 0.0, 1.0])).reshape(-1, 3)
    rise = (sections[:, :, 2] - rows["floors"][:, None, 2]) / rows["heights"][:, None]
    # The breakup grows from nothing at the floor to its full amplitude where the vault springs, so the floor's edge stays one clean
    # line and the walls lean out of it rather than standing on a straight band.
    weights = (numpy.clip(rise / definition["wallShare"], 0, 1) * rows["scales"][:, None]).ravel()
    fade = definition["mouthFade"] if definition["mouthFade"] is not None else 2 * definition["edgeLength"]
    outside = surface.depths(vertices) <= 0
    if fade > 0 and outside.any():
      tree = mathutils.kdtree.KDTree(int(outside.sum()))
      for index, point in enumerate(vertices[outside].tolist()):
        tree.insert(point, index)
      tree.balance()
      weights *= numpy.clip(numpy.array([tree.find(point)[2] for point in vertices.tolist()]) / fade, 0, 1)
    values = bridgeNoise.fractalNoise(bridgeNoise.noiseSamplePoints(vertices, breakup["featureSize"], breakup["seed"]), breakupOctaves, breakupRoughness)
    vertices = vertices + (breakup["amplitude"] * values * weights)[:, None] * outward
  faces, spans, bands = [], [], []
  ownRow = rows["scales"] == 1.0
  for row in range(count - 1):
    for index in range(size):
      following = (index + 1) % size
      faces.append((row * size + index, (row + 1) * size + index, (row + 1) * size + following, row * size + following))
      spans.append((row, row + 1))
      bands.append(int(shape["bands"][index]) if ownRow[row] and ownRow[row + 1] else -1)
  for end, row in (("start", 0), ("end", count - 1)):
    ring = [row * size + index for index in range(size)]
    if end in rows["apexes"]:
      apex = len(vertices)
      vertices = numpy.vstack([vertices, rows["apexes"][end]])
      capFaces, capBands = [(ring[index], ring[(index + 1) % size], apex) for index in range(size)], [-1] * size
    elif definition["wallShare"] == 1 and rows["ends"][end] == "blind":
      vertices, capFaces, capBands = flatEnd(vertices, ring, shape)
    else:
      capFaces, capBands = [tuple(ring)], [-1]
    faces += capFaces
    spans += [(row, row)] * len(capFaces)
    bands += capBands
  return vertices, orientedOutward(vertices, faces), numpy.array(spans, dtype=numpy.int64), numpy.array(bands, dtype=numpy.int64)


def flatEnd(vertices, ring, shape):
  """A hall's blind end as a flat wall: a grid of cells spanned between its floor and ceiling and its two walls (a Coons patch of its
  ring), each row of cells in the band of the walls' stretch beside it. Returns the vertices with the grid's inner ones added, its
  cells, and their bands."""
  across, walls = shape["floorCount"], shape["wallCount"]
  bottom = [ring[index] for index in range(across + 1)]
  right = [ring[across + index] for index in range(walls + 1)]
  top = [ring[2 * across + walls]] + [ring[across + walls + across - index] for index in range(1, across + 1)]
  left = [ring[0]] + [ring[2 * across + walls + walls - index] for index in range(1, walls)] + [ring[2 * across + walls]]
  points = vertices[right, 2]
  rises = (points - points[0]) / (points[-1] - points[0])
  grid = {}
  for column in range(across + 1):
    grid[column, 0], grid[column, walls] = bottom[column], top[column]
  for level in range(walls + 1):
    grid[0, level], grid[across, level] = left[level], right[level]
  corners = vertices[[bottom[0], bottom[-1], top[0], top[-1]]]
  added = []
  for column in range(1, across):
    share = column / across
    for level in range(1, walls):
      rise = rises[level]
      point = (
        (1 - rise) * vertices[bottom[column]] + rise * vertices[top[column]] + (1 - share) * vertices[left[level]] + share * vertices[right[level]]
        - ((1 - share) * (1 - rise) * corners[0] + share * (1 - rise) * corners[1] + (1 - share) * rise * corners[2] + share * rise * corners[3])
      )
      grid[column, level] = len(vertices) + len(added)
      added.append(point)
  cells = [(grid[column, level], grid[column + 1, level], grid[column + 1, level + 1], grid[column, level + 1]) for column in range(across) for level in range(walls)]
  bands = [int(shape["bands"][across + level]) for column in range(across) for level in range(walls)]
  return (numpy.vstack([vertices, added]) if added else vertices), cells, bands


def orientedOutward(vertices, faces):
  editor = bmesh.new()
  made = [editor.verts.new(point) for point in vertices]
  for face in faces:
    editor.faces.new([made[index] for index in face])
  editor.verts.index_update()
  bmesh.ops.recalc_face_normals(editor, faces=list(editor.faces))
  if editor.calc_volume(signed=True) < 0:
    bmesh.ops.reverse_faces(editor, faces=list(editor.faces))
  oriented = [tuple(vertex.index for vertex in face.verts) for face in editor.faces]
  closed = all(edge.is_manifold for edge in editor.edges)
  editor.free()
  if not closed:
    raise ValueError("The cave's tube does not close on itself; its breakup may fold it (lower breakup amplitude)")
  return oriented


# Layers a cut records and restores

class LayerSet:
  """Every generic layer of a mesh's faces, corners, and edges but the caves' own and Blender's hidden ones, read and written by name."""

  faceKinds = ("int", "float", "bool")
  cornerKinds = ("uv", "float_vector", "float", "int", "bool", "color")
  edgeKinds = ("float", "int", "bool")
  numeric = ("uv", "float_vector", "float", "color")

  def __init__(self, editor):
    def collect(layers, kinds):
      return [(f"{kind}:{name}", kind, layer) for kind in kinds for name, layer in getattr(layers, kind).items() if not name.startswith(".") and not name.startswith(bridgeCaveData.caveLayerStart)]

    self.face = collect(editor.faces.layers, self.faceKinds)
    self.corner = collect(editor.loops.layers, self.cornerKinds)
    self.edge = collect(editor.edges.layers, self.edgeKinds)

  @staticmethod
  def read(kind, value):
    if kind == "uv":
      return list(value.uv)
    if kind in ("float_vector", "color"):
      return list(value)
    return {"int": int, "float": float, "bool": bool}[kind](value)

  def record(self, face, identifiers):
    return {
      "vertices": identifiers, "material": face.material_index, "smooth": face.smooth,
      "face": {key: self.read(kind, face[layer]) for key, kind, layer in self.face},
      "corner": {key: [self.read(kind, loop[layer]) for loop in face.loops] for key, kind, layer in self.corner},
      "edge": {key: [self.read(kind, loop.edge[layer]) for loop in face.loops] for key, kind, layer in self.edge},
    }

  def write(self, face, values, corners):
    """Set a face's values from a record's (or a piece's) and its corners' from corners {key: [value per loop]}, a value a record
    lacks taking what an empty one holds: no surfacing layer covering it, the UV map as its base mapping, no transition mapping."""
    face.material_index, face.smooth = values["material"], values["smooth"]
    for key, kind, layer in self.face:
      if key in values["face"]:
        face[layer] = values["face"][key]
      elif kind == "int" and key.startswith("int:" + bridgeAuthoring.layerAttributePrefix):
        face[layer] = bridgeAuthoring.uncovered
    uvKey = "uv:" + bridgeSurfacing.uvLayerName
    for key, kind, layer in self.corner:
      if key in corners:
        loopValues = corners[key]
      elif key == "float_vector:" + bridgeSurfacing.baseMappingName and uvKey in corners:
        loopValues = [[u, v, 0.0] for u, v in corners[uvKey]]
      elif key.startswith("float_vector:" + bridgeSurfacing.transitionMappingPrefix):
        loopValues = [[math.nan] * 3 for _ in face.loops]
      else:
        continue
      for loop, value in zip(face.loops, loopValues):
        if kind == "uv":
          loop[layer].uv = value
        else:
          loop[layer] = value
    for key, kind, layer in self.edge:
      if key in values["edge"]:
        for loop, value in zip(face.loops, values["edge"][key]):
          loop.edge[layer] = value

  def blended(self, corners, weights):
    """Corner values at points given by weights (one row per point) over a triangle's corner values {key: [value per corner]}: numeric
    ones blended, the rest the value of the corner weighing most."""
    blended = {}
    for key, values in corners.items():
      kind = key.split(":", 1)[0]
      if kind not in self.numeric:
        blended[key] = [values[int(row.argmax())] for row in weights]
        continue
      mixed = weights @ numpy.array(values, dtype=numpy.float64).reshape(len(values), -1)
      blended[key] = mixed[:, 0].tolist() if kind == "float" else mixed.tolist()
    return blended


def barycentric(points, triangle):
  """Weights of points (n x 3) over a triangle's corners (3 x 3) in its plane, which may run outside 0 to 1 for points outside it."""
  first, second = triangle[1] - triangle[0], triangle[2] - triangle[0]
  offsets = points - triangle[0]
  d00, d01, d11 = first @ first, first @ second, second @ second
  d20, d21 = offsets @ first, offsets @ second
  denominator = d00 * d11 - d01 * d01
  if abs(denominator) <= 1e-18:
    raise ValueError("A plug triangle of the cave has no area")
  v = (d11 * d20 - d01 * d21) / denominator
  w = (d00 * d21 - d01 * d20) / denominator
  return numpy.column_stack([1 - v - w, v, w])


# Cutting and taking back

def requireTerrain(objectName):
  sceneObject = bridgeMeshAccess.requireEditableMesh(objectName, "cutCave")
  if sceneObject.modifiers:
    raise ValueError(f"'{objectName}' has modifiers; a cave is cut into the mesh as its passes show it, so apply or remove them first")
  if sceneObject.data.users > 1:
    raise ValueError(f"'{objectName}' shares its mesh with {sceneObject.data.users - 1} other object(s); a cave is cut into one terrain, so give it a mesh of its own first")
  return sceneObject


def requireCave(sceneObject, name):
  known = bridgeCaveData.caves(sceneObject)
  if name not in known:
    raise ValueError(f"'{sceneObject.name}' has no cave '{name}'; its caves: {sorted(known)}")
  return known[name]


@contextlib.contextmanager
def restoredOnFailure(sceneObject):
  """Run a change to a terrain's caves whole or not at all: on any failure the mesh and its caves go back as they were."""
  mesh = sceneObject.data
  backup = mesh.copy()
  known = sceneObject.get(bridgeCaveData.caveProperty)
  try:
    yield
  except Exception:
    failed = sceneObject.data
    sceneObject.data = backup
    name = failed.name
    bpy.data.meshes.remove(failed)
    backup.name = name
    if known is None:
      sceneObject.pop(bridgeCaveData.caveProperty, None)
    else:
      sceneObject[bridgeCaveData.caveProperty] = known
    raise
  else:
    bpy.data.meshes.remove(backup)


def nearTube(shown, rows, margin):
  """Which vertices lie within margin of the tube's walls in plan."""
  floors = rows["floors"]
  radii = rows["widths"] / 2 + margin
  low, high = floors[:, :2].min(axis=0) - radii.max(), floors[:, :2].max(axis=0) + radii.max()
  candidates = numpy.flatnonzero(((shown[:, :2] >= low) & (shown[:, :2] <= high)).all(axis=1))
  near = numpy.zeros(len(shown), dtype=bool)
  path = floors if len(floors) > 1 else numpy.vstack([floors, floors + [1e-3, 0, 0]])
  pathRadii = radii if len(floors) > 1 else numpy.repeat(radii, 2)
  for start in range(0, len(candidates), patchChunk):
    chunk = candidates[start:start + patchChunk]
    fractions = bridgeMeshAccess.strokeAlongPath(shown[chunk], path, pathRadii, horizontal=True)[0]
    near[chunk[fractions <= 1]] = True
  return near


def keyArrays(mesh):
  """The mesh's own coordinates and each pass's, local, by name ('' for the mesh's own)."""
  coordinates = numpy.empty(len(mesh.vertices) * 3, dtype=numpy.float32)
  mesh.vertices.foreach_get("co", coordinates)
  arrays = {"": coordinates.reshape(-1, 3).astype(numpy.float64)}
  if mesh.shape_keys is not None:
    for key in mesh.shape_keys.key_blocks:
      arrays[key.name] = bridgePasses.keyCoordinates(key)
  return arrays


def booleanCut(patchPositions, patchFaces, ring, tubeVertices, tubeFaces):
  """The patch closed into a solid down to below the tube, less the tube by the exact boolean: positions, faces, each face's source
  (its patch face, -1 for the solid's sides and bottom, -2 less its index for a face of the tube), and face normals."""
  bottom = min(float(patchPositions[:, 2].min()), float(tubeVertices[:, 2].min())) - solidDepth
  count = len(patchPositions)
  positions = numpy.vstack([patchPositions, numpy.column_stack([patchPositions[ring, :2], numpy.full(len(ring), bottom)])])
  faces = [tuple(face) for face in patchFaces]
  sources = list(range(len(patchFaces)))
  for index, vertex in enumerate(ring):
    following = ring[(index + 1) % len(ring)]
    faces.append((following, vertex, count + index, count + (index + 1) % len(ring)))
    sources.append(-1)
  faces.append(tuple(count + index for index in reversed(range(len(ring)))))
  sources.append(-1)
  solid = temporaryObject("zonewrightCaveSolid", positions, faces, sources)
  tube = temporaryObject("zonewrightCaveTube", tubeVertices, tubeFaces, [-2 - index for index in range(len(tubeFaces))])
  try:
    modifier = solid.modifiers.new("cut", "BOOLEAN")
    modifier.object, modifier.operation, modifier.solver = tube, "DIFFERENCE", "EXACT"
    depsgraph = bpy.context.evaluated_depsgraph_get()
    cut = bpy.data.meshes.new_from_object(solid.evaluated_get(depsgraph), preserve_all_data_layers=True, depsgraph=depsgraph)
  finally:
    removeTemporary(solid)
    removeTemporary(tube)
  try:
    cutPositions = numpy.empty(len(cut.vertices) * 3)
    cut.vertices.foreach_get("co", cutPositions)
    cutSources = numpy.empty(len(cut.polygons), dtype=numpy.int32)
    cut.attributes["caveSource"].data.foreach_get("value", cutSources)
    cutNormals = numpy.empty(len(cut.polygons) * 3)
    cut.polygons.foreach_get("normal", cutNormals)
    cutFaces = [tuple(polygon.vertices) for polygon in cut.polygons]
  finally:
    bpy.data.meshes.remove(cut)
  return cutPositions.reshape(-1, 3), cutFaces, cutSources, cutNormals.reshape(-1, 3)


def snappedCut(faces, sources, normals, original):
  """The cut's faces with the corners standing on one of the ground's own vertices taken as that one corner, repeats that leaves in a
  row dropped, and faces left with fewer than three corners dropped. Where the tube runs on the ground's own vertices and edges (a
  hall's floor and walls on the grid's lines), the exact solver leaves copies of one point a float's rounding apart, joined by faces
  with no area."""
  canonical = numpy.arange(len(original))
  first = {}
  for index in numpy.flatnonzero(original >= 0).tolist():
    canonical[index] = first.setdefault(int(original[index]), index)
  kept, keptSources, keptNormals = [], [], []
  for face, source, normal in zip(faces, sources, normals):
    corners = [int(canonical[index]) for index in face]
    corners = [corner for position, corner in enumerate(corners) if corner != corners[position - 1]]
    if len(corners) >= 3:
      kept.append(corners)
      keptSources.append(int(source))
      keptNormals.append(normal)
  return kept, keptSources, keptNormals


def withoutSlivers(positions, faces, sources, normals):
  """The faces with each triangle of the tube whose corners lie in a line (within matchDistance of it) taken out, its middle corner set
  into the face across its long edge (unless that is the solid's side), so the surface stays closed: the tube running along the
  ground's own edges leaves such triangles."""
  owners = {}
  for index, face in enumerate(faces):
    for position, corner in enumerate(face):
      owners[corner, face[(position + 1) % len(face)]] = index
  dropped = set()
  for index, face in enumerate(faces):
    if len(face) != 3 or sources[index] > -2:
      continue
    points = positions[face]
    lengths = [numpy.linalg.norm(points[(position + 1) % 3] - points[position]) for position in range(3)]
    longest = int(numpy.argmax(lengths))
    if numpy.linalg.norm(numpy.cross(points[1] - points[0], points[2] - points[0])) / lengths[longest] > matchDistance:
      continue
    start, end, middle = face[longest], face[(longest + 1) % 3], face[(longest + 2) % 3]
    across = owners.get((end, start))
    if across is None or across in dropped or sources[across] == -1 or middle in faces[across]:
      continue
    neighbour = faces[across]
    position = next(position for position, corner in enumerate(neighbour) if corner == end and neighbour[(position + 1) % len(neighbour)] == start)
    neighbour.insert(position + 1, middle)
    for first, second in ((end, middle), (middle, start)):
      owners[first, second] = across
    del owners[end, start]
    dropped.add(index)
  keep = [index for index in range(len(faces)) if index not in dropped]
  return [faces[index] for index in keep], [sources[index] for index in keep], [normals[index] for index in keep]


def temporaryObject(name, positions, faces, sources):
  mesh = bpy.data.meshes.new(name)
  mesh.from_pydata([tuple(point) for point in positions.tolist()], [], faces)
  mesh.attributes.new("caveSource", "INT", "FACE").data.foreach_set("value", numpy.asarray(sources, dtype=numpy.int32))
  sceneObject = bpy.data.objects.new(name, mesh)
  bpy.context.scene.collection.objects.link(sceneObject)
  return sceneObject


def removeTemporary(sceneObject):
  mesh = sceneObject.data
  bpy.data.objects.remove(sceneObject)
  bpy.data.meshes.remove(mesh)


def closedPatch(patch):
  """The patch with its notches filled: the faces around a vertex its edge passes through twice (two of its faces meeting at a corner),
  and faces all of whose corners it holds, taken in until there are none."""
  patchSet = set(patch)
  for _ in range(patchRounds):
    vertices = {vertex for face in patchSet for vertex in face.verts}
    added = {face for vertex in vertices for face in vertex.link_faces if face not in patchSet and all(corner in vertices for corner in face.verts)}
    for vertex in vertices:
      if sum(1 for edge in vertex.link_edges if sum(face in patchSet for face in edge.link_faces) == 1) > 2:
        added |= {face for face in vertex.link_faces if face not in patchSet}
    if not added:
      return sorted(patchSet, key=lambda face: face.index)
    patchSet |= added
  raise ValueError(f"The ground within the cave's reach still pinches after {patchRounds} rounds of filling it in (an arch, an overhang, or a hole in the same mesh lies within reach of the path); keep the cave clear of it")


def patchBoundary(patch):
  """The patch's edge as one loop of vertex indices, wound as its faces wind it (inside on the left seen from above)."""
  patchSet = set(patch)
  following = {}
  for face in patch:
    for loop in face.loops:
      if sum(other in patchSet for other in loop.edge.link_faces) == 1:
        if loop.vert.index in following:
          raise ValueError("The ground around the cave pinches to a point within its reach (two pieces of ground meeting at a corner); keep the cave clear of it")
        following[loop.vert.index] = loop.link_loop_next.vert.index
  start = next(iter(following))
  ring = [start]
  while following[ring[-1]] != start:
    ring.append(following[ring[-1]])
    if len(ring) > len(following):
      break
  if len(ring) != len(following):
    raise ValueError(
      f"The ground within the cave's reach is not one piece without holes: its edge runs {len(following)} vertices, its first loop only"
      f" {len(ring)} (an arch, an overhang, or a hole in the same mesh lies within reach of the path); keep the cave clear of it"
    )
  return ring


def caveLayers(editor, name):
  verts = editor.verts.layers
  if editor.loops.layers.uv.get(bridgeSurfacing.uvLayerName) is None:
    editor.loops.layers.uv.new(bridgeSurfacing.uvLayerName)
  return {
    "tag": verts.int.get(bridgeCaveData.vertexTagPrefix + name) or verts.int.new(bridgeCaveData.vertexTagPrefix + name),
    "source": verts.int.get(bridgeCaveData.sourcePrefix + name) or verts.int.new(bridgeCaveData.sourcePrefix + name),
    "weights": verts.float_vector.get(bridgeCaveData.weightsPrefix + name) or verts.float_vector.new(bridgeCaveData.weightsPrefix + name),
    "ground": verts.float_vector.get(bridgeCaveData.groundPrefix + name) or verts.float_vector.new(bridgeCaveData.groundPrefix + name),
    "face": editor.faces.layers.int.get(bridgeCaveData.faceTagPrefix + name) or editor.faces.layers.int.new(bridgeCaveData.faceTagPrefix + name),
    "tube": editor.faces.layers.int.new(tubeFaceLayerName),
  }


def weldMouth(editor, faces, rankOf, pointOf, shortest):
  """Weld the cut's short edges at the mouth: a ring or lining vertex closer than `shortest` to a neighbour on a cave face merges into
  it, the terrain's own vertices surviving first and ring vertices next, so every survivor keeps its place in every pass."""
  welded = 0
  # Only edges at a ring vertex or a vertex of the ground can weld; the lining's own edges never do.
  seamVertices = {vertex for face in faces for vertex in face.verts if rankOf[vertex] > 0}
  for _ in range(weldRounds):
    candidates = []
    for edge in {edge for vertex in seamVertices if vertex.is_valid for edge in vertex.link_edges if any(face.is_valid for face in edge.link_faces)}:
      first, second = edge.verts
      if first not in rankOf or second not in rankOf:
        continue
      ranks = (rankOf[first], rankOf[second])
      if min(ranks) == 2 or max(ranks) == 0:
        continue
      length = (pointOf[first] - pointOf[second]).length
      if length < shortest:
        candidates.append((length, edge))
    targets, claimed = {}, set()
    for _, edge in sorted(candidates, key=lambda candidate: candidate[0]):
      first, second = edge.verts
      if first in claimed or second in claimed:
        continue
      survivor, victim = (first, second) if rankOf[first] >= rankOf[second] else (second, first)
      targets[victim] = survivor
      claimed.update((first, second))
    if not targets:
      break
    bmesh.ops.weld_verts(editor, targetmap=targets)
    welded += len(targets)
  return welded


def splice(sceneObject, name, definition, strokes):
  """Cut a cave into a terrain and splice it into the mesh; keeps its record, paints its lining with the strokes, and returns what the
  cut made."""
  mesh = sceneObject.data
  matrix = bridgeMeshAccess.matrixArray(sceneObject.matrix_world)
  inverse = numpy.linalg.inv(matrix)
  shown, _ = bridgeMeshAccess.readVertexArrays(sceneObject)
  surface = caveSurface(shown, bridgeMeshAccess.meshTriangles(sceneObject), definition)
  line = CaveLine(definition)
  line.requireGrades(definition["maximumFloorDegrees"])
  rows = tubeRows(definition, line, surface)
  tubeVertices, tubeFaces, tubeSpans, tubeBands = tubeMesh(definition, rows, surface)
  tube = {
    "vertices": tubeVertices, "faces": tubeFaces, "spans": tubeSpans, "bands": tubeBands, "floors": rows["floors"][:, 2], "size": len(rows["shape"]["across"]),
    "shortestStretch": rows["shape"]["shortestStretch"],
  }
  amplitude = definition["breakup"]["amplitude"] if definition["breakup"] is not None else 0.0
  reach = breakupReach * amplitude + patchEdges * definition["edgeLength"]
  near = numpy.flatnonzero(nearTube(shown, rows, reach))
  if not len(near):
    raise ValueError(f"No ground of '{sceneObject.name}' lies within reach of the cave's path")
  owners = bridgeCaveData.CaveVertices(sceneObject) if bridgeCaveData.holdsCaves(sceneObject) else None
  wallSlot = bridgeAuthoring.materialSlot(sceneObject, definition["wallMaterial"])
  floorSlot = bridgeAuthoring.materialSlot(sceneObject, definition["floorMaterial"])
  bandSlots = [bridgeAuthoring.materialSlot(sceneObject, band["material"]) for band in definition["trimBands"]]
  layered = bridgeMeshAccess.surfaceLayers(sceneObject)
  editor = bridgeMeshAccess.loadBMesh(sceneObject)
  try:
    # Adding a layer leaves Python's references to the mesh's elements dangling, so the cave's layers come first.
    layers = caveLayers(editor, name)
    editor.verts.ensure_lookup_table()
    editor.faces.ensure_lookup_table()
    patch = closedPatch(list({face for index in near.tolist() for face in editor.verts[index].link_faces}))
    patchVertices = numpy.array(sorted({vertex.index for face in patch for vertex in face.verts}), dtype=numpy.int64)
    border = next((vertex.index for face in patch for vertex in face.verts if vertex.is_boundary), None)
    if border is not None:
      raise ValueError(f"The cave reaches the edge of '{sceneObject.name}' near {[round(float(value), 1) for value in shown[border]]}; keep it {reach:g} inside the terrain's border")
    if owners is not None and owners.namesOf(patchVertices):
      raise ValueError(f"Cave '{name}' would overlap cave(s) {owners.namesOf(patchVertices)} in plan: the ground within its reach holds theirs; keep caves apart (or take one back)")
    report = spliceInto(editor, sceneObject, name, definition, shown, inverse, layers, patch, tube, wallSlot, floorSlot, bandSlots)
    editor.faces.layers.int.remove(layers["tube"])
    editor.normal_update()
    editor.to_mesh(mesh)
  finally:
    editor.free()
  mesh.update()
  ground = numpy.full((len(mesh.vertices), 3), numpy.nan, dtype=numpy.float32)
  ground[patchVertices] = shown[patchVertices]
  mesh.attributes[bridgeCaveData.groundPrefix + name].data.foreach_set("vector", ground.ravel())
  if layered:
    bridgeAuthoring.showLayers(sceneObject)
  known = bridgeCaveData.caves(sceneObject)
  known[name] = {"definition": definition, "plug": report.pop("records"), "paint": strokes}
  bridgeCaveData.writeCaves(sceneObject, known)
  replayStrokes(sceneObject, name, strokes)
  ends = [{"end": end, "kind": kind, "rounded": rows["rounded"][end]} for end, kind in rows["ends"].items()]
  return report | {"ends": ends, "levelStretches": line.levelStretches()}


def spliceInto(editor, sceneObject, name, definition, shown, inverse, layers, patch, tube, wallSlot, floorSlot, bandSlots):
  shapeLayers = dict(editor.verts.layers.shape.items())
  layerSet = LayerSet(editor)
  surfaceLayers = [layer for key, kind, layer in layerSet.face if key.startswith("int:" + bridgeAuthoring.layerAttributePrefix)]
  baseLayer = next((layer for key, kind, layer in layerSet.face if key == "int:" + bridgeAuthoring.baseAttributeName), None)
  polygons = [face for face in patch if len(face.verts) > 3]
  if polygons:
    # Drawn as triangles split from their first corner (Blender's own), so the ground keeps its shape.
    made = bmesh.ops.triangulate(editor, faces=polygons, quad_method="FIXED", ngon_method="EAR_CLIP")
    editor.faces.index_update()
    patch = sorted({face for face in patch if face.is_valid} | set(made["faces"]), key=lambda face: face.index)
  ring = patchBoundary(patch)
  patchVertices = sorted({vertex.index for face in patch for vertex in face.verts})
  localIndex = {vertex: position for position, vertex in enumerate(patchVertices)}
  cutPositions, cutFaces, cutSources, cutNormals = booleanCut(
    shown[patchVertices], [[localIndex[vertex.index] for vertex in face.verts] for face in patch], [localIndex[vertex] for vertex in ring], tube["vertices"], tube["faces"],
  )
  tree = mathutils.kdtree.KDTree(len(patchVertices))
  for position, vertex in enumerate(patchVertices):
    tree.insert(shown[vertex], position)
  tree.balance()
  original = numpy.full(len(cutPositions), -1)
  for index, point in enumerate(cutPositions.tolist()):
    _, found, distance = tree.find(point)
    if distance < matchDistance:
      original[index] = patchVertices[found]
  cutFaces, cutSources, cutNormals = withoutSlivers(cutPositions, *snappedCut(cutFaces, cutSources, cutNormals, original))
  onTerrain, onLining = {}, numpy.zeros(len(cutPositions), dtype=bool)
  for face, source in zip(cutFaces, cutSources):
    for vertex in face:
      if source >= 0:
        onTerrain.setdefault(vertex, int(source))
      elif source <= -2:
        onLining[vertex] = True
  unchanged = {
    int(source) for face, source in zip(cutFaces, cutSources)
    if source >= 0 and len(face) == 3 and (original[list(face)] >= 0).all() and {int(original[vertex]) for vertex in face} == {vertex.index for vertex in patch[source].verts}
  }
  changed = [position for position in range(len(patch)) if position not in unchanged]
  if not changed:
    raise ValueError("The cave's tube never meets the ground's surface, so it would open nowhere; bring an end out to open ground")
  plugIdentifiers, records, sources = {}, [], {}
  for position in changed:
    face = patch[position]
    corners = [vertex.index for vertex in face.verts]
    for vertex in corners:
      plugIdentifiers.setdefault(vertex, len(plugIdentifiers) + 1)
    sources[position] = len(records)
    records.append(layerSet.record(face, [plugIdentifiers[vertex] for vertex in corners]))
  cornersOf = {position: [vertex.index for vertex in patch[position].verts] for position in changed}
  bmesh.ops.delete(editor, geom=[patch[position] for position in changed], context="FACES_ONLY")
  editor.verts.ensure_lookup_table()
  # New vertices leave the lookup table out of date, so the patch's own are held before any is made.
  patchVertex = {vertex: editor.verts[vertex] for vertex in patchVertices}
  for vertex, identifier in plugIdentifiers.items():
    patchVertex[vertex][layers["tag"]] = identifier
  # Each plug triangle's corners in the mesh and every pass, read off the few vertices that hold them.
  cornerKeys = {}
  for position in changed:
    corners = [patchVertex[vertex] for vertex in cornersOf[position]]
    cornerKeys[position] = {"": numpy.array([list(vertex.co) for vertex in corners])} | {keyName: numpy.array([list(vertex[layer]) for vertex in corners]) for keyName, layer in shapeLayers.items()}
  made, rankOf, pointOf = {}, {}, {}
  ringCount = liningCount = 0
  for index, point in enumerate(cutPositions):
    if original[index] >= 0:
      continue
    source = onTerrain.get(index)
    if source is not None and source in sources:
      weights = barycentric(point[None, :], shown[cornersOf[source]])[0]
      vertex = editor.verts.new(tuple(weights @ cornerKeys[source][""]))
      for keyName, layer in shapeLayers.items():
        vertex[layer] = mathutils.Vector(weights @ cornerKeys[source][keyName])
      vertex[layers["tag"]], vertex[layers["source"]], vertex[layers["weights"]] = bridgeCaveData.ringTag, sources[source], mathutils.Vector(weights)
      rankOf[vertex] = 1
      ringCount += 1
    elif onLining[index]:
      local = tuple(inverse[:3, :3] @ point + inverse[:3, 3])
      vertex = editor.verts.new(local)
      for layer in shapeLayers.values():
        vertex[layer] = mathutils.Vector(local)
      vertex[layers["tag"]] = bridgeCaveData.liningTag
      rankOf[vertex] = 0
      liningCount += 1
    else:
      continue
    made[index] = vertex
    pointOf[vertex] = mathutils.Vector(point)

  def vertexOf(index):
    if original[index] >= 0:
      vertex = patchVertex[int(original[index])]
      rankOf.setdefault(vertex, 2)
      pointOf.setdefault(vertex, mathutils.Vector(shown[int(original[index])]))
      return vertex
    return made[index]

  newFaces = []
  pieces = lining = 0
  for face, source, normal in zip(cutFaces, cutSources, cutNormals):
    if source == -1 or (source >= 0 and int(source) not in sources):
      continue
    created = editor.faces.new([vertexOf(index) for index in face])
    if source >= 0:
      record = records[sources[int(source)]]
      weights = barycentric(cutPositions[list(face)], shown[cornersOf[int(source)]])
      layerSet.write(created, record, layerSet.blended(record["corner"], weights))
      created[layers["face"]] = sources[int(source)] + 1
      pieces += 1
    else:
      tubeFace = -2 - int(source)
      band = int(tube["bands"][tubeFace])
      slot = bandSlots[band] if band >= 0 else floorSlot if normal[2] > floorNormalZ else wallSlot
      created.material_index, created.smooth = slot, True
      created[layers["tube"]] = tubeFace
      for layer in surfaceLayers:
        created[layer] = bridgeAuthoring.uncovered
      if baseLayer is not None:
        created[baseLayer] = slot
      created[layers["face"]] = bridgeCaveData.liningFaceTag
      lining += 1
    newFaces.append(created)
  # A hall's mouth is left as cut, so its walls and ceiling meet the ground square; a band's edges meet it as lines of the seam a band's
  # height apart, which welding at a third of an edge would fold together.
  shortest = 0.0 if definition["wallShare"] == 1 else definition["edgeLength"] * mouthEdgeShare
  if definition["trimBands"]:
    shortest = min(shortest, tube["shortestStretch"] / 2)
  welded = weldMouth(editor, newFaces, rankOf, pointOf, shortest)
  caveFaces = {face for face in newFaces if face.is_valid}
  polygons = [face for face in caveFaces if len(face.verts) > 3]
  if polygons:
    caveFaces |= set(bmesh.ops.triangulate(editor, faces=polygons, quad_method="BEAUTY", ngon_method="BEAUTY")["faces"])
  # A weld can close a sliver of lining between two pieces of ground onto their own corners: a lining face and a piece on the same
  # vertices, back to back, enclosing nothing. Both go.
  byCorners = {}
  for face in caveFaces:
    byCorners.setdefault(frozenset(face.verts), []).append(face)
  doubled = [face for faces in byCorners.values() if len(faces) > 1 for face in faces]
  if doubled:
    bmesh.ops.delete(editor, geom=doubled, context="FACES_ONLY")
  caveFaces = [face for face in caveFaces if face.is_valid]
  requireSealed(caveFaces)
  liningFaces = [face for face in caveFaces if face[layers["face"]] == bridgeCaveData.liningFaceTag]
  bandFaces = mapLining(editor, liningFaces, pointOf, definition, tube, layers["tube"])
  return {
    "object": sceneObject.name, "cave": name, "patchFaces": len(patch), "plugFaces": len(changed), "plugVertices": len(plugIdentifiers),
    "ringVertices": ringCount, "liningVertices": liningCount, "pieces": pieces, "liningFaces": lining, "weldedAtMouth": welded, "doubledFacesRemoved": len(doubled),
    "tubeFaces": len(tube["faces"]), "records": records,
    "trimBands": [{"fromFloor": band["fromFloor"], "height": band["height"], "material": band["material"], "faces": count} for band, count in zip(definition["trimBands"], bandFaces)],
  } | cutReport(caveFaces, liningFaces, pointOf)


def requireSealed(caveFaces):
  """Refuse a cut that came out broken: an edge at the cave on three or more faces, or open where its lining should meet the ground."""
  edges = {edge for face in caveFaces for vertex in face.verts for edge in vertex.link_edges}
  overUsed = sum(1 for edge in edges if len(edge.link_faces) > 2)
  open = sum(1 for edge in edges if len(edge.link_faces) == 1)
  if overUsed or open:
    raise ValueError(
      f"The cut came out broken at the cave: {overUsed} edges on three or more faces and {open} open edges where its lining meets the"
      " ground; nothing was changed. A slightly different path, width, or breakup cuts it cleanly"
    )


def mapLining(editor, faces, pointOf, definition, tube, tubeLayer):
  """Map lining faces from where they stand, as their UVs and, where the layers map transitions, as their base mapping with no
  transition of their own: box-mapped at worldUnitsPerRepeat, as projectUVs maps a face along its normal's largest axis; a trim band's
  faces along the band, u along their box axis and v up from the band's bottom edge (over the floor where the face lies), at the band's
  repeat, so a strip texture runs once up the band. Returns how many faces each band has."""
  uvLayer = editor.loops.layers.uv[bridgeSurfacing.uvLayerName]
  vectors = editor.loops.layers.float_vector
  base = vectors.get(bridgeSurfacing.baseMappingName)
  transitions = [layer for name, layer in vectors.items() if name.startswith(bridgeSurfacing.transitionMappingPrefix)]
  boxAxes = [numpy.array(bridgeSurfacing.planarAxes(numpy.eye(3)[axis])) for axis in range(3)]
  bandFaces = [0] * len(definition["trimBands"])
  for face in faces:
    points = numpy.array([list(pointOf[vertex]) for vertex in face.verts])
    normal = numpy.abs(numpy.cross(points[1] - points[0], points[2] - points[0]))
    tubeFace = face[tubeLayer]
    band = int(tube["bands"][tubeFace])
    if band < 0:
      uvs = points @ boxAxes[int(normal.argmax())].T / definition["worldUnitsPerRepeat"]
    else:
      trim = definition["trimBands"][band]
      along = boxAxes[0 if normal[0] >= normal[1] else 1][0]
      uvs = numpy.column_stack([points @ along, points[:, 2] - liningFloors(points, tube, tubeFace) - trim["fromFloor"]]) / trim["worldUnitsPerRepeat"]
      bandFaces[band] += 1
    for loop, (u, v) in zip(face.loops, uvs.tolist()):
      loop[uvLayer].uv = (u, v)
      if base is not None:
        loop[base] = (u, v, 0.0)
      for layer in transitions:
        loop[layer] = (math.nan, math.nan, math.nan)
  return bandFaces


def liningFloors(points, tube, tubeFace):
  """The floor's height under points of the lining that came from one face of the tube: graded between the two rows the face spans,
  by how far along from one to the other each lies (the end's floor on a cap)."""
  first, second = tube["spans"][tubeFace]
  if first == second:
    return numpy.full(len(points), tube["floors"][first])
  corners = numpy.array(tube["faces"][tubeFace])
  rows = corners // tube["size"]
  start, end = tube["vertices"][corners[rows == first], :2].mean(axis=0), tube["vertices"][corners[rows == second], :2].mean(axis=0)
  run = end - start
  shares = numpy.clip((points[:, :2] - start) @ run / (run @ run), 0.0, 1.0)
  return tube["floors"][first] + shares * (tube["floors"][second] - tube["floors"][first])


def cutReport(faces, liningFaces, pointOf):
  """Where a cave opens and how fine its cut came out: each opening (a seam where its lining meets the ground: its seam vertices, middle,
  and lowest and highest point; an opening not at an end is the tube breaking through the ground), and the shortest edges of its
  lining, of the pieces of ground at its mouth, and of its seams, as the mesh shows them."""
  lining = set(liningFaces)
  seam = {edge for face in liningFaces for edge in face.edges if any(other not in lining for other in edge.link_faces)}
  parent = {}

  def root(vertex):
    while parent.setdefault(vertex, vertex) is not vertex:
      parent[vertex] = parent[parent[vertex]]
      vertex = parent[vertex]
    return vertex

  for edge in seam:
    first, second = (root(vertex) for vertex in edge.verts)
    if first is not second:
      parent[first] = second
  components = {}
  for vertex in list(parent):
    components.setdefault(root(vertex), []).append(pointOf[vertex])
  openings = []
  for points in components.values():
    points = numpy.array([list(point) for point in points])
    openings.append({
      "seamVertices": len(points), "middle": [round(float(value), 1) for value in points.mean(axis=0)],
      "lowest": round(float(points[:, 2].min()), 1), "highest": round(float(points[:, 2].max()), 1),
    })

  def shortest(edges):
    return round(min((pointOf[edge.verts[0]] - pointOf[edge.verts[1]]).length for edge in edges), 2) if edges else None

  return {
    "openings": sorted(openings, key=lambda opening: -opening["seamVertices"]),
    "shortestEdges": {
      "lining": shortest({edge for face in liningFaces for edge in face.edges}), "pieces": shortest({edge for face in faces if face not in lining for edge in face.edges}),
      "seam": shortest(seam),
    },
  }


def takeBack(sceneObject, name):
  """Take a cave back out of a terrain: its lining and the pieces of ground at its mouth go, and the plug faces come back on their own
  vertices, each with the surfacing, mapping, and values its largest piece held (painted or mapped since the cut), or as recorded."""
  mesh = sceneObject.data
  record = bridgeCaveData.caves(sceneObject)[name]
  shown, _ = bridgeMeshAccess.readVertexArrays(sceneObject)
  tags = bridgeCaveData.attributeValues(mesh, bridgeCaveData.vertexTagPrefix + name)
  pieceTags = bridgeCaveData.faceTags(sceneObject, name)
  editor = bridgeMeshAccess.loadBMesh(sceneObject)
  try:
    layerSet = LayerSet(editor)
    caveFaces = [editor.faces[index] for index in numpy.flatnonzero(pieceTags != 0)]
    largest = {}
    faceTag = editor.faces.layers.int[bridgeCaveData.faceTagPrefix + name]
    for face in caveFaces:
      tag = face[faceTag]
      if tag <= 0:
        continue
      corners = [mathutils.Vector(shown[vertex.index]) for vertex in face.verts]
      area = (corners[1] - corners[0]).cross(corners[2] - corners[0]).length / 2
      if area > largest.get(tag - 1, (0.0, None))[0]:
        largest[tag - 1] = (area, face)
    plugRows = numpy.flatnonzero(tags > 0)
    identifiers = tags[plugRows]
    if len(numpy.unique(identifiers)) != len(identifiers):
      raise ValueError(f"Cave '{name}' of '{sceneObject.name}' has plug ids carried by two vertices (cut by another topology edit since); it cannot be taken back")
    byIdentifier = {int(identifier): editor.verts[int(row)] for identifier, row in zip(identifiers, plugRows)}
    restoring = []
    for position, plugFace in enumerate(record["plug"]):
      missing = [identifier for identifier in plugFace["vertices"] if identifier not in byIdentifier]
      if missing:
        raise ValueError(f"Cave '{name}' of '{sceneObject.name}' has lost plug vertices {missing}; it cannot be taken back")
      vertices = [byIdentifier[identifier] for identifier in plugFace["vertices"]]
      values, corners = plugFace, plugFace["corner"]
      if position in largest:
        piece = largest[position][1]
        pieceRecord = layerSet.record(piece, [])
        values = pieceRecord | {"edge": plugFace["edge"]}
        weights = barycentric(shown[[vertex.index for vertex in vertices]], shown[[vertex.index for vertex in piece.verts]])
        corners = layerSet.blended(pieceRecord["corner"], weights)
      restoring.append((vertices, values, corners))
    bmesh.ops.delete(editor, geom=caveFaces, context="FACES_ONLY")
    bmesh.ops.delete(editor, geom=[editor.verts[int(row)] for row in numpy.flatnonzero(tags < 0)], context="VERTS")
    for vertices, values, corners in restoring:
      layerSet.write(editor.faces.new(vertices), values, corners)
    plugVertices = set(byIdentifier.values())
    for edge in [edge for vertex in plugVertices for edge in vertex.link_edges if not edge.link_faces]:
      if edge.is_valid:
        editor.edges.remove(edge)
    for collection, prefix in ((editor.verts.layers.int, bridgeCaveData.vertexTagPrefix), (editor.verts.layers.int, bridgeCaveData.sourcePrefix), (editor.verts.layers.float_vector, bridgeCaveData.weightsPrefix),
                               (editor.verts.layers.float_vector, bridgeCaveData.groundPrefix), (editor.faces.layers.int, bridgeCaveData.faceTagPrefix)):
      collection.remove(collection[prefix + name])
    editor.normal_update()
    editor.to_mesh(mesh)
  finally:
    editor.free()
  mesh.update()
  known = bridgeCaveData.caves(sceneObject)
  del known[name]
  bridgeCaveData.writeCaves(sceneObject, known)
  return {"restoredFaces": len(record["plug"])}


# Checks

def integrityProblems(sceneObject):
  """Every way a terrain's caves are not as their cuts left them: an edge on three or more faces, an open edge at a mouth, a plug id lost
  or carried twice, a ring vertex off its plug triangle in a pass, or a lining vertex moved by one."""
  mesh = sceneObject.data
  known = bridgeCaveData.caves(sceneObject)
  if not known:
    return []
  problems = []
  loopEdges = numpy.empty(len(mesh.loops), dtype=numpy.int64)
  mesh.loops.foreach_get("edge_index", loopEdges)
  uses = numpy.bincount(loopEdges, minlength=len(mesh.edges))
  edges = bridgeMeshAccess.meshEdges(mesh)
  blocks = keyArrays(mesh)
  for name, record in sorted(known.items()):
    tags = bridgeCaveData.attributeValues(mesh, bridgeCaveData.vertexTagPrefix + name)
    atCave = (tags[edges] != 0).any(axis=1)
    if (atCave & (uses > 2)).any():
      problems.append(f"cave '{name}': {int((atCave & (uses > 2)).sum())} edges are used by three or more faces")
    if (atCave & (uses == 1) & (tags[edges] < 0).any(axis=1)).any():
      problems.append(f"cave '{name}': {int((atCave & (uses == 1) & (tags[edges] < 0).any(axis=1)).sum())} open edges at its mouth")
    identifiers = tags[tags > 0]
    expected = {identifier for plugFace in record["plug"] for identifier in plugFace["vertices"]}
    if len(numpy.unique(identifiers)) != len(identifiers) or set(identifiers.tolist()) != expected:
      problems.append(f"cave '{name}': its plug ids are not each carried by one vertex ({len(identifiers)} carried, {len(numpy.unique(identifiers))} distinct, {len(expected)} recorded)")
      continue
    ringRows, corners, weights = bridgeCaveData.ringData(sceneObject, name, record, tags)
    lining = tags == bridgeCaveData.liningTag
    for blockName, coordinates in blocks.items():
      offRing = numpy.abs(numpy.einsum("rk,rkj->rj", weights, coordinates[corners]) - coordinates[ringRows]).max(initial=0.0)
      if offRing > ringTolerance:
        problems.append(f"cave '{name}': a ring vertex stands {offRing:.4g} off its plug triangle in {'the mesh' if not blockName else 'pass ' + repr(blockName)}")
      if blockName and lining.any():
        moved = numpy.abs(coordinates[lining] - blocks[""][lining]).max()
        if moved > ringTolerance:
          problems.append(f"cave '{name}': its lining is moved {moved:.4g} by pass {blockName!r}")
  return problems


def requireIntact(sceneObject):
  problems = integrityProblems(sceneObject)
  if problems:
    raise ValueError(f"The caves of '{sceneObject.name}' are not as their cuts left them: {'; '.join(problems)}. A change outside the cave guards (runPython) broke them; restore a checkpoint")


def groundMoves(sceneObject):
  """For each cave, how far the ground within its reach moved since it was cut, and where it moved most."""
  if not bridgeCaveData.holdsCaves(sceneObject):
    return {}
  shown, _ = bridgeMeshAccess.readVertexArrays(sceneObject)
  moves = {}
  for name in sorted(bridgeCaveData.caves(sceneObject)):
    recorded = bridgeCaveData.attributeValues(sceneObject.data, bridgeCaveData.groundPrefix + name, 3)
    rows = numpy.flatnonzero(~numpy.isnan(recorded).any(axis=1))
    distances = numpy.linalg.norm(shown[rows] - recorded[rows], axis=1)
    worst = int(distances.argmax()) if len(rows) else None
    moves[name] = {"largest": round(float(distances.max(initial=0.0)), 3), "at": None if worst is None else [round(float(value), 1) for value in shown[rows[worst]]]}
  return moves


def staleCaves(sceneObject):
  return sorted(name for name, move in groundMoves(sceneObject).items() if move["largest"] > groundTolerance)


def describeCaves(sceneObject):
  """Each cave on a mesh: its path's ends, whether its ground moved since it was cut (stale, and by how much), its lining faces, and the strokes kept with it."""
  moves = groundMoves(sceneObject)
  described = []
  for name, record in sorted(bridgeCaveData.caves(sceneObject).items()):
    tags = bridgeCaveData.faceTags(sceneObject, name)
    described.append({
      "name": name, "from": record["definition"]["path"][0], "to": record["definition"]["path"][-1], "stale": moves[name]["largest"] > groundTolerance,
      "groundMoved": moves[name], "liningFaces": int((tags == bridgeCaveData.liningFaceTag).sum()), "strokes": len(record["paint"]),
    })
  return described


# Commands

def cutCave(objectName, name, path, widths, heights, wallMaterial, floorMaterial, worldUnitsPerRepeat, edgeLength, wallShare, breakup, mouthFade, maximumFloorDegrees, trimBands):
  sceneObject = requireTerrain(objectName)
  if not name.strip():
    raise ValueError("A cave needs a name")
  if name in bridgeCaveData.caves(sceneObject):
    raise ValueError(f"'{objectName}' already has a cave '{name}'; editCave changes it")
  definition = caveDefinition(path, widths, heights, wallMaterial, floorMaterial, worldUnitsPerRepeat, edgeLength, wallShare, breakup, mouthFade, maximumFloorDegrees, trimBands)
  requireIntact(sceneObject)
  with restoredOnFailure(sceneObject):
    return splice(sceneObject, name, definition, [])


def recut(sceneObject, name, changes):
  """Take a cave back and cut it again from its definition with the changes merged in, whole or not at all."""
  record = requireCave(sceneObject, name)
  unknown = sorted(set(changes) - set(definitionKeys))
  if unknown:
    raise ValueError(f"A cave's definition holds {list(definitionKeys)}; unknown {unknown}")
  definition = caveDefinition(**(record["definition"] | changes))
  with restoredOnFailure(sceneObject):
    restored = takeBack(sceneObject, name)
    return {"restored": restored, "cut": splice(sceneObject, name, definition, record["paint"])}


def editCave(objectName, name, changes):
  sceneObject = requireTerrain(objectName)
  requireIntact(sceneObject)
  return {"object": objectName, "cave": name, "changes": changes or {}} | recut(sceneObject, name, changes or {})


def removeCave(objectName, name):
  sceneObject = requireTerrain(objectName)
  record = requireCave(sceneObject, name)
  requireIntact(sceneObject)
  with restoredOnFailure(sceneObject):
    restored = takeBack(sceneObject, name)
    if bridgeMeshAccess.surfaceLayers(sceneObject):
      bridgeAuthoring.showLayers(sceneObject)
  return {"object": objectName, "cave": name, "definition": record["definition"], "strokes": record["paint"]} | restored


def traceLedge(objectName, start, end, floorFrom, floorTo, width, height, side, insideShare, step):
  """A starting path for a gallery or rock shelter along a cliff, for cutCave; changes nothing."""
  sceneObject = bridgeMeshAccess.requireMeshObject(objectName)
  if len(start) != 2 or len(end) != 2:
    raise ValueError(f"start and end are [x, y] points in plan, got {start!r} and {end!r}")
  if width <= 0 or height <= 0 or step <= 0:
    raise ValueError(f"width, height, and step are positive, got {width}, {height}, and {step}")
  if side not in ("left", "right"):
    raise ValueError(f"side is the side of travel from start to end the rock stands on, left or right, got {side!r}")
  if not 0 < insideShare < 1:
    raise ValueError(f"insideShare is the share of the width inside the rock, above 0 and under 1, got {insideShare}")
  start, end = numpy.array(start, dtype=numpy.float64), numpy.array(end, dtype=numpy.float64)
  length = float(numpy.linalg.norm(end - start))
  if length < step:
    raise ValueError(f"start and end are {length:.1f} apart, less than one step ({step:g})")
  direction = (end - start) / length
  toSide = numpy.array([-direction[1], direction[0], 0.0] if side == "left" else [direction[1], -direction[0], 0.0])
  alongs = numpy.linspace(0.0, length, math.ceil(length / step) + 1)
  line = start + alongs[:, None] * direction
  reach = faceReachWidths * width
  shown, _ = bridgeMeshAccess.readVertexArrays(sceneObject)
  surface = TerrainSurface(shown, bridgeMeshAccess.meshTriangles(sceneObject), numpy.vstack([start, end]), reach + width)
  floors = floorFrom + (floorTo - floorFrom) * alongs / length
  # A point's floor height follows its distance along the traced path, so the grade is even, and where its face lies depends on that
  # height: a few rounds settle the two.
  for _ in range(traceRounds):
    offsets = numpy.full(len(line), numpy.nan)
    for index, (point, floor) in enumerate(zip(line.tolist(), floors.tolist())):
      face = surface.faceBeside(numpy.array([point[0], point[1], floor + playerScale.stepHeight]), toSide, reach)
      if face is not None:
        offsets[index] = float((face[:2] - line[index]) @ toSide[:2]) + (insideShare - 0.5) * width
    traced = ~numpy.isnan(offsets)
    if not traced.any():
      raise ValueError(f"No rock stands within {reach:g} to the {side} of the line from {start.tolist()} to {end.tolist()} at its floor's heights; trace along the cliff, with side toward its rock")
    # Past the cliff's top no rock stands beside a point; it keeps the offset of the nearest point that found the cliff, so the path
    # runs on in line rather than kinking back to the line from start to end.
    found = numpy.flatnonzero(traced)
    offsets = offsets[found[numpy.abs(numpy.arange(len(line))[:, None] - found[None, :]).argmin(axis=1)]]
    centers = line + offsets[:, None] * toSide[:2]
    distances = numpy.concatenate([[0.0], numpy.cumsum(numpy.linalg.norm(numpy.diff(centers, axis=0), axis=1))])
    floors = floorFrom + (floorTo - floorFrom) * distances / distances[-1]
  path = numpy.column_stack([centers, floors])
  count = len(path)
  try:
    CaveLine({"path": path.tolist(), "widths": [float(width)] * count, "heights": [float(height)] * count})
  except ValueError as error:
    raise ValueError(f"Traced every {step:g} along the cliff, the path would not cut: {error}. Trace it with a longer step or a narrower width") from error
  across = numpy.linspace(-0.5, 0.5, shareSamples)[:, None] * width * toSide
  shares = [float((surface.depths(point + [0.0, 0.0, playerScale.stepHeight] + across) > 0).mean()) for point in path]
  segments = []
  for index in range(count - 1):
    run = float(numpy.linalg.norm(path[index + 1, :2] - path[index, :2]))
    middle = (path[index] + path[index + 1]) / 2
    segments.append({
      "from": index, "to": index + 1, "gradeDegrees": round(math.degrees(math.atan2(path[index + 1, 2] - path[index, 2], run)), 2),
      "covered": bool(surface.depths(middle[None])[0] >= height),
    })
  return {
    "object": objectName, "path": [[round(float(value), 2) for value in point] for point in path], "widths": [float(width)] * count,
    "heights": [float(height)] * count,
    "points": [{"traced": bool(isTraced), "insideShare": round(share, 3)} for isTraced, share in zip(traced, shares)],
    "segments": segments,
  }


def refitStaleCaves(sceneObject):
  """Cut again every cave whose ground moved since it was cut, so each fits the ground as it now is."""
  if not bridgeCaveData.holdsCaves(sceneObject):
    return []
  requireIntact(sceneObject)
  refitted = []
  for name in staleCaves(sceneObject):
    move = groundMoves(sceneObject)[name]
    recut(sceneObject, name, {})
    refitted.append({"cave": name, "groundMoved": move["largest"], "at": move["at"]})
  return refitted


commands = {
  "cutCave": (cutCave, True),
  "editCave": (editCave, True),
  "removeCave": (removeCave, True),
  "traceLedge": (traceLedge, False),
}
