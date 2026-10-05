"""Caves cut into a terrain mesh as the client's own are built: a tube swept along each of a cave's runs (its main path and its branches)
is subtracted, all at once, with the exact boolean from the terrain around them and spliced back into the same mesh, so one terrain
holds the hill and the rooms under it. The terrain faces the cut changed (the plug) are recorded and deleted as faces only: their
vertices stay and take every shaping pass, so taking the cave back puts the ground back exactly. The new vertices where the tubes meet
the ground (the ring) follow the plug's triangles in every pass, so the mouth stays sealed whatever shapes the ground, and the tubes' own
surface (the lining) holds no offset in any pass. Every tool that shapes, cuts, or paints the terrain keeps to that through the guards
in bridgeCaveData. A cave's definition is kept on the terrain, so it is cut again whole (editCave) once the ground under it moves; its
runs, their floors, and the strokes shaping them are worked out in bridgeCaveRuns. Runs under Blender's Python."""
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
import bridgeCaveLight
import bridgeCaveRuns
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
# A face of the tube's floor this close to the ground's plane lies in it.
groundPlaneTolerance = 1e-3
mouthEdgeShare = 1.0 / 3.0
weldRounds = 8
# Candidate rows lie this share of edgeLength apart; rows are kept so no point of the section moves more than edgeLength between two.
rowSampleShare = 0.25
breakupOctaves = 3
breakupRoughness = 0.5
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
definitionDefaults = {
  "edgeLength": 16.0, "wallShare": 0.35, "breakup": None, "mouthFade": None, "maximumFloorDegrees": 30.0, "trimBands": None, "grades": None,
  "landings": None, "daylight": None, "branches": None, "floor": None, "minimumRock": 8.0,
}
definitionKeys = ("path", "widths", "heights", "wallMaterial", "floorMaterial", "worldUnitsPerRepeat") + tuple(definitionDefaults)
bandKeys = {"fromFloor", "height", "material", "worldUnitsPerRepeat"}
# Two section stops closer than this in every row are one stop.
stopTolerance = 1e-6
# Which tube face each piece of lining came from, while a cut is spliced; never written to the mesh.
tubeFaceLayerName = "zonewrightCaveTubeFace"
cornerOutward = math.sqrt(0.5)
up = mathutils.Vector((0.0, 0.0, 1.0))
down = mathutils.Vector((0.0, 0.0, -1.0))
notGround = mathutils.Vector((math.nan, math.nan, math.nan))
# A branch's first section may stand this far out of its parent's walls (the floors of the two meeting in one plane).
junctionTolerance = 1e-3
# A branch is clear of its parent this far along past where it leaves the parent's walls: a share of its section plus the rock.
junctionReachShare = 1.5
# Whether a point lies inside a tube is counted along rays leaning off every axis, so none runs along a row or a wall, stepping past
# each crossing: one up, one down, and one across.
parityDirections = tuple(mathutils.Vector(direction).normalized() for direction in ((0.1234, 0.2345, 0.9643), (-0.2711, 0.1517, -0.9506), (0.9311, -0.3127, 0.1873)))
parityStep = 1e-4
parityCrossings = 64
# A flap pressed onto a neighbour faces back along it within this.
flapAntiparallel = 0.99


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

def caveDefinition(
  path, widths, heights, wallMaterial, floorMaterial, worldUnitsPerRepeat, edgeLength, wallShare, breakup, mouthFade, maximumFloorDegrees,
  trimBands=None, grades=None, landings=None, daylight=None, branches=None, floor=None, minimumRock=8.0,
):
  main = bridgeCaveRuns.runDefinition({"path": path, "widths": widths, "heights": heights, "grades": grades, "landings": landings, "daylight": daylight}, "The cave", False)
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
  if not minimumRock > 0:
    raise ValueError(f"minimumRock is the least rock left between two passages of a cave or two caves, positive, got {minimumRock}")
  for material in (wallMaterial, floorMaterial):
    if bpy.data.materials.get(material) is None:
      raise ValueError(f"No material named '{material}'")
  branchList = branchDefinitions(branches)
  runNames = [bridgeCaveRuns.mainRun] + [branch["name"] for branch in branchList]
  strokes = bridgeCaveRuns.strokeDefinitions(floor, runNames, float(edgeLength))
  for stroke in strokes:
    if stroke.get("material") is not None and bpy.data.materials.get(stroke["material"]) is None:
      raise ValueError(f"Floor stroke '{stroke['name']}''s material '{stroke['material']}' is not a material; make it with createMaterial first")
  definition = main | {
    "wallMaterial": wallMaterial, "floorMaterial": floorMaterial, "worldUnitsPerRepeat": float(worldUnitsPerRepeat), "edgeLength": float(edgeLength),
    "wallShare": float(wallShare), "breakup": None if breakup is None else {"featureSize": float(breakup["featureSize"]), "amplitude": float(breakup["amplitude"]), "seed": int(breakup["seed"])},
    "mouthFade": None if mouthFade is None else float(mouthFade), "maximumFloorDegrees": float(maximumFloorDegrees),
    "trimBands": bandDefinitions(trimBands), "branches": branchList, "floor": strokes, "minimumRock": float(minimumRock),
  }
  for name, run, _ in runSpecs(definition):
    wallGaps(definition, run["heights"], run["path"], bridgeCaveRuns.runOwner(name))
  return definition


def branchDefinitions(branches):
  """Branches as a definition keeps them: each named once, leaving the main run or a branch named before it."""
  if branches is None:
    return []
  if not isinstance(branches, list):
    raise ValueError(f"branches is a list of {{name, from, path, widths, heights, grades?, landings?, daylight?, overlook?}}, got {branches!r}")
  required, optional = bridgeCaveRuns.branchKeys
  defined, known = [], [bridgeCaveRuns.mainRun]
  for index, branch in enumerate(branches):
    if not isinstance(branch, dict):
      raise ValueError(f"Branch {index} is {{name, from, path, widths, heights, ...}}, got {branch!r}")
    missing, unknown = sorted(required - set(branch)), sorted(set(branch) - required - optional)
    if missing or unknown:
      raise ValueError(f"Branch {index} takes {sorted(required)} and optionally {sorted(optional)}; missing {missing}, unknown {unknown}")
    name = branch["name"]
    if not isinstance(name, str) or not name.strip() or name == bridgeCaveRuns.mainRun:
      raise ValueError(f"Branch {index} needs a name of its own (not '{bridgeCaveRuns.mainRun}', the cave's own path), got {name!r}")
    if name in known:
      raise ValueError(f"Two branches are named '{name}'; each branch's name is its own")
    if branch["from"] not in known:
      raise ValueError(f"Branch '{name}' leaves {branch['from']!r}, which is not the main run or a branch named before it ({known})")
    defined.append(bridgeCaveRuns.runDefinition(branch, f"Branch '{name}'", True))
    known.append(name)
  return defined


def runSpecs(definition):
  """Each run of a cave as (name, run, the run it leaves): the main path first, then the branches in order."""
  main = {key: definition[key] for key in ("path", "widths", "heights", "grades", "landings", "daylight")} | {"overlook": False}
  return [(bridgeCaveRuns.mainRun, main, None)] + [(branch["name"], branch, branch["from"]) for branch in definition["branches"]]


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
    if material is None or bridgeMeshAccess.cutoutProperty not in material:
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


def wallGaps(definition, heights, path, owner):
  """The stretches a run's walls are cut into from the floor up, between the floor, each trim band's edges, and the top of the straight
  walls: each stretch's ends as (share of the row's height, units over the floor), its trim band (or -1), and how many points it takes
  to keep its edges within edgeLength in the tallest row. Stops that meet in every row are one stop; refuses a band reaching above the
  straight walls, or meeting their top in some rows but not others."""
  heights = numpy.array(heights)
  wallShare = definition["wallShare"]
  pointName = "path point" if owner == "the cave" else f"{owner}'s path point"
  for index, band in enumerate(definition["trimBands"]):
    top = band["fromFloor"] + band["height"]
    over = numpy.flatnonzero(top > wallShare * heights + stopTolerance)
    if len(over):
      point = int(over[0])
      raise ValueError(
        f"Trim band {index} ({band['fromFloor']:g} to {top:g} over the floor) reaches above the walls' straight part at {pointName} {point}"
        f" {roundedPoint(path[point])}, where the walls rise straight {wallShare * heights[point]:g} (wallShare {wallShare:g} of the"
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
        f"A trim band's top meets the top of the straight walls at {pointName} {point} {roundedPoint(path[point])} but runs below it"
        " elsewhere; run it below the walls' top everywhere, or along it everywhere"
      )
    gaps.append({"low": low, "high": high, "band": band, "count": max(1, math.ceil(lengths.max() / definition["edgeLength"])), "shortest": float(lengths.min())})
  return gaps


def sectionShape(definition, widths, heights, path, owner, breaks=()):
  """A run's tube section counterclockwise looking along it, from the floor's left corner: each point across (a share of the width) and
  up (a share of the height plus units over the floor, both shrinking with the rows a rounded end adds), its outward direction, the
  trim band (or -1) of the stretch from it to the next point, and which wall it stands on (1 right, -1 left, 0 neither). The floor runs
  between its corners and the breaks (offsets across where a floor stroke's side must stand in every row), each stretch between two at
  edgeLength or less in the widest row (floorLayout: each floor point's stretch and its share along it); each wall in its stretches
  between the floor, the band edges, and its top (wallGaps), then a vault, or for a hall (wallShare 1) a flat ceiling evenly across
  with square corners."""
  edgeLength, wallShare = definition["edgeLength"], definition["wallShare"]
  widest, tallest = max(widths), max(heights)
  stops = [-widest / 2] + [offset for offset, _ in breaks] + [widest / 2]
  layout = []
  for interval, (low, high) in enumerate(zip(stops, stops[1:])):
    count = max(1, math.ceil((high - low) / edgeLength - 1e-9))
    layout += [(interval, step / count) for step in range(count)]
  if len(layout) < 2:
    layout = [(0, 0.0), (0, 0.5)]
  floorCount = len(layout)
  gaps = wallGaps(definition, heights, path, owner)
  points = [(-0.5 + index / floorCount, 0.0, 0.0, (0.0, -1.0), -1, 0) for index in range(floorCount)]
  for gap in gaps:
    for step in range(gap["count"]):
      points.append((0.5, *gapStop(gap, step / gap["count"]), (1.0, 0.0), gap["band"], 0 if gap is gaps[0] and step == 0 else 1))
  if wallShare < 1:
    vault = math.pi * math.sqrt(((widest / 2) ** 2 + ((1 - wallShare) * tallest) ** 2) / 2)
    arcCount = max(4, math.ceil(vault / edgeLength))
    for index in range(arcCount):
      angle = math.pi * index / arcCount
      points.append((0.5 * math.cos(angle), wallShare + (1 - wallShare) * math.sin(angle), 0.0, (math.cos(angle), math.sin(angle)), -1, 0))
  else:
    points += [(0.5 - index / floorCount, 1.0, 0.0, (cornerOutward, cornerOutward) if index == 0 else (0.0, 1.0), -1, 0) for index in range(floorCount)]
  for gap in reversed(gaps):
    for step in range(gap["count"]):
      corner = wallShare == 1 and gap is gaps[-1] and step == 0
      points.append((-0.5, *gapStop(gap, 1 - step / gap["count"]), (-cornerOutward, cornerOutward) if corner else (-1.0, 0.0), gap["band"], -1))
  return {
    "across": numpy.array([point[0] for point in points]), "upShare": numpy.array([point[1] for point in points]),
    "upUnits": numpy.array([point[2] for point in points]), "outward": numpy.array([point[3] for point in points]),
    "bands": numpy.array([point[4] for point in points]), "wallSide": numpy.array([point[5] for point in points]), "floorCount": floorCount,
    "wallCount": sum(gap["count"] for gap in gaps), "onFloor": numpy.array([point[1] == 0.0 and point[2] == 0.0 for point in points]),
    "shortestStretch": min(gap["shortest"] / gap["count"] for gap in gaps), "floorLayout": layout, "breaks": list(breaks),
  }


