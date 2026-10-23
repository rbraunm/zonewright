"""An export's files from what the bridge checked and collected, and their writing. Both purposes write the EQG archive, its client side
files, and the manifest (<zone>_export.json: every file with its SHA-256 and size). A game export also builds the server's side from the
archive's bytes and the scene's decisions: under server\\maps, laid out as akk-stack's server/maps so the owner copies it whole, the
collision map (.map), the region map (.wtr), and the nav mesh (.nav), which is inspected as the server loads it for NPC islands and path
probes; the player's lane (playerReach), whose leaks and stranded arrivals are failures; the ground the server finds on the .map under
where players arrive; and under server\\sql the zone's rows (serverRows) and its interactive pieces' doors and object rows (pieceRows).
The client models the scene places go into the archive as clientModelBundle copies them, with where each came from in the manifest. The
zone_points, doors, and object rows every game export of a short name wrote are kept under the tooling root (the records), so a later
export deletes a removed one's row wherever it is written. A test export removes this zone's server files, which would no longer match
its archive. Every file goes in together, or none does."""
import hashlib
import json
import math
import os

import numpy
from mcp.server.mcpserver.exceptions import ToolError

import eqAxes
import eqEmitters
import eqgExport
import eqgFiles
import clientModelBundle
import pieceRows
import playerReach
import playerScale
import recastHelper
import serverMapFiles
import serverNav
import serverRows
import toolingManifest
import zoneSources

islandsListed = 25
recordName = "zonePointsWritten.json"
piecesRecordName = "piecesWritten.json"
laneFindingKinds = (
  "disconnectedEntry", "unreachedPlot", "trap", "viewRegion", "unreachedZoneLine", "unreachedSwim", "unreachedPlayFloor", "undecidedWater",
)
laneFindingMessages = {
  "disconnectedEntry": "entries some body group the place is not built for cannot pass both ways to and from the safe point",
  "unreachedPlot": "plot entrances and apartment thresholds some body group the place is not built for does not reach",
  "trap": "places players reach and cannot leave: no way back to where they arrive or to a zone line",
  "viewRegion": "reach into regions planned only to be seen (access view)",
  "unreachedZoneLine": "zone lines some body group does not reach",
  "unreachedSwim": "swim boxes some body group does not reach",
  "unreachedPlayFloor": "walkable floor in play regions that some body group does not reach",
  "undecidedWater": "pools and rivers without swim boxes whose bed players walk, as the client would",
}
testContainment = "not checked for a test export"
placeKeys = (
  "number", "center", "extent", "length", "area", "region", "entry", "plot", "zoneLine", "swimBox", "body", "groups", "verdict", "failingGroups",
  "builtFor",
)


def serverFilePaths(folder, zone):
  """This zone's files under <folder>\\server by kind, each named by the lowercase short name (the nav loader does not lowercase,
  pathfinder_interface.cpp:10)."""
  maps, sql = folder / "server" / "maps", folder / "server" / "sql"
  return {
    "map": maps / "base" / f"{zone}.map", "water": maps / "water" / f"{zone}.wtr", "nav": maps / "nav" / f"{zone}.nav",
    "rows": sql / f"{zone}.sql", "waysIn": sql / f"{zone}_waysIn.sql", "pieces": sql / f"{zone}_pieces.sql",
  }


def sideFilePaths(folder, zone):
  return {
    "emitterList": folder / f"{zone}_EnvironmentEmitters.txt", "housing": folder / f"{zone}_housing.json", "assetList": folder / f"{zone}_assets.txt",
  }


def manifestPath(folder, zone):
  return folder / f"{zone}_export.json"


def exportFilePaths(folder, zone):
  """Every file an export to folder writes or removes: the archive, its manifest, its side files, and this zone's server files."""
  return [folder / f"{zone}.eqg", manifestPath(folder, zone), *sideFilePaths(folder, zone).values(), *serverFilePaths(folder, zone).values()]


def recordPath(toolingRoot, short):
  return toolingRoot / "exports" / short / recordName


def readRecord(toolingRoot, short):
  """Every zone_points row a game export of this short name wrote (serverRows.rowsFiles' zonePoints), as the record under the tooling root
  keeps them: none before the first."""
  path = recordPath(toolingRoot, short)
  if not path.is_file():
    return []
  try:
    record = json.loads(path.read_text(encoding="ascii"))
    if not isinstance(record, dict) or set(record) != {"shortName", "zonePoints"} or record["shortName"] != short or not isinstance(record["zonePoints"], list):
      raise ValueError(f"it holds no {{shortName: {short!r}, zonePoints: [rows]}}")
    serverRows.ownedRows(record["zonePoints"], short)
  except (OSError, ValueError) as error:
    raise ToolError(
      f"The record of the zone_points rows {short}'s game exports wrote, {path}, cannot be read: {type(error).__name__}: {error}. An export"
      " deletes only the rows it holds, so put it back as it was rather than remove it"
    ) from error
  return record["zonePoints"]


def piecesRecordPath(toolingRoot, short):
  return toolingRoot / "exports" / short / piecesRecordName


