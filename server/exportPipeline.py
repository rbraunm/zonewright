"""An export's files from what the bridge checked and collected, and their writing. Both purposes write the EQG archive, its client side
files, and the manifest (<zone>_export.json: every file with its SHA-256 and size). A game export also builds the server's map files
from the archive's bytes, under server\\maps laid out as akk-stack's server/maps so the owner copies it whole: the collision map
(.map), the region map (.wtr), and the nav mesh (.nav), which is inspected as the server loads it for NPC islands and path probes. A
test export removes this zone's server files, which would no longer match its archive. Every file goes in together, or none does."""
import hashlib
import json
import os

from mcp.server.mcpserver.exceptions import ToolError

import eqEmitters
import eqgExport
import eqgFiles
import recastHelper
import serverMapFiles
import serverNav
import toolingManifest

islandsListed = 25


def serverFilePaths(folder, zone):
  """This zone's files under <folder>\\server by kind, each named by the lowercase short name (the nav loader does not lowercase,
  pathfinder_interface.cpp:10)."""
  maps, sql = folder / "server" / "maps", folder / "server" / "sql"
  return {
    "map": maps / "base" / f"{zone}.map", "water": maps / "water" / f"{zone}.wtr", "nav": maps / "nav" / f"{zone}.nav",
    "rows": sql / f"{zone}.sql", "waysIn": sql / f"{zone}_waysIn.sql",
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


def sideContents(collected, zone):
  """The client side files' bytes by kind. A list left from an earlier export would place emitters or plots this scene no longer has,
  so a list with nothing to say is None, and is removed."""
  housing = collected["housing"]
  hasPlots = housing is not None and bool(housing["plots"])
  return {
    "emitterList": eqEmitters.emitterListText(collected["emitters"]).encode("latin1") if collected["emitters"] else None,
    "housing": json.dumps({"zone": zone} | housing, indent=1).encode("ascii") if hasPlots else None,
    "assetList": "".join(f"{archive}\r\n" for archive in housing["assets"]).encode("latin1") if hasPlots else None,
  }


def zoneArchive(collected):
  try:
    return eqgExport.zoneArchive(collected)
  except (OSError, ValueError) as error:
    raise ToolError(f"{type(error).__name__}: {error}") from error


def serverMaps(archiveBytes, safePoint, toolingRoot, reportProgress):
  """The server's map files from an archive's bytes: the .map and .wtr from its files, then the .nav map_edit would build from them,
  inspected as the server loads it, with NPC path probes from the safe point (zone axes, or None) to each zone line's center. A nav
  the helper refuses is a failure and leaves no .nav. Returns {files by kind, inspection, helper report, failures}."""
  try:
    zoneFiles = serverMapFiles.zoneFilesOf(archiveBytes)
    regions = eqgFiles.parseZone(zoneFiles["zon"], "the .zon")["regions"]
    mapBytes = serverMapFiles.mapBytes(zoneFiles)
    waterBytes = serverMapFiles.waterBytes(regions)
  except ValueError as error:
    raise ToolError(f"The server's map files cannot be built from this archive: {error}") from error
  built = {"files": {"map": mapBytes, "water": waterBytes, "nav": None}, "inspection": None, "navReport": None, "failures": []}
  try:
    navBytes, built["navReport"] = serverNav.navBytes(mapBytes, waterBytes, toolingRoot, reportProgress)
  except (ToolError, ValueError) as error:
    built["failures"].append({"failure": "nav not built", "message": f"The server's nav mesh cannot be built: {error}"})
    return built
  built["files"]["nav"] = navBytes
  targets = [{"name": region["name"], "point": list(region["center"])} for region in regions if eqgFiles.isZoneLine(region["name"])]
  try:
    built["inspection"] = serverNav.inspectNav(navBytes, safePoint, targets, toolingRoot, reportProgress)
  except ValueError as error:
    raise ToolError(f"The nav built from this archive cannot be inspected: {error}") from error
  return built


def navFindings(inspection):
  islands = inspection["islands"]
  findings = [{"finding": "NPC nav", "message": message} for message in inspection["findings"]]
  if islands:
    atRisk = sum(island["snapRisk"] for island in islands)
    findings.append({
      "finding": "NPC islands", "islands": len(islands), "atSnapRisk": atRisk,
      "message": f"{len(islands)} pieces of the NPC nav lie apart from its main piece, {atRisk} where the server's nearest-polygon search can"
        f" put an NPC onto one; serverFiles.nav lists the {islandsListed} largest, the manifest every one",
    })
  return findings


def build(checked, zone, zoneProperties, toolingRoot, reportProgress):
  """Steps 2 to 4 of an export on what the bridge checked and collected: the archive (in memory), and for a game export the server's
  map files built from it, their failures and findings added to the report. Nothing is built when the checks found a hard failure:
  an unsaved file, or what no zone file can hold."""
  report = checked["report"]
  built = {"report": report, "archive": None, "summary": None, "sides": None, "housing": None, "server": None}
  if checked["collected"] is None:
    return built
  collected = checked["collected"]
  built["archive"], built["summary"] = zoneArchive(collected)
  built["sides"], built["housing"] = sideContents(collected, zone), collected["housing"]
  if report["purpose"] == "game":
    safePoint = zoneProperties["safePoint"][:3] if "safePoint" in zoneProperties else None
    built["server"] = serverMaps(built["archive"], safePoint, toolingRoot, reportProgress)
    report["failures"] += built["server"]["failures"]
    if built["server"]["inspection"] is not None:
      report["findings"] += navFindings(built["server"]["inspection"])
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


def serverFilesReport(built, folder):
  """A game export's server files for its result: where each was written and the nav's inspection with its largest islands, or why
  none were built."""
  if built["server"] is None:
    named = [failureName(failure) for failure in built["report"]["hardFailures"]]
    return {"built": False, "message": f"server files not built: the checks found an unsaved file or what no zone file can hold: {named}"}
  server = built["server"]
  paths = serverFilePaths(folder, built["summary"]["zone"])
  inspection = server["inspection"]
  return {
    "built": True, "files": {kind: str(paths[kind]) for kind, data in server["files"].items() if data is not None},
    "nav": None if inspection is None else navSummary(inspection, server["navReport"]) | {"largestIslands": inspection["islands"][:islandsListed]},
  }


def fileRecord(data):
  return {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def manifestRecord(folder, built, files, blendPath):
  """What the manifest records: the purpose, the zone's short name, the .blend, every file written (path relative to the folder) with
  its size and SHA-256, the failure and finding counts, and for a game export the nav settings, the helper, and the nav's islands and
  probes."""
  report = built["report"]
  record = {
    "purpose": report["purpose"], "shortName": built["summary"]["zone"], "blend": blendPath,
    "files": {path.relative_to(folder).as_posix(): fileRecord(data) for path, data in files.items()},
    "failures": len(report["failures"]), "findings": len(report["findings"]),
  }
  server = built["server"]
  if server is not None:
    pin = toolingManifest.loadManifest()["recast"]
    record |= {"serverNavSettings": serverNav.serverNavSettings, "recastHelper": {"commit": pin["commit"], "fingerprint": recastHelper.helperFingerprint(pin, recastHelper.toolchain()[1])}}
    inspection = server["inspection"]
    record["nav"] = None if inspection is None else navSummary(inspection, server["navReport"]) | {"islands": inspection["islands"]}
  return record


def writeExport(folder, built, blendPath):
  """Every file of a built export into folder through replaceExportFiles: the archive, its side files, the manifest, and this zone's
  server files (a game export's as built, none for a test export). Returns the manifest's path and record, and the server files a
  test export removed."""
  zone = built["summary"]["zone"]
  archivePath = folder / f"{zone}.eqg"
  serverFiles = built["server"]["files"] if built["server"] is not None else {}
  contents = {path: built["sides"][kind] for kind, path in sideFilePaths(folder, zone).items()}
  contents |= {path: serverFiles.get(kind) for kind, path in serverFilePaths(folder, zone).items()}
  removed = [str(path) for path in serverFilePaths(folder, zone).values() if contents[path] is None and path.is_file()]
  written = {archivePath: built["archive"]} | {path: data for path, data in contents.items() if data is not None}
  record = manifestRecord(folder, built, written, blendPath)
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
