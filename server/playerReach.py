"""The players' lanes, one per body group (playerScale): every place in the zone a group can reach from where players arrive, by the moves
the Recast helper's reach mode models with the group's measures (docs/zoneWorkflow.md, Player space: reach), read here with numpy,
classified against the regions, and turned into numbered problems, each judged against the groups its region (else the zone) is built
for. A lane is computed from what the zone ships (bridgeLane.laneInput) or from a zone's server files (laneFromServerFiles), and kept
under the tooling root by a key of everything it depends on, so an unchanged zone's lanes are reused; the newest three scenes' are kept.
Reach never runs in Blender: the bridge only collects and draws.

Frames: the helper works in Recast axes (zone y, zone z, zone x); its columns run along Recast x (zone y) and z (zone x), and every
point given or returned here is in zone axes."""
import hashlib
import json
import math
import os
import struct
import time
from pathlib import Path

import numpy
from mcp.server.mcpserver.exceptions import ToolError
from PIL import Image, ImageDraw

import buildTolerances
import checkpoints
import eqAxes
import machineProfile
import playerScale
import recastHelper
import serverMapFiles
import toolingManifest

# Chosen by measurement (README, Recast helper): at cellSize 1 the canyonhousing copy (8,448 x 4,224) takes 14 s and 1.1 GB
# in the helper and Highpass Hold 1.4 s; 0.5 reaches the same area within 1% and takes the canyon to 56 s and 4.4 GB; 4 and 8 cut
# every face steeper than about 48 and 29 degrees, whose rise per column passes the step, and reach 77% and 9% of Highpass Hold. The
# helper centers its columns on multiples of cellSize.
reachGrid = {"cellSize": 1.0, "cellHeight": 0.25, "tileCells": 256}
# The newest three scenes' lanes, one per body group each.
keptLanes = 3 * len(playerScale.groupNames)
untitledFolder = "untitled"
serverFilesFolder = "serverFiles"
footingSearch = {"above": buildTolerances.groundSearchAbove, "below": buildTolerances.groundSearchDistance}
# Problems gather the columns they hold into blocks this wide, joined where blocks touch, so one leak is one problem.
problemBlock = 8.0
problemsListed = 20
# A problem is viewed from an eye problemViewFrame times its radius back from it, which frames it in the middle of a problem view's
# field, and never nearer than problemViewDistance; the eye is raised by each elevation in turn, steeper only where a lower one is
# blocked, and turned to each bearing (degrees from the side of the reached floor nearest it).
problemViewFrame = 3.0
problemViewDistance = 4 * playerScale.tallest().playerHeight
problemViewElevations = (30.0, 50.0, 70.0, 85.0)
problemViewBearings = (0, 45, -45, 90, -90, 135, -135, 180)

laneHeader = struct.Struct("<8s3f2f4iI")
laneMagic = b"ZWLANE01"
moveNames = ("unreached", "seed", "walk", "drop", "slide", "enterWater", "swim", "climbOut", "sink", "teleport")
labelNames = ("unreached", "walk", "swim", "slide", "squeeze", "zoneLine")
unreachedLabel, walkLabel, swimLabel, slideLabel, squeezeLabel, zoneLineLabel = range(6)
wetShape, walkableShape, absorbedShape, touchesLineShape, boundaryShape, shallowShape = 1, 2, 4, 8, 16, 32
reachedFullState, reachedState, trapState, fromSafeState, toSafeState, openState = 1, 2, 4, 8, 16, 128
voidEvent, belowUnderworldEvent, absorbedEvent = 1, 2, 3
seedKinds = ("safePoint", "zoneIn", "landing")
problemKinds = (
  "voidEdge", "belowUnderworld", "noneRegion", "outsideRegions", "disconnectedEntry", "unreachedPlot", "trap", "viewRegion",
  "unreachedZoneLine", "unreachedSwim", "unreachedPlayFloor", "undecidedWater",
)
notModeled = [
  "jumps", "levitation", "the camera",
  "doors: taken as open, though the RoF2 client collides with a door in its closed position, so a closed door can block a way the lane shows",
  "NPCs",
  "players' width: the lane's columns are a point wide, so an opening narrower than a group's playerWidth reads as passable to it"
  " (walkRoute judges width along a route)",
]
# Containment floods with one cell height of headroom: no group's own clearance is measured, so the containment errs toward reach.
containmentHeadroom = reachGrid["cellHeight"]


def movement(groupName):
  """The movement a body group's lane floods by, from playerScale: the group's step, height, walkable normal z, and the depth of water it
  swims in; and the containment headroom every group's lane floods leaks with."""
  body = playerScale.group(groupName)
  return {
    "stepHeight": body.stepHeight, "playerHeight": body.playerHeight, "walkableNormalZ": body.walkableNormalZ,
    "minimumSwimDepth": body.minimumSwimDepth, "containmentHeadroom": containmentHeadroom,
  }


def constantsReport():
  """Each body group's constants its lane used, with their values and playerScale sources, and the containment headroom."""
  used = {
    name: {measure: playerScale.groups[name].measure(measure) for measure in ("playerHeight", "walkableNormalZ", "steepestWalkableDegrees", "stepHeight", "minimumSwimDepth")}
    for name in playerScale.groupNames
  }
  return used | {"containmentHeadroom": {
    "value": containmentHeadroom,
    "source": "unmeasured: one cell height, under every body's own clearance, so containment and the traps err toward reach; no group's clearance is measured",
  }}


def limits(lane, underworld, boundariesKnown):
  """What can make a lane wrong in either direction besides notModeled; boundariesKnown false for a lane from server files."""
  heights = ", ".join(f"{name} {playerScale.groups[name].playerHeight:g}" for name in playerScale.groupNames)
  swimDepths = ", ".join(f"{name} {playerScale.groups[name].minimumSwimDepth:g}" for name in playerScale.groupNames)
  steps = ", ".join(f"{name} {playerScale.groups[name].stepHeight:g}" for name in playerScale.groupNames)
  stated = [
    f"a gap narrower than 2 x cellSize ({2 * lane.cellSize:g}) may rasterize closed and read as no way through: containment holds only for gaps at least that wide",
    "each body group floods a lane of its own with its own moves (playerScale): a group reaches what its playerHeight fits through"
    f" ({heights}); containment floods each with {containmentHeadroom:g} of headroom, and what only a body smaller than the group fits"
    " is labelled squeeze in that group's lane",
    "an entry's connection both ways (from the safe point and back) is judged by containment with the group's moves; the group's own height"
    " is judged at the entry's node",
    f"a group enters a swim box whose bottom lies within its playerHeight ({heights}) of the floor, so a box floating higher is entered"
    " only from above",
    f"a group swims only where the water stands deeper than its minimumSwimDepth ({swimDepths}) over the floor, from the top of the boxes"
    " touching one over another there, and wades the bed where it is shallower, as the walked human did; containment and the traps swim in a"
    " box at any depth, since a smaller race may float in shallower water, so what only that reaches is labelled squeeze; a column's floor"
    " is taken from the bottom of the cell its top lies in, which errs toward swimming by up to one cell height",
    f"a swimmer climbs out onto walkable floor up to the box's top + the group's stepHeight ({steps}): an assumption erring toward reach,"
    " as in the RoF2 walk a swimmer floated with his feet about 3.85 under the box's top",
    f"swimming between columns takes {containmentHeadroom:g} of open overlap, an assumption until a walk measures it",
    "a zone line takes a player whose floor point (the column's center, one cell height over the floor) lies in its box: the client's own"
    " test point is unmeasured, so the lane errs toward reach continuing",
    "unreached play floor is walkable floor in a play region that open space joins to the lane, at any rise: the lane is built from"
    " surfaces, so the inside of a closed solid holds floor no one reaches, and floor sealed off as it is (a sealed room) is left out"
    " with it; a boundary's top and floor under the underworld are never play floor",
  ]
  if not boundariesKnown:
    stated.append("server files do not say which collision is a boundary, so a lid's top that open space joins to the lane reads as unreached play floor")
  if underworld is None:
    stated.append("no underworld is set (setZoneProperties): only a fall onto nothing reads as a leak, never one below the underworld")
  return stated


def seedTolerance(normalZ):
  """How far a seed's floor node may lie from its footing: one cell height, and the rise of its face across a column's diagonal, since
  a column's floor is the highest point of the ground over it and the footing may lie in any corner."""
  slope = math.tan(math.acos(min(1.0, max(normalZ, 1e-6))))
  return reachGrid["cellHeight"] + reachGrid["cellSize"] * math.sqrt(2) * slope


def zoneBox(center, halfExtents):
  center, half = numpy.asarray(center, dtype=numpy.float64), numpy.abs(numpy.asarray(halfExtents, dtype=numpy.float64))
  return (center - half).tolist(), (center + half).tolist()


def recastBox(low, high):
  corners = eqAxes.recastFromZone(numpy.array([low, high], dtype=numpy.float64))
  return {"low": corners.min(axis=0).tolist(), "high": corners.max(axis=0).tolist()}


def quantized(values):
  return numpy.rint(numpy.asarray(values, dtype=numpy.float64) * 1000).astype(numpy.int64)


def laneKey(fingerprint, triangles, boundary, description, groupName):
  """SHA-256 of the helper's build, the grid, the group's movement and footing search, the triangles (rounded to 1e-3, each with whether
  it is a boundary's, rows sorted), and the boxes, zone lines and their landings, seeds, and underworld."""
  rows = numpy.concatenate([quantized(triangles).reshape(-1, 9), numpy.asarray(boundary, dtype=numpy.int64).reshape(-1, 1)], axis=1)
  rows = rows[numpy.lexsort(rows.T[::-1])] if len(rows) else rows
  digest = hashlib.sha256()
  digest.update(fingerprint.encode("ascii"))
  digest.update(json.dumps([reachGrid, movement(groupName), footingSearch], sort_keys=True).encode("ascii"))
  digest.update(rows.tobytes())
  rounded = {
    "swimBoxes": [quantized([box["low"], box["high"]]).tolist() for box in description["swimBoxes"]],
    "zoneLines": [[quantized([line["low"], line["high"]]).tolist(), None if line["landing"] is None else quantized(line["landing"]).tolist()] for line in description["zoneLines"]],
    "seeds": [[seed["name"], quantized(seed["footing"]).tolist(), round(seed["tolerance"], 6)] for seed in description["seeds"]],
    "underworld": description["underworld"],
  }
  digest.update(json.dumps(rounded, sort_keys=True).encode("utf-8"))
  return digest.hexdigest()


def helperFingerprint(toolingRoot):
  status = recastHelper.helperStatus(toolingRoot, toolingManifest.loadManifest()["recast"])
  if status["state"] != "built":
    problem = f": {status['problem']}" if "problem" in status else ""
    raise ToolError(f"The Recast helper is {status['state']}{problem}; run syncTooling to build it")
  return status["fingerprint"]


def reachRoot(toolingRoot):
  return toolingRoot / "reach"


def laneFolder(toolingRoot, workFile):
  """Where a work file's lanes are kept: reach/<name>-<hash of its full path>, as checkpoints are, or reach/untitled."""
  return reachRoot(toolingRoot) / (untitledFolder if workFile is None else checkpoints.workFileKey(workFile))


def keepNewest(folder):
  lanes = sorted(folder.glob("lane-*.bin"), key=lambda path: path.stat().st_mtime_ns, reverse=True)
  for stale in lanes[keptLanes:]:
    stale.unlink()


def requireTriangles(triangles):
  if len(triangles) == 0:
    raise ToolError("The zone ships no collision triangles, so players stand on nothing: there is no lane")


def computeLanes(toolingRoot, folder, triangles, boundary, description, reportProgress):
  """Every body group's lane for collision triangles (zone axes, [n, 3, 3]), which of them are a boundary's ([n] booleans), and a
  description (swimBoxes, zoneLines, seeds, underworld), each reused when one of its key is kept: ({group: Lane}, "computed" when any
  was, else "reused", seconds). The groups' lanes hold the same nodes (one grid, one containment headroom), which is checked."""
  started = time.perf_counter()
  lanes, computed = {}, False
  for groupName in playerScale.groupNames:
    lanes[groupName], status = computeLane(toolingRoot, folder, triangles, boundary, description, groupName, reportProgress)
    computed |= status == "computed"
  first = lanes[playerScale.groupNames[0]]
  for groupName, lane in lanes.items():
    if lane.nodeCount != first.nodeCount or not numpy.array_equal(lane.read("columnStart"), first.read("columnStart")) or not numpy.array_equal(lane.read("low"), first.read("low")):
      raise ValueError(f"The {groupName} lane {lane.path} holds other nodes than the {first.group} lane {first.path}: the groups' lanes must share one grid")
  return lanes, "computed" if computed else "reused", round(time.perf_counter() - started, 2)


