"""The server's nav mesh (.nav), built as map_edit builds it by the Recast helper, and inspected as the server loads and searches it: NPC
islands and path probes. Collision comes in server axes (zone y, zone x, z), .wtr records and probe points in zone axes; Recast and
Detour work in (zone y, zone z, zone x), an even permutation that keeps winding."""
import struct
import zlib

import numpy
from mcp.server.mcpserver.exceptions import ToolError

import machineProfile
import recastHelper

# map_edit's defaults, recovered from Highpass Hold's .nav header and its .navprj. They describe the NPC agent the server paths, never
# the player (playerScale).
serverNavSettings = {
  "cellSize": 0.8, "cellHeight": 0.4,
  "agentHeight": 6.55, "agentRadius": 1.31, "agentClimb": 6.55,
  "maxSlope": 60.0,
  "regionMinSize": 8.0, "regionMergeSize": 20.0,
  "edgeMaxLength": 12.0, "edgeMaxError": 1.3,
  "verticesPerPolygon": 6,
  "detailSampleDistance": 18.0, "detailSampleMaxError": 1.0,
  "tileSize": 512, "borderSize": 5,
  "partitioning": "watershed",
}
navAreaNames = {0: "Normal", 1: "Water", 2: "Lava", 3: "ZoneLine", 4: "PvP", 5: "Slime", 6: "Ice", 7: "VWater", 8: "GeneralArea", 9: "Portal", 10: "Prefer", 11: "Disabled"}
disabledArea = 11
# .wtr region type to nav area, map_edit's cases; a zone line becomes Disabled, as map_edit has no ZoneLine case.
volumeAreas = {0: 0, 1: 1, 2: 2, 3: disabledArea, 4: 4, 5: 5, 6: 6, 7: 7, 8: 8, 9: 10, 10: disabledArea}
# map_edit turns .wtr rotations, stored in degrees, with this pi.
degreesToRadians = numpy.float32(3.14159) / numpy.float32(180.0)
# Peridot's ground pathing: EQEmu PathfinderNavmesh::FindPath as MobMovementManager::UpdatePathGround calls it (its filter, costs,
# 256-polygon path, and (10, 200, 10) nearest search), Peridot's Pathing:MaxNavmeshNodes 1024, and the (5, 100, 5) nearest search of
# FindRoute, which decides the polygon an NPC stands on.
serverPathing = {
  "searchNodes": 1024, "pathPolygons": 256,
  "nearestHalfExtents": (5.0, 100.0, 5.0), "pathHalfExtents": (10.0, 200.0, 10.0), "snapHeight": 100.0,
  "excludedAreas": (3, disabledArea),
  "areaCosts": {0: 1.0, 1: 3.0, 2: 5.0, 4: 1.0, 5: 2.0, 6: 2.0, 7: 4.0, 8: 1.0, 9: 0.1, 10: 0.1},
}
navFileMagic = b"EQNAVMESH"
navFileVersion = 2
navHeaderBytes = len(navFileMagic) + 12
payloadHeaderBytes = 32


def recastFromServer(points):
  return numpy.asarray(points)[..., [0, 2, 1]]


def recastFromZone(points):
  return numpy.asarray(points)[..., [1, 2, 0]]


def zoneFromRecast(points):
  return numpy.asarray(points)[..., [2, 0, 1]]


def rotationMatrix(rotationDegrees):
  """Rz . Ry . Rx in float32, the turn the server gives an oriented box."""
  x, y, z = (numpy.float32(value) * degreesToRadians for value in rotationDegrees)
  cx, sx, cy, sy, cz, sz = numpy.cos(x), numpy.sin(x), numpy.cos(y), numpy.sin(y), numpy.cos(z), numpy.sin(z)
  aboutX = numpy.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]], dtype=numpy.float32)
  aboutY = numpy.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]], dtype=numpy.float32)
  aboutZ = numpy.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]], dtype=numpy.float32)
  return aboutZ @ aboutY @ aboutX