def gapStop(gap, share):
  """The point share of the way up a wall stretch, as (share of the row's height, units over the floor)."""
  return tuple((1 - share) * low + share * high for low, high in zip(gap["low"], gap["high"]))


def floorOffsets(shape, widths):
  """Each row's floor points across (rows x floorCount, units from the middle, negative to the left): between the floor's corners and
  its breaks, each break where its stroke set it; where the floor narrows past a break (a stroke's side beyond this row's walls, as in a
  tunnel leading to the stroke's room), it and those beyond it stand evenly between the last break that fits and the wall."""
  halves = numpy.asarray(widths, dtype=numpy.float64) / 2
  breaks = numpy.array([offset for offset, _ in shape["breaks"]], dtype=numpy.float64)
  placed = numpy.repeat(breaks[None, :], len(halves), axis=0)
  for sign in (1.0, -1.0):
    side = numpy.flatnonzero(sign * breaks > 0)
    side = side[numpy.argsort(sign * breaks[side])]
    for row, half in enumerate(halves):
      fits = sign * breaks[side] <= half - bridgeCaveRuns.breakClearance
      outside = side[~fits]
      if len(outside):
        last = float((sign * breaks[side[fits]]).max(initial=0.0))
        placed[row, outside] = sign * (last + (half - last) * numpy.arange(1, len(outside) + 1) / (len(outside) + 1))
  stops = numpy.column_stack([-halves, placed, halves])
  intervals = numpy.array([interval for interval, _ in shape["floorLayout"]])
  shares = numpy.array([share for _, share in shape["floorLayout"]])
  return stops[:, intervals] + (stops[:, intervals + 1] - stops[:, intervals]) * shares[None, :]


def sectionPoints(shape, floors, directions, widths, heights, scales):
  """World points of the section at each row (scales: how far a rounded end's row has shrunk, 1 elsewhere): rows x points x 3."""
  right = numpy.column_stack([directions[:, 1], -directions[:, 0], numpy.zeros(len(directions))])
  across = shape["across"][None, :] * widths[:, None]
  across[:, :shape["floorCount"]] = floorOffsets(shape, widths)
  rise = shape["upShare"][None, :] * heights[:, None] + shape["upUnits"][None, :] * scales[:, None]
  return floors[:, None, :] + across[..., None] * right[:, None, :] + rise[..., None] * numpy.array([0.0, 0.0, 1.0])


class TerrainSurface:
  """The terrain (its triangles over shown positions) as it is seen near a line in plan, for casts. Only its triangles with a corner
  within `margin` of the line's plan box are taken, so a large terrain costs no more than the ground in reach."""

  def __init__(self, shown, triangles, plan, margin):
    plan = numpy.asarray(plan, dtype=numpy.float64)[:, :2]
    near = ((shown[:, :2] >= plan.min(axis=0) - margin) & (shown[:, :2] <= plan.max(axis=0) + margin)).all(axis=1)
    triangles = triangles[near[triangles].any(axis=1)]
    used, local = numpy.unique(triangles, return_inverse=True)
    self.top = float(shown[used, 2].max()) + 1.0 if len(used) else 0.0
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

  def groundHeight(self, point, what):
    """The height of the highest ground over a plan point, refusing a point over none."""
    location, _, _, _ = self.tree.ray_cast(mathutils.Vector((float(point[0]), float(point[1]), self.top)), down)
    if location is None:
      raise ValueError(f"{what} at {roundedPoint(point[:2])} has no height and no ground under it to take one from; give it a height")
    return float(location.z)

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
  """The terrain around a cave's runs out to everything their tubes and breakup can reach."""
  amplitude = definition["breakup"]["amplitude"] if definition["breakup"] is not None else 0.0
  runs = [run for _, run, _ in runSpecs(definition)]
  margin = max(max(run["widths"]) + max(run["heights"]) for run in runs) + breakupReach * amplitude + patchEdges * definition["edgeLength"]
  return TerrainSurface(shown, triangles, [point[:2] for run in runs for point in run["path"]], margin)


def roundedPoint(point):
  return [round(float(value), 1) for value in point]


def workedRuns(definition, surface):
  """Each run worked out on the ground as it stands: its line with every point's floor height (an end without one takes the ground there,
  where it opens onto the ground (requireOpenEnd); a branch's start its parent's floor), its grades checked, and its floor strokes
  placed."""
  worked = {}
  for name, run, parent in runSpecs(definition):
    owner = bridgeCaveRuns.runOwner(name)
    line = bridgeCaveRuns.runLine(run, name)
    tookGround = {}

    def startHeight(run=run, parent=parent, owner=owner, tookGround=tookGround):
      if parent is None:
        tookGround["start"] = surface.groundHeight(run["path"][0], f"{bridgeCaveRuns.capitalized(owner)}'s start")
        return tookGround["start"]
      return float(relievedFloorsAt(worked[parent], numpy.array([run["path"][0][:2]]))[0][0])

    def endHeight(run=run, owner=owner, tookGround=tookGround):
      tookGround["end"] = surface.groundHeight(run["path"][-1], f"{bridgeCaveRuns.capitalized(owner)}'s end")
      return tookGround["end"]

    line.setFloors(bridgeCaveRuns.resolveFloors(line, run["path"], run["grades"], startHeight, endHeight))
    line.requireGrades(definition["maximumFloorDegrees"])
    for end in tookGround:
      requireOpenEnd(surface, line, run, end, owner)
    worked[name] = {
      "name": name, "run": run, "parent": parent, "owner": owner, "line": line, "groundHeights": tookGround,
      "relief": bridgeCaveRuns.FloorRelief([stroke for stroke in definition["floor"] if stroke["run"] == name], line),
    }
  return worked


def requireOpenEnd(surface, line, run, end, owner):
  """Refuse an end that took the ground's height where it does not open onto the ground: the run graded to it from the nearest height
  given comes out of the ground before it, its vault in the open over a floor buried in the rock (a trench) for more than its width;
  as an end under a hill takes the hilltop and the run climbs out through the plateau."""
  path = run["path"]
  if end == "end":
    index = len(path) - 2
    while index > 0 and len(path[index]) == 2 and (run["grades"] is None or run["grades"][index - 1] is None):
      index -= 1
    low, high = float(line.stations[index]), line.length
  else:
    index = 1
    while index < len(path) - 1 and len(path[index]) == 2:
      index += 1
    low, high = 0.0, float(line.stations[index])
  alongs = numpy.linspace(low, high, max(2, math.ceil((high - low) / 2.0) + 1))
  floors, _, widths, heights = line.at(alongs)
  buried = surface.depths(floors + [0.0, 0.0, 0.01]) > playerScale.stepHeight
  vaultOpen = surface.depths(floors + numpy.column_stack([numpy.zeros((len(floors), 2)), heights])) <= 0
  trench = buried & vaultOpen
  spacing = (high - low) / (len(alongs) - 1)
  if trench.sum() * spacing <= widths[trench].max(initial=0.0):
    return
  first, last = numpy.flatnonzero(trench)[[0, -1]]
  point = path[-1] if end == "end" else path[0]
  raise ValueError(
    f"{bridgeCaveRuns.capitalized(owner)}'s {end} at {roundedPoint(point[:2])} has no height, so it took the ground's there"
    f" ({float(floors[-1 if end == 'end' else 0, 2]):.1f}), and the run graded to it comes out of the ground before it: its vault stands in the open over"
    f" a floor buried in the rock for {trench.sum() * spacing:.0f} from {roundedPoint(floors[first])} to {roundedPoint(floors[last])}, a trench."
    " Give the end a height (inside the rock for a blind end), or end it where the run meets the ground"
  )


def parentFloorAt(line, point):
  along, _ = line.nearestAlong(point)
  return float(line.floorAt(numpy.array([along]))[0])


def recordedLines(name, record):
  """A cut cave's runs as cut: each run's line with its floors worked out again from its definition and the ground heights its ends
  took when it was cut (groundHeights, kept with the record for the ends given none), a branch's start its parent's floor as its
  strokes leave it. Refuses a cave whose definition leaves an end to the ground with no height kept for it."""
  definition = caveDefinition(**record["definition"])
  kept = record.get("groundHeights", {})
  worked = {}
  for runName, run, parent in runSpecs(definition):
    line = bridgeCaveRuns.runLine(run, runName)

    def taken(end, runName=runName):
      if end not in kept.get(runName, {}):
        raise ValueError(
          f"Cave '{name}' takes the ground's height at run '{runName}''s {end}, and was cut before the heights its ends took were kept with it;"
          " cut it again (editCave with no changes) first"
        )
      return kept[runName][end]

    def startHeight(run=run, parent=parent, taken=taken):
      return taken("start") if parent is None else float(relievedFloorsAt(worked[parent], numpy.array([run["path"][0][:2]]))[0][0])

    line.setFloors(bridgeCaveRuns.resolveFloors(line, run["path"], run["grades"], startHeight, lambda taken=taken: taken("end")))
    worked[runName] = {"line": line, "relief": bridgeCaveRuns.FloorRelief([stroke for stroke in definition["floor"] if stroke["run"] == runName], line)}
  return {runName: entry["line"] for runName, entry in worked.items()}


def requireFloorOnRock(surface, floors, owner):
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
    f"{bridgeCaveRuns.capitalized(owner)}'s floor hangs in the air from {roundedPoint(floors[first])} to {roundedPoint(floors[last])}"
    + (f" and in {stretches - 1} more stretches" if stretches > 1 else "")
    + f": no rock lies under its middle within a step ({playerScale.stepHeight:g}), the ground {'nowhere' if math.isinf(gap) else f'up to {gap:.1f}'} below it."
    " Keep the floor inside the rock or on the ground; a gallery along a cliff wants more of its width inside the rock (traceLedge insideShare)"
  )