def readPiecesRecord(toolingRoot, short, zoneID):
  """Every doors and object row a game export of this short name wrote (pieceRows.piecesFile's), as the record keeps them: none before
  the first."""
  path = piecesRecordPath(toolingRoot, short)
  if not path.is_file():
    return {"doors": [], "objects": []}
  try:
    record = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(record, dict) or set(record) != {"shortName", "doors", "objects"} or record["shortName"] != short:
      raise ValueError(f"it holds no {{shortName: {short!r}, doors, objects}}")
    pieceRows.ownedRecord(record, short, zoneID)
  except (OSError, ValueError) as error:
    raise ToolError(
      f"The record of the doors and object rows {short}'s game exports wrote, {path}, cannot be read: {type(error).__name__}: {error}. An export"
      " deletes only the rows it holds, so put it back as it was rather than remove it"
    ) from error
  return {"doors": record["doors"], "objects": record["objects"]}


def writeRecord(path, content):
  path.parent.mkdir(parents=True, exist_ok=True)
  partial = path.with_name(path.name + ".partial")
  partial.write_text(json.dumps(content, indent=1), encoding="utf-8")
  os.replace(partial, path)


def recordExport(toolingRoot, built):
  """Add a game export's zone_points rows, and its doors and object rows, to its short name's records, written whole and put in place,
  before the export's files are: a row recorded and never applied only makes a later delete match nothing."""
  short = built["zoneProperties"]["shortName"]
  path = recordPath(toolingRoot, short)
  kept = serverRows.ownedRows(readRecord(toolingRoot, short) + built["rows"]["zonePoints"], short)
  writeRecord(path, {"shortName": short, "zonePoints": kept})
  recorded = {"path": str(path), "rows": len(kept)}
  if built["pieces"] is not None:
    record = readPiecesRecord(toolingRoot, short, built["zoneProperties"]["zoneId"])
    doors = record["doors"] + [row for row in built["pieces"]["doors"] if row not in record["doors"]]
    objects = record["objects"] + [row for row in built["pieces"]["objects"] if row not in record["objects"]]
    writeRecord(piecesRecordPath(toolingRoot, short), {"shortName": short, "doors": doors, "objects": objects})
    recorded["pieces"] = {"path": str(piecesRecordPath(toolingRoot, short)), "doors": len(doors), "objects": len(objects)}
  return recorded


def sideContents(collected, zone):
  """The client side files' bytes by kind. A list left from an earlier export would place emitters or plots this scene no longer has,
  so a list with nothing to say is None, and is removed."""
  housing = collected["housing"]
  hasHomes = housing is not None and bool(housing["plots"] or housing["apartments"])
  return {
    "emitterList": eqEmitters.emitterListText(collected["emitters"]).encode("latin1") if collected["emitters"] else None,
    "housing": json.dumps({"zone": zone} | housing, indent=1).encode("ascii") if hasHomes else None,
    "assetList": "".join(f"{archive}\r\n" for archive in housing["assets"]).encode("latin1") if hasHomes and housing["assets"] else None,
  }


def zoneArchive(collected):
  """The archive's bytes, what went into it, and the client models it copies (clientModelBundle.bundle)."""
  try:
    data, bundle, summary = eqgExport.zoneArchive(collected, zoneSources.resolveClientRoot())
  except (OSError, ValueError) as error:
    raise ToolError(f"{type(error).__name__}: {error}") from error
  return data, summary, bundle


def bundleReport(bundle, summary, game):
  """The client models' failures and findings: what cannot be copied, our texture a startup one shadows, and what the copies add to
  the archive; for a test export, the interactive pieces it writes no rows for."""
  failures = bundle["failures"] + clientModelBundle.ownTextureFailures(zoneSources.resolveClientRoot(), summary["ownTextures"])
  findings = list(bundle["findings"])
  if bundle["copies"]:
    findings.append({
      "finding": "client models copied", "models": len(bundle["copies"]), "bytes": summary["clientBytes"],
      "message": f"{len(bundle['copies'])} client models and their textures add {summary['clientBytes']} bytes to the archive (no budget until Phase 2 measures a real zone)",
    })
  if bundle["rowPieces"] and not game:
    findings.append({
      "finding": "pieces as rows", "pieces": len(bundle["rowPieces"]),
      "message": f"{len(bundle['rowPieces'])} interactive pieces are doors and object rows, which a test export does not write: a test load draws them only once a game export's rows are applied",
    })
  return failures, findings


def objectStands(archiveBytes, collision, pieces, game):
  """Where the server will stand each interactive object (pieceRows.objectHeights) on the archive's .map collision (collision, or built
  from the archive when None), as failures for a game export and findings for a test one."""
  if not any(piece["row"]["table"] == "object" for piece in pieces):
    return [], []
  if collision is None:
    collision = serverMapFiles.mapCollision(serverMapFiles.mapBytes(serverMapFiles.zoneFilesOf(archiveBytes)))
  moved = pieceRows.movedFailures(pieceRows.objectHeights(collision, pieces))
  if game:
    return moved, []
  return [], [{"finding": entry.pop("failure")} | entry for entry in moved]