def computeLane(toolingRoot, folder, triangles, boundary, description, groupName, reportProgress):
  """One body group's lane, reused when one of its key is kept: (Lane, "computed" or "reused")."""
  requireTriangles(triangles)
  if not description["seeds"]:
    raise ToolError("The lane has no seeds: set the safe point (setZoneProperties safePoint) or placeEntry where players arrive")
  key = laneKey(helperFingerprint(toolingRoot), triangles, boundary, description, groupName)
  folder.mkdir(parents=True, exist_ok=True)
  lanePath = folder / f"lane-{groupName}-{key}.bin"
  if lanePath.is_file():
    os.utime(lanePath)
    return Lane(lanePath), "reused"
  safeSeeds = [index for index, seed in enumerate(description["seeds"]) if seed["kind"] == "safePoint"]
  inputBytes = recastHelper.reachInput(
    eqAxes.recastFromZone(numpy.asarray(triangles, dtype=numpy.float32)), numpy.asarray(boundary, dtype=bool), reachGrid, movement(groupName), footingSearch,
    description["underworld"],
    machineProfile.workerCount(),
    [recastBox(box["low"], box["high"]) for box in description["swimBoxes"]],
    [recastBox(line["low"], line["high"]) | {"target": None if line["landing"] is None else eqAxes.recastFromZone(numpy.array(line["landing"])).tolist()} for line in description["zoneLines"]],
    [{"name": seed["name"], "point": eqAxes.recastFromZone(numpy.array(seed["footing"])).tolist(), "tolerance": seed["tolerance"]} for seed in description["seeds"]],
    safeSeeds[0] if safeSeeds else None,
  )
  partialPath = folder / f"lane-{groupName}-{key}.bin.partial"
  try:
    _, report = recastHelper.runHelper(toolingRoot, "reach", inputBytes, reportProgress, partialPath)
    os.replace(partialPath, lanePath)
  finally:
    partialPath.unlink(missing_ok=True)
  keepNewest(folder)
  lane = Lane(lanePath)
  lane.report = report
  return lane, "computed"