def endKind(definition, shape, surface, floor, direction, width, height, end, owner="the cave"):
  """How one end of a tube meets the ground, and whether it is rounded off: open (some of its floor on the ground: a mouth wholly in
  the open but for a sill a step deep, or a gallery's end beside a cliff, part in the rock), ledge (part in the rock with its floor
  running out over a drop beside it), or blind (wholly inside the rock). A hall's ends are never rounded, and stand wholly in the open
  or wholly in the rock."""
  cap = sectionPoints(shape, floor[None], direction[None], numpy.array([width]), numpy.array([height]), numpy.ones(1))[0]
  depths = surface.depths(cap)
  hall = definition["wallShare"] == 1
  named = bridgeCaveRuns.capitalized(owner)
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
    f"{named}'s {end} at {roundedPoint(floor)} is part in the rock (up to {depths.max():.1f} into it) and part in the open, its"
    " floor buried more than a step under the ground; end it with its floor on the ground in front of it or beside it, or wholly inside the rock"
  )


def tubeRows(definition, worked, surface, shape):
  """A run's tube rows (floor points, plan directions, widths, heights, distances along, and scales: how far a rounded end's row has
  shrunk, which its breakup and band edges shrink with) and each end's kind (endKind; a branch's start is its junction). Every station,
  landing arc end, and floor stroke's end is a row. An end with rock in its section is rounded off, closing over half its width beyond
  its last point on an apex at floor height, as a dome closes; a hall's blind end closes as a flat wall."""
  edgeLength = definition["edgeLength"]
  line, owner = worked["line"], worked["owner"]
  exact = numpy.unique(numpy.concatenate([line.stations, line.knotAlongs, worked["relief"].stations()]))
  candidates = numpy.unique(numpy.concatenate([numpy.arange(0.0, line.length, edgeLength * rowSampleShare), exact]))
  floors, directions, widths, heights = line.at(candidates)
  requireFloorOnRock(surface, floors, owner)
  ends, rounded = {}, {}
  for end, row in (("start", 0), ("end", -1)):
    if end == "start" and worked["parent"] is not None:
      ends[end], rounded[end] = "junction", False
      continue
    ends[end], rounded[end] = endKind(definition, shape, surface, floors[row], directions[row], widths[row], heights[row], end, owner)
  rows = [(floors, directions, widths, heights, numpy.ones(len(candidates)), numpy.isin(candidates, exact), candidates)]
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
      numpy.full(len(beyond), candidates[row]),
    )
    apexes[end] = numpy.array([*(floors[row, :2] + sign * reach * directions[row]), floors[row, 2]])
    depths = surface.depths(numpy.vstack([sectionPoints(shape, *extended[:5]).reshape(-1, 3), apexes[end] + [0.0, 0.0, playerScale.stepHeight]]))
    if ends[end] == "blind" and not (depths > 0).all():
      raise ValueError(f"{bridgeCaveRuns.capitalized(owner)}'s blind {end} at {roundedPoint(floors[row])} is rounded off over {reach:.1f} beyond it, which reaches out of the rock; end it deeper inside")
    if end == "start":
      rows.insert(0, tuple(numpy.flip(part, axis=0) for part in extended))
    else:
      rows.append(extended)
  floors, directions, widths, heights, scales, stations, alongs = (numpy.concatenate([part[index] for part in rows]) for index in range(7))
  sections = sectionPoints(shape, floors, directions, widths, heights, scales)
  kept = [0]
  for index in range(1, len(floors)):
    if stations[index] or index == len(floors) - 1 or numpy.linalg.norm(sections[index + 1] - sections[kept[-1]], axis=1).max() > edgeLength:
      kept.append(index)
  return {
    "shape": shape, "floors": floors[kept], "directions": directions[kept], "widths": widths[kept], "heights": heights[kept],
    "scales": scales[kept], "alongs": alongs[kept], "ends": ends, "rounded": rounded, "apexes": apexes,
  }


def tubeFaces(definition, rows, vertices):
  """A tube's faces over its section rows (and the vertices with any its caps add), and for each face the rows it spans (the end's row
  twice for a cap) and its trim band (or -1): a face is in a band only between two rows of the tube itself, not those a rounded end adds."""
  shape = rows["shape"]
  count, size = len(rows["floors"]), len(shape["across"])
  faces, spans, bands = [], [], []
  ownRow = rows["scales"] == 1.0
  for row in range(count - 1):
    for index in range(size):
      following = (index + 1) % size
      faces.append((row * size + index, (row + 1) * size + index, (row + 1) * size + following, row * size + following))
      spans.append((row, row + 1))
      bands.append(int(shape["bands"][index]) if ownRow[row] and ownRow[row + 1] else -1)
  vertexRows = list(numpy.repeat(numpy.arange(count), size))
  for end, row in (("start", 0), ("end", count - 1)):
    ring = [row * size + index for index in range(size)]
    before = len(vertices)
    if end in rows["apexes"]:
      apex = len(vertices)
      vertices = numpy.vstack([vertices, rows["apexes"][end]])
      capFaces, capBands = [(ring[index], ring[(index + 1) % size], apex) for index in range(size)], [-1] * size
    elif definition["wallShare"] == 1 and rows["ends"][end] == "blind":
      vertices, capFaces, capBands = flatEnd(vertices, ring, shape)
    else:
      capFaces, capBands = [tuple(ring)], [-1]
    vertexRows += [row] * (len(vertices) - before)
    faces += capFaces
    spans += [(row, row)] * len(capFaces)
    bands += capBands
  return vertices, faces, numpy.array(spans, dtype=numpy.int64), numpy.array(bands, dtype=numpy.int64), numpy.array(vertexRows, dtype=numpy.int64)


def unbrokenTube(definition, rows):
  """A run's tube as its section gives it, without breakup or floor relief: its world vertices, outward faces, spans, bands, and each
  vertex's row; what a branch's junction is measured against."""
  shape = rows["shape"]
  sections = sectionPoints(shape, rows["floors"], rows["directions"], rows["widths"], rows["heights"], rows["scales"])
  vertices, faces, spans, bands, vertexRows = tubeFaces(definition, rows, sections.reshape(-1, 3))
  return {"vertices": vertices, "faces": orientedOutward(vertices, faces), "spans": spans, "bands": bands, "vertexRows": vertexRows, "sections": sections}


def surfaceTree(vertices, faces):
  return mathutils.bvhtree.BVHTree.FromPolygons(numpy.asarray(vertices).tolist(), [list(face) for face in faces])


def insideClosed(tree, point):
  """Whether a point lies inside a closed surface: rays from it cross the surface an odd number of times, by the vote of rays in three
  directions, so one grazing a wall along its tangent does not decide it."""
  votes = 0
  for direction in parityDirections:
    crossings, origin = 0, point
    for _ in range(parityCrossings):
      location, _, _, _ = tree.ray_cast(origin, direction)
      if location is None:
        break
      crossings += 1
      origin = location + direction * parityStep
    votes += crossings % 2
  return votes * 2 > len(parityDirections)


def signedDistances(tree, points):
  """Each point's distance from a closed surface: negative inside it."""
  distances = numpy.empty(len(points))
  for index, point in enumerate(numpy.asarray(points).tolist()):
    origin = mathutils.Vector(point)
    distance = tree.find_nearest(origin)[3]
    distances[index] = -distance if insideClosed(tree, origin) else distance
  return distances


def nearestDistances(tree, points):
  return numpy.array([tree.find_nearest(mathutils.Vector(point))[3] for point in numpy.asarray(points).tolist()])


def brokenTube(definition, worked, rows, unbroken, surface, junctionTrees):
  """A run's tube as cut: the walls and vault broken up along their outward directions, fading out within mouthFade of wherever the tube
  lies in the open and of the tubes it meets at its junctions (junctionTrees), so a mouth's lip and a junction's opening stay clean
  arches; then the floor given its strokes' relief, the walls' straight part lifted (or lowered) evenly above a raised (or sunk) wall
  foot so no wall folds. Returns its world vertices, faces, spans, bands, each face's stroke material (or None), and each vertex's row."""
  shape = rows["shape"]
  sections = unbroken["sections"]
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
    if fade > 0:
      # Within half the fade of a tube it meets at a junction the section is left unbroken, so the opening is the meeting of two clean
      # sections; past that the breakup eases in over the rest of the fade.
      for junctionTree in junctionTrees:
        weights *= bridgeCaveRuns.smoothstep(numpy.clip(2 * nearestDistances(junctionTree, vertices) / fade - 1, 0, 1))
    values = bridgeNoise.fractalNoise(bridgeNoise.noiseSamplePoints(vertices, breakup["featureSize"], breakup["seed"]), breakupOctaves, breakupRoughness)
    vertices = vertices + (breakup["amplitude"] * values * weights)[:, None] * outward
  vertices = vertices.reshape(count, size, 3).copy()
  strokeMaterials = relieved(definition, worked, rows, sections, vertices, surface)
  vertices, faces, spans, bands, vertexRows = tubeFaces(definition, rows, vertices.reshape(-1, 3))
  faceMaterials = [None] * len(faces)
  floorCount = shape["floorCount"]
  for row in range(count - 1):
    for index in range(min(floorCount, size)):
      faceMaterials[row * size + index] = strokeMaterials[row][index]
  return {"vertices": vertices, "faces": orientedOutward(vertices, faces), "spans": spans, "bands": bands, "vertexRows": vertexRows, "materials": faceMaterials}