def probeTargets(entries):
  """Where NPC paths are probed to besides the zone lines: every stored entry and every plot's entrance, where players arrive and live."""
  targets = [{"name": f"entry {entry['name']}", "point": entry["at"], "onFloor": True} for entry in entries if entry["source"] == "placeEntry"]
  return targets + [{"name": f"plot {entry['name']}", "point": entry["at"], "onFloor": True} for entry in entries if entry["kind"] == "plotEntrance"]


def serverMaps(archiveBytes, safePoint, targets, toolingRoot, reportProgress):
  """The server's map files from an archive's bytes: the .map and .wtr from its files, then the .nav map_edit would build from them,
  inspected as the server loads it, with NPC path probes from the safe point (zone axes, or None) to each zone line's center and then to
  each of targets ({name, point}, zone axes). A nav the helper refuses is a failure and leaves no .nav. Returns {files by kind, the
  collision the server rebuilds from the .map (server axes), inspection, helper report, failures}."""
  try:
    zoneFiles = serverMapFiles.zoneFilesOf(archiveBytes)
    regions = eqgFiles.parseZone(zoneFiles["zon"], "the .zon")["regions"]
    mapBytes = serverMapFiles.mapBytes(zoneFiles)
    waterBytes = serverMapFiles.waterBytes(regions)
    collision = serverMapFiles.mapCollision(mapBytes)
  except ValueError as error:
    raise ToolError(f"The server's map files cannot be built from this archive: {error}") from error
  built = {"files": {"map": mapBytes, "water": waterBytes, "nav": None}, "collision": collision, "inspection": None, "navReport": None, "failures": []}
  try:
    navBytes, built["navReport"] = serverNav.navFromCollision(collision, serverMapFiles.readWater(waterBytes), toolingRoot, reportProgress)
  except (ToolError, ValueError) as error:
    built["failures"].append({"failure": "nav not built", "message": f"The server's nav mesh cannot be built: {error}"})
    return built
  built["files"]["nav"] = navBytes
  lines = [{"name": region["name"], "point": list(region["center"]), "onFloor": False} for region in regions if eqgFiles.isZoneLine(region["name"])]
  try:
    built["inspection"] = serverNav.inspectNav(navBytes, safePoint, lines + targets, toolingRoot, reportProgress)
  except ValueError as error:
    raise ToolError(f"The nav built from this archive cannot be inspected: {error}") from error
  return built


def columnsBetween(low, high, origin, cellSize, count):
  """The indices of the grid's columns along one axis whose centers lie from low to high."""
  return numpy.arange(max(0, math.ceil((low - origin) / cellSize - 0.5)), min(count - 1, math.floor((high - origin) / cellSize - 0.5)) + 1)


def islandReach(inspection, nodes, step):
  """Whether players reach each NPC island, by its number: whether some dry floor any body reaches with the lane's group's moves lies
  within that group's step (so a player there steps onto it) of one of the island's polygons, in a column whose center the polygon holds.
  The polygon's height there is read off its corners, fanned from its first (Recast's polygons are convex)."""
  lane = nodes.lane
  reached = numpy.zeros(len(inspection["islands"]), dtype=bool)
  standing = numpy.flatnonzero(nodes.reached & ~nodes.wet)
  if not len(reached) or not len(standing):
    return reached.tolist()
  heights = nodes.height[standing].astype(numpy.float64)
  base, extent = float(heights.min()), float(heights.max() - heights.min())
  span = extent + 1
  keys = numpy.sort((nodes.z[standing].astype(numpy.int64) * lane.columnsX + nodes.x[standing]) * span + (heights - base))
  for tile in inspection["tiles"]:
    for polygon in tile["polygons"]:
      number = polygon["component"]
      if number < 1 or reached[number - 1]:
        continue
      corners = numpy.column_stack([numpy.asarray(polygon["outline"], dtype=numpy.float64), polygon["heights"]])
      low, high = corners[:, :2].min(axis=0), corners[:, :2].max(axis=0)
      rows = columnsBetween(low[0], high[0], lane.origin[2], lane.cellSize, lane.columnsZ)
      across = columnsBetween(low[1], high[1], lane.origin[0], lane.cellSize, lane.columnsX)
      if not len(rows) or not len(across):
        continue
      row, column = numpy.meshgrid(rows, across, indexing="ij")
      row, column = row.ravel(), column.ravel()
      x, y = lane.origin[2] + (row + 0.5) * lane.cellSize, lane.origin[0] + (column + 0.5) * lane.cellSize
      surface = numpy.full(len(row), numpy.nan)
      for second in range(1, len(corners) - 1):
        a, b, c = corners[0], corners[second], corners[second + 1]
        denominator = (b[1] - c[1]) * (a[0] - c[0]) + (c[0] - b[0]) * (a[1] - c[1])
        if denominator == 0:
          continue
        u = ((b[1] - c[1]) * (x - c[0]) + (c[0] - b[0]) * (y - c[1])) / denominator
        v = ((c[1] - a[1]) * (x - c[0]) + (a[0] - c[0]) * (y - c[1])) / denominator
        inside = (u >= -1e-9) & (v >= -1e-9) & (1 - u - v >= -1e-9) & numpy.isnan(surface)
        surface[inside] = (u * a[2] + v * b[2] + (1 - u - v) * c[2])[inside]
      held = ~numpy.isnan(surface)
      if not held.any():
        continue
      low, high = surface[held] - step - base, surface[held] + step - base
      near = (high >= 0) & (low <= extent)
      start = (row[held] * lane.columnsX + column[held])[near] * span
      # A column's keys lie within extent of its start, so a window kept to that range never reads the next column's.
      lowest = numpy.searchsorted(keys, start + numpy.clip(low[near], 0, extent), side="left")
      highest = numpy.searchsorted(keys, start + numpy.clip(high[near], 0, extent), side="right")
      reached[number - 1] = bool((highest > lowest).any())
  return reached.tolist()


