"""A game export's server side as a deploy request for the claudeperidot ledger's zoneDeploy (claudeperidot/tools/zoneDeploy.py),
and the run of that program. zonewright reaches no server itself: zoneDeploy runs under the ledger's own Python (PyYAML and
facet's client, as claudeperidot/README.md gives them), records every change in the ledger before it
runs, and reports its progress and result as JSON lines.

The request is read from the export as it was written and verified: its game manifest (written by exportZone, no failures),
every file it lists in place with its size and SHA-256, nothing under server\\ it does not list, the rows read back by
serverRows' and pieceRows' strict readers. Only a new zone deploys: an id and a short name the client does not register, the id
above 786 as EQEmu's new-zone process gives it."""
import hashlib
import json
import subprocess
import threading
from pathlib import Path

import eqClientZones
import exportPipeline
import pieceRows
import serverRows

repositoryRoot = Path(__file__).resolve().parent.parent
zoneDeployScript = repositoryRoot / "claudeperidot" / "tools" / "zoneDeploy.py"
# The ledger's interpreter: claudeperidot/README.md runs the ledger with python, which needs PyYAML and facet's client.
ledgerPython = "python"
deployTargets = ("claudeperidot",)
slotServers = ("claudeperidot", "peridot")
hostMaps = "/opt/eqemu/akk-stack/server/maps"
mapKinds = {"map": "base", "water": "water", "nav": "nav"}


def readGameExport(archivePath):
  """(folder, short name, manifest) of the game export at archivePath, every file its manifest lists in place as listed."""
  archivePath = Path(archivePath)
  folder, short = archivePath.parent, archivePath.stem
  manifestPath = exportPipeline.manifestPath(folder, short)
  if not archivePath.is_absolute() or archivePath.suffix != ".eqg" or not manifestPath.is_file():
    raise ValueError(f"'{archivePath}' is not an absolute <short>.eqg with its manifest {manifestPath.name} beside it")
  manifest = json.loads(manifestPath.read_text(encoding="ascii"))
  if (manifest.get("purpose"), manifest.get("writtenBy"), manifest.get("shortName"), manifest.get("failures")) != ("game", "exportZone", short, 0):
    raise ValueError(f"{manifestPath} is not a game export exportZone wrote of {short} with no failures; deploy what exportZone and verifyExport passed")
  for name, record in manifest["files"].items():
    path = folder / name
    if not path.is_file():
      raise ValueError(f"{path}, which the manifest lists, is gone")
    data = path.read_bytes()
    if (len(data), hashlib.sha256(data).hexdigest()) != (record["bytes"], record["sha256"]):
      raise ValueError(f"{path} is not the file the manifest lists (size or SHA-256 differ): export again")
  listed = {folder / name for name in manifest["files"]}
  unlisted = sorted(str(path) for path in (folder / "server").rglob("*") if path.is_file() and path not in listed)
  if unlisted:
    raise ValueError(f"the export's server folder holds files its manifest does not list: {unlisted}")
  return folder, short, manifest


def requireNewZone(zone, clientZones):
  """A deployed zone takes its own slot: an id and short name the client does not register, the id above 786."""
  if zone["zoneId"] in clientZones.byID or zone["shortName"] in clientZones.byShortName:
    raise ValueError(
      f"{zone['shortName']} (zone {zone['zoneId']}) takes a slot the client registers: a deployed zone gets its own id and short name,"
      " never an existing slot (setZoneProperties)"
    )
  if not eqClientZones.lowestNewZoneID <= zone["zoneId"] <= eqClientZones.highestZoneID or zone["zoneId"] == eqClientZones.runtimeZoneID:
    raise ValueError(f"zone {zone['zoneId']} is no new zone's id: {eqClientZones.lowestNewZoneID} to {eqClientZones.highestZoneID} but not {eqClientZones.runtimeZoneID}")


