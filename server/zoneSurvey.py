import json
import logging
import os
import struct
from pathlib import Path

import numpy
from mcp.server.mcpserver.exceptions import ToolError

import eqArchive
import eqgFiles
import eqgTerrain
import eqWorldFile

logger = logging.getLogger(__name__)
surveyCacheVersion = 3


def resolveClientRoot():
  clientRoot = os.environ.get("EVERQUEST_CLIENT")
  if not clientRoot:
    raise ToolError("EVERQUEST_CLIENT is not set; point it at the EverQuest client folder in .mcp.json")
  clientPath = Path(clientRoot)
  if not (clientPath / "eqgame.exe").is_file():
    raise ToolError(f"EVERQUEST_CLIENT '{clientPath}' has no eqgame.exe")
  return clientPath


def discoverZones(clientRoot):
  """Every zone variant in the client, keyed zone:format; a zone can ship both a classic and an EQG version."""
  variants = {}

  def addVariant(zoneName, zoneFormat, source, keySuffix=""):
    variants[f"{zoneName}:{zoneFormat}{keySuffix}"] = {"zone": zoneName, "format": zoneFormat} | source

  for archivePath in sorted(clientRoot.glob("*.s3d")):
    zoneName = archivePath.stem.lower()
    # Character, object, and light archives (name_chr.s3d and so on) never hold a zone; some have malformed directories.
    if "_" in zoneName:
      continue
    entries = eqArchive.EQArchive(archivePath).entries
    # Equipment and sky archives also carry a same-named .wld; only zones pair it with objects.wld or lights.wld.
    if f"{zoneName}.wld" in entries and ("objects.wld" in entries or "lights.wld" in entries):
      addVariant(zoneName, "wld", {"archive": archivePath})
  for archivePath in sorted(clientRoot.glob("*.eqg")):
    zoneName = archivePath.stem.lower()
    archive = eqArchive.EQArchive(archivePath)
    for entryName in archive.names():
      if not entryName.endswith(".zon"):
        continue
      zonBytes = archive.read(entryName)
      if zonBytes[:5] == b"EQTZP" and entryName[:-4] + ".dat" in archive.entries:
        addVariant(zoneName, "eqtzp", {"archive": archivePath, "zon": entryName})
      elif zonBytes[:4] == b"EQGZ":
        addVariant(zoneName, "eqgz", {"archive": archivePath, "zon": entryName})
  for zonPath in sorted(clientRoot.glob("*.zon")):
    zoneName = zonPath.stem.lower()
    archivePath = clientRoot / f"{zonPath.stem}.eqg"
    if zonPath.read_bytes()[:4] == b"EQGZ" and archivePath.is_file():
      addVariant(zoneName, "eqgz", {"archive": archivePath, "zonPath": zonPath}, ":loose")
  return variants


def assetArchivePaths(clientRoot, source):
  """The zone archive plus every .eqg its <zone>_assets.txt names, and the named .eqg files the client lacks."""
  archivePaths = [source["archive"]]
  missingArchives = []
  assetListPath = clientRoot / f"{source['archive'].stem}_assets.txt"
  if assetListPath.is_file():
    for line in assetListPath.read_text(encoding="latin1").splitlines():
      # Extensionless entries are character archives (some misspelled in the client data); zone models live only in .eqg.
      if not line.strip().lower().endswith(".eqg"):
        continue
      archivePath = clientRoot / line.strip()
      if archivePath.is_file():
        archivePaths.append(archivePath)
      else:
        missingArchives.append(line.strip())
  return archivePaths, missingArchives


class ModelLibrary:
  def __init__(self, archivePaths, zoneName):
    self.archives = [eqArchive.EQArchive(archivePath) for archivePath in archivePaths]
    self.zoneName = zoneName
    self.models = {}

  def model(self, modelName):
    if modelName not in self.models:
      holder = next((archive for archive in self.archives if modelName in archive.entries), None)
      self.models[modelName] = eqgFiles.parseModel(holder.read(modelName), f"{self.zoneName}:{modelName}") if holder else None
    return self.models[modelName]