def navFindings(inspection):
  islands = inspection["islands"]
  findings = [{"finding": "NPC nav", "message": message} for message in inspection["findings"]]
  if islands:
    atRisk = sum(island["snapRisk"] for island in islands)
    finding = {"finding": "NPC islands", "islands": len(islands), "atSnapRisk": atRisk}
    message = (
      f"{len(islands)} pieces of the NPC nav lie apart from its main piece, {atRisk} where the server's nearest-polygon search can put an NPC"
      " onto one"
    )
    reach = [island["playersReach"] for island in islands]
    if None in reach:
      message += "; whether players reach them is not checked, the lane not being computed"
    else:
      finding["playersReach"] = sum(reach)
      message += f"; players reach {sum(reach)}, where NPCs placed on them stand apart from the rest of the nav"
    findings.append(finding | {"message": message + f"; serverFiles.nav lists the {islandsListed} largest, the manifest every one"})
  return findings


def placeOf(problem):
  return {key: problem[key] for key in placeKeys if key in problem}


def leakFailure(problem, underworld, numbered="mapReach problem"):
  """A lane problem that refuses a game export, with its place and its number (numbered says whose: mapReach's, for the scene's lane)."""
  kind, number, center = problem["kind"], problem["number"], problem["center"]
  said = {
    "voidEdge": f"Players walk or slide off an edge onto nothing, along {problem.get('length')}: a boundary wall, or shaping, closes it",
    "belowUnderworld": (
      f"Players reach floor under the underworld ({underworld}), along {problem.get('length')}, where the server takes them for fallen out"
      " of the world"
    ),
    "noneRegion": (
      f"Players reach {problem.get('area')} of region '{problem['region']}', whose access is none: a boundary or shaping keeps them out, or"
      " its access is wrong"
    ),
    "outsideRegions": f"Players reach {problem.get('area')} of ground outside every region: plan it with a region, or keep players off it",
    "disconnectedEntry": (
      f"Entry '{problem.get('entry')}' and the safe point do not reach each other both ways for the {problem.get('failingGroups')} body"
      f" groups, and the place is built for {problem.get('builtFor')}: players arriving there cannot reach the rest of the zone or come back;"
      " an area only its own port reaches is marked isolated (placeEntry), and a place built for other groups says so (builtFor)"
    ),
    "unreachedPlot": (
      f"The {problem.get('failingGroups')} body groups do not reach the entrance of '{problem.get('plot')}', and the place is built"
      f" for {problem.get('builtFor')}"
    ),
  }[kind]
  groups = f"; the {problem['groups']} body groups reach it" if "groups" in problem else ""
  return {"failure": kind} | placeOf(problem) | {"message": f"{said}{groups} ({numbered} {number}, at {center})"}