def relieved(definition, worked, rows, sections, vertices, surface):
  """Give a tube's own rows (vertices, rows x points x 3, changed in place) their floor strokes' relief: each floor point raised or
  lowered by it, and each wall's straight part (below the lowest trim band) moved evenly between its foot's relief at the floor and
  nothing at its top. Refuses a wall foot raised to within a step of that top, and a sunk floor hanging in the air. Returns each floor
  face's stroke material (rows - 1 lists, None where none)."""
  shape, relief = rows["shape"], worked["relief"]
  count, size = sections.shape[:2]
  floorCount = shape["floorCount"]
  own = numpy.flatnonzero(rows["scales"] == 1.0)
  offsets = numpy.zeros((count, floorCount + 1))
  offsets[:, :floorCount] = floorOffsets(shape, rows["widths"])
  offsets[:, floorCount] = rows["widths"] / 2
  materials = [[None] * floorCount for _ in range(count - 1)]
  if not (relief.level or relief.pads or relief.rough):
    return materials
  relief.requireRoughOnFloor(sections[own, :floorCount + 1, :2].reshape(-1, 2))
  values = numpy.zeros((count, floorCount + 1))
  values[own] = relief.relief(rows["alongs"][own], offsets[own], sections[own, :floorCount + 1, :2], rows["widths"][own])
  vertices[:, :floorCount + 1, 2] += values
  lowestBand = min((band["fromFloor"] for band in definition["trimBands"]), default=math.inf)
  tops = numpy.minimum(definition["wallShare"] * rows["heights"], lowestBand)
  ups = sections[:, :, 2] - rows["floors"][:, None, 2]
  for side, foot in ((1, values[:, floorCount]), (-1, values[:, 0])):
    raised = foot > tops - playerScale.stepHeight
    if raised.any():
      row = int(numpy.flatnonzero(raised)[0])
      limit = "the lowest trim band" if lowestBand <= definition["wallShare"] * rows["heights"][row] else "the top of the walls' straight part"
      raise ValueError(
        f"{bridgeCaveRuns.capitalized(worked['owner'])}'s floor strokes raise its {'right' if side == 1 else 'left'} wall's foot {foot[row]:.1f} at"
        f" {rows['alongs'][row]:.1f} along it, within a step of {limit} ({tops[row]:.1f} over the floor), so the wall would fold; lower the"
        " stroke's rise or keep it off the wall there"
      )
    onSide = numpy.flatnonzero(shape["wallSide"] == side)
    lift = foot[:, None] * numpy.clip(1 - ups[:, onSide] / tops[:, None], 0.0, 1.0)
    vertices[:, onSide, 2] += numpy.where(ups[:, onSide] < tops[:, None], lift, 0.0)
  sunk = values < -1e-6
  if sunk.any():
    requireFloorOnRock(surface, vertices[:, :floorCount + 1][sunk], worked["owner"])
  faceRows = numpy.array([row for row in own[:-1] if row + 1 in own], dtype=numpy.int64)
  if not len(faceRows):
    return materials
  middleAlongs = numpy.repeat((rows["alongs"][faceRows] + rows["alongs"][faceRows + 1]) / 2, floorCount)
  middleOffsets = ((offsets[faceRows, :floorCount] + offsets[faceRows, 1:floorCount + 1] + offsets[faceRows + 1, :floorCount] + offsets[faceRows + 1, 1:floorCount + 1]) / 4).ravel()
  plan = sections[:, :floorCount + 1, :2]
  middlePoints = ((plan[faceRows, :floorCount] + plan[faceRows, 1:] + plan[faceRows + 1, :floorCount] + plan[faceRows + 1, 1:]) / 4).reshape(-1, 2)
  found = relief.materials(middleAlongs, middleOffsets, middlePoints).reshape(len(faceRows), floorCount)
  for position, row in enumerate(faceRows.tolist()):
    materials[row] = found[position].tolist()
  return materials


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


# Runs together: junctions and the rock between them

def caveTubes(definition, worked, surface):
  """Every run's tube, cut as one cave: each branch checked where it leaves its parent (junction), every tube broken up with its
  breakup fading out at its junctions, its floor relieved, and the rock left between runs checked (requireRockBetweenRuns). Refuses a
  cave with no open end."""
  shapes, rowsOf, unbroken, trees = {}, {}, {}, {}
  for name, run in worked.items():
    spec = run["run"]
    shapes[name] = sectionShape(definition, spec["widths"], spec["heights"], spec["path"], run["owner"], run["relief"].breaks())
    rowsOf[name] = tubeRows(definition, run, surface, shapes[name])
    unbroken[name] = unbrokenTube(definition, rowsOf[name])
    trees[name] = surfaceTree(unbroken[name]["vertices"], unbroken[name]["faces"])
  opens = [(name, end) for name, rows in rowsOf.items() for end, kind in rows["ends"].items() if kind in ("open", "ledge")]
  if not opens:
    raise ValueError("Every end of the cave lies wholly inside the rock, so nothing would open into it; start or end it on open ground in front of its mouth")
  junctions = [junctionOf(worked[name], worked[run["parent"]], rowsOf[name], unbroken[name], trees[run["parent"]]) for name, run in worked.items() if run["parent"] is not None]
  tubes = {}
  for name in worked:
    related = [trees[worked[name]["parent"]]] if worked[name]["parent"] is not None else []
    related += [trees[child] for child, run in worked.items() if run["parent"] == name]
    tubes[name] = brokenTube(definition, worked[name], rowsOf[name], unbroken[name], surface, related) | {"rows": rowsOf[name]}
  requireRockBetweenRuns(definition, worked, tubes, trees, junctions)
  return tubes, junctions


def junctionOf(branch, parent, rows, unbroken, parentTree):
  """Where a branch leaves its parent: its first section must stand wholly inside the parent's walls, its floor neither under the
  parent's (a hole in the floor) nor more than a step over it unless it is an overlook; the place it passes out through the parent's
  walls is its opening, framed for a portal piece {center (the floor's middle), facingDegrees (back into the parent), width, height}."""
  line, parentLine, owner = branch["line"], parent["line"], branch["owner"]
  named = bridgeCaveRuns.capitalized(owner)
  own = numpy.flatnonzero(rows["scales"] == 1.0)
  first = unbroken["sections"][own[0]]
  start = line.at(numpy.array([0.0]))[0][0]
  floorPoints = first[:rows["shape"]["floorCount"] + 1]
  designed = numpy.array([parentFloorAt(parentLine, point) for point in floorPoints])
  parentFloors, _, _, standing = relievedFloorsAt(parent, floorPoints)
  drop = parentFloors - floorPoints[:, 2]
  if drop.max() > bridgeCaveRuns.levelTolerance:
    worst = int(drop.argmax())
    if designed.max() - designed.min() > bridgeCaveRuns.levelTolerance:
      raise ValueError(
        f"{named}'s first section lies across its parent '{parent['name']}''s floor where it climbs {designed.max() - designed.min():.1f} over the"
        f" branch's width, so part of the branch's level floor would lie {drop.max():.2f} under the parent's (at {roundedPoint(floorPoints[worst])}), a hole in"
        " the parent's floor. Leave the parent where its floor is level across the branch's width (a stretch graded 0, or a landing at a bend,"
        " at least as long as the branch is wide), or make the branch an overlook"
      )
    if standing[worst]:
      raise ValueError(
        f"{named}'s floor where it starts lies {drop.max():.2f} under its parent '{parent['name']}''s floor stroke(s) {sorted(set(standing[worst]))} there"
        f" (at {roundedPoint(floorPoints[worst])}): it would cut into them. Start it on them, at their height, or keep them clear of where it starts"
      )
    raise ValueError(
      f"{named}'s floor where it starts, at {roundedPoint(floorPoints[worst])}, lies {drop.max():.2f} under its parent '{parent['name']}''s floor there:"
      " it would leave a hole in the parent's floor. Start it level with the parent's floor or above it"
    )
  rise = float(start[2] - relievedFloorsAt(parent, start[None])[0][0])
  if rise > playerScale.stepHeight + 1e-9 and not branch["run"]["overlook"]:
    raise ValueError(
      f"{named} starts {rise:.1f} over its parent '{parent['name']}''s floor, more than a step ({playerScale.stepHeight:g}):"
      " a branch starts on its parent's floor; an opening high in the parent's wall that nobody walks through (a balcony, a window) is an"
      " overlook: give the branch overlook true"
    )
  reach = signedDistances(parentTree, first)
  if reach.max() > junctionTolerance:
    raise ValueError(
      f"{bridgeCaveRuns.capitalized(owner)} starts at {roundedPoint(start)} with its first section reaching {reach.max():.1f} out of its parent"
      f" '{parent['name']}''s walls (at {roundedPoint(first[int(reach.argmax())])}); start it inside its parent, its whole width and height within the parent's"
    )
  alongs = line.samples(1.0)
  floors, directions, widths, heights = line.at(alongs)
  lifted = floors + numpy.column_stack([numpy.zeros((len(floors), 2)), numpy.minimum(playerScale.stepHeight, heights / 2)])
  outside = signedDistances(parentTree, lifted) > 0
  if not outside.any():
    raise ValueError(f"{bridgeCaveRuns.capitalized(owner)} never leaves its parent '{parent['name']}': its whole length lies inside the parent's walls")
  index = int(numpy.argmax(outside))
  low, high = float(alongs[max(index - 1, 0)]), float(alongs[index])
  for _ in range(20):
    middle = (low + high) / 2
    floor, _, _, height = line.at(numpy.array([middle]))
    if signedDistances(parentTree, floor + [0.0, 0.0, min(playerScale.stepHeight, height[0] / 2)])[0] > 0:
      high = middle
    else:
      low = middle
  exitFloor, exitDirection, exitWidth, exitHeight = (part[0] for part in line.at(numpy.array([high])))
  requireNoTrench(branch, parent, high)
  return {
    "branch": branch["name"], "from": parent["name"], "start": roundedPoint(start), "rise": round(rise, 2), "overlook": branch["run"]["overlook"],
    "exitAlong": high, "frame": {
      "center": roundedPoint(exitFloor), "facingDegrees": round(math.degrees(math.atan2(-exitDirection[0], -exitDirection[1])) % 360.0, 1),
      "width": round(float(exitWidth), 1), "height": round(float(exitHeight), 1),
    },
  }


def relievedFloorsAt(run, points):
  """A run's floor as its strokes leave it under plan points: its designed floor at the nearest point of its line plus its strokes'
  relief there; with how far across the line each point stands, the run's width there, and the rubble and pads standing there."""
  line, relief = run["line"], run["relief"]
  points = numpy.asarray(points, dtype=numpy.float64)[:, :2]
  alongs = numpy.array([line.nearestAlong(point)[0] for point in points])
  floors, directions, widths, _ = line.at(alongs)
  offsets = ((points - floors[:, :2]) * numpy.column_stack([directions[:, 1], -directions[:, 0]])).sum(axis=1)
  raised = relief.relief(alongs, offsets[:, None], points[:, None, :], widths)[:, 0]
  return floors[:, 2] + raised, offsets, widths, relief.strokesAt(alongs, offsets, points)


def requireNoTrench(branch, parent, exitAlong):
  """Refuse a branch whose floor, from its start to where it leaves its parent's walls, runs more than a step under the parent's floor
  strokes standing there (rubble, a pad): the union would cut a trench through them that nobody drew."""
  line = branch["line"]
  alongs = numpy.linspace(0.0, exitAlong, max(2, math.ceil(exitAlong / 2.0) + 1))
  floors, directions, widths, _ = line.at(alongs)
  shares = numpy.linspace(-0.5, 0.5, 9)
  right = numpy.column_stack([directions[:, 1], -directions[:, 0]])
  points = (floors[:, None, :2] + shares[None, :, None] * widths[:, None, None] * right[:, None, :]).reshape(-1, 2)
  heights = numpy.repeat(floors[:, 2], len(shares))
  parentFloors, offsets, parentWidths, standing = relievedFloorsAt(parent, points)
  stroked = numpy.array([bool(names) for names in standing])
  over = numpy.where((numpy.abs(offsets) <= parentWidths / 2) & stroked, parentFloors - heights, -math.inf)
  if over.max() <= playerScale.stepHeight + 1e-9:
    return
  worst = int(over.argmax())
  raise ValueError(
    f"{bridgeCaveRuns.capitalized(branch['owner'])} leaves its parent '{parent['name']}' through its floor stroke(s) {sorted(set(standing[worst]))}, standing"
    f" {over[worst]:.1f} over the branch's floor at {roundedPoint(points[worst])}: the branch would cut a trench through them. Run a level way to"
    " where it leaves (a threshold), keep them clear of it, or start the branch on them"
  )