def boundsRecord(points):
  minimum = points.min(0)
  maximum = points.max(0)
  return {"minimum": [round(float(value), 1) for value in minimum], "maximum": [round(float(value), 1) for value in maximum], "size": [round(float(value), 1) for value in maximum - minimum]}


def surveyWLDZone(clientRoot, source):
  zoneName = source["zone"]
  archive = eqArchive.EQArchive(source["archive"])
  meshes = eqWorldFile.WorldFile(archive.read(f"{zoneName}.wld"), zoneName).meshes()
  vertices = numpy.concatenate([mesh["vertices"] for mesh in meshes])
  textureNames = {textureName for mesh in meshes for material in mesh["materials"] for textureName in material["textureNames"]}
  bounds = boundsRecord(vertices)
  return {
    "terrainBounds": bounds,
    "allGeometryBounds": bounds,
    "triangleCount": sum(len(mesh["triangles"]) for mesh in meshes),
    "textureCount": len(textureNames),
    "placementCount": 0,
  }


def surveyPlacedModels(library, placements):
  placedPoints = []
  triangleCount = 0
  textureNames = set()
  missingModels = set()
  for placement in placements:
    model = library.model(placement["model"])
    if model is None:
      missingModels.add(placement["model"])
      continue
    placedPoints.append(eqgFiles.placeVertices(model["vertices"], placement))
    triangleCount += len(model["triangles"])
    textureNames |= {value.lower() for material in model["materials"] for value in material["properties"].values() if isinstance(value, str) and value.lower().endswith(".dds")}
  return placedPoints, triangleCount, textureNames, sorted(missingModels)


def surveyEQGZone(clientRoot, source):
  zoneName = source["zone"]
  archivePaths, missingArchives = assetArchivePaths(clientRoot, source)
  library = ModelLibrary(archivePaths, zoneName)
  zonBytes = source["zonPath"].read_bytes() if "zonPath" in source else library.archives[0].read(source["zon"])
  zone = eqgFiles.parseZone(zonBytes, zoneName)
  placedPoints, triangleCount, textureNames, missingModels = surveyPlacedModels(library, zone["placements"])
  if not placedPoints:
    raise ValueError(f"{zoneName}: none of the {len(zone['placements'])} placements has a model in {[archive.archivePath.name for archive in library.archives]}")
  terrainPoints = surveyPlacedModels(library, [placement for placement in zone["placements"] if placement["model"].endswith(".ter")])[0]
  return {
    "terrainBounds": boundsRecord(numpy.concatenate(terrainPoints)) if terrainPoints else None,
    "allGeometryBounds": boundsRecord(numpy.concatenate(placedPoints)),
    "triangleCount": triangleCount,
    "textureCount": len(textureNames),
    "placementCount": len(zone["placements"]),
    "missingModels": missingModels,
    "missingAssetArchives": missingArchives,
  }


def surveyTerrainZone(clientRoot, source):
  zoneName = source["zone"]
  archivePaths, missingArchives = assetArchivePaths(clientRoot, source)
  library = ModelLibrary(archivePaths, zoneName)
  archive = library.archives[0]
  terrain = eqgTerrain.parseTerrain(archive.read(source["zon"]).decode("latin1"), archive.read(source["zon"][:-4] + ".dat"), zoneName)
  minimum, maximum = eqgTerrain.terrainBounds(terrain)
  terrainPoints = numpy.array([minimum, maximum])
  placedPoints, triangleCount, textureNames, missingModels = surveyPlacedModels(library, terrain["placements"])
  quads = terrain["header"]["quadsPerTile"]
  return {
    "terrainBounds": boundsRecord(terrainPoints),
    "allGeometryBounds": boundsRecord(numpy.concatenate([terrainPoints, *placedPoints])),
    "triangleCount": triangleCount + len(terrain["tiles"]) * quads * quads * 2,
    "textureCount": len(textureNames),
    "placementCount": len(terrain["placements"]),
    "missingModels": missingModels,
    "missingAssetArchives": missingArchives,
    "tileShape": {"quadsPerTile": quads, "unitsPerVertex": terrain["header"]["unitsPerVertex"]},
  }