def laneFindings(lane, description, analysis):
  """What the lane shows that is to be looked at, never refused: each finding kind of problem with its places and numbers, squeeze reach,
  a zone line in a swim box, no zone lines, no zoneIn entry, and what the lane does not model and its limits."""
  findings = []
  problems, more = analysis["problems"], analysis["moreProblems"]
  for kind in laneFindingKinds:
    found = [placeOf(problem) for problem in problems if problem["kind"] == kind and not playerReach.isFailure(problem)]
    if found:
      finding = {"finding": kind, "problems": found}
      if kind in more:
        finding["more"] = more[kind]
      count = len(found) + more.get(kind, {}).get("count", 0)
      findings.append(finding | {"message": f"{laneFindingMessages[kind]}: {count} (mapReach numbers them as here)"})
  nodes = analysis["nodes"]
  for name in playerScale.groupNames:
    # Water too shallow for a group to swim in is squeeze too, though it walks the floor; it is no gap to look at.
    squeezed = nodes.reached & ~analysis["fullBy"][name] & ~(nodes.wet & nodes.shallow)
    if not squeezed.any():
      continue
    places = playerReach.groupedProblems("squeeze", lane, squeezed, nodes, analysis["regionOfNode"], description["regions"])
    places.sort(key=lambda place: -place["area"])
    body = playerScale.groups[name]
    findings.append({
      "finding": "squeeze reach", "group": name, "area": round(float(squeezed.sum()) * lane.cellSize * lane.cellSize, 1),
      "places": [placeOf(place) for place in places[:playerReach.problemsListed]],
      "message": (
        f"Bodies smaller than the {name} group's playerHeight ({body.playerHeight:g}) pass here, through less open space than the"
        f" {name} group needs; containment counts it as reached"
      ),
    })
  for line in description["zoneLines"]:
    for box in description["swimBoxes"]:
      if all(line["low"][axis] < box["high"][axis] and box["low"][axis] < line["high"][axis] for axis in range(3)):
        findings.append({
          "finding": "zone line in a swim box", "zoneLine": line["name"], "swimBox": box["name"],
          "message": (
            f"Zone line '{line['name']}' overlaps swim box '{box['name']}': the .wtr lists the swim boxes first and the server takes the first"
            " box holding a point, so it reads their overlap as water"
          ),
        })
  if not description["zoneLines"]:
    findings.append({"finding": "no zone lines", "message": "The zone has no zone lines: players leave it only by ports or death (placeZoneLine)"})
  if not any(seed["kind"] == "zoneIn" for seed in description["seeds"]):
    findings.append({
      "finding": "no zoneIn entries",
      "message": (
        "No zoneIn entry says where a neighbour's zone line lands players (placeEntry kind zoneIn); the lane starts from the safe point and"
        " the landings alone"
      ),
    })
  terms = playerReach.laneTerms(lane, description)
  findings.append({
    "finding": "lane limits", "notModeled": terms["notModeled"], "limits": terms["limits"],
    "message": "What the lane does not model and where it may err: containment is proven within these",
  })
  return findings


def problemCounts(analysis):
  counts = {}
  for problem in analysis["problems"]:
    counts[problem["kind"]] = counts.get(problem["kind"], 0) + 1
  for kind, rest in analysis["moreProblems"].items():
    if kind in playerReach.problemKinds:
      counts[kind] = counts.get(kind, 0) + rest["count"]
  return counts


def containment(inputs, underworld, toolingRoot, reportProgress):
  """Step 5 of a game export: the scene's lanes (playerReach.currentLanes, from bridgeLane.laneInput's file), its leaks and what fails a
  body group a place is built for as failures (playerReach.isFailure), and its findings. Without seeds, or when the lanes cannot be
  computed, a failure saying containment was not checked. Returns (summary for the report, failures, findings, the analysis's nodes
  or None)."""
  if not any(entry["kind"] in playerReach.seedKinds for entry in inputs["entries"]):
    message = (
      "containment not checked: no seeds: the lane starts where players arrive, the safe point (setZoneProperties safePoint) and the stored"
      " zoneIn and landing entries (placeEntry), and the zone has none"
    )
    return message, [{"failure": "containment not checked", "message": message}], [], None
  try:
    lanes, description, status, seconds = playerReach.currentLanes(toolingRoot, inputs["workFile"], inputs["laneInput"], reportProgress)
  except ToolError as error:
    message = f"containment not checked: {error}"
    return message, [{"failure": "containment not checked", "message": message}], [], None
  shown = playerScale.planGroup(playerScale.builtForAt(description["builtFor"], None))
  analysis = playerReach.analyse(lanes, description, shown)
  lane = lanes[shown]
  failures = [leakFailure(problem, underworld) for problem in analysis["problems"] if playerReach.isFailure(problem)]
  for kind in playerReach.leakKinds:
    if kind in analysis["moreProblems"]:
      rest = analysis["moreProblems"][kind]
      said = f"{rest['count']} more {kind} problems than mapReach lists (the largest {playerReach.problemsListed} of each kind)"
      failures.append({"failure": kind, "more": rest, "message": said})
  summary = {
    "problems": problemCounts(analysis), "lane": status, "seconds": seconds, "laneFiles": {name: str(found.path) for name, found in lanes.items()},
    "laneKeys": {name: found.path.stem.removeprefix(f"lane-{name}-") for name, found in lanes.items()},
    "builtFor": list(playerScale.builtForAt(description["builtFor"], None)),
    "reachGrid": playerReach.reachGrid, "constants": playerReach.constantsReport(), "entries": analysis["entries"],
    "notFollowed": description["notFollowed"], "reachedArea": analysis["reachedArea"], "regions": analysis["regions"],
    "zoneLines": analysis["zoneLines"], "plotEntrances": analysis["plotEntrances"], "swimBoxes": analysis["swimBoxes"],
  }
  return summary, failures, laneFindings(lane, description, analysis), analysis["nodes"]


def arrivals(zoneProperties, entries):
  """Where players arrive that the server must find ground under: the safe point, every stored entry, and the landing of every zone line
  leading back into the zone."""
  places = [{"arrival": "safe point", "kind": "safePoint", "at": list(zoneProperties["safePoint"][:3])}] if "safePoint" in zoneProperties else []
  stored = [entry for entry in entries if entry["source"] == "placeEntry" or entry["kind"] == "teleport"]
  return places + [{"arrival": entry["name"], "kind": entry["kind"], "at": entry["at"]} for entry in stored]