def navVolumes(waterRecords):
  """map_edit's volume for each .wtr record, in .wtr order (a later one marks over an earlier): the box's corners v5, v2, v3, and v8
  (moved, scaled, and turned as the server places the box) in Recast axes, its height range, and its nav area."""
  volumes = []
  for index, record in enumerate(waterRecords):
    if record["type"] not in volumeAreas:
      raise ToolError(f".wtr record {index} at zone {tuple(round(float(value), 2) for value in record['position'])} is type {record['type']},"
        f" which no nav area maps; map_edit would quietly make it Disabled")
    extents = numpy.asarray(record["halfExtents"], dtype=numpy.float32)
    low, high = numpy.minimum(-extents, extents), numpy.maximum(-extents, extents)
    rotation = rotationMatrix(record["rotation"])
    position = numpy.asarray(record["position"], dtype=numpy.float32)
    scale = numpy.asarray(record["scale"], dtype=numpy.float32)
    local = numpy.array([low, [low[0], high[1], high[2]], high, [high[0], low[1], low[2]]], dtype=numpy.float32)
    corners = recastFromZone(position + scale * (local @ rotation.T)).astype(numpy.float32)
    volumes.append({"corners": corners, "low": float(corners[:, 1].min()), "high": float(corners[:, 1].max()), "area": volumeAreas[record["type"]]})
  return volumes


def tileEntries(payload):
  """Each tile's (reference, size) in a nav payload, its framing checked to the last byte."""
  if len(payload) < payloadHeaderBytes:
    raise ToolError(f"A nav payload of {len(payload)} bytes is shorter than its {payloadHeaderBytes}-byte header")
  tileCount = struct.unpack_from("<I", payload, 0)[0]
  offset = payloadHeaderBytes
  entries = []
  for index in range(tileCount):
    if offset + 8 > len(payload):
      raise ToolError(f"The nav payload ends at byte {len(payload)} inside tile {index}'s reference and size at offset {offset}")
    reference, size = struct.unpack_from("<Ii", payload, offset)
    if reference == 0 or size <= 0:
      raise ToolError(f"Nav tile {index} has reference {reference} and size {size}; the server drops the whole mesh on a zero")
    offset += 8 + size
    if offset > len(payload):
      raise ToolError(f"Nav tile {index}'s {size} bytes run past the payload's end at byte {len(payload)}")
    entries.append((reference, size))
  if offset != len(payload):
    raise ToolError(f"The nav payload holds {len(payload) - offset} bytes after its last tile")
  return entries


def navContainer(payload):
  """EQNAVMESH version 2: the payload's inflated and compressed sizes, then the zlib stream."""
  tileEntries(payload)
  compressed = zlib.compress(payload)
  return navFileMagic + struct.pack("<3I", navFileVersion, len(compressed), len(payload)) + compressed


def navPayload(navFile):
  """The inflated payload of an EQNAVMESH file, checked as the server reads it."""
  if navFile[:len(navFileMagic)] != navFileMagic:
    raise ToolError(f"Not an EQNAVMESH file: it starts {navFile[:len(navFileMagic)]!r}")
  version, compressedSize, inflatedSize = struct.unpack_from("<3I", navFile, len(navFileMagic))
  if version != navFileVersion:
    raise ToolError(f"EQNAVMESH version {version}; the server reads version {navFileVersion}")
  if navHeaderBytes + compressedSize != len(navFile):
    raise ToolError(f"The .nav states {compressedSize} compressed bytes, but {len(navFile) - navHeaderBytes} follow its header")
  payload = zlib.decompress(navFile[navHeaderBytes:])
  if len(payload) != inflatedSize:
    raise ToolError(f"The .nav inflates to {len(payload)} bytes, not the {inflatedSize} it states")
  return payload


def navBounds(collision):
  """The nav bounds map_edit takes by default: the collidable extents, Recast axes, as (minimum, maximum)."""
  points = recastFromServer(numpy.asarray(collision, dtype=numpy.float32)).reshape(-1, 3)
  return [float(value) for value in points.min(axis=0)], [float(value) for value in points.max(axis=0)]