surveyByFormat = {"wld": surveyWLDZone, "eqgz": surveyEQGZone, "eqtzp": surveyTerrainZone}


def brewallMapPaths(clientRoot, zoneName):
  mapPaths = []
  for mapPath in sorted((clientRoot / "maps" / "Brewall").glob(f"{zoneName}*.txt")):
    layerSuffix = mapPath.stem.lower()[len(zoneName):]
    if layerSuffix == "" or (layerSuffix.startswith("_") and layerSuffix[1:].isdigit()):
      mapPaths.append(mapPath)
  return mapPaths


def readBrewallLabels(clientRoot, zoneName):
  """Place labels from the Brewall map files: design notes only, never geometry or scale."""
  labels = []
  for mapPath in brewallMapPaths(clientRoot, zoneName):
    for line in mapPath.read_text(encoding="latin1").splitlines():
      if not line.startswith("P "):
        continue
      fields = [field.strip() for field in line[2:].split(",", 7)]
      if len(fields) != 8:
        raise ValueError(f"{mapPath.name}: label line has {len(fields)} fields: {line}")
      labels.append({"text": fields[7].replace("_", " "), "mapPosition": [float(value) for value in fields[0:3]], "layer": mapPath.stem})
  return labels


def sourceSignature(clientRoot, source):
  paths = assetArchivePaths(clientRoot, source)[0] + ([source["zonPath"]] if "zonPath" in source else [])
  return [[str(path), path.stat().st_size, path.stat().st_mtime_ns] for path in paths]


def surveyZones(clientRoot, toolingRoot, zoneNames, reportProgress):
  variants = discoverZones(clientRoot)
  if zoneNames is not None:
    unknownZones = sorted(set(zoneNames) - {variant["zone"] for variant in variants.values()})
    if unknownZones:
      raise ToolError(f"Not zones in {clientRoot}: {unknownZones}")
    variants = {key: variant for key, variant in variants.items() if variant["zone"] in zoneNames}
  cachePath = toolingRoot / "survey" / "zoneSurvey.json"
  cache = json.loads(cachePath.read_text(encoding="utf-8")) if cachePath.is_file() else {"version": surveyCacheVersion, "variants": {}}
  if cache["version"] != surveyCacheVersion:
    cache = {"version": surveyCacheVersion, "variants": {}}
  results = {}
  for index, (key, source) in enumerate(sorted(variants.items())):
    signature = sourceSignature(clientRoot, source)
    survey = cache["variants"].get(key)
    if survey is None or survey["signature"] != signature:
      reportProgress(index, len(variants), f"surveying {key}")
      logger.info("surveying %s", key)
      # A variant this parser cannot read is reported in its row, and retried next run, rather than aborting the others.
      try:
        survey = {"signature": signature} | surveyByFormat[source["format"]](clientRoot, source)
        cache["variants"][key] = survey
      except (ValueError, KeyError, struct.error) as error:
        logger.warning("survey of %s failed: %s", key, error)
        survey = {"error": f"{type(error).__name__}: {error}"}
    results[key] = {"zone": source["zone"], "format": source["format"]} | {name: value for name, value in survey.items() if name != "signature"} | {"brewallLabelCount": len(readBrewallLabels(clientRoot, source["zone"]))}
  cachePath.parent.mkdir(parents=True, exist_ok=True)
  cachePath.write_text(json.dumps(cache, indent=1), encoding="utf-8")
  return results