def groundFailures(collision, underworld, places):
  """Step 6 of a game export: each arrival over no ground the server finds on its .map. As FindBestZ looks (map.cpp:52-71), from
  findBestZHeightAdjust (serverNav.serverPathing) over the point straight down: the first collision met must lie at or above the
  underworld, which the server ignores at 0."""
  triangles = eqAxes.zoneFromServer(numpy.asarray(collision, dtype=numpy.float64))
  adjust = serverNav.serverPathing["findBestZHeightAdjust"]
  failures = []
  for place in places:
    x, y, z = place["at"]
    heights, _ = playerReach.heightsUnder(triangles, x, y)
    below = heights[heights <= z + adjust]
    ground = round(float(below.max()), 3) if len(below) else None
    if ground is not None and (underworld in (None, 0) or ground >= underworld):
      continue
    met = "nothing" if ground is None else f"collision at {ground:g}, under the underworld {underworld:g}"
    named = "The safe point" if place["kind"] == "safePoint" else f"{place['kind']} '{place['arrival']}'"
    failures.append({"failure": "no .map ground"} | place | {
      "ground": ground,
      "message": (
        f"{named} at {list(place['at'])} stands over no ground the server finds in the .map: looking down from {adjust:g} over it, as"
        f" FindBestZ does, it meets {met}; players arriving there fall"
      ),
    })
  return failures


def zoneRows(zoneProperties, zone, inputs, archiveBytes):
  """Step 7 of a game export: the zone's rows (serverRows.rowsFiles), once the zone row's keys are set and the archive is named by its
  short name; until then the zone row's gaps are the failures. Returns (files or None, why none, failures)."""
  missing = serverRows.missingRowKeys(zoneProperties)
  if missing:
    return None, f"rows not written: the zone row lacks {missing} (setZoneProperties)", []
  if zoneProperties["shortName"] != zone:
    return None, f"rows not written: the archive is not named by the short name '{zoneProperties['shortName']}'", []
  try:
    rows = serverRows.rowsFiles(
      zoneProperties, inputs["zoneLines"], inputs["entries"], inputs["clientZones"], inputs["skyFogColor"], inputs["record"],
      hashlib.sha256(archiveBytes).hexdigest(),
    )
  except ValueError as error:
    why = f"rows not written: {error}"
    return None, why, [{"failure": "rows not written", "message": f"The zone's server rows cannot be written: {error}"}]
  return rows, None, []


def build(checked, zone, zoneProperties, inputs, toolingRoot, reportProgress):
  """Steps 2 to 7 of an export on what the bridge checked and collected: the archive (in memory), and for a game export the server's
  side, its failures and findings added to the report: the map files built from the archive, the lane's containment, the islands'
  reach, the ground under where players arrive, and the rows. inputs, for a game export, are what bridgeLane.laneInput wrote (laneInput)
  for the open file (workFile), getEntries' entries, getZoneLines' zoneLines, the client's registrations (clientZones), the sky's fog
  color (skyFogColor), and the record (readRecord). Nothing is built when the checks found a hard failure: an unsaved file, or what no
  zone file can hold."""
  report = checked["report"]
  game = report["purpose"] == "game"
  report["containment"] = testContainment if not game else "not checked: server files not built"
  built = {
    "report": report, "zoneProperties": zoneProperties, "archive": None, "summary": None, "sides": None, "housing": None, "server": None, "rows": None,
    "rowsWhy": None, "bundle": None, "pieces": None, "clientEntry": None,
  }
  if checked["collected"] is None:
    return built
  collected = checked["collected"]
  built["archive"], built["summary"], built["bundle"] = zoneArchive(collected)
  failures, findings = bundleReport(built["bundle"], built["summary"], game)
  del built["summary"]["ownTextures"]
  report["failures"] += failures
  report["findings"] += findings
  built["sides"], built["housing"] = sideContents(collected, zone), collected["housing"]
  if not game:
    report["findings"] += objectStands(built["archive"], None, built["bundle"]["rowPieces"], False)[1]
    return built
  built["clientEntry"] = inputs["clientEntry"]
  safePoint = zoneProperties["safePoint"][:3] if "safePoint" in zoneProperties else None
  server = built["server"] = serverMaps(built["archive"], safePoint, probeTargets(inputs["entries"]), toolingRoot, reportProgress)
  report["failures"] += server["failures"]
  underworld = zoneProperties.get("underworld")
  report["containment"], failures, findings, nodes = containment(inputs, underworld, toolingRoot, reportProgress)
  report["failures"] += failures
  if server["inspection"] is not None:
    reach = [None] * len(server["inspection"]["islands"]) if nodes is None else islandReach(server["inspection"], nodes, nodes.lane.body.stepHeight)
    for island, reached in zip(server["inspection"]["islands"], reach):
      island["playersReach"] = reached
    report["findings"] += navFindings(server["inspection"])
  report["findings"] += findings
  del nodes
  report["failures"] += groundFailures(server["collision"], underworld, arrivals(zoneProperties, inputs["entries"]))
  report["failures"] += objectStands(built["archive"], server["collision"], built["bundle"]["rowPieces"], True)[0]
  built["rows"], built["rowsWhy"], failures = zoneRows(zoneProperties, zone, inputs, built["archive"])
  report["failures"] += failures
  if built["rows"] is not None:
    try:
      built["pieces"] = pieceRows.piecesFile(zoneProperties, built["bundle"]["rowPieces"], inputs["piecesRecord"], hashlib.sha256(built["archive"]).hexdigest())
    except ValueError as error:
      report["failures"].append({"failure": "rows not written", "message": f"The zone's doors and object rows cannot be written: {error}"})
  return built