def navFromCollision(collision, waterRecords, toolingRoot, reportProgress, bounds=None):
  """The .nav map_edit would build from the server's collidable triangles (float32 [n, 3, 3], server axes, in the .map's order) and the
  .wtr's records ({type, position, rotation (degrees), scale, halfExtents}, zone axes, in .wtr order), with serverNavSettings. Tiles build
  on parallel threads and are added in (ty, tx) order, so the file is byte-identical run to run. The bounds are the collidable extents
  (navBounds), as every export takes them; a reference nav map_edit built from a project whose bounds were edited by hand gives those
  (Recast axes), and a triangle reaching outside them is refused, where map_edit dropped it. Returns (file bytes, helper report)."""
  triangles = numpy.asarray(collision, dtype=numpy.float32)
  if triangles.ndim != 3 or triangles.shape[1:] != (3, 3):
    raise ValueError(f"collision must be float32 [n, 3, 3] in server axes, got shape {list(triangles.shape)}")
  if len(triangles) == 0:
    raise ToolError("There are no collidable triangles to build a nav from")
  if serverNavSettings["partitioning"] != "watershed":
    raise ValueError(f"the helper partitions by watershed only, not {serverNavSettings['partitioning']}")
  inputBytes = recastHelper.navInput(
    recastFromServer(triangles), navBounds(triangles) if bounds is None else bounds, navVolumes(waterRecords), serverNavSettings, machineProfile.workerCount(),
  )
  payload, report = recastHelper.runHelper(toolingRoot, "nav", inputBytes, reportProgress)
  return navContainer(payload), report


def probeFinding(probe):
  reasons = {
    "noStartPolygon": f"no polygon the server's ground filter allows lies within {serverPathing['pathHalfExtents']} of the safe point",
    "noGoalPolygon": f"no polygon the server's ground filter allows lies within {serverPathing['pathHalfExtents']} of it",
    "failed": "the search failed",
  }
  if probe["result"] in reasons:
    reason = reasons[probe["result"]]
  elif probe["outOfNodes"]:
    reason = f"the search ran out of its {serverPathing['searchNodes']:,} nodes"
  elif probe["pathBufferFull"]:
    reason = f"the path fills the server's {serverPathing['pathPolygons']}-polygon path buffer"
  else:
    reason = f"it lies on island {probe['goalComponent']}, apart from the main piece"
  return f"NPCs cannot path from the safe point to {probe['name']} within Peridot's {serverPathing['searchNodes']:,} search nodes: {reason}"


def zoneBox(component):
  """A component's bounds, center, and size in zone axes."""
  return {
    "polygons": component["polygons"], "area": round(component["area"], 1),
    "boundsMin": [round(float(value), 2) for value in zoneFromRecast(component["boundsMin"])],
    "boundsMax": [round(float(value), 2) for value in zoneFromRecast(component["boundsMax"])],
    "center": [round(float(value), 2) for value in zoneFromRecast(component["center"])],
  }


def inspectNav(navFile, safePoint, targets, toolingRoot, reportProgress):
  """Load a .nav as the server does and label its polygons into components across links, Disabled and ZoneLine polygons in none (the
  server's ground filter never walks them). The main piece is the component of the polygon nearest the safe point (zone axes) within
  (5, 100, 5); without one, the largest component stands in and the result says so. Every other component is an island, numbered by
  area, largest first, with its bounds, its center (the middle of its largest polygon, so it lies on the island), and snapRisk: some
  main-piece polygon lies within 100 vertically over its footprint, where the server's nearest-polygon search can put an NPC onto it.
  Then a Detour search with Peridot's limits from the safe point to each target ({name, point}, zone axes)."""
  payload = navPayload(navFile)
  recastSafe = None if safePoint is None else [float(value) for value in recastFromZone(safePoint)]
  recastTargets = [[float(value) for value in recastFromZone(target["point"])] for target in targets]
  _, report = recastHelper.runHelper(toolingRoot, "inspect", recastHelper.inspectInput(payload, recastSafe, recastTargets, serverPathing), reportProgress)
  components = report["components"]
  findings = []
  standIn = safePoint is None or report["safePolygon"] == 0
  if safePoint is None:
    findings.append("No safe point: islands are counted against the largest component, which stands in for the main piece")
  elif report["safePolygon"] == 0:
    findings.append(f"The safe point is off the NPC mesh: no polygon lies within {serverPathing['nearestHalfExtents']} of it; islands are"
      " counted against the largest component, which stands in for the main piece")
  islands = [zoneBox(component) | {"number": number, "snapRisk": component["snapRisk"]} for number, component in enumerate(components[1:], 1)]
  probes = []
  for target, probe in zip(targets, report["probes"]):
    probe = {"name": target["name"], "point": list(target["point"])} | probe | {"reached": probe["result"] == "complete"}
    probes.append(probe)
    if not probe["reached"]:
      findings.append(probeFinding(probe))
  return {
    "polygons": report["polygons"], "excludedPolygons": report["excludedPolygons"],
    "mainPiece": zoneBox(components[0]) | {"standIn": standIn},
    "islands": islands, "probes": probes, "findings": findings,
    "polygonComponents": report["polygonComponents"],
  }