def tubeSamples(tube):
  """A tube's vertices and face middles, where the rock round it is measured, with the row each stands on."""
  vertices = tube["vertices"]
  middles = numpy.array([vertices[list(face)].mean(axis=0) for face in tube["faces"]])
  faceRows = numpy.array([max(span) for span in tube["spans"]])
  return numpy.vstack([vertices, middles]), numpy.concatenate([tube["vertexRows"], faceRows])


def requireRockBetweenRuns(definition, worked, tubes, trees, junctions):
  """Refuse two runs of the cave coming closer than minimumRock anywhere but at their own junction: a branch measured from where it is
  clear of its parent (its section's length plus the rock past where it leaves the parent's walls); a run's parts that lie inside a
  third run (two branches leaving one room) left out; and a run against itself, measured between parts far apart along it (a spiral)."""
  minimumRock = definition["minimumRock"]
  exits = {junction["branch"]: junction["exitAlong"] for junction in junctions}
  names = list(worked)
  samples = {name: tubeSamples(tubes[name]) for name in names}
  brokenTrees = {name: surfaceTree(tubes[name]["vertices"], tubes[name]["faces"]) for name in names}

  def clearAlong(name):
    run = worked[name]["run"]
    return exits[name] + junctionReachShare * max(max(run["widths"]), max(run["heights"])) + minimumRock

  for name in names:
    points, rowIndices = samples[name]
    alongs = tubes[name]["rows"]["alongs"][rowIndices]
    for other in names:
      if other == name:
        continue
      keep = numpy.ones(len(points), dtype=bool)
      if worked[name]["parent"] == other:
        keep &= alongs > clearAlong(name)
      for third in names:
        if third not in (name, other):
          keep &= signedDistances(trees[third], points) > 0 if keep.any() else keep
      if worked[other]["parent"] == name:
        otherAlongs = tubes[other]["rows"]["alongs"]
        clear = numpy.flatnonzero(otherAlongs > clearAlong(other))
        if not len(clear):
          continue
        faces = [face for face, span in zip(tubes[other]["faces"], tubes[other]["spans"]) if min(span) >= clear[0]]
        target = surfaceTree(tubes[other]["vertices"], faces)
        distances = nearestDistances(target, points[keep]) if keep.any() else numpy.zeros(0)
      else:
        distances = signedDistances(brokenTrees[other], points[keep]) if keep.any() else numpy.zeros(0)
      if len(distances) and distances.min() < minimumRock:
        worst = int(distances.argmin())
        place = points[keep][worst]
        raise ValueError(
          f"The cave's runs '{name}' and '{other}' come within {max(distances.min(), 0.0):.1f} of each other at {roundedPoint(place)}"
          + (" (they cross)" if distances.min() < 0 else "") + f", less than minimumRock ({minimumRock:g}) of rock between them away from their"
          " junction; move one, or give it a grade that keeps them apart"
        )
    reach = 2 * max(max(worked[name]["run"]["widths"]), max(worked[name]["run"]["heights"])) + minimumRock
    rowAlongs = tubes[name]["rows"]["alongs"]
    faceAlongs = numpy.array([rowAlongs[max(span)] for span in tubes[name]["spans"]])
    for point, along in zip(points.tolist(), alongs.tolist()):
      for _, _, faceIndex, distance in brokenTrees[name].find_nearest_range(mathutils.Vector(point), minimumRock):
        if abs(faceAlongs[faceIndex] - along) > reach:
          raise ValueError(
            f"{bridgeCaveRuns.capitalized(worked[name]['owner'])} passes within {distance:.1f} of itself at {roundedPoint(point)}, less than"
            f" minimumRock ({minimumRock:g}) of rock between its parts {along:.0f} and {faceAlongs[faceIndex]:.0f} along it; keep them further apart"
          )


def combinedTube(tubes):
  """The runs' tubes as one: vertices, faces, spans (rows numbered across the runs), bands, stroke materials, each vertex's row, and each
  row's floor height."""
  vertices, faces, spans, bands, materials, vertexRows, floors = [], [], [], [], [], [], []
  vertexOffset = rowOffset = 0
  for tube in tubes.values():
    vertices.append(tube["vertices"])
    faces += [tuple(index + vertexOffset for index in face) for face in tube["faces"]]
    spans.append(tube["spans"] + rowOffset)
    bands.append(tube["bands"])
    materials += tube["materials"]
    vertexRows.append(tube["vertexRows"] + rowOffset)
    floors.append(tube["rows"]["floors"][:, 2])
    vertexOffset += len(tube["vertices"])
    rowOffset += len(tube["rows"]["floors"])
  return {
    "vertices": numpy.vstack(vertices), "faces": faces, "spans": numpy.vstack(spans), "bands": numpy.concatenate(bands), "materials": materials,
    "vertexRows": numpy.concatenate(vertexRows), "floors": numpy.concatenate(floors),
    "shortestStretch": min(tube["rows"]["shape"]["shortestStretch"] for tube in tubes.values()),
  }


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
  """Run a change to a terrain's caves whole or not at all: on any failure the mesh, its caves, and the lights anchored on them (which
  every cut places again) go back as they were."""
  mesh = sceneObject.data
  backup = mesh.copy()
  known = sceneObject.get(bridgeCaveData.caveProperty)
  anchored = [
    (lightObject, lightObject.location.copy()) for lightObject in bpy.data.objects
    if lightObject.type == "LIGHT" and bridgeCaveLight.anchorProperty in lightObject and json.loads(lightObject[bridgeCaveLight.anchorProperty])["objectName"] == sceneObject.name
  ]
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
    for lightObject, location in anchored:
      lightObject.location = location
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


def booleanCut(patchPositions, patchFaces, ring, tubeParts):
  """The patch closed into a solid down to below the tubes, less the union of the tubes (tubeParts: each run's vertices, faces, and its
  first face's index among all the runs' faces) by the exact boolean, a collection operand: positions, faces, each face's source (its
  patch face, -1 for the solid's sides and bottom, -2 less its index for a face of a tube), and face normals."""
  bottom = min(float(patchPositions[:, 2].min()), min(float(vertices[:, 2].min()) for vertices, _, _ in tubeParts)) - solidDepth
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
  tubes = [temporaryObject("zonewrightCaveTube", vertices, tubeFaces, [-2 - first - index for index in range(len(tubeFaces))]) for vertices, tubeFaces, first in tubeParts]
  temporary = [solid] + tubes
  try:
    tube = tubes[0] if len(tubes) == 1 else unitedTubes(tubes, temporary)
    modifier = solid.modifiers.new("cut", "BOOLEAN")
    modifier.object, modifier.operation, modifier.solver = tube, "DIFFERENCE", "EXACT"
    depsgraph = bpy.context.evaluated_depsgraph_get()
    cut = bpy.data.meshes.new_from_object(solid.evaluated_get(depsgraph), preserve_all_data_layers=True, depsgraph=depsgraph)
  finally:
    for sceneObject in temporary:
      removeTemporary(sceneObject)
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


def snappedCut(positions, faces, sources, normals, original):
  """The cut's faces with the corners standing on one of the ground's own vertices taken as that one corner, and copies of one new point
  (within matchDistance) taken as its first, repeats that leaves in a row dropped, and faces left with fewer than three corners dropped.
  Where the tube runs on the ground's own vertices and edges (a hall's floor and walls on the grid's lines, a tube's wall standing on
  the ground in front of its mouth), the exact solver leaves copies of one point a float's rounding apart, joined by faces with no area."""
  canonical = numpy.arange(len(original))
  first = {}
  for index in numpy.flatnonzero(original >= 0).tolist():
    canonical[index] = first.setdefault(int(original[index]), index)
  tree = mathutils.kdtree.KDTree(len(positions))
  for index, point in enumerate(positions.tolist()):
    tree.insert(point, index)
  tree.balance()
  for index in numpy.flatnonzero(original < 0).tolist():
    copies = [other for _, other, _ in tree.find_range(positions[index], matchDistance) if other < index and original[other] < 0]
    if copies:
      canonical[index] = canonical[min(copies)]
  kept, keptSources, keptNormals = [], [], []
  for face, source, normal in zip(faces, sources, normals):
    corners = [int(canonical[index]) for index in face]
    for loop in simpleLoops([corner for position, corner in enumerate(corners) if corner != corners[position - 1]]):
      kept.append(loop)
      keptSources.append(int(source))
      keptNormals.append(normal)
  return kept, keptSources, keptNormals


def simpleLoops(corners):
  """A face's corners as loops that pass each corner once, each of at least three: snapping can pinch a face's outline at a corner it
  passes twice, leaving two lobes meeting there."""
  for position, corner in enumerate(corners):
    again = corners.index(corner, position + 1) if corner in corners[position + 1:] else None
    if again is not None:
      return simpleLoops(corners[position:again]) + simpleLoops(corners[again:] + corners[:position])
  return [corners] if len(corners) >= 3 else []


def groundedFloor(positions, faces, sources, normals, patchPositions, patchFaces):
  """The faces' sources with each face of the tube's floor that lies in the ground's own plane (where the tube runs out in the open in
  front of its mouth, its floor level with the ground, the exact boolean may keep the tube's floor there rather than the ground's) taken
  as a piece of the ground face it lies on, so the open ground in front of a mouth stays ground."""
  tree = mathutils.bvhtree.BVHTree.FromPolygons(patchPositions.tolist(), patchFaces)
  sources = list(sources)
  for index, (face, source, normal) in enumerate(zip(faces, sources, normals)):
    if source > -2 or normal[2] <= floorNormalZ:
      continue
    middle = mathutils.Vector(positions[face].mean(axis=0).tolist())
    location, _, patchFace, _ = tree.ray_cast(middle + up * groundPlaneTolerance, down, 2 * groundPlaneTolerance)
    if location is not None:
      sources[index] = patchFace
  return sources


def isFlat(points):
  """Whether a face's corners lie on a line, within matchDistance of the line through its two farthest apart."""
  points = numpy.asarray(points, dtype=numpy.float64)
  spans = numpy.linalg.norm(points[:, None, :] - points[None, :, :], axis=2)
  first, second = numpy.unravel_index(int(spans.argmax()), spans.shape)
  length = float(spans[first, second])
  if length <= matchDistance:
    return True
  direction = (points[second] - points[first]) / length
  offsets = points - points[first]
  return bool(numpy.linalg.norm(offsets - (offsets @ direction)[:, None] * direction, axis=1).max() <= matchDistance)