def failureName(failure):
  return failure["failure"] + (f" ({failure['object']})" if "object" in failure else "")


def navSummary(inspection, navReport):
  """The nav's inspection without its islands and polygons: the main piece, the island count, and every probe."""
  islands = inspection["islands"]
  return {
    "tiles": len(inspection["tiles"]), "polygons": inspection["polygons"], "excludedPolygons": inspection["excludedPolygons"],
    "mostChunksPerTile": navReport["mostChunksPerTile"], "mainPiece": inspection["mainPiece"], "islandCount": len(islands),
    "islandsAtSnapRisk": sum(island["snapRisk"] for island in islands), "probes": inspection["probes"],
  }


def rowsReport(built, paths):
  rows = built["rows"]
  if rows is None:
    return {"written": False, "message": built["rowsWhy"]}
  return {
    "written": True, "rows": str(paths["rows"]), "waysIn": None if rows["waysIn"] is None else str(paths["waysIn"]),
    "zonePointsWritten": rows["numbersWritten"], "zonePointsDeleted": rows["numbersDeleted"], "waysInToAdd": rows["waysInToAdd"],
    "pieces": None if built["pieces"] is None else {"file": str(paths["pieces"]), "doors": built["pieces"]["doorNumbers"], "objects": len(built["pieces"]["objects"])},
  }


def serverFilesReport(built, folder):
  """A game export's server files for its result: where each was written, the nav's inspection with its largest islands, and the rows,
  or why none were built."""
  if built["server"] is None:
    named = [failureName(failure) for failure in built["report"]["hardFailures"]]
    return {"built": False, "message": f"server files not built: the checks found an unsaved file or what no zone file can hold: {named}"}
  server = built["server"]
  paths = serverFilePaths(folder, built["summary"]["zone"])
  inspection = server["inspection"]
  return {
    "built": True, "files": {kind: str(paths[kind]) for kind, data in server["files"].items() if data is not None},
    "nav": None if inspection is None else navSummary(inspection, server["navReport"]) | {"largestIslands": inspection["islands"][:islandsListed]},
    "rows": rowsReport(built, paths),
  }