def deployRequest(archivePath, clientZones, target, planStep, author, ownerClearance, disposition):
  """zoneDeploy's deploy request for the game export at archivePath, and what of it the deploy leaves out, with why."""
  if target not in deployTargets:
    raise ValueError(f"a deploy's target is one of {list(deployTargets)}: Peridot is never written, got {target!r}")
  folder, short, manifest = readGameExport(archivePath)
  identity = manifest["zone"]
  zone = {"shortName": identity["shortName"], "zoneId": identity["zoneId"], "longName": identity["longName"], "template": identity["serverTemplate"]}
  requireNewZone(zone, clientZones)
  paths = exportPipeline.serverFilePaths(folder, short)
  archiveSHA256 = manifest["files"][f"{short}.eqg"]["sha256"]
  rows = serverRows.readRows(paths["rows"].read_text(encoding="ascii"), paths["rows"].name)
  if (rows["shortName"], rows["archiveSHA256"], rows["template"]) != (short, archiveSHA256, zone["template"]):
    raise ValueError(f"{paths['rows'].name} is not the rows of this export's archive and template")
  pieces = {"doors": [], "objects": []}
  if paths["pieces"].is_file():
    pieces = pieceRows.readPieces(paths["pieces"].read_text(encoding="utf-8"), paths["pieces"].name)
    if (pieces["shortName"], pieces["archiveSHA256"]) != (short, archiveSHA256):
      raise ValueError(f"{paths['pieces'].name} is not the pieces of this export's archive")
  files = []
  for kind, subfolder in mapKinds.items():
    path = paths[kind]
    if f"server/maps/{subfolder}/{path.name}" not in manifest["files"]:
      raise ValueError(f"the export holds no {path.name}: a game export writes the .map, .wtr and .nav")
    files.append({"path": f"{hostMaps}/{subfolder}/{path.name}", "source": str(path), "sha256": manifest["files"][f"server/maps/{subfolder}/{path.name}"]["sha256"]})
  insert = rows["zoneInsert"]
  request = {
    "command": "deploy", "target": target, "zone": zone,
    "rows": {"zone": {"written": insert["written"], "copied": insert["copied"]}, "zonePoints": rows["zonePoints"], "doors": pieces["doors"], "objects": pieces["objects"]},
    "files": files, "export": {"manifest": str(exportPipeline.manifestPath(folder, short)), "archiveSHA256": archiveSHA256},
    "planStep": planStep, "author": author, "ownerClearance": ownerClearance, "disposition": disposition,
  }
  left = []
  if paths["waysIn"].is_file():
    left.append({"file": str(paths["waysIn"]), "why": "it rewrites neighbours' zone_points rows, outside the zone; the owner decides those"})
  housing = exportPipeline.sideFilePaths(folder, short)["housing"]
  if housing.is_file():
    left.append({"file": str(housing), "why": "no server reads plot content yet: claudeperidot's database holds no housing tables"})
  return request, left


def runZoneDeploy(request, reportProgress):
  """Runs zoneDeploy with request, handing each progress line to reportProgress(progress, total, message); its result, or a
  ValueError with its error."""
  process = subprocess.Popen(
    [ledgerPython, str(zoneDeployScript)], cwd=repositoryRoot, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    text=True, encoding="utf-8",
  )
  errors = []
  drain = threading.Thread(target=lambda: errors.append(process.stderr.read()))
  drain.start()
  process.stdin.write(json.dumps(request))
  process.stdin.close()
  outcome = None
  for line in process.stdout:
    message = json.loads(line)
    if "progress" in message:
      reportProgress(message["progress"], message["total"], message["message"])
    else:
      outcome = message
  process.wait()
  drain.join()
  if outcome is None or "error" in outcome or process.returncode != 0:
    said = outcome["error"] if outcome is not None and "error" in outcome else f"exit {process.returncode}: {''.join(errors).strip()[-2000:]}"
    raise ValueError(f"zoneDeploy failed: {said}")
  return outcome["result"]