def withoutSlivers(positions, faces, sources, normals):
  """The faces with each face of the tube whose corners lie on a line (isFlat) taken out, so the surface stays closed where the tube runs
  along the ground's own edges: a triangle with no area has its middle corner set into the face across its long edge (unless that is the
  solid's side); a slit, whose outline runs out along a line and back (where a tube's flat end stands on the ground), has each of its
  corners set into the face across the edge of it the corner lies on, so the faces either side of the line meet edge to edge (unless an
  edge of it is on no other face, the solid's side takes a corner, or two of its corners stand at one place)."""
  owners = {}
  for index, face in enumerate(faces):
    for position, corner in enumerate(face):
      owners[corner, face[(position + 1) % len(face)]] = index
  dropped = set()
  for index, face in enumerate(faces):
    if sources[index] > -2 or not isFlat(positions[face]):
      continue
    plans = sliverTriangle(positions, faces, sources, owners, dropped, index) if len(face) == 3 else slitPolygon(positions, faces, sources, owners, dropped, index)
    if plans is None:
      continue
    for across, start, end, between in plans:
      neighbour = faces[across]
      position = next(position for position, corner in enumerate(neighbour) if corner == end and neighbour[(position + 1) % len(neighbour)] == start)
      neighbour[position + 1:position + 1] = between
      del owners[end, start]
      chain = [end] + between + [start]
      for corner, following in zip(chain, chain[1:]):
        owners[corner, following] = across
    dropped.add(index)
  keep = [index for index in range(len(faces)) if index not in dropped]
  return [faces[index] for index in keep], [sources[index] for index in keep], [normals[index] for index in keep]


def sliverTriangle(positions, faces, sources, owners, dropped, index):
  """For a flat triangle: its middle corner into the face across its long edge, as [(across, start, end, [middle])]; None to keep it."""
  face = faces[index]
  points = positions[face]
  lengths = [numpy.linalg.norm(points[(position + 1) % 3] - points[position]) for position in range(3)]
  longest = int(numpy.argmax(lengths))
  start, end, middle = face[longest], face[(longest + 1) % 3], face[(longest + 2) % 3]
  across = owners.get((end, start))
  if across is None or across in dropped or sources[across] == -1 or middle in faces[across]:
    return None
  return [(across, start, end, [middle])]


def slitPolygon(positions, faces, sources, owners, dropped, index):
  """For a flat face of four or more corners: for each of its edges, the corners lying between the edge's ends to set into the face
  across it, ordered from the edge's end, as [(across, start, end, corners)]; None to keep it."""
  face = faces[index]
  points = positions[face]
  spans = numpy.linalg.norm(points[:, None, :] - points[None, :, :], axis=2)
  if (spans + numpy.eye(len(face)) * matchDistance * 2 <= matchDistance).any():
    return None
  first, second = numpy.unravel_index(int(spans.argmax()), spans.shape)
  alongs = (points - points[first]) @ ((points[second] - points[first]) / spans[first, second])
  plans = []
  for position, (start, end) in enumerate(zip(face, face[1:] + face[:1])):
    across = owners.get((end, start))
    if across is None or across == index or across in dropped:
      return None
    low, high = sorted((alongs[position], alongs[(position + 1) % len(face)]))
    between = sorted((corner for corner, along in zip(face, alongs) if low < along < high and corner not in faces[across]), key=lambda corner: abs(alongs[face.index(corner)] - alongs[(position + 1) % len(face)]))
    if between and sources[across] == -1:
      return None
    if between:
      plans.append((across, start, end, between))
  return plans


def unitedTubes(tubes, temporary):
  """The runs' tubes as one closed solid, their union by the exact boolean (the others a collection operand of the first), each face
  keeping its tube face's source; added to temporary, to be removed."""
  operands = bpy.data.collections.new("zonewrightCaveTubes")
  bpy.context.scene.collection.children.link(operands)
  try:
    for tube in tubes[1:]:
      bpy.context.scene.collection.objects.unlink(tube)
      operands.objects.link(tube)
    modifier = tubes[0].modifiers.new("union", "BOOLEAN")
    modifier.operand_type, modifier.collection, modifier.operation, modifier.solver = "COLLECTION", operands, "UNION", "EXACT"
    depsgraph = bpy.context.evaluated_depsgraph_get()
    united = bpy.data.meshes.new_from_object(tubes[0].evaluated_get(depsgraph), preserve_all_data_layers=True, depsgraph=depsgraph)
    tubes[0].modifiers.remove(modifier)
  finally:
    for tube in tubes[1:]:
      operands.objects.unlink(tube)
      bpy.context.scene.collection.objects.link(tube)
    bpy.data.collections.remove(operands)
  sceneObject = bpy.data.objects.new("zonewrightCaveTubes", united)
  bpy.context.scene.collection.objects.link(sceneObject)
  temporary.append(sceneObject)
  return sceneObject


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


def closedPatch(patch, excluded):
  """The patch with its notches filled: the faces around a vertex its edge passes through twice (two of its faces meeting at a corner),
  and faces all of whose corners it holds, taken in until there are none; never a face whose index excluded marks (another cave's
  lining, which the patch passes over or under)."""
  patchSet = set(patch)
  for _ in range(patchRounds):
    vertices = {vertex for face in patchSet for vertex in face.verts}
    added = {face for vertex in vertices for face in vertex.link_faces if face not in patchSet and not excluded[face.index] and all(corner in vertices for corner in face.verts)}
    for vertex in vertices:
      if sum(1 for edge in vertex.link_edges if sum(face in patchSet for face in edge.link_faces) == 1) > 2:
        added |= {face for face in vertex.link_faces if face not in patchSet and not excluded[face.index]}
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
    # A new vertex takes zero in every layer; in another cave's fingerprint of its ground that reads as ground moved from the origin.
    "otherGrounds": [layer for layerName, layer in verts.float_vector.items() if layerName.startswith(bridgeCaveData.groundPrefix) and layerName != bridgeCaveData.groundPrefix + name],
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


def foldedFlaps(caveFaces):
  """Faces a weld pressed flat over a neighbour, turned over: a face on an edge three faces share, facing opposite one of the others it
  lies on, with an edge of its own no other face shares. It encloses nothing; taking it out leaves the other two."""
  flaps = set()
  for face in caveFaces:
    if not face.is_valid or not any(len(edge.link_faces) == 1 for edge in face.edges):
      continue
    for edge in face.edges:
      if len(edge.link_faces) != 3:
        continue
      if any(other is not face and other.normal.dot(face.normal) < -flapAntiparallel for other in edge.link_faces):
        flaps.add(face)
  return flaps


def requireApart(name, owners, mask):
  """Refuse a cave whose reach takes in another cave's mouth (its ring, or the ground its cut changed): caves keep their mouths apart.
  Another cave's lining passing over or under it inside the rock is measured instead (requireRockFromOtherCaves)."""
  others = owners.namesOf(mask & (owners.plug | owners.ring))
  if others:
    raise ValueError(
      f"Cave '{name}' would reach the mouth of cave(s) {others}: the ground within its reach holds the ground their cut changed; caves keep"
      " their mouths apart (inside the rock they may pass over or under each other), so move it (or take one back)"
    )


def otherLiningFaces(sceneObject):
  """The faces of every cave's lining on a mesh, which another cave's cut passes over or under but never takes into its ground."""
  mask = numpy.zeros(len(sceneObject.data.polygons), dtype=bool)
  for other in bridgeCaveData.caves(sceneObject):
    mask |= bridgeCaveData.faceTags(sceneObject, other) == bridgeCaveData.liningFaceTag
  return mask


def liningTriangles(sceneObject, name):
  """A cave's lining as vertex index triangles."""
  loopTotals, loopVertices = bridgeMeshAccess.faceLoops(sceneObject)
  starts = numpy.concatenate([[0], numpy.cumsum(loopTotals)[:-1]])
  lining = bridgeCaveData.faceTags(sceneObject, name) == bridgeCaveData.liningFaceTag
  polygons = [loopVertices[start:start + total] for start, total, keep in zip(starts.tolist(), loopTotals.tolist(), lining.tolist()) if keep]
  return [[int(polygon[0]), int(polygon[index]), int(polygon[index + 1])] for polygon in polygons for index in range(1, len(polygon) - 1)]


def insideLining(tree, origin):
  """Whether a point stands inside a cave's rooms: under its vault and over its floor, the lining met from inside both ways."""
  above = tree.ray_cast(origin, up)
  below = tree.ray_cast(origin, down)
  return above[0] is not None and above[1].z < 0 and below[0] is not None and below[1].z > 0


def requireRockFromOtherCaves(sceneObject, name, definition, shown, tube):
  """Refuse a cave whose tubes would come closer to another cave's lining than minimumRock (the larger of the two caves'), or cross it,
  naming both caves, the place, and the rock there."""
  points, _ = tubeSamples(tube)
  tubeTree = surfaceTree(tube["vertices"], tube["faces"])
  for other, record in sorted(bridgeCaveData.caves(sceneObject).items()):
    triangles = liningTriangles(sceneObject, other)
    if not triangles:
      continue
    tree = mathutils.bvhtree.BVHTree.FromPolygons(shown.tolist(), triangles)
    rock = max(definition["minimumRock"], record["definition"].get("minimumRock", definitionDefaults["minimumRock"]))
    crossing = tubeTree.overlap(tree)
    if crossing:
      place = tube["vertices"][list(tube["faces"][crossing[0][0]])].mean(axis=0)
      raise ValueError(f"Cave '{name}' would cross cave '{other}''s lining at {roundedPoint(place)}; keep minimumRock ({rock:g}) of rock between them: move it, or grade its floor clear")
    worst, worstPlace = math.inf, None
    for point in points.tolist():
      origin = mathutils.Vector(point)
      location, _, _, distance = tree.find_nearest(origin)
      if location is None:
        continue
      if insideLining(tree, origin):
        raise ValueError(f"Cave '{name}' would reach into cave '{other}' at {roundedPoint(point)}; keep minimumRock ({rock:g}) of rock between them: move it, or grade its floor clear")
      if distance < worst:
        worst, worstPlace = distance, point
    if worst < rock:
      raise ValueError(
        f"Cave '{name}' would come within {worst:.1f} of cave '{other}''s lining at {roundedPoint(worstPlace)}, less than minimumRock ({rock:g}) of"
        " rock between them; move it, or grade its floor clear"
      )