def fileRecord(data):
  return {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def manifestRecord(folder, built, files, blendPath, writtenBy):
  """What the manifest records: the purpose, the tool that wrote it (writtenBy: exportZone, or checkExport for a check's files, which are
  no export), the zone's short name, the .blend, every file written (path relative to the folder) with its size and SHA-256, the failure
  and finding counts, and for a game export the zone's identity and template, the client's entry for a zone it does not register
  (eqClientZones.clientEntry: eq-core-dll's ZoneEntry line and eqstr_us.txt's line, by EQEmu's documented route), the zone_points
  numbers written and deleted, the nav settings, the helper, the nav's islands (each with whether players reach it) and probes, and the
  lane: each body group's key, the groups the zone is built for, grid, constants with their sources, and reached area per group."""
  report = built["report"]
  record = {
    "purpose": report["purpose"], "writtenBy": writtenBy, "shortName": built["summary"]["zone"], "blend": blendPath,
    "files": {path.relative_to(folder).as_posix(): fileRecord(data) for path, data in files.items()},
    "failures": len(report["failures"]), "findings": len(report["findings"]),
    "clientModels": built["bundle"]["copies"],
  }
  server = built["server"]
  if server is not None:
    pin = toolingManifest.loadManifest()["recast"]
    zoneProperties = built["zoneProperties"]
    lane = report["containment"]
    record |= {
      "zone": {key: zoneProperties.get(key) for key in ("shortName", "zoneId", "longName", "serverTemplate", "eqstrID")},
      "clientEntry": built["clientEntry"],
      "zonePoints": None if built["rows"] is None else {"written": built["rows"]["numbersWritten"], "deleted": built["rows"]["numbersDeleted"]},
      "pieces": None if built["pieces"] is None else {"doors": built["pieces"]["doorNumbers"], "objects": len(built["pieces"]["objects"])},
      "serverNavSettings": serverNav.serverNavSettings,
      "recastHelper": {"commit": pin["commit"], "fingerprint": recastHelper.helperFingerprint(pin, recastHelper.toolchain()[1])},
      "lane": {key: lane[key] for key in ("laneKeys", "builtFor", "reachGrid", "constants", "reachedArea")} if isinstance(lane, dict) else None,
    }
    inspection = server["inspection"]
    record["nav"] = None if inspection is None else navSummary(inspection, server["navReport"]) | {"islands": inspection["islands"]}
  return record


def writeExport(folder, built, blendPath, writtenBy):
  """Every file of a built export into folder through replaceExportFiles: the archive, its side files, the manifest (naming writtenBy,
  the tool writing it), and this zone's server files (a game export's as built, its rows among them, none for a test export). Returns
  the manifest's path and record, and the server files a test export removed."""
  zone = built["summary"]["zone"]
  archivePath = folder / f"{zone}.eqg"
  serverFiles = dict(built["server"]["files"]) if built["server"] is not None else {}
  if built["rows"] is not None:
    serverFiles["rows"] = built["rows"]["rows"].encode("ascii")
    serverFiles["waysIn"] = None if built["rows"]["waysIn"] is None else built["rows"]["waysIn"].encode("ascii")
    serverFiles["pieces"] = None if built["pieces"] is None else built["pieces"]["text"].encode("utf-8")
  contents = {path: built["sides"][kind] for kind, path in sideFilePaths(folder, zone).items()}
  contents |= {path: serverFiles.get(kind) for kind, path in serverFilePaths(folder, zone).items()}
  removed = [str(path) for path in serverFilePaths(folder, zone).values() if contents[path] is None and path.is_file()]
  written = {archivePath: built["archive"]} | {path: data for path, data in contents.items() if data is not None}
  record = manifestRecord(folder, built, written, blendPath, writtenBy)
  contents[manifestPath(folder, zone)] = json.dumps(record, indent=1).encode("ascii")
  replaceExportFiles(archivePath, built["archive"], contents)
  return {"path": str(manifestPath(folder, zone)), "record": record, "removedServerFiles": removed}


def missingFolders(folders):
  """The folders, and their parents, that do not exist yet, outermost first. A file where a folder must be fails."""
  missing = set()
  for folder in folders:
    for ancestor in (folder, *folder.parents):
      if ancestor.is_dir():
        break
      if ancestor.exists():
        raise NotADirectoryError(f"{ancestor} is a file, where the export needs a folder")
      missing.add(ancestor)
  return sorted(missing, key=lambda path: len(path.parts))


def keptPath(target):
  return target.with_name(target.name + ".previous")


def refuseUnreplaceable(targets):
  """Fail, before anything is written, on a target replaceExportFiles could not take aside and put back: a folder, a read-only file
  (Windows lets it be renamed but not removed), or one whose <target>.previous an export that did not finish left, which may hold that
  export's only copy of the file."""
  for target in targets:
    kept = keptPath(target)
    if kept.exists():
      raise FileExistsError(f"{kept} is left from an export that did not finish and may hold the last export's file: put it back as {target.name} or remove it")
    if target.exists() and not target.is_file():
      raise IsADirectoryError(f"{target} is a folder, where the export writes or removes a file")
    if target.exists() and not os.access(target, os.W_OK):
      raise PermissionError(f"{target} is read-only, where the export replaces or removes a file")


def attempted(action, *arguments):
  """Run one step of putting files back; its failure as text, so the steps after it still run."""
  try:
    action(*arguments)
  except OSError as error:
    return [f"{type(error).__name__}: {error}"]
  return []


def replaceExportFiles(archivePath, archiveBytes, otherContents):
  """Put an export's files in the last export's places: each written whole under a temporary name beside its target (making the folders
  it needs), then the other files taken aside to <target>.previous and replaced (or removed where the content is None), and the archive
  last. What could not be put back (refuseUnreplaceable) fails before anything is written. A failure before the archive is in place
  puts every file back as it was and leaves no temporary file or new folder, its notes naming any step of that which failed. Once the
  archive is in place the files taken aside are removed; one that cannot be fails, saying the export is in place."""
  contents = otherContents | {archivePath: archiveBytes}
  refuseUnreplaceable(contents)
  temporaries = {target: target.with_name(target.name + ".partial") for target, content in contents.items() if content is not None}
  newFolders = missingFolders({target.parent for target in temporaries})
  made, previous, placed = [], {}, []
  try:
    for folder in newFolders:
      folder.mkdir()
      made.append(folder)
    for target, temporary in temporaries.items():
      temporary.write_bytes(contents[target])
    for target in otherContents:
      if target.exists():
        target.replace(keptPath(target))
        previous[target] = keptPath(target)
    for target in otherContents:
      if target in temporaries:
        temporaries[target].replace(target)
        placed.append(target)
    temporaries[archivePath].replace(archivePath)
  except OSError as error:
    unrestored = []
    for target in placed:
      if target not in previous:
        unrestored += attempted(target.unlink)
    for target, kept in previous.items():
      unrestored += attempted(kept.replace, target)
    for temporary in temporaries.values():
      unrestored += attempted(temporary.unlink, True)
    for folder in reversed(made):
      unrestored += attempted(folder.rmdir)
    if unrestored:
      error.add_note("Not put back: " + "; ".join(unrestored))
    raise
  left = []
  for kept in previous.values():
    left += attempted(kept.unlink)
  if left:
    raise OSError(f"Every file of the export is in place, but what it took aside from the last export could not be removed: {'; '.join(left)}")