class Lane:
  """A body group's lane file (ZWLANE01, named lane-<group>-<key>.bin): the grid, each column's nodes, and each node's floor and ceiling
  (cells), swim box, shape, flood state, origin seed, predecessor, and move; then the void, underworld, and zone line events, each zone
  line's state, and each seed's node. Arrays are read on demand; the file is read to its last byte or refused."""

  def __init__(self, path):
    self.path = Path(path)
    named = self.path.name.split("-")
    if len(named) < 3 or named[0] != "lane" or named[1] not in playerScale.groups:
      raise ValueError(f"{self.path} is not named lane-<group>-<key>.bin for a body group of {list(playerScale.groupNames)}")
    self.group = named[1]
    self.body = playerScale.groups[self.group]
    self.report = None
    size = self.path.stat().st_size
    with self.path.open("rb") as source:
      header = source.read(laneHeader.size)
    if len(header) < laneHeader.size or header[:8] != laneMagic:
      raise ValueError(f"{self.path} is not a lane file")
    _, *origin, self.cellSize, self.cellHeight, self.tileCells, self.tilesX, self.tilesZ, self.verticalCells, self.nodeCount = laneHeader.unpack(header)
    self.origin = numpy.array(origin, dtype=numpy.float64)
    self.columnsX, self.columnsZ = self.tilesX * self.tileCells, self.tilesZ * self.tileCells
    self.columns = self.columnsX * self.columnsZ
    count = self.nodeCount
    offset = laneHeader.size
    self.layout = {}
    for name, dtype, length in (
      ("columnStart", "<u4", self.columns + 1), ("low", "<u2", count), ("high", "<u2", count), ("box", "<i2", count), ("shape", "u1", count),
      ("state", "u1", count), ("originSeed", "<u2", count), ("predecessor", "<i4", count), ("move", "u1", count),
    ):
      self.layout[name] = (offset, numpy.dtype(dtype), length)
      offset += numpy.dtype(dtype).itemsize * length
    eventCount = self.tail(offset, "<u4", 1, size)[0]
    self.events = self.tail(offset + 4, "<i4", eventCount * 5, size).reshape(-1, 5)
    offset += 4 + eventCount * 20
    lineCount = self.tail(offset, "<u4", 1, size)[0]
    lines = self.tail(offset + 4, "<i4", lineCount * 2, size).reshape(-1, 2)
    self.lineReached, self.lineLanding = lines[:, 0].astype(bool), lines[:, 1]
    offset += 4 + lineCount * 8
    seedCount = self.tail(offset, "<u4", 1, size)[0]
    self.seedNodes = self.tail(offset + 4, "<i4", seedCount, size)
    offset += 4 + seedCount * 4
    if offset != size:
      raise ValueError(f"{self.path}: the lane ends at byte {offset} but the file holds {size}")

  def tail(self, offset, dtype, count, size):
    width = numpy.dtype(dtype).itemsize * count
    if offset + width > size:
      raise ValueError(f"{self.path}: {count} values of {dtype} at byte {offset} run past its end at {size}")
    return numpy.fromfile(self.path, dtype=dtype, count=count, offset=offset)

  def read(self, name):
    offset, dtype, length = self.layout[name]
    return numpy.fromfile(self.path, dtype=dtype, count=length, offset=offset)

  def columnPlaces(self, columns):
    """Columns' (x, z) indices along Recast x (zone y) and z (zone x), from their tile-major numbers."""
    perTile = self.tileCells * self.tileCells
    tile, within = numpy.divmod(columns, perTile)
    x = (tile % self.tilesX) * self.tileCells + within % self.tileCells
    z = (tile // self.tilesX) * self.tileCells + within // self.tileCells
    return x.astype(numpy.int32), z.astype(numpy.int32)

  def columnAt(self, point):
    """The column under a zone point, or None outside the grid."""
    return self.columnOf(int(math.floor((point[1] - self.origin[0]) / self.cellSize)), int(math.floor((point[0] - self.origin[2]) / self.cellSize)))

  def columnOf(self, x, z):
    """The column at (x, z) indices along Recast x (zone y) and z (zone x), or None outside the grid."""
    if not (0 <= x < self.columnsX and 0 <= z < self.columnsZ):
      return None
    perTile = self.tileCells * self.tileCells
    return (z // self.tileCells * self.tilesX + x // self.tileCells) * perTile + (z % self.tileCells) * self.tileCells + x % self.tileCells

  def center(self, x, z):
    """The plan center (zone x, y) of the column at (x, z) indices."""
    return [float(self.origin[2] + (z + 0.5) * self.cellSize), float(self.origin[0] + (x + 0.5) * self.cellSize)]

  def height(self, cells):
    return self.origin[1] + numpy.asarray(cells, dtype=numpy.float64) * self.cellHeight


def sceneDescription(inputPath):
  """The lane's triangles, which of them are a boundary's, and its description from bridgeLane.laneInput's file, refusing what no lane
  can start from: a stored entry off its footing, a seed without footing."""
  with numpy.load(inputPath, allow_pickle=False) as loaded:
    triangles, boundary = loaded["collision"], loaded["boundary"]
    requireTriangles(triangles)
    scene = json.loads(str(loaded["description"]))
    bodies = [{"name": name, "triangles": loaded[f"undecided{index}"]} for index, name in enumerate(scene["undecidedBodies"])]
  seeds, plots = [], []
  for entry in scene["entries"]:
    if entry["kind"] in ("zoneIn", "landing"):
      if entry["state"] != "onFooting":
        raise ToolError(f"Entry '{entry['name']}' stands off its footing ({entry['distance']} from {entry['footing']}): place it again (placeEntry) before mapping reach")
    if entry["kind"] in seedKinds:
      if entry["seedFooting"] is None:
        raise ToolError(f"The {'safe point' if entry['kind'] == 'safePoint' else 'entry ' + repr(entry['name'])} at {entry['at']} has no footing on what the zone ships, so the lane has no node there to start from")
      seeds.append({
        "name": entry["name"], "kind": entry["kind"], "point": entry["at"], "footing": entry["seedFooting"]["point"],
        "tolerance": seedTolerance(entry["seedFooting"]["normalZ"]), "isolated": entry["kind"] != "safePoint" and entry["isolated"],
        "headingDegrees": entry["headingDegrees"],
      })
    elif entry["kind"] in ("plotEntrance", "apartmentThreshold"):
      plots.append({"name": entry["name"], "point": entry["at"]})
  seeds.sort(key=lambda seed: seed["kind"] != "safePoint")
  zoneLines = []
  for line in scene["zoneLines"]:
    low, high = zoneBox(line["center"], line["halfExtents"])
    zoneLines.append({"name": line["name"], "low": low, "high": high, "landing": line["landing"], "target": line["target"]})
  swimBoxes = []
  for box in scene["swimBoxes"]:
    low, high = zoneBox(box["center"], box["halfExtents"])
    swimBoxes.append({"name": box["name"], "low": low, "high": high, "liquid": box["liquid"], "body": box["body"]})
  return triangles, boundary, {
    "swimBoxes": swimBoxes, "zoneLines": zoneLines, "seeds": seeds, "plotEntrances": plots, "regions": scene["regions"],
    "builtFor": scene["builtFor"], "underworld": scene["underworld"], "undecidedBodies": bodies, "notFollowed": scene["notFollowed"],
    "boundariesKnown": True,
  }


def currentLanes(toolingRoot, workFile, inputPath, reportProgress):
  """The open scene's lanes, one per body group, from bridgeLane.laneInput's file: ({group: Lane}, description, "computed" or "reused",
  seconds). Every consumer takes the lanes from here, so nothing is recomputed silently."""
  triangles, boundary, description = sceneDescription(inputPath)
  lanes, status, seconds = computeLanes(toolingRoot, laneFolder(toolingRoot, workFile), triangles, boundary, description, reportProgress)
  description["bounds"] = shippedBounds(triangles)
  return lanes, description, status, seconds


def shippedBounds(triangles):
  points = numpy.asarray(triangles, dtype=numpy.float64).reshape(-1, 3)
  return {"minimum": [round(float(value), 2) for value in points.min(axis=0)], "maximum": [round(float(value), 2) for value in points.max(axis=0)]}


def heightsUnder(triangles, x, y):
  """Where a vertical line through (x, y) meets triangles (zone axes, [n, 3, 3]), upright ones aside: each height, and the |normal z| of
  its triangle."""
  corners = numpy.asarray(triangles, dtype=numpy.float64)
  low, high = corners[:, :, :2].min(axis=1), corners[:, :, :2].max(axis=1)
  near = (low[:, 0] <= x) & (high[:, 0] >= x) & (low[:, 1] <= y) & (high[:, 1] >= y)
  a, b, c = corners[near, 0], corners[near, 1], corners[near, 2]
  normal = numpy.cross(b - a, c - a)
  flat = numpy.abs(normal[:, 2]) > 1e-12
  a, b, c, normal = a[flat], b[flat], c[flat], normal[flat]
  denominator = (b[:, 1] - c[:, 1]) * (a[:, 0] - c[:, 0]) + (c[:, 0] - b[:, 0]) * (a[:, 1] - c[:, 1])
  u = ((b[:, 1] - c[:, 1]) * (x - c[:, 0]) + (c[:, 0] - b[:, 0]) * (y - c[:, 1])) / denominator
  v = ((c[:, 1] - a[:, 1]) * (x - c[:, 0]) + (a[:, 0] - c[:, 0]) * (y - c[:, 1])) / denominator
  w = 1 - u - v
  hit = (u >= -1e-9) & (v >= -1e-9) & (w >= -1e-9)
  z = u * a[:, 2] + v * b[:, 2] + w * c[:, 2]
  return z[hit], numpy.abs(normal[hit, 2]) / numpy.linalg.norm(normal[hit], axis=1)


def footingOnTriangles(triangles, point):
  """The highest footing on triangles (zone axes) under a point, from footingSearch's above over it down to its below under it: (point,
  |normal z|), or None."""
  x, y = point[0], point[1]
  z, normalZ = heightsUnder(triangles, x, y)
  hit = (z <= point[2] + footingSearch["above"]) & (z >= point[2] - footingSearch["below"])
  if not hit.any():
    return None
  best = int(numpy.argmax(numpy.where(hit, z, -numpy.inf)))
  return [float(x), float(y), float(z[best])], float(normalZ[best])


def laneFromServerFiles(mapBytes, waterBytes, seeds, toolingRoot, reportProgress, regions=None, plots=None, underworld=None, builtFor=None):
  """The lanes a zone's server files give, one per body group: the collision the server rebuilds from the .map (zone axes) and the
  .wtr's boxes, water and lava as swim boxes and zone lines as zone lines, each negative extent swapped as the server swaps it. seeds are
  [{name, point (zone axes), kind ("safePoint", "zoneIn", or "landing"), isolated, and headingDegrees or none}], each standing on the
  footing found under its point; regions as getRegions gives them, plot entrances [{name, point}], the underworld (the zone row's), and
  the zone's builtFor are optional. A record of another type is listed, not modelled. Returns ({group: Lane}, description, "computed"
  or "reused", seconds)."""
  triangles = eqAxes.zoneFromServer(serverMapFiles.mapCollision(mapBytes))
  swimBoxes, zoneLines, ignored = [], [], []
  for index, record in enumerate(serverMapFiles.readWater(waterBytes)):
    if any(float(value) != 0.0 for value in record["rotation"]):
      raise ToolError(f".wtr record {index} is turned {tuple(record['rotation'])} degrees; the lane takes unturned boxes")
    low, high = zoneBox(record["position"], numpy.asarray(record["halfExtents"]) * numpy.asarray(record["scale"]))
    name = f".wtr record {index} ({serverMapFiles.waterRegionTypeNames.get(record['type'], record['type'])})"
    if record["type"] in (1, 2):
      swimBoxes.append({"name": name, "low": low, "high": high, "liquid": "water" if record["type"] == 1 else "lava", "body": None})
    elif record["type"] == 3:
      zoneLines.append({"name": name, "low": low, "high": high, "landing": None, "target": None})
    else:
      ignored.append(name)
  described = []
  for seed in seeds:
    found = footingOnTriangles(triangles, seed["point"])
    if found is None:
      raise ToolError(f"Seed '{seed['name']}' at {list(seed['point'])} has no footing in the collision from {footingSearch['above']:g} over it to {footingSearch['below']:g} under it")
    described.append({
      "name": seed["name"], "kind": seed["kind"], "point": list(seed["point"]), "footing": found[0], "tolerance": seedTolerance(found[1]),
      "isolated": bool(seed.get("isolated", False)), "headingDegrees": seed.get("headingDegrees"),
    })
  described.sort(key=lambda seed: seed["kind"] != "safePoint")
  description = {
    "swimBoxes": swimBoxes, "zoneLines": zoneLines, "seeds": described, "plotEntrances": list(plots or []), "regions": list(regions or []),
    "builtFor": builtFor, "underworld": underworld, "undecidedBodies": [], "notFollowed": [], "ignoredRegions": ignored, "boundariesKnown": False,
  }
  boundary = numpy.zeros(len(triangles), dtype=bool)
  lanes, status, seconds = computeLanes(toolingRoot, reachRoot(toolingRoot) / serverFilesFolder, triangles, boundary, description, reportProgress)
  description["bounds"] = shippedBounds(triangles)
  return lanes, description, status, seconds


class Nodes:
  """A body group's lane's nodes as arrays: each one's plan cell (x along Recast x, zone y; z along Recast z, zone x), height (a floor
  node's floor, a wet node's the middle of its water), and label, with the lane's per-node state. reached is containment's (any body
  with the group's moves), reachedFull the group's own. Labels are the group's: a floor under water it wades is walked, and that water,
  which only containment swims in, is squeeze."""

  def __init__(self, lane):
    self.lane = lane
    self.columnStart = lane.read("columnStart")
    counts = numpy.diff(self.columnStart)
    side = numpy.arange(lane.tileCells, dtype=numpy.uint16)
    tiles = numpy.arange(lane.tilesX * lane.tilesZ)
    tileX = ((tiles % lane.tilesX) * lane.tileCells).astype(numpy.uint16)
    tileZ = ((tiles // lane.tilesX) * lane.tileCells).astype(numpy.uint16)
    self.x = numpy.repeat((tileX[:, None, None] + side[None, None, :] + numpy.zeros((1, lane.tileCells, 1), dtype=numpy.uint16)).ravel(), counts)
    self.z = numpy.repeat((tileZ[:, None, None] + side[None, :, None] + numpy.zeros((1, 1, lane.tileCells), dtype=numpy.uint16)).ravel(), counts)
    del counts
    self.low, self.high = lane.read("low"), lane.read("high")
    self.box, self.shape, self.state = lane.read("box"), lane.read("shape"), lane.read("state")
    self.wet = (self.shape & wetShape) != 0
    self.shallow = (self.shape & shallowShape) != 0
    self.walkable = (self.shape & walkableShape) != 0
    self.reached = (self.state & reachedState) != 0
    self.reachedFull = (self.state & reachedFullState) != 0
    self.trap = (self.state & trapState) != 0
    middle = numpy.where(self.wet, (self.low.astype(numpy.float32) + self.high.astype(numpy.float32)) / 2, self.low.astype(numpy.float32))
    self.height = (lane.origin[1] + middle * lane.cellHeight).astype(numpy.float32)
    del middle
    label = numpy.zeros(lane.nodeCount, dtype=numpy.uint8)
    label[self.reached & ~self.walkable] = slideLabel
    label[self.reached & self.walkable] = walkLabel
    label[self.reached & (self.wet | (self.box >= 0)) & ~self.shallow] = swimLabel
    label[self.reached & ~self.reachedFull] = squeezeLabel
    label[self.reached & ((self.shape & absorbedShape) != 0)] = zoneLineLabel
    self.label = label

  def plan(self, chosen):
    """Chosen nodes' column centers in zone x and y."""
    return self.lane.origin[2] + (self.z[chosen] + 0.5) * self.lane.cellSize, self.lane.origin[0] + (self.x[chosen] + 0.5) * self.lane.cellSize

  def floorNodeAt(self, point, tolerance=None):
    """The floor node under a zone point: at [x, y] the highest in its column; at [x, y, z] the one nearest z within tolerance, or, without
    one, the highest from footingSearch's above over z down to its below under it. None where there is none."""
    column = self.lane.columnAt(point)
    if column is None:
      return None
    nodes = numpy.arange(self.columnStart[column], self.columnStart[column + 1])
    nodes = nodes[~self.wet[nodes]]
    if not len(nodes):
      return None
    if len(point) == 2:
      return int(nodes[-1])
    heights = self.height[nodes]
    if tolerance is not None:
      distance = numpy.abs(heights - point[2])
      best = int(numpy.argmin(distance))
      return int(nodes[best]) if distance[best] <= tolerance else None
    inside = (heights <= point[2] + footingSearch["above"]) & (heights >= point[2] - footingSearch["below"])
    return int(nodes[inside][-1]) if inside.any() else None


def polygonMask(lane, outline):
  """Which columns' centers lie inside an outline ([x, y] zone points) or on it, as a (columnsZ, columnsX) array: along each row of
  columns, every edge crossing the row's center line turns the columns past it in or out; and a column whose center lies on an edge is
  in, so an outline drawn on whole coordinates, through the columns' centers, holds the columns along both its sides alike."""
  start = numpy.asarray(outline, dtype=numpy.float64)
  end = numpy.roll(start, -1, axis=0)
  rows = lane.origin[2] + (numpy.arange(lane.columnsZ) + 0.5) * lane.cellSize
  low, high = numpy.minimum(start[:, 0], end[:, 0]), numpy.maximum(start[:, 0], end[:, 0])
  edge, row = numpy.nonzero((low[:, None] <= rows[None, :]) & (rows[None, :] < high[:, None]))
  share = (rows[row] - start[edge, 0]) / (end[edge, 0] - start[edge, 0])
  crossing = start[edge, 1] + share * (end[edge, 1] - start[edge, 1])
  first = numpy.clip(numpy.ceil((crossing - lane.origin[0]) / lane.cellSize - 0.5), 0, lane.columnsX).astype(numpy.int64)
  turns = numpy.zeros((lane.columnsZ, lane.columnsX + 1), dtype=bool)
  numpy.bitwise_xor.at(turns, (row, first), True)
  mask = numpy.logical_xor.accumulate(turns, axis=1)[:, :lane.columnsX]
  for (x1, y1), (x2, y2) in zip(start, end):
    onRows = numpy.arange(math.ceil((min(x1, x2) - lane.origin[2]) / lane.cellSize - 0.5 - 1e-6), math.floor((max(x1, x2) - lane.origin[2]) / lane.cellSize - 0.5 + 1e-6) + 1)
    onRows = onRows[(onRows >= 0) & (onRows < lane.columnsZ)]
    if x1 == x2:
      across = numpy.arange(math.ceil((min(y1, y2) - lane.origin[0]) / lane.cellSize - 0.5 - 1e-6), math.floor((max(y1, y2) - lane.origin[0]) / lane.cellSize - 0.5 + 1e-6) + 1)
      across = across[(across >= 0) & (across < lane.columnsX)]
      mask[numpy.ix_(onRows, across)] = True
      continue
    column = (y1 + (rows[onRows] - x1) * (y2 - y1) / (x2 - x1) - lane.origin[0]) / lane.cellSize - 0.5
    nearest = numpy.rint(column).astype(numpy.int64)
    centered = (numpy.abs(column - nearest) < 1e-6) & (nearest >= 0) & (nearest < lane.columnsX)
    mask[onRows[centered], nearest[centered]] = True
  return mask


def outlineArea(outline):
  points = numpy.asarray(outline, dtype=numpy.float64)
  return abs(float(numpy.dot(points[:, 0], numpy.roll(points[:, 1], -1)) - numpy.dot(points[:, 1], numpy.roll(points[:, 0], -1)))) / 2


def precedence(regions):
  """Region indices in the order a point is judged against them: the smallest in plan first, so a region drawn inside a broader one
  (a none room in a play area) holds what lies in it."""
  return sorted(range(len(regions)), key=lambda index: (outlineArea(regions[index]["outline"]), index))


def nodeRegions(nodes, regions):
  """Each node's region (its index in regions, -1 for none): the first, in precedence, whose outline holds its column's center and
  whose height band holds its height. Each column keeps the first region over it; nodes whose column more than one region covers are
  judged region by region."""
  found = numpy.full(nodes.lane.nodeCount, -1, dtype=numpy.int16)
  if not regions:
    return found
  first = numpy.full((nodes.lane.columnsZ, nodes.lane.columnsX), -1, dtype=numpy.int16)
  shared = numpy.zeros(first.shape, dtype=bool)
  for index in precedence(regions):
    mask = polygonMask(nodes.lane, regions[index]["outline"])
    shared |= mask & (first >= 0)
    first[mask & (first < 0)] = index
  candidate = first[nodes.z, nodes.x]
  del first
  bottoms = numpy.array([region["bottom"] for region in regions] + [numpy.inf], dtype=numpy.float32)
  tops = numpy.array([region["top"] for region in regions] + [-numpy.inf], dtype=numpy.float32)
  inside = (nodes.height >= bottoms[candidate]) & (nodes.height <= tops[candidate])
  found[inside] = candidate[inside]
  del candidate, inside
  crowded = numpy.flatnonzero(shared[nodes.z, nodes.x])
  del shared
  if len(crowded):
    found[crowded] = -1
    for index in precedence(regions):
      region = regions[index]
      pending = crowded[found[crowded] < 0]
      holds = polygonMask(nodes.lane, region["outline"])[nodes.z[pending], nodes.x[pending]]
      holds &= (nodes.height[pending] >= region["bottom"]) & (nodes.height[pending] <= region["top"])
      found[pending[holds]] = index
  return found


def groups(lane, x, z):
  """Group columns (x, z indices) into problems: blocks of problemBlock units holding any, joined where blocks touch by a side or a
  corner. Returns each column's group number (0 up)."""
  block = max(1, round(problemBlock / lane.cellSize))
  blockX, blockZ = x // block + 1, z // block + 1
  width = int(blockX.max()) + 2
  keys, member = numpy.unique(blockZ * width + blockX, return_inverse=True)
  firsts, seconds = [], []
  for step in (1, width - 1, width, width + 1):
    found = numpy.searchsorted(keys, keys + step)
    hit = (found < len(keys)) & (keys[numpy.minimum(found, len(keys) - 1)] == keys + step)
    firsts.append(numpy.flatnonzero(hit))
    seconds.append(found[hit])
  first, second = numpy.concatenate(firsts), numpy.concatenate(seconds)
  labels = numpy.arange(len(keys))
  while True:
    lower = numpy.minimum(labels[first], labels[second])
    before = labels.copy()
    numpy.minimum.at(labels, labels[first], lower)
    numpy.minimum.at(labels, labels[second], lower)
    while True:
      jumped = labels[labels]
      if numpy.array_equal(jumped, labels):
        break
      labels = jumped
    if numpy.array_equal(labels, before):
      break
  _, numbered = numpy.unique(labels[member.ravel()], return_inverse=True)
  return numbered.ravel()


def describeGroup(kind, lane, planX, planY, height, regionIndex, regions, length=False):
  """A problem: its kind, center (the member nearest the members' middle, so it lies on the problem) and extent (zone axes), its area
  (nodes) or length (edge runs), and the region most of it lies in."""
  nearest = int(numpy.argmin(numpy.hypot(planX - planX.mean(), planY - planY.mean())))
  center = [round(float(planX[nearest]), 1), round(float(planY[nearest]), 1), round(float(height[nearest]), 1)]
  extent = {
    "minimum": [round(float(planX.min()) - lane.cellSize / 2, 1), round(float(planY.min()) - lane.cellSize / 2, 1), round(float(height.min()), 1)],
    "maximum": [round(float(planX.max()) + lane.cellSize / 2, 1), round(float(planY.max()) + lane.cellSize / 2, 1), round(float(height.max()), 1)],
  }
  inside = regionIndex[regionIndex >= 0]
  region = regions[int(numpy.bincount(inside).argmax())]["name"] if len(inside) else None
  problem = {"kind": kind, "center": center, "extent": extent, "region": region}
  measure = len(planX) * lane.cellSize
  return problem | ({"length": round(measure, 1)} if length else {"area": round(measure * lane.cellSize, 1)})


def groupedProblems(kind, lane, selected, nodes, regionOfNode, regions, tags=None):
  """One problem per group of the selected nodes' columns; with tags ({body group: node mask}), each names the groups whose mask holds
  any of its nodes."""
  chosen = numpy.flatnonzero(selected)
  if not len(chosen):
    return []
  numbered = groups(lane, nodes.x[chosen].astype(numpy.int64), nodes.z[chosen].astype(numpy.int64))
  order = numpy.argsort(numbered, kind="stable")
  bounds = numpy.flatnonzero(numpy.diff(numbered[order])) + 1
  problems = []
  for part in (chosen[indices] for indices in numpy.split(order, bounds)):
    planX, planY = nodes.plan(part)
    problem = describeGroup(kind, lane, planX, planY, nodes.height[part], regionOfNode[part], regions)
    if tags is not None:
      problem["groups"] = [name for name, mask in tags.items() if mask[part].any()]
    problems.append(problem)
  return problems


def eventProblems(kind, eventKind, lanes, regionOfNode, regions):
  """One problem per run of the body groups' lanes' events of a kind, grouped by the columns they fall into: a void edge, or a fall under
  the underworld, each in the region the falls start from, naming the groups whose lanes hold it."""
  kept, owners = {}, {}
  for name, lane in lanes.items():
    for event in lane.events[lane.events[:, 0] == eventKind]:
      key = (int(event[1]), int(event[2]), int(event[3]))
      kept.setdefault(key, event)
      owners.setdefault(key, set()).add(name)
  if not kept:
    return []
  keys = list(kept)
  events = numpy.array([kept[key] for key in keys])
  lane = next(iter(lanes.values()))
  x, z = lane.columnPlaces(events[:, 2].astype(numpy.int64))
  planX = (lane.origin[2] + (z + 0.5) * lane.cellSize).astype(numpy.float32)
  planY = (lane.origin[0] + (x + 0.5) * lane.cellSize).astype(numpy.float32)
  height = lane.height(events[:, 3]).astype(numpy.float32)
  sources = regionOfNode[events[:, 1]]
  numbered = groups(lane, x, z)
  problems = []
  for group in numpy.unique(numbered):
    part = numpy.flatnonzero(numbered == group)
    found = set().union(*(owners[keys[index]] for index in part))
    problems.append(describeGroup(kind, lane, planX[part], planY[part], height[part], sources[part], regions, length=True) | {
      "groups": [name for name in playerScale.groupNames if name in found],
    })
  return problems


def bodyFootprint(lane, triangles):
  """Which columns' centers a body's triangles (zone axes) cover in plan, and the highest point of its surface."""
  image = Image.new("1", (lane.columnsX, lane.columnsZ), 0)
  draw = ImageDraw.Draw(image)
  for triangle in numpy.asarray(triangles, dtype=numpy.float64):
    draw.polygon([((point[1] - lane.origin[0]) / lane.cellSize - 0.5, (point[0] - lane.origin[2]) / lane.cellSize - 0.5) for point in triangle], fill=1)
  return numpy.asarray(image, dtype=bool), float(numpy.asarray(triangles)[..., 2].max())


def playFloorOf(nodes, underworld):
  """Which nodes are floor a player could be meant to stand on: walkable, dry, not taken by a zone line, not a boundary's top, not under
  the underworld, and joined to the lane by open space (reached, or open: the inside of a closed solid and a sealed room are not)."""
  floor = nodes.walkable & ~nodes.wet & ((nodes.shape & (absorbedShape | boundaryShape)) == 0) & ((nodes.state & openState) != 0)
  if underworld is not None:
    lowest = math.ceil((underworld - nodes.lane.origin[1]) / nodes.lane.cellHeight)
    if lowest > 0:
      floor &= nodes.low >= numpy.uint16(min(lowest, numpy.iinfo(numpy.uint16).max))
  return floor


def laneMarks(nodes, description):
  """What a lane plan marks besides its fill: each entry and whether it is connected, plot entrances, swim boxes, and zone lines reached
  or not; with the node arrays, as analyse gives them without judging regions or problems."""
  lane = nodes.lane
  hasSafe = any(seed["kind"] == "safePoint" for seed in description["seeds"])
  entries = []
  for seed, node in zip(description["seeds"], lane.seedNodes):
    state = int(nodes.state[node])
    fits = bool(nodes.reachedFull[node])
    connected = fits if seed["kind"] == "safePoint" else (fits and bool(state & fromSafeState and state & toSafeState) if hasSafe else None)
    entries.append({"name": seed["name"], "kind": seed["kind"], "at": seed["footing"], "isolated": seed["isolated"], "connected": connected})
  plots = []
  for plot in description["plotEntrances"]:
    node = nodes.floorNodeAt(plot["point"])
    plots.append({"name": plot["name"], "at": plot["point"], "reached": node is not None and bool(nodes.reachedFull[node])} | ({} if node is not None else {"why": "no floor under it"}))
  swimReached = numpy.zeros(len(description["swimBoxes"]), dtype=bool)
  inBox = nodes.reachedFull & (nodes.wet | (nodes.box >= 0))
  swimReached[numpy.unique(nodes.box[inBox])] = True
  swimBoxes = [{"name": box["name"], "reached": bool(swimReached[index])} for index, box in enumerate(description["swimBoxes"])]
  taken = numpy.flatnonzero(nodes.reachedFull & ((nodes.shape & absorbedShape) != 0))
  takenX, takenY = nodes.plan(taken)
  takenZ = nodes.height[taken] + lane.cellHeight
  caught = lane.events[lane.events[:, 0] == absorbedEvent] if len(lane.events) else lane.events
  caughtLines = set(caught[nodes.reachedFull[caught[:, 1]], 4].tolist()) if len(caught) else set()
  zoneLines = []
  for index, line in enumerate(description["zoneLines"]):
    low, high = numpy.asarray(line["low"]) - lane.cellSize, numpy.asarray(line["high"]) + lane.cellSize
    held = (takenX >= low[0]) & (takenX <= high[0]) & (takenY >= low[1]) & (takenY <= high[1]) & (takenZ >= low[2]) & (takenZ <= high[2])
    full = bool(held.any()) or index in caughtLines
    reported = {"name": line["name"], "reached": bool(lane.lineReached[index]) and full}
    if line["landing"] is not None:
      reported["teleport"] = {"landing": line["landing"], "followed": bool(lane.lineLanding[index] >= 0)} | ({} if lane.lineLanding[index] >= 0 else {"why": "no floor under its target"})
    zoneLines.append(reported)
  return {"nodes": nodes, "entries": entries, "zoneLines": zoneLines, "plotEntrances": plots, "swimBoxes": swimBoxes}


def regionBuiltFor(description, regionName):
  """The groups a region (by name, None outside every region) is built for: its own, else the zone's, else every group."""
  own = next((region.get("builtFor") for region in description["regions"] if region["name"] == regionName), None)
  return playerScale.builtForAt(description.get("builtFor"), own)


def judgedProblem(problem, passing, description):
  """A problem judged by the body groups that pass it against the groups its region is built for (playerScale.judged)."""
  return problem | playerScale.judged(passing, regionBuiltFor(description, problem["region"]))


# Problems refused by a game export whatever the groups (a leak any body reaches), and those refused only where a group the place is
# built for fails them; every other kind is a finding, judged the same way.
leakKinds = ("voidEdge", "belowUnderworld", "noneRegion", "outsideRegions")
builtForFailureKinds = ("disconnectedEntry", "unreachedPlot")


def analyse(lanes, description, shownGroup):
  """Everything mapReach reports of the body groups' lanes ({group: Lane}): per group, entries connected or not, reached area by label,
  per-region reach, and zone lines and plot entrances reached or not, and the swim boxes by body (swimBoxSummary); and the numbered
  problems, each naming the groups it concerns and, where groups can miss it, judged against the groups its region (else the zone) is
  built for; with shownGroup's node arrays, which the plan draws from."""
  regions = description["regions"]
  underworld = description["underworld"]
  shown, regionOfNode, nodeAccess = None, None, None
  reachedBy, fullBy, trapBy, missedPlayBy, marksBy, areaBy, perRegionBy = {}, {}, {}, {}, {}, {}, {}
  for name in playerScale.groupNames:
    lane = lanes[name]
    nodes = Nodes(lane)
    if regionOfNode is None:
      regionOfNode = nodeRegions(nodes, regions)
      access = numpy.array([{"play": 0, "view": 1, "none": 2, None: 3}[region["access"]] for region in regions] + [4], dtype=numpy.int8)
      nodeAccess = access[regionOfNode]
    marksBy[name] = laneMarks(nodes, description)
    area = lane.cellSize * lane.cellSize
    areaBy[name] = {label: round(float((nodes.label == index).sum()) * area, 1) for index, label in enumerate(labelNames) if index != unreachedLabel}
    areaBy[name]["trap"] = round(float(nodes.trap.sum()) * area, 1)
    playFloor = playFloorOf(nodes, underworld)
    perRegionBy[name] = []
    for index, region in enumerate(regions):
      inside = playFloor & (regionOfNode == index)
      total, reached = int(inside.sum()), int((inside & nodes.reachedFull).sum())
      perRegionBy[name].append((None if total == 0 else round(reached / total, 4), round((total - reached) * area, 1) if region["access"] == "play" else None))
    reachedBy[name], fullBy[name], trapBy[name] = nodes.reached.copy(), nodes.reachedFull.copy(), nodes.trap.copy()
    missedPlayBy[name] = playFloor & ~nodes.reachedFull & (nodeAccess == 0)
    del playFloor
    if name == shownGroup:
      shown = nodes
    del nodes
  if shown is None:
    raise ValueError(f"shownGroup must be one of {list(playerScale.groupNames)}, got {shownGroup!r}")
  lane = lanes[shownGroup]
  reachedAny = numpy.logical_or.reduce([reachedBy[name] for name in playerScale.groupNames])
  trapAny = numpy.logical_or.reduce([trapBy[name] for name in playerScale.groupNames])
  missedAny = numpy.logical_or.reduce([missedPlayBy[name] for name in playerScale.groupNames])

  def byGroup(listName, field, index):
    return {name: marksBy[name][listName][index][field] for name in playerScale.groupNames}

  entries = []
  for index, entry in enumerate(marksBy[shownGroup]["entries"]):
    entries.append({key: entry[key] for key in ("name", "kind", "at", "isolated")} | {"connected": byGroup("entries", "connected", index)})
  plots = []
  for index, plot in enumerate(marksBy[shownGroup]["plotEntrances"]):
    reached = byGroup("plotEntrances", "reached", index)
    plots.append({key: plot[key] for key in plot if key != "reached"} | {"reachedBy": [name for name in playerScale.groupNames if reached[name]]})
  boxesReachedBy = []
  for index in range(len(marksBy[shownGroup]["swimBoxes"])):
    reached = byGroup("swimBoxes", "reached", index)
    boxesReachedBy.append([name for name in playerScale.groupNames if reached[name]])
  zoneLines = []
  for index, line in enumerate(marksBy[shownGroup]["zoneLines"]):
    reached = byGroup("zoneLines", "reached", index)
    zoneLines.append({key: line[key] for key in line if key != "reached"} | {"reachedBy": [name for name in playerScale.groupNames if reached[name]]})
  perRegion = []
  for index, region in enumerate(regions):
    perRegion.append({
      "name": region["name"], "access": region["access"], "builtFor": list(regionBuiltFor(description, region["name"])),
      "reachedShare": {name: perRegionBy[name][index][0] for name in playerScale.groupNames},
      "unreachedPlayFloor": {name: perRegionBy[name][index][1] for name in playerScale.groupNames},
    })

  problems = {kind: [] for kind in problemKinds}
  problems["voidEdge"] = eventProblems("voidEdge", voidEvent, lanes, regionOfNode, regions)
  problems["belowUnderworld"] = eventProblems("belowUnderworld", belowUnderworldEvent, lanes, regionOfNode, regions)
  problems["noneRegion"] = groupedProblems("noneRegion", lane, reachedAny & (nodeAccess == 2), shown, regionOfNode, regions, reachedBy)
  if regions:
    problems["outsideRegions"] = groupedProblems("outsideRegions", lane, reachedAny & (regionOfNode < 0), shown, regionOfNode, regions, reachedBy)
  for entry in entries:
    connected = entry["connected"]
    if not entry["isolated"] and any(value is False for value in connected.values()):
      found = {"kind": "disconnectedEntry", "entry": entry["name"], "center": entry["at"], "region": regionAt(regions, entry["at"])}
      problems["disconnectedEntry"].append(judgedProblem(found, [name for name, value in connected.items() if value], description))
  for plot in plots:
    if len(plot["reachedBy"]) < len(playerScale.groupNames):
      found = {"kind": "unreachedPlot", "plot": plot["name"], "center": plot["at"], "region": regionAt(regions, plot["at"])}
      problems["unreachedPlot"].append(judgedProblem(found, plot["reachedBy"], description))
  problems["trap"] = groupedProblems("trap", lane, trapAny, shown, regionOfNode, regions, trapBy)
  problems["viewRegion"] = groupedProblems("viewRegion", lane, reachedAny & (nodeAccess == 1), shown, regionOfNode, regions, reachedBy)
  for line in zoneLines:
    if len(line["reachedBy"]) < len(playerScale.groupNames):
      box = description["zoneLines"][[candidate["name"] for candidate in description["zoneLines"]].index(line["name"])]
      center = [round((a + b) / 2, 1) for a, b in zip(box["low"], box["high"])]
      found = {"kind": "unreachedZoneLine", "zoneLine": line["name"], "center": center, "region": regionAt(regions, center)}
      problems["unreachedZoneLine"].append(judgedProblem(found, line["reachedBy"], description))
  for box, groups in zip(description["swimBoxes"], boxesReachedBy):
    if len(groups) < len(playerScale.groupNames):
      center = [round((a + b) / 2, 1) for a, b in zip(box["low"], box["high"])]
      found = {"kind": "unreachedSwim", "swimBox": box["name"], "center": center, "region": regionAt(regions, center)}
      problems["unreachedSwim"].append(judgedProblem(found, groups, description))
  for found in groupedProblems("unreachedPlayFloor", lane, missedAny, shown, regionOfNode, regions, missedPlayBy):
    problems["unreachedPlayFloor"].append(judgedProblem(found, [name for name in playerScale.groupNames if name not in found["groups"]], description))
  for body in description["undecidedBodies"]:
    footprint, surface = bodyFootprint(lane, body["triangles"])
    walked = reachedAny & ~shown.wet & footprint[shown.z, shown.x] & (shown.height < surface)
    for problem in groupedProblems("undecidedWater", lane, walked, shown, regionOfNode, regions, reachedBy):
      problems["undecidedWater"].append(problem | {"body": body["name"]})
  # Floor smaller than the gap the grid resolves is a sliver of rasterization (a thin wall's top, a face's rim), not a place.
  smallest = (2 * lane.cellSize) ** 2
  slivers = [problem for problem in problems["unreachedPlayFloor"] if problem["area"] < smallest]
  problems["unreachedPlayFloor"] = [problem for problem in problems["unreachedPlayFloor"] if problem["area"] >= smallest]
  listed, more = [], {}
  if slivers:
    more["unreachedPlayFloorSlivers"] = {"count": len(slivers), "area": round(sum(problem["area"] for problem in slivers), 1), "under": smallest}
  for kind in problemKinds:
    found = sorted(problems[kind], key=lambda problem: -problem.get("area", problem.get("length", 0)))
    listed += found[:problemsListed]
    if len(found) > problemsListed:
      rest = found[problemsListed:]
      more[kind] = {"count": len(rest), "area": round(sum(problem.get("area", 0) for problem in rest), 1), "length": round(sum(problem.get("length", 0) for problem in rest), 1)}
  for number, problem in enumerate(listed, 1):
    problem["number"] = number
  return {
    "group": shownGroup, "nodes": shown, "regionOfNode": regionOfNode, "fullBy": fullBy,
    "entries": entries, "reachedArea": areaBy, "regions": perRegion, "zoneLines": zoneLines, "plotEntrances": plots,
    "swimBoxes": swimBoxSummary(description["swimBoxes"], boxesReachedBy), "problems": listed, "moreProblems": more,
  }


def swimBoxSummary(boxes, boxesReachedBy):
  """The swim boxes by the body each belongs to (None for a box standing alone or read from a .wtr), each body with how many boxes it
  holds, how many of them each group reaches, and the boxes some group does not reach with the groups that do, the first
  problemsListed listed and the rest counted: a river of a thousand boxes would fill the answer."""
  byBody = {}
  for box, reachedBy in zip(boxes, boxesReachedBy):
    byBody.setdefault(box["body"], []).append((box["name"], reachedBy))
  summary = []
  for body in sorted(byBody, key=lambda name: (name is None, name or "")):
    held = byBody[body]
    unreached = [{"name": name, "reachedBy": reachedBy} for name, reachedBy in held if len(reachedBy) < len(playerScale.groupNames)]
    summary.append({
      "body": body, "boxes": len(held), "boxesReachedBy": {group: sum(group in reachedBy for _, reachedBy in held) for group in playerScale.groupNames},
      "unreached": unreached[:problemsListed], "moreUnreached": max(0, len(unreached) - problemsListed),
    })
  return summary


def groupsReaching(lanes, node):
  """The body groups whose own lane reaches a node (the groups' lanes share their nodes)."""
  return [name for name in playerScale.groupNames if lanes[name].read("state")[node] & reachedFullState]


def isFailure(problem):
  """Whether a game export refuses a lane problem: a leak always, a disconnected entry or an unreached plot where a group its place is
  built for misses it."""
  return problem["kind"] in leakKinds or (problem["kind"] in builtForFailureKinds and problem["verdict"] == "fails")


def regionAt(regions, point):
  for index in precedence(regions):
    region = regions[index]
    if region["bottom"] <= point[2] <= region["top"] and insideOutline(point, region["outline"]):
      return region["name"]
  return None


def insideOutline(point, outline):
  inside = False
  for (x1, y1), (x2, y2) in zip(outline, outline[1:] + outline[:1]):
    if (y1 > point[1]) != (y2 > point[1]) and point[0] < x1 + (point[1] - y1) * (x2 - x1) / (y2 - y1):
      inside = not inside
  return inside


def viewedProblems(problems, count):
  """Which problems the problem sheet views, up to count: the first of each kind in turn, then the second of each, so every kind found
  is seen; in their numbered order."""
  byKind = {}
  for problem in problems:
    byKind.setdefault(problem["kind"], []).append(problem)
  chosen, rank = [], 0
  while len(chosen) < count and any(rank < len(found) for found in byKind.values()):
    chosen += [found[rank] for found in byKind.values() if rank < len(found)][:count - len(chosen)]
    rank += 1
  return sorted(chosen, key=lambda problem: problem["number"])


def walkPlaces(nodes):
  """The plan places (zone x, y) of every reached walkable floor node, which problem views turn from."""
  standing = numpy.flatnonzero(nodes.label == walkLabel)
  return numpy.stack(nodes.plan(standing), axis=1)


def problemRadius(problem, description):
  """How far a problem reaches from its center: half its extent's diagonal in plan or half its height, its box's for a zone line or swim
  box, and never under the tallest group's playerHeight."""
  least = playerScale.tallest().playerHeight
  if "extent" in problem:
    low, high = numpy.asarray(problem["extent"]["minimum"]), numpy.asarray(problem["extent"]["maximum"])
  elif "zoneLine" in problem or "swimBox" in problem:
    boxes = description["zoneLines"] if "zoneLine" in problem else description["swimBoxes"]
    box = next(box for box in boxes if box["name"] == problem.get("zoneLine", problem.get("swimBox")))
    low, high = numpy.asarray(box["low"]), numpy.asarray(box["high"])
  else:
    return least
  return max(float(numpy.hypot(*(high[:2] - low[:2]))) / 2, float(high[2] - low[2]) / 2, least)


def problemSights(standing, problem, description, farthest):
  """The eyes a problem may be viewed from, in the order they are tried: problemViewFrame times its radius back from it (at least
  problemViewDistance, at most farthest, where the view still draws it), raised by each of problemViewElevations in turn and turned to
  each of problemViewBearings from the reached floor nearest it beyond its radius; each {eye, target}, the target floorSightLift over
  its center. Returns (candidates, radius, distance)."""
  radius = problemRadius(problem, description)
  distance = min(max(problemViewFrame * radius, problemViewDistance), farthest)
  center = numpy.asarray(problem["center"], dtype=numpy.float64)
  away = numpy.hypot(standing[:, 0] - center[0], standing[:, 1] - center[1]) if len(standing) else numpy.zeros(0)
  beyond = away >= radius
  start = 0.0
  if beyond.any():
    nearest = standing[int(numpy.argmin(numpy.where(beyond, away, numpy.inf)))]
    start = math.atan2(nearest[1] - center[1], nearest[0] - center[0])
  target = center + [0.0, 0.0, buildTolerances.floorSightLift]
  candidates = []
  for elevation in problemViewElevations:
    rise, across = math.sin(math.radians(elevation)) * distance, math.cos(math.radians(elevation)) * distance
    for turn in problemViewBearings:
      bearing = start + math.radians(turn)
      eye = target + [math.cos(bearing) * across, math.sin(bearing) * across, rise]
      candidates.append({"eye": [round(float(value), 2) for value in eye], "target": [round(float(value), 2) for value in target]})
  return candidates, radius, distance


def laneTerms(lane, description):
  """What every reach result states: each constant the lane used (value and source), what it does not model, and its limits."""
  stated = limits(lane, description["underworld"], description["boundariesKnown"])
  if not description["regions"]:
    stated.append("the zone has no regions: reach outside every region (outsideRegions) is not judged")
  if description.get("ignoredRegions"):
    stated.append(f".wtr records the lane does not model (only water, lava, and zone lines move players): {description['ignoredRegions']}")
  return {"constants": constantsReport(), "notModeled": notModeled, "limits": stated}


def report(lanes, description, analysis, status, seconds):
  """mapReach's JSON: what the lanes found, per body group, the problems first; then what the lanes used and cannot see."""
  shown = lanes[analysis["group"]]
  return {"problems": analysis["problems"], "moreProblems": analysis["moreProblems"]} | laneTerms(shown, description) | {
    "reachGrid": reachGrid, "lane": status, "seconds": seconds, "laneFiles": {name: str(lane.path) for name, lane in lanes.items()}, "nodes": shown.nodeCount,
    "builtFor": list(playerScale.builtForAt(description.get("builtFor"), None)), "planGroup": analysis["group"],
    "entries": analysis["entries"], "notFollowed": description["notFollowed"], "reachedArea": analysis["reachedArea"], "regions": analysis["regions"],
    "zoneLines": analysis["zoneLines"], "plotEntrances": analysis["plotEntrances"], "swimBoxes": analysis["swimBoxes"],
  }


routeMergedMoves = ("walk", "step", "swim", "slide")
decisiveMoves = ("step", "drop", "slide", "enterWater", "climbOut", "teleport")
routeFrameCount = 5
fallingMoves = ("drop", "slide")
# A decisive move's frame stands this far before it along the route in plan, so the move lies ahead in view rather than underfoot.
frameStandBack = 12.0
# Decisive moves of one kind with less than this much of the route in plan between one and the next are one, as a stair of terraces or a
# run of ledges is: frames standing frameStandBack before each would show the same ground.
decisiveGap = 2 * frameStandBack
# A route of more legs than this lists them in a file beside its plan, and its result counts them: a long route's legs alone would fill
# the answer.
routeLegsListed = 40
tintColumns = numpy.dtype([("x", "<u2"), ("z", "<u2"), ("low", "<u2"), ("label", "u1")])
# Nodes are read this many at a time, so a zone's whole lane never sits in memory at once.
chunkNodes = 1 << 22


def entryLanes(toolingRoot, workFile, inputPath, entryName, reportProgress):
  """The body groups' lanes seeded at one arrival alone (a stored entry by name, or "safe point"), from bridgeLane.laneInput's file:
  ({group: Lane}, description, "computed" or "reused", seconds)."""
  triangles, boundary, description = sceneDescription(inputPath)
  chosen = [seed for seed in description["seeds"] if seed["name"] == entryName]
  if not chosen:
    raise ToolError(f"No arrival named '{entryName}': the lane starts from {[seed['name'] for seed in description['seeds']]}")
  description["seeds"] = chosen
  lanes, status, seconds = computeLanes(toolingRoot, laneFolder(toolingRoot, workFile), triangles, boundary, description, reportProgress)
  description["bounds"] = shippedBounds(triangles)
  return lanes, description, status, seconds


def stepRise(lane):
  """The rise across one column over which a walk is a step up a riser: more than the steepest face the lane's group walks rises across
  a column, by the cell height its floors are known to."""
  return lane.cellSize * math.tan(math.radians(lane.body.steepestWalkableDegrees)) + lane.cellHeight


def nodePoint(nodes, node):
  """A node's place (zone axes): its column's center at its floor, or, for water, at its top, where a swimmer floats."""
  lane = nodes.lane
  return [*lane.center(int(nodes.x[node]), int(nodes.z[node])), float(lane.height(nodes.high[node] if nodes.wet[node] else nodes.low[node]))]


def roundedPoint(point, digits=2):
  return [round(float(value), digits) for value in point]


def columnNodes(nodes, column):
  return range(int(nodes.columnStart[column]), int(nodes.columnStart[column + 1]))


def targetNode(nodes, to):
  """The node at a traced point: at [x, y] its column's highest floor; at [x, y, z] the water holding z, else the floor found under z as an
  entry's footing is. None where there is neither."""
  column = nodes.lane.columnAt(to)
  if column is None:
    return None
  if len(to) == 3:
    for node in columnNodes(nodes, column):
      if nodes.wet[node] and nodes.lane.height(nodes.low[node]) <= to[2] <= nodes.lane.height(nodes.high[node]):
        return node
  return nodes.floorNodeAt(to)


def routeChain(nodes, target):
  """The nodes from the seed whose flood reached a node first to the node, by the predecessors the lane keeps, and the move into each."""
  lane = nodes.lane
  predecessor, move = lane.read("predecessor"), lane.read("move")
  seed = moveNames.index("seed")
  chain = [int(target)]
  while move[chain[-1]] != seed:
    before = int(predecessor[chain[-1]])
    if before < 0 or len(chain) > lane.nodeCount:
      raise ValueError(f"{lane.path}: node {chain[-1]} is reached by {moveNames[move[chain[-1]]]} from no node before it")
    chain.append(before)
  chain.reverse()
  return chain, [moveNames[move[node]] for node in chain]


def routeLegs(nodes, chain, moves):
  """A route's legs: runs of one move into nodes of one label (walk, step, swim, and slide runs merged; every other move a leg of its
  own), each with its start and end (zone axes), plan length (a teleport carries, so has none), number of moves, and the indices of its
  first and last node in the chain. A walk rising more than stepRise across a column is a step, and steps with less than playerHeight
  of walking between them are one climb run, a step leg counting its steps (a stair, or hopping along a ledge's edge)."""
  points = [nodePoint(nodes, node) for node in chain]
  rise = stepRise(nodes.lane)
  legs = []
  for index in range(1, len(chain)):
    start, end = points[index - 1], points[index]
    kind = "step" if moves[index] == "walk" and end[2] - start[2] > rise else moves[index]
    label = labelNames[nodes.label[chain[index]]]
    length = 0.0 if kind == "teleport" else math.hypot(end[0] - start[0], end[1] - start[1])
    if legs and kind in routeMergedMoves and (legs[-1]["move"], legs[-1]["label"]) == (kind, label):
      legs[-1].update(end=end, length=legs[-1]["length"] + length, moves=legs[-1]["moves"] + 1, last=index)
    else:
      legs.append({"move": kind, "label": label, "start": start, "end": end, "length": length, "moves": 1, "first": index - 1, "last": index})
  climbed = []
  for leg in legs:
    if leg["move"] == "step":
      leg["steps"] = leg["moves"]
    between = climbed[-1] if climbed else None
    run = climbed[-2] if len(climbed) > 1 else None
    if leg["move"] == "step" and run is not None and run["move"] == "step" and between["move"] == "walk" and between["length"] < nodes.lane.body.playerHeight and run["label"] == between["label"] == leg["label"]:
      run.update(
        end=leg["end"], length=run["length"] + between["length"] + leg["length"], moves=run["moves"] + between["moves"] + leg["moves"],
        steps=run["steps"] + leg["steps"], last=leg["last"],
      )
      climbed.pop()
    else:
      climbed.append(leg)
  return points, climbed


def legReport(leg):
  return {
    "move": leg["move"], "label": leg["label"], "start": roundedPoint(leg["start"], 1), "end": roundedPoint(leg["end"], 1),
    "rise": round(leg["end"][2] - leg["start"][2], 2), "length": round(leg["length"], 1), "moves": leg["moves"],
  } | ({"steps": leg["steps"]} if leg["move"] == "step" else {})


def decisiveLegs(legs):
  """The moves a route turns on, in order, each {number from 1, move, first, last (leg indices), count}: every drop, slide, entering or
  leaving water (entering it, a drop into it, climbing out), teleport, and the first step of each climb run; those of one move with less
  than decisiveGap of the route in plan between one and the next are one, counting them."""
  decisive, between = [], 0.0
  for index, leg in enumerate(legs):
    if leg["move"] not in decisiveMoves:
      between += leg["length"]
      continue
    if decisive and decisive[-1]["move"] == leg["move"] and between < decisiveGap:
      decisive[-1].update(last=index, count=decisive[-1]["count"] + 1)
    else:
      decisive.append({"number": len(decisive) + 1, "move": leg["move"], "first": index, "last": index, "count": 1})
    between = 0.0
  return decisive


def decisiveCaption(legs, move):
  """A decisive move in words: its number, its move (a climb by its steps), and the rise of its legs of that move together (a fall
  negative), with how many it counts when more than one."""
  members = [leg for leg in legs[move["first"]:move["last"] + 1] if leg["move"] == move["move"]]
  rise = round(sum(leg["end"][2] - leg["start"][2] for leg in members), 2)
  if move["move"] == "step":
    steps = sum(leg["steps"] for leg in members)
    kind = "step" if steps == 1 else f"climb of {steps} steps"
    more = f" in {move['count']} runs" if move["count"] > 1 else ""
  else:
    kind, more = move["move"], f", {move['count']} {move['move']}s" if move["count"] > 1 else ""
  return f"{move['number']} {kind} {rise:+g}{more}"


def framedLegs(decisive, count):
  """Which decisive moves get a frame: all of up to count, else count spread evenly from the first to the last."""
  if len(decisive) <= count:
    return list(decisive)
  return [decisive[round(index * (len(decisive) - 1) / (count - 1))] for index in range(count)]


def routeFrame(nodes, chain, points, moves, leg, figureTall):
  """An eye-level view of a decisive leg: standing on the route frameStandBack before it in plan (from its start when it is nearer),
  heading and pitched toward where the leg ends, the scale figure standing there; a drop's or a slide's toward where the route stands
  frameStandBack past its end, since the floor at a fall's foot lies hidden under its brink from above; a teleport's from its landing
  toward where the route stands frameStandBack on. The figure stands only out of the water and her own height (figureTall) or more
  ahead, where she does not fill the frame. In water, the eye floats swimEyeAboveSurface over the water's top, without the figure."""
  along = [0.0]
  for at in range(1, len(points)):
    along.append(along[-1] + (0.0 if moves[at] == "teleport" else math.hypot(points[at][0] - points[at - 1][0], points[at][1] - points[at - 1][1])))
  if leg["move"] == "teleport":
    index = leg["last"]
    look = next((at for at in range(index + 1, len(points)) if along[at] >= along[index] + frameStandBack), len(points) - 1)
  else:
    index = next(at for at in range(leg["first"], -1, -1) if at == 0 or along[leg["first"]] - along[at] >= frameStandBack)
    look = leg["last"]
    if leg["move"] in fallingMoves:
      look = next((at for at in range(look, len(points)) if along[at] >= along[leg["last"]] + frameStandBack), len(points) - 1)
  stand, seen = points[index], points[look]
  across = math.hypot(seen[0] - stand[0], seen[1] - stand[1])
  if across < 1e-6:
    earlier = next((at for at in range(index - 1, -1, -1) if math.hypot(points[at][0] - stand[0], points[at][1] - stand[1]) > 1e-6), None)
    heading = 0.0 if earlier is None else math.degrees(math.atan2(stand[0] - points[earlier][0], stand[1] - points[earlier][1])) % 360
  else:
    heading = math.degrees(math.atan2(seen[0] - stand[0], seen[1] - stand[1])) % 360
  body = nodes.lane.body
  if nodes.wet[chain[index]]:
    eye = [stand[0], stand[1], stand[2] + body.swimEyeAboveSurface]
    ahead = [math.sin(math.radians(heading)), math.cos(math.radians(heading))]
    target = seen if across >= 1e-6 else [eye[0] + ahead[0], eye[1] + ahead[1], eye[2]]
    return {"eye": roundedPoint(eye), "target": roundedPoint(target)}
  pitch = math.degrees(math.atan2(seen[2] - (stand[2] + body.eyeHeight), max(across, nodes.lane.cellSize)))
  view = {"standAt": roundedPoint(stand, 3), "headingDegrees": round(heading, 2), "pitchDegrees": round(pitch, 2), "bodyGroup": nodes.lane.group}
  return view | ({"figureAt": roundedPoint(seen, 3)} if across >= figureTall and not nodes.wet[chain[look]] else {})


def levelColumns(nodes, level):
  """The columns (z * columnsX + x) holding dry floor the lane's group does not reach within its step of a height, sorted."""
  held = []
  step = nodes.lane.body.stepHeight
  for start in range(0, nodes.lane.nodeCount, chunkNodes):
    part = slice(start, min(nodes.lane.nodeCount, start + chunkNodes))
    chosen = numpy.flatnonzero(~nodes.reachedFull[part] & ~nodes.wet[part] & (numpy.abs(nodes.height[part] - level) <= step))
    held.append(nodes.z[part][chosen].astype(numpy.int64) * nodes.lane.columnsX + nodes.x[part][chosen])
  return numpy.unique(numpy.concatenate(held)) if held else numpy.zeros(0, dtype=numpy.int64)


def nearestReached(nodes, point):
  """The node the lane's group reaches nearest a point (in plan alone when it has no height), leaving out those a zone line took (players
  there are gone) and, for a point with a height, those under or over its level: in a column holding floor the group does not reach
  within its step of the point's height while standing further from it than the step, where the point's level lies straight overhead or
  underfoot. None when nothing is left."""
  best, bestDistance = None, math.inf
  step = nodes.lane.body.stepHeight
  covered = levelColumns(nodes, point[2]) if len(point) == 3 else None
  for start in range(0, nodes.lane.nodeCount, chunkNodes):
    part = slice(start, min(nodes.lane.nodeCount, start + chunkNodes))
    chosen = numpy.flatnonzero(nodes.reachedFull[part] & ((nodes.shape[part] & absorbedShape) == 0)) + start
    if covered is not None and len(chosen):
      column = nodes.z[chosen].astype(numpy.int64) * nodes.lane.columnsX + nodes.x[chosen]
      chosen = chosen[~(numpy.isin(column, covered) & (numpy.abs(nodes.height[chosen] - point[2]) > step))]
    if not len(chosen):
      continue
    planX, planY = nodes.plan(chosen)
    distance = (planX - point[0]) ** 2 + (planY - point[1]) ** 2
    if len(point) == 3:
      distance = distance + (nodes.height[chosen].astype(numpy.float64) - point[2]) ** 2
    found = int(numpy.argmin(distance))
    if distance[found] < bestDistance:
      best, bestDistance = int(chosen[found]), float(distance[found])
  return best


def lineColumns(lane, start, end):
  """The columns a plan segment from start to end (zone x, y) crosses, in order: (x, z, t0, t1), x and z its indices (outside the grid
  too) and t0 to t1 the share of the segment within it."""
  shares = {0.0, 1.0}
  for first, last, origin in ((start[0], end[0], lane.origin[2]), (start[1], end[1], lane.origin[0])):
    if first != last:
      lines = origin + numpy.arange(math.ceil((min(first, last) - origin) / lane.cellSize), math.floor((max(first, last) - origin) / lane.cellSize) + 1) * lane.cellSize
      shares.update(float(share) for share in (lines - first) / (last - first) if 0.0 < share < 1.0)
  ordered = sorted(shares)
  cells = []
  for t0, t1 in zip(ordered[:-1], ordered[1:]):
    if t1 - t0 < 1e-9:
      continue
    middle = (t0 + t1) / 2
    x = start[0] + (end[0] - start[0]) * middle
    y = start[1] + (end[1] - start[1]) * middle
    cells.append((int(math.floor((y - lane.origin[0]) / lane.cellSize)), int(math.floor((x - lane.origin[2]) / lane.cellSize)), t0, t1))
  return cells


def sideBySide(cells):
  """Columns along a line, each beside the next: where the line passes a grid corner from one column to its diagonal neighbour, the
  column sharing a side with both comes between."""
  joined = cells[:1]
  for cell in cells[1:]:
    before = joined[-1]
    if cell[0] != before[0] and cell[1] != before[1]:
      joined.append((cell[0], before[1], cell[2], cell[2]))
    joined.append(cell)
  return joined


def inWater(nodes, node):
  return bool(nodes.wet[node]) or int(nodes.box[node]) >= 0


def tracedNode(nodes, column, current):
  """The node in a column a line from the current node goes on to: of those open space joins to the lane (never the inside of a closed
  solid), the one whose level lies nearest the current one's (water holding it counts as level with it), water first from water and
  floor first from floor."""
  lane = nodes.lane
  level = float(lane.height(nodes.high[current] if nodes.wet[current] else nodes.low[current]))

  def apart(node):
    low = float(lane.height(nodes.low[node]))
    if nodes.wet[node]:
      high = float(lane.height(nodes.high[node]))
      return 0.0 if low <= level <= high else min(abs(level - low), abs(level - high))
    return abs(low - level)

  candidates = [node for node in columnNodes(nodes, column) if nodes.state[node] & (reachedState | openState)]
  return min(candidates, key=lambda node: (apart(node), bool(nodes.wet[node]) != bool(nodes.wet[current]))) if candidates else None


def zoneLineHolding(description, point):
  for line in description["zoneLines"]:
    if all(low <= value <= high for low, value, high in zip(line["low"], point, line["high"])):
      return line["name"]
  return None


def transitionFailure(nodes, description, current, node):
  """What stops a player of the lane's body group going from one node to the node beside it on a traced line, by the lane's moves: a dict
  with the kind it looks like before the collision is asked (rise, steep, headroom, ceiling, water, zoneLine), or None where a move goes
  on. As the helper's moves for the group do, a step or a drop takes the group's playerHeight of open space shared by the two nodes: a
  drop onto floor whose open space ends under the floor it is made from meets the ceiling over that floor, or, where that space is lower
  than the group's height, the gap's headroom."""
  lane = nodes.lane
  moving = movement(lane.group)
  clearance = moving["playerHeight"]
  low, high = float(lane.height(nodes.low[current])), float(lane.height(nodes.high[current]))
  otherLow, otherHigh = float(lane.height(nodes.low[node])), float(lane.height(nodes.high[node]))
  if nodes.shape[node] & absorbedShape:
    point = nodePoint(nodes, node)
    return {"kind": "zoneLine", "zoneLine": zoneLineHolding(description, [point[0], point[1], otherLow + lane.cellHeight])}
  if inWater(nodes, current):
    if nodes.wet[node] or otherLow <= low:
      return None
    box = description["swimBoxes"][int(nodes.box[current])]
    top = box["high"][2]
    if otherLow <= top + moving["stepHeight"]:
      return None if nodes.walkable[node] else {"kind": "steep", "steepNode": node}
    return {"kind": "water", "swimBox": box["name"], "boxTop": round(top, 2), "bank": round(otherLow, 2), "overClimb": round(otherLow - top - moving["stepHeight"], 2)}
  if nodes.wet[node]:
    if otherLow <= low + moving["playerHeight"]:
      return None
    return {"kind": "rise", "height": round(otherLow - low, 2), "limit": moving["playerHeight"], "swimBox": description["swimBoxes"][int(nodes.box[node])]["name"]}
  if otherLow - low > moving["stepHeight"]:
    return {"kind": "rise", "height": round(otherLow - low, 2), "limit": moving["stepHeight"]}
  overlap = min(high, otherHigh) - max(low, otherLow)
  if low - otherLow > moving["stepHeight"]:
    if overlap >= clearance:
      return None
    if otherHigh - otherLow < clearance:
      return {"kind": "headroom", "clearance": round(otherHigh - otherLow, 2)}
    return {"kind": "ceiling", "ceiling": round(otherHigh, 2), "floor": round(low, 2)}
  if otherLow > low and not (nodes.walkable[current] and nodes.walkable[node]):
    return {"kind": "steep", "steepNode": node if not nodes.walkable[node] else current}
  if overlap < clearance:
    return {"kind": "headroom", "clearance": round(overlap, 2)}
  return None


def separationWalk(nodes, description, start, targetPoint, target):
  """Walk the plan line from a reached node to a traced point column by column, onto the node tracedNode picks in each (the target's own
  in its column), and list each step no move takes (transitionFailure) and each run of columns with no floor, before the collision is
  asked about them: each with where along the line it lies, and the nodes on either side. A face steep where a stop led onto it is named
  by that stop alone. A point in the start's own column is one step, from the start straight to it. A point on floor no open space joins
  to the lane (inside a closed solid, a sealed room) adds nothing past what stops the line before it; with nothing before, the step into
  it, or, where heights alone let a move through, its being sealed."""
  lane = nodes.lane
  startPoint = nodePoint(nodes, start)
  length = math.hypot(targetPoint[0] - startPoint[0], targetPoint[1] - startPoint[1])
  if length > 0:
    cells = sideBySide(lineColumns(lane, startPoint[:2], targetPoint[:2]))
  else:
    own = (int(nodes.x[start]), int(nodes.z[start]), 0.0, 1.0)
    cells = [own, own]
  current, found = start, []
  for index, (x, z, t0, _) in enumerate(cells[1:], 1):
    column = lane.columnOf(x, z)
    last = index == len(cells) - 1
    if last and target is None and column is not None and len(columnNodes(nodes, column)):
      break
    node = None if column is None else target if last and target is not None else tracedNode(nodes, column, current)
    along = round(t0 * length, 1)
    if node is None:
      if found and found[-1]["kind"] == "noFloor" and found[-1]["lastColumn"] == index - 1:
        found[-1].update(lastColumn=index, columns=found[-1]["columns"] + 1, through=roundedPoint(lane.center(x, z)), alongThrough=along)
      else:
        found.append({"kind": "noFloor", "along": along, "at": roundedPoint(lane.center(x, z)), "lastColumn": index, "columns": 1})
      continue
    failure = transitionFailure(nodes, description, current, node)
    if last and not nodes.state[node] & (reachedState | openState):
      if not found:
        found.append((failure or {"kind": "sealed"}) | {"along": along, "from": current, "to": node})
      break
    named = failure is not None and failure["kind"] == "steep" and failure["steepNode"] == current and found and found[-1].get("to") == current
    if failure is not None and not named:
      found.append(failure | {"along": along, "from": current, "to": node})
    current = node
  return found


def standBack(nodes, description, start, targetPoint):
  """Where to look at a traced point from: the reached node farthest back from a reached node, away from the point along the plan line
  through them, up to frameStandBack, that every column between joins by a move that drops no more than the step, so the eye keeps the
  level it looks from and no ledge it dropped off stands between it and the point; the node itself when the two share a column's
  place."""
  lane = nodes.lane
  startPoint = nodePoint(nodes, start)
  across = math.hypot(startPoint[0] - targetPoint[0], startPoint[1] - targetPoint[1])
  if across < 1e-6:
    return start
  away = [startPoint[0] + (startPoint[0] - targetPoint[0]) / across * frameStandBack, startPoint[1] + (startPoint[1] - targetPoint[1]) / across * frameStandBack]
  current = start
  for x, z, _, _ in sideBySide(lineColumns(lane, startPoint[:2], away))[1:]:
    column = lane.columnOf(x, z)
    node = None if column is None else tracedNode(nodes, column, current)
    if node is None or not nodes.reachedFull[node] or nodes.shape[node] & absorbedShape or transitionFailure(nodes, description, current, node) is not None:
      break
    if not nodes.wet[current] and lane.height(nodes.low[current]) - lane.height(nodes.low[node]) > lane.body.stepHeight:
      break
    current = node
  return current


def separationProbes(nodes, found):
  """What the collision is asked about each stop separationWalk found: a cast across it at half a player's height over the floor it is
  left from, reaching past the far column's middle; and, for a rise or a steep face, every face up a line inside the far column (or the
  steep one) around that floor. Each list holds None where nothing is asked: no cast across a stop within one column, from a floor to
  the floor over or under it."""
  lane = nodes.lane
  moving = movement(lane.group)
  crossings, columns = [], []
  for stop in found:
    if stop["kind"] in ("noFloor", "zoneLine"):
      crossings.append(None)
      columns.append(None)
      continue
    near, far = nodePoint(nodes, stop["from"]), nodePoint(nodes, stop["to"])
    across = math.hypot(far[0] - near[0], far[1] - near[1])
    level = float(lane.height(nodes.low[stop["from"]]))
    height = level + moving["playerHeight"] / 2
    crossings.append(None if across < 1e-6 else [[near[0], near[1], height], [near[0] + 1.5 * (far[0] - near[0]), near[1] + 1.5 * (far[1] - near[1]), height]])
    probed = stop.get("steepNode", stop["to"]) if stop["kind"] in ("rise", "steep") else None
    if probed is None:
      columns.append(None)
      continue
    # A column's floor is the highest ground anywhere in it, and the face rising to it can cross the column at its middle; a quarter
    # column further from the floor on the other side, the line stands over the probed floor rather than on that face.
    away = 0.0 if across < 1e-6 else (0.25 if probed == stop["to"] else -0.25) * lane.cellSize / across
    center = nodePoint(nodes, probed)
    columns.append([
      center[0] + away * (far[0] - near[0]), center[1] + away * (far[1] - near[1]), level - moving["stepHeight"],
      max(float(lane.height(nodes.low[probed])), level + moving["stepHeight"]) + lane.cellHeight,
    ])
  return crossings, columns


def separations(nodes, found, crossed, profiles, targetLevel):
  """What separates a reached node from a traced point at targetLevel (its height, or None for a point without one), from
  separationWalk's stops and what the collision showed at each (bridgeLane traceProbes): a boundary the cast across meets, by name;
  under a rise, a gap within the step of the floor left from that is lower than the lane's group's playerHeight (headroom, with its
  clearance) when the gap's floor is the level the line goes for (within the step of targetLevel, or of the floor left from for a point
  without a height), else the rise; a rise by how much it passes its limit; a steep face by its degrees; a ceiling over the floor a drop
  would land on; water no bank within the step of the box's top leaves; a zone line's box; no floor at all; floor sealed off."""
  lane = nodes.lane
  moving = movement(lane.group)
  clearance = moving["playerHeight"]
  stated = []
  for stop, crossing, profile in zip(found, crossed, profiles):
    kind = stop["kind"]
    shown = {"kind": kind, "along": stop["along"]}
    if crossing is not None and crossing["boundary"]:
      floor = round(float(lane.height(nodes.low[stop["from"]])), 2)
      stated.append(shown | {"kind": "boundary", "name": crossing["object"], "at": roundedPoint(crossing["point"]), "floor": floor})
      continue
    if kind == "noFloor":
      stated.append(shown | {key: stop[key] for key in ("at", "columns", "through", "alongThrough") if key in stop})
      continue
    near, far = nodePoint(nodes, stop["from"]), nodePoint(nodes, stop["to"])
    level = float(lane.height(nodes.low[stop["from"]]))
    if kind == "rise" and profile is not None:
      floor = max((face["z"] for face in profile if face["normalZ"] > 0 and abs(face["z"] - level) <= moving["stepHeight"]), default=None)
      ceiling = None if floor is None else min((face["z"] for face in profile if face["z"] > floor), default=None)
      goesFor = level if targetLevel is None else targetLevel
      if ceiling is not None and ceiling - floor < clearance and abs(goesFor - floor) <= moving["stepHeight"]:
        stated.append(shown | {"kind": "headroom", "at": roundedPoint([far[0], far[1], floor]), "clearance": round(ceiling - floor, 2), "needs": clearance})
        continue
    if kind == "rise":
      reported = shown | {"at": roundedPoint(far), "from": roundedPoint(near), "height": stop["height"], "limit": stop["limit"], "over": round(stop["height"] - stop["limit"], 2)}
      if "swimBox" in stop:
        reported["swimBox"] = stop["swimBox"]
      if crossing is not None and crossing["object"] is not None:
        reported["face"] = crossing["object"]
      stated.append(reported)
    elif kind == "steep":
      steep = stop["steepNode"]
      height = float(lane.height(nodes.low[steep]))
      face = min((face for face in profile or [] if abs(face["z"] - height) <= moving["stepHeight"]), key=lambda face: abs(face["z"] - height), default=None)
      measured = {"degrees": None, "why": "no face crosses the column's middle"} if face is None else {"degrees": round(math.degrees(math.acos(min(1.0, abs(face["normalZ"])))), 1)}
      stated.append(shown | {"at": roundedPoint(nodePoint(nodes, steep))} | measured | {"steepestWalkable": round(lane.body.steepestWalkableDegrees, 1)})
    elif kind == "headroom":
      stated.append(shown | {"at": roundedPoint(far), "clearance": stop["clearance"], "needs": clearance})
    elif kind == "sealed":
      stated.append(shown | {"at": roundedPoint(far), "why": "open space joins this floor to no reached place: it lies inside a closed solid or a sealed room"})
    elif kind == "ceiling":
      stated.append(shown | {"at": roundedPoint([far[0], far[1], stop["ceiling"]]), "ceiling": stop["ceiling"], "floor": stop["floor"], "landing": round(far[2], 2)})
    elif kind == "water":
      stated.append(shown | {"at": roundedPoint(far)} | {key: stop[key] for key in ("swimBox", "boxTop", "bank", "overClimb")})
    else:
      stated.append(shown | {"at": roundedPoint(far), "zoneLine": stop["zoneLine"]})
  return mergedSeparations(stated, nodes.lane.cellSize, moving["stepHeight"])


def separationText(stop):
  """A separation in a few words with its measure, as a trace's section names it."""
  kind = stop["kind"]
  if kind == "rise":
    return f"rise {stop['height']:g} (limit {stop['limit']:g})"
  if kind == "steep":
    return "steep" if stop["degrees"] is None else f"steep {stop['degrees']:g}\N{DEGREE SIGN}"
  if kind == "headroom":
    return f"headroom {stop['clearance']:g} (needs {stop['needs']:g})"
  if kind == "ceiling":
    return f"ceiling {stop['ceiling']:g} over a drop to {stop['landing']:g}"
  if kind == "water":
    return f"water: bank {stop['overClimb']:g} over the climb"
  if kind == "boundary":
    return f"boundary {stop['name']}"
  if kind == "zoneLine":
    return f"zone line {stop['zoneLine']}"
  return {"noFloor": "no floor", "sealed": "sealed"}[kind]


def mergedSeparations(stated, cellSize, stepHeight):
  """Separations of one kind (and one boundary, box, or zone line) in columns side by side along the line as one, from where it starts
  through where it ends (through, at alongThrough), over how many columns: a steep face by its steepest, a rise by its whole height, a
  gap by its lowest clearance; and gaps one after another at floors within the step of each other, nothing else between, as one gap
  however far apart along the line."""
  merged = []
  for stop in stated:
    before = merged[-1] if merged else None
    alike = before is not None and before["kind"] == stop["kind"] and all(before.get(key) == stop.get(key) for key in ("name", "swimBox", "zoneLine"))
    oneGap = alike and stop["kind"] == "headroom" and abs(stop["at"][2] - before["at"][2]) <= stepHeight
    if not alike or (stop["along"] - before["until"] > 1.5 * cellSize and not oneGap):
      merged.append(stop | {"until": stop.get("alongThrough", stop["along"]), "columns": stop.get("columns", 1)})
      continue
    before.update(until=stop["along"], columns=before["columns"] + 1, through=stop["at"], alongThrough=stop["along"])
    if stop["kind"] == "steep" and stop.get("degrees") is not None and (before.get("degrees") is None or stop["degrees"] > before["degrees"]):
      before.pop("why", None)
      before["degrees"] = stop["degrees"]
    elif stop["kind"] == "rise":
      before["height"] = round(stop["at"][2] - before["from"][2], 2)
      before["over"] = round(before["height"] - before["limit"], 2)
    elif stop["kind"] == "headroom":
      before["clearance"] = min(before["clearance"], stop["clearance"])
  for stop in merged:
    stop.pop("until")
  return merged


def laneCut(nodes, points, bottom, top):
  """Where a section's line (points [x, y], its legs laid end to end, s along it) crosses the lane, between bottom and top: reached
  floors as bars at their heights by label, reached water as bands from its bottom to its top (water the lane's group wades shown by
  the floor under it, as the plan shows it), walkable floor never reached that open
  space joins to the lane (never the inside of a closed solid or a boundary's top), and leak edges (where a fall onto nothing or under
  the underworld starts, at the floor it starts from); each run of columns of one kind, label, and height merged."""
  lane = nodes.lane
  pieces, offset = [], 0.0
  for start, end in zip(points[:-1], points[1:]):
    length = math.hypot(end[0] - start[0], end[1] - start[1])
    for x, z, t0, t1 in lineColumns(lane, start, end):
      column = lane.columnOf(x, z)
      if column is not None:
        pieces.append((column, offset + t0 * length, offset + t1 * length))
    offset += length
  marks = {}
  for column, s0, s1 in pieces:
    for node in columnNodes(nodes, column):
      low, high = float(lane.height(nodes.low[node])), float(lane.height(nodes.high[node]))
      if nodes.reached[node] and nodes.wet[node]:
        if nodes.shallow[node]:
          continue
        key, inside = ("bands", labelNames[nodes.label[node]], low, high), high >= bottom and low <= top
      elif nodes.reached[node]:
        key, inside = ("bars", labelNames[nodes.label[node]], low, None), bottom <= low <= top
      elif nodes.walkable[node] and not nodes.wet[node] and not nodes.shape[node] & (absorbedShape | boundaryShape) and nodes.state[node] & openState:
        key, inside = ("unreached", None, low, None), bottom <= low <= top
      else:
        continue
      if inside:
        marks.setdefault(key, []).append([s0, s1])
  cut = {"group": lane.group, "bars": [], "bands": [], "unreached": [], "leaks": []}
  for (kind, label, low, high), spans in marks.items():
    spans.sort()
    merged = [spans[0]]
    for s0, s1 in spans[1:]:
      if s0 <= merged[-1][1] + 1e-6:
        merged[-1][1] = max(merged[-1][1], s1)
      else:
        merged.append([s0, s1])
    for s0, s1 in merged:
      height = [round(low, 2), round(high, 2)] if kind == "bands" else round(low, 2)
      cut[kind].append({"s": [round(s0, 2), round(s1, 2)], "z": height} | ({} if label is None else {"label": label}))
  for kind in ("bars", "bands", "unreached"):
    cut[kind].sort(key=lambda mark: (mark["s"][0], mark["z"] if kind != "bands" else mark["z"][0]))
  leaking = lane.events[numpy.isin(lane.events[:, 0], (voidEvent, belowUnderworldEvent))] if len(lane.events) else lane.events
  if len(leaking) and pieces:
    order = {}
    for index, (column, _, _) in enumerate(pieces):
      order.setdefault(column, []).append(index)
    leaks = set()
    for _, fromNode, column, _, _ in leaking[numpy.isin(leaking[:, 2], numpy.array(list(order), dtype=numpy.int64))]:
      height = float(lane.height(nodes.low[fromNode]))
      if not bottom <= height <= top:
        continue
      fromColumn = lane.columnOf(int(nodes.x[fromNode]), int(nodes.z[fromNode]))
      for index in order[int(column)]:
        beside = [other for other in (index - 1, index + 1) if 0 <= other < len(pieces) and pieces[other][0] == fromColumn]
        s = (pieces[index][1] if beside[0] < index else pieces[index][2]) if beside else (pieces[index][1] + pieces[index][2]) / 2
        leaks.add((round(s, 2), round(height, 2)))
    cut["leaks"] = [{"s": s, "z": z} for s, z in sorted(leaks)]
  return cut


def tintPath(lane):
  return lane.path.with_name("tint-" + lane.path.name.removeprefix("lane-")).with_suffix(".npy")


def laneTint(lane):
  """The lane's reached floors as a file beside it, written once per lane (tint-<key>.npy, tintColumns: each reached dry floor node's
  column, floor, and label), for renderView's lane tint; tints whose lane is gone are removed. Returns its path."""
  path = tintPath(lane)
  if not path.is_file():
    nodes = Nodes(lane)
    chosen = numpy.flatnonzero(nodes.reached & ~nodes.wet)
    columns = numpy.empty(len(chosen), dtype=tintColumns)
    columns["x"], columns["z"], columns["low"], columns["label"] = nodes.x[chosen], nodes.z[chosen], nodes.low[chosen], nodes.label[chosen]
    del nodes
    partial = path.with_suffix(".partial")
    with partial.open("wb") as written:
      numpy.save(written, columns)
    os.replace(partial, path)
  for stale in path.parent.glob("tint-*.npy"):
    if not stale.with_name("lane-" + stale.name.removeprefix("tint-")).with_suffix(".bin").is_file():
      stale.unlink()
  return path