def splice(sceneObject, name, definition, strokes):
  """Cut a cave into a terrain and splice it into the mesh; keeps its record (with each run's floors as worked out), paints its lining
  with the strokes, and returns what the cut made."""
  mesh = sceneObject.data
  matrix = bridgeMeshAccess.matrixArray(sceneObject.matrix_world)
  inverse = numpy.linalg.inv(matrix)
  shown, _ = bridgeMeshAccess.readVertexArrays(sceneObject)
  surface = caveSurface(shown, bridgeMeshAccess.meshTriangles(sceneObject), definition)
  worked = workedRuns(definition, surface)
  amplitude = definition["breakup"]["amplitude"] if definition["breakup"] is not None else 0.0
  reach = breakupReach * amplitude + patchEdges * definition["edgeLength"]
  owners = bridgeCaveData.CaveVertices(sceneObject) if bridgeCaveData.holdsCaves(sceneObject) else None
  if owners is not None:
    for run in worked.values():
      floors, _, widths, _ = run["line"].at(run["line"].samples(definition["edgeLength"] * rowSampleShare))
      requireApart(name, owners, nearTube(shown, {"floors": floors, "widths": widths}, reach))
  tubes, junctions = caveTubes(definition, worked, surface)
  tube = combinedTube(tubes)
  if owners is not None:
    requireRockFromOtherCaves(sceneObject, name, definition, shown, tube)
  near = numpy.zeros(len(shown), dtype=bool)
  for run in tubes.values():
    near |= nearTube(shown, run["rows"], reach)
  near = numpy.flatnonzero(near)
  if not len(near):
    raise ValueError(f"No ground of '{sceneObject.name}' lies within reach of the cave's path")
  excluded = otherLiningFaces(sceneObject)
  wallSlot = bridgeAuthoring.materialSlot(sceneObject, definition["wallMaterial"])
  floorSlot = bridgeAuthoring.materialSlot(sceneObject, definition["floorMaterial"])
  bandSlots = [bridgeAuthoring.materialSlot(sceneObject, band["material"]) for band in definition["trimBands"]]
  strokeSlots = {material: bridgeAuthoring.materialSlot(sceneObject, material) for material in sorted({material for material in tube["materials"] if material is not None})}
  layered = bridgeMeshAccess.surfaceLayers(sceneObject)
  editor = bridgeMeshAccess.loadBMesh(sceneObject)
  try:
    # Adding a layer leaves Python's references to the mesh's elements dangling, so the cave's layers come first.
    layers = caveLayers(editor, name)
    editor.verts.ensure_lookup_table()
    editor.faces.ensure_lookup_table()
    patch = closedPatch(list({face for index in near.tolist() for face in editor.verts[index].link_faces if not excluded[face.index]}), excluded)
    patchVertices = numpy.array(sorted({vertex.index for face in patch for vertex in face.verts}), dtype=numpy.int64)
    border = next((vertex.index for face in patch for vertex in face.verts if vertex.is_boundary), None)
    if border is not None:
      raise ValueError(f"The cave reaches the edge of '{sceneObject.name}' near {[round(float(value), 1) for value in shown[border]]}; keep it {reach:g} inside the terrain's border")
    if owners is not None:
      inPatch = numpy.zeros(len(shown), dtype=bool)
      inPatch[patchVertices] = True
      requireApart(name, owners, inPatch)
    report = spliceInto(editor, sceneObject, name, definition, shown, inverse, layers, patch, tube, tubes, wallSlot, floorSlot, bandSlots, strokeSlots)
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
  known[name] = {
    "definition": definition, "plug": report.pop("records"), "paint": strokes,
    "groundHeights": {runName: run["groundHeights"] for runName, run in worked.items() if run["groundHeights"]},
  }
  bridgeCaveData.writeCaves(sceneObject, known)
  replayStrokes(sceneObject, name, strokes)
  anchored = bridgeCaveLight.placeAnchoredLights(sceneObject, name)
  ends = [{"run": runName, "end": end, "kind": kind, "rounded": tube["rows"]["rounded"][end]} for runName, tube in tubes.items() for end, kind in tube["rows"]["ends"].items()]
  stretches = [stretch | {"run": runName} for runName, run in worked.items() for stretch in run["line"].levelStretches()]
  return report | {
    "ends": ends, "levelStretches": stretches, "runs": {runName: run["line"].grading() for runName, run in worked.items()},
    "junctions": [{key: value for key, value in junction.items() if key != "exitAlong"} for junction in junctions],
    "floorStrokes": strokeReport(definition, worked, tubes), "anchoredLights": anchored,
  }


def strokeReport(definition, worked, tubes):
  """Each floor stroke as cut: its run and kind, and for level ways and pads the height they hold."""
  report = []
  for stroke in definition["floor"]:
    relief = worked[stroke["run"]]["relief"]
    entry = {"name": stroke["name"], "kind": stroke["kind"], "run": stroke["run"]}
    if stroke["kind"] == "level":
      placed = next(level for level in relief.level if level["name"] == stroke["name"])
      entry |= {"from": round(placed["start"], 1), "to": round(placed["end"], 1), "across": placed["across"]}
    elif stroke["kind"] == "pad":
      placed = next(pad for pad in relief.pads if pad["name"] == stroke["name"])
      entry |= {"from": round(placed["start"], 1), "to": round(placed["end"], 1), "across": placed["across"], "top": round(placed["topHeight"], 2)}
    else:
      entry |= {"rise": stroke["rise"], "bank": stroke["bank"], "outline": stroke["outline"], "material": stroke["material"]}
    report.append(entry)
  return report


def spliceInto(editor, sceneObject, name, definition, shown, inverse, layers, patch, tube, tubes, wallSlot, floorSlot, bandSlots, strokeSlots):
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
  byCorners = {}
  for face in patch:
    byCorners.setdefault(frozenset(face.verts), []).append(face)
  doubledGround = [faces[0] for faces in byCorners.values() if len(faces) > 1]
  if doubledGround:
    where = roundedPoint(sceneObject.matrix_world @ doubledGround[0].calc_center_median())
    raise ValueError(
      f"The ground within the cave's reach holds {len(doubledGround)} pair(s) of faces on the same corners, near {where} (ground folded onto itself by an"
      " earlier shaping); smooth the ground there (sculptAtPoint smooth) or move the cave"
    )
  ring = patchBoundary(patch)
  patchVertices = sorted({vertex.index for face in patch for vertex in face.verts})
  localIndex = {vertex: position for position, vertex in enumerate(patchVertices)}
  firsts = numpy.cumsum([0] + [len(run["faces"]) for run in tubes.values()])
  cutPositions, cutFaces, cutSources, cutNormals = booleanCut(
    shown[patchVertices], [[localIndex[vertex.index] for vertex in face.verts] for face in patch], [localIndex[vertex] for vertex in ring],
    [(run["vertices"], run["faces"], int(first)) for run, first in zip(tubes.values(), firsts)],
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
  cutFaces, cutSources, cutNormals = withoutSlivers(cutPositions, *snappedCut(cutPositions, cutFaces, cutSources, cutNormals, original))
  cutSources = groundedFloor(cutPositions, cutFaces, cutSources, cutNormals, shown[patchVertices], [[localIndex[vertex.index] for vertex in face.verts] for face in patch])
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
  # A face with no area (a sliver an earlier contour triangulation left along a row) gives the boolean nothing to keep, so it would be
  # taken out as plug with no piece in its place: an open seam along it. Where all its corners stay, it stays as it is.
  produced = {int(source) for source in cutSources if source >= 0}
  stayed = {int(original[vertex]) for face in cutFaces for vertex in face if original[vertex] >= 0}
  unchanged |= {
    position for position, face in enumerate(patch)
    if position not in produced and all(vertex.index in stayed for vertex in face.verts) and isFlat(shown[[vertex.index for vertex in face.verts]])
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
    for layer in layers["otherGrounds"]:
      vertex[layer] = notGround
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
      material = tube["materials"][tubeFace]
      if band >= 0:
        slot = bandSlots[band]
      elif normal[2] > floorNormalZ:
        slot = floorSlot if material is None else strokeSlots[material]
      else:
        slot = wallSlot
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
    doubledEdges = {edge for face in doubled for edge in face.edges}
    bmesh.ops.delete(editor, geom=doubled, context="FACES_ONLY")
    for edge in doubledEdges:
      if edge.is_valid and not edge.link_faces:
        editor.edges.remove(edge)
    for vertex in made.values():
      if vertex.is_valid and not vertex.link_faces:
        editor.verts.remove(vertex)
  caveFaces = {face for face in caveFaces if face.is_valid}
  editor.normal_update()
  flaps = foldedFlaps(caveFaces)
  if flaps:
    flapEdges = {edge for face in flaps for edge in face.edges}
    bmesh.ops.delete(editor, geom=list(flaps), context="FACES_ONLY")
    for edge in flapEdges:
      if edge.is_valid and not edge.link_faces:
        editor.edges.remove(edge)
  caveFaces = [face for face in caveFaces if face.is_valid]
  requireSealed(caveFaces, pointOf)
  liningFaces = [face for face in caveFaces if face[layers["face"]] == bridgeCaveData.liningFaceTag]
  bandFaces = mapLining(editor, liningFaces, pointOf, definition, tube, layers["tube"])
  return {
    "object": sceneObject.name, "cave": name, "patchFaces": len(patch), "plugFaces": len(changed), "plugVertices": len(plugIdentifiers),
    "ringVertices": ringCount, "liningVertices": liningCount, "pieces": pieces, "liningFaces": lining, "weldedAtMouth": welded, "doubledFacesRemoved": len(doubled), "foldedFlapsRemoved": len(flaps),
    "tubeFaces": len(tube["faces"]), "records": records,
    "trimBands": [{"fromFloor": band["fromFloor"], "height": band["height"], "material": band["material"], "faces": count} for band, count in zip(definition["trimBands"], bandFaces)],
  } | cutReport(caveFaces, liningFaces, pointOf)


def requireSealed(caveFaces, pointOf):
  """Refuse a cut that came out broken: an edge at the cave on three or more faces, or open where its lining should meet the ground."""
  edges = {edge for face in caveFaces for vertex in face.verts for edge in vertex.link_edges}
  broken = [edge for edge in edges if len(edge.link_faces) not in (0, 2)]
  if broken:
    overUsed = sum(1 for edge in broken if len(edge.link_faces) > 2)
    where = roundedPoint((pointOf[broken[0].verts[0]] + pointOf[broken[0].verts[1]]) / 2) if broken[0].verts[0] in pointOf and broken[0].verts[1] in pointOf else None
    raise ValueError(
      f"The cut came out broken at the cave: {overUsed} edges on three or more faces and {len(broken) - overUsed} open edges where its lining"
      f" meets the ground{'' if where is None else f', first near {where}'}; nothing was changed. A slightly different path, width, or breakup cuts it cleanly"
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
  rows = tube["vertexRows"][corners]
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
    restoring, restoredCorners, doubled = [], set(), 0
    for position, plugFace in enumerate(record["plug"]):
      missing = [identifier for identifier in plugFace["vertices"] if identifier not in byIdentifier]
      if missing:
        raise ValueError(f"Cave '{name}' of '{sceneObject.name}' has lost plug vertices {missing}; it cannot be taken back")
      # Ground that an earlier contour triangulation folded onto itself held two faces on the same corners; one face puts it back.
      corners = frozenset(plugFace["vertices"])
      if corners in restoredCorners:
        doubled += 1
        continue
      restoredCorners.add(corners)
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
  return {"restoredFaces": len(record["plug"]) - doubled} | ({"doubledFacesRestoredOnce": doubled} if doubled else {})


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
  """Each cave on a mesh: its path's ends, its branches, whether its ground moved since it was cut (stale, and by how much), its lining
  faces, and the strokes kept with it."""
  moves = groundMoves(sceneObject)
  described = []
  for name, record in sorted(bridgeCaveData.caves(sceneObject).items()):
    tags = bridgeCaveData.faceTags(sceneObject, name)
    described.append({
      "name": name, "from": record["definition"]["path"][0], "to": record["definition"]["path"][-1],
      "branches": [branch["name"] for branch in record["definition"].get("branches") or []], "stale": moves[name]["largest"] > groundTolerance,
      "groundMoved": moves[name], "liningFaces": int((tags == bridgeCaveData.liningFaceTag).sum()), "strokes": len(record["paint"]),
    })
  return described


# Guides: what drawings and walks read of a cave's runs as cut

guideSpacing = 2.0
arcChord = 4.0


def runPolyline(line):
  """A run's centerline as a polyline at its floor: straight runs whole, arcs in chords of arcChord or less; with each point's distance
  along the run."""
  alongs = [line.stations, line.knotAlongs]
  for center, radius, startAngle, sweep, arcAlong in line.arcs:
    length = radius * abs(sweep)
    alongs.append(numpy.linspace(arcAlong, arcAlong + length, max(2, math.ceil(length / arcChord) + 1)))
  alongs = numpy.unique(numpy.concatenate(alongs))
  floors, _, _, _ = line.at(alongs)
  return floors, alongs


def caveGuides(sceneObject=None, caveName=None):
  """Each cave's runs as cut, for drawings and walks: per terrain mesh holding caves (or the one named, and the cave named), each run's
  centerline samples [x, y, floor, width, height, along] every guideSpacing, its polyline (runPolyline), its stations, landings, ends,
  daylight, and the floor strokes on it; and each junction."""
  objects = [sceneObject] if sceneObject is not None else [candidate for candidate in bpy.context.scene.objects if candidate.type == "MESH" and bridgeCaveData.holdsCaves(candidate)]
  guides = []
  for terrain in objects:
    for name, record in sorted(bridgeCaveData.caves(terrain).items()):
      if caveName is not None and name != caveName:
        continue
      definition = caveDefinition(**record["definition"])
      lines = recordedLines(name, record)
      runs = []
      for runName, run, parent in runSpecs(definition):
        line = lines[runName]
        alongs = line.samples(guideSpacing)
        floors, _, widths, heights = line.at(alongs)
        polyline, polylineAlongs = runPolyline(line)
        relief = bridgeCaveRuns.FloorRelief([stroke for stroke in definition["floor"] if stroke["run"] == runName], line)
        strokes = [{"name": stroke["name"], "kind": "level", "from": stroke["start"], "to": stroke["end"], "across": stroke["across"]} for stroke in relief.level]
        strokes += [{"name": pad["name"], "kind": "pad", "from": pad["start"], "to": pad["end"], "across": pad["across"], "top": pad["topHeight"], "edge": pad["edge"]} for pad in relief.pads]
        strokes += [{"name": stroke["name"], "kind": "rough", "outline": stroke["outline"], "rise": stroke["rise"], "edge": stroke["edge"], "bank": stroke["bank"]} for stroke in relief.rough]
        runs.append({
          "run": runName, "from": parent, "samples": numpy.column_stack([floors, widths, heights, alongs]).round(3).tolist(),
          "polyline": polyline.round(3).tolist(), "polylineAlongs": polylineAlongs.round(3).tolist(),
          "stations": [round(float(value), 3) for value in line.stations], "landings": [{"point": index, "arc": [round(span, 3) for span in line.turns[index]]} for index in line.landings],
          "daylight": run["daylight"], "strokes": strokes, "overlook": run["overlook"],
          "segments": [
            {"from": index, "degrees": round(math.degrees(math.atan2(float(line.floors[index + 1] - line.floors[index]), line.segmentRun(index))), 2), "middle": (line.exit(index) + line.entry(index + 1)) / 2}
            for index in range(len(line.floors) - 1)
          ],
        })
      guides.append({"object": terrain.name, "cave": name, "runs": runs})
  return guides


def guideRun(objectName, cave, run):
  """One run's guide (caveGuides), refusing an unknown terrain, cave, or run."""
  sceneObject = bridgeMeshAccess.requireMeshObject(objectName)
  requireCave(sceneObject, cave)
  runs = caveGuides(sceneObject, cave)[0]["runs"]
  found = next((entry for entry in runs if entry["run"] == run), None)
  if found is None:
    raise ValueError(f"Cave '{cave}' of '{objectName}' has no run '{run}'; its runs are {[entry['run'] for entry in runs]}")
  return found


def strokeUnder(x, y, height):
  """The level way or pad of a cave's run a point stands on (within it in plan and within a step of its height): its object, cave, run,
  name, kind, and height; or None."""
  for guide in caveGuides():
    for run in guide["runs"]:
      samples = numpy.array(run["samples"])
      nearest = int(numpy.argmin(numpy.linalg.norm(samples[:, :2] - [x, y], axis=1)))
      following = min(nearest + 1, len(samples) - 1)
      previous = max(following - 1, 0)
      direction = samples[following, :2] - samples[previous, :2]
      direction /= max(float(numpy.linalg.norm(direction)), 1e-9)
      offset = numpy.array([x, y]) - samples[nearest, :2]
      along = float(samples[nearest, 5] + offset @ direction)
      across = float(offset @ numpy.array([direction[1], -direction[0]]))
      for stroke in run["strokes"]:
        if stroke["kind"] == "rough" or not (stroke["from"] <= along <= stroke["to"] and stroke["across"][0] <= across <= stroke["across"][1]):
          continue
        level = float(numpy.interp(along, samples[:, 5], samples[:, 2])) if stroke["kind"] == "level" else stroke["top"]
        if abs(level - height) <= playerScale.stepHeight:
          return {"object": guide["object"], "cave": guide["cave"], "run": run["run"], "stroke": stroke["name"], "kind": stroke["kind"], "height": round(level, 2)}
  return None


def caveWalkPath(objectName, cave, run):
  """A run's walk at its floor from its first point to its last; a branch's walk starts at its parent's start (the cave's mouth, for a
  branch off the main run) and follows the parent to where the branch leaves it, through each junction."""
  sceneObject = bridgeMeshAccess.requireMeshObject(objectName)
  record = requireCave(sceneObject, cave)
  definition = caveDefinition(**record["definition"])
  lines = recordedLines(cave, record)
  parents = {runName: parent for runName, _, parent in runSpecs(definition)}
  if run not in parents:
    raise ValueError(f"Cave '{cave}' of '{objectName}' has no run '{run}'; its runs are {list(parents)}")
  chain = [run]
  while parents[chain[0]] is not None:
    chain.insert(0, parents[chain[0]])
  path = []
  for position, runName in enumerate(chain):
    line = lines[runName]
    stop = line.length
    if position + 1 < len(chain):
      branchStart = next(branch for branch in definition["branches"] if branch["name"] == chain[position + 1])["path"][0]
      stop, _ = line.nearestAlong(branchStart)
    alongs = line.samples(guideSpacing)
    floors, _, _, _ = line.at(numpy.append(alongs[alongs < stop], stop))
    for point in floors.round(3).tolist():
      if not path or math.dist(path[-1][:2], point[:2]) > 1e-6:
        path.append(point)
  return path


# Commands

def cutCave(
  objectName, name, path, widths, heights, wallMaterial, floorMaterial, worldUnitsPerRepeat, edgeLength, wallShare, breakup, mouthFade, maximumFloorDegrees,
  trimBands, grades, landings, daylight, branches, floor, minimumRock,
):
  sceneObject = requireTerrain(objectName)
  if not name.strip():
    raise ValueError("A cave needs a name")
  if name in bridgeCaveData.caves(sceneObject):
    raise ValueError(f"'{objectName}' already has a cave '{name}'; editCave changes it")
  definition = caveDefinition(
    path, widths, heights, wallMaterial, floorMaterial, worldUnitsPerRepeat, edgeLength, wallShare, breakup, mouthFade, maximumFloorDegrees, trimBands,
    grades, landings, daylight, branches, floor, minimumRock,
  )
  requireIntact(sceneObject)
  with restoredOnFailure(sceneObject):
    return splice(sceneObject, name, definition, [])


def mergedByName(current, changes, what):
  """A list of named entries with changes {name: entry or null} merged in: null takes an entry back, an entry's keys merge into the one
  of that name, and a new name is added at the end (the definition refuses it unless whole)."""
  if not isinstance(changes, dict):
    raise ValueError(f"editCave changes {what} by name: {{name: {what[:-1]} or null}} (null takes one back), got {changes!r}")
  merged = [dict(entry) for entry in current]
  names = [entry["name"] for entry in merged]
  for name, change in changes.items():
    if change is None:
      if name not in names:
        raise ValueError(f"The cave has no {what[:-1]} '{name}' to take back; it has {names}")
      merged = [entry for entry in merged if entry["name"] != name]
      names.remove(name)
    elif not isinstance(change, dict):
      raise ValueError(f"A change to {what[:-1]} '{name}' is its keys to change, or null to take it back, got {change!r}")
    elif name in names:
      merged = [entry | change | {"name": name} if entry["name"] == name else entry for entry in merged]
    else:
      merged.append(change | {"name": name})
      names.append(name)
  return merged


def recut(sceneObject, name, changes):
  """Take a cave back and cut it again from its definition with the changes merged in (its branches and floor strokes by name), whole or
  not at all."""
  record = requireCave(sceneObject, name)
  unknown = sorted(set(changes) - set(definitionKeys))
  if unknown:
    raise ValueError(f"A cave's definition holds {list(definitionKeys)}; unknown {unknown}")
  stored = record["definition"]
  merged = dict(changes)
  for key in ("branches", "floor"):
    if key in changes:
      merged[key] = mergedByName(stored.get(key) or [], changes[key], "branches" if key == "branches" else "floor strokes")
  definition = caveDefinition(**(stored | merged))
  with restoredOnFailure(sceneObject):
    restored = takeBack(sceneObject, name)
    return {"restored": restored, "cut": splice(sceneObject, name, definition, record["paint"])}


def removeCave(objectName, name):
  sceneObject = requireTerrain(objectName)
  record = requireCave(sceneObject, name)
  requireIntact(sceneObject)
  with restoredOnFailure(sceneObject):
    restored = takeBack(sceneObject, name)
    if bridgeMeshAccess.surfaceLayers(sceneObject):
      bridgeAuthoring.showLayers(sceneObject)
  return {
    "object": objectName, "cave": name, "definition": record["definition"], "strokes": record["paint"],
    "anchoredLightsLeft": bridgeCaveLight.anchoredLights(sceneObject, name),
  } | restored


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
    bridgeCaveRuns.CaveLine(path[:, :2], [float(width)] * count, [float(height)] * count)
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
  "removeCave": (removeCave, True),
  "traceLedge": (traceLedge, False),
}
