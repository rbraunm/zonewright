import hashlib
import json
import logging
import struct

from mcp.server.mcpserver.exceptions import ToolError

import surveyFields
import zoneGeometry
import zoneSources

logger = logging.getLogger(__name__)
cacheFormat = 1
hashChunkBytes = 1024 * 1024


def hashFile(filePath):
  digest = hashlib.sha256()
  with filePath.open("rb") as source:
    while chunk := source.read(hashChunkBytes):
      digest.update(chunk)
  return digest.hexdigest()


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


class SurveyCache:
  """Per zone variant: the SHA-256 of every source file, and each field group's value with the version that produced it."""

  def __init__(self, toolingRoot):
    self.cachePath = toolingRoot / "survey" / "zoneSurvey.json"
    cache = json.loads(self.cachePath.read_text(encoding="utf-8")) if self.cachePath.is_file() else {"cacheFormat": cacheFormat, "variants": {}}
    if cache.get("cacheFormat") != cacheFormat:
      raise ValueError(f"{self.cachePath}: cache format {cache.get('cacheFormat')} is not {cacheFormat}; delete it to rebuild")
    self.variants = cache["variants"]

  def entry(self, key, fileHashes):
    entry = self.variants.get(key)
    if entry is None or entry["fileHashes"] != fileHashes:
      entry = {"fileHashes": fileHashes, "measured": {}, "interpreted": {}}
      self.variants[key] = entry
    return entry

  def save(self):
    self.cachePath.parent.mkdir(parents=True, exist_ok=True)
    self.cachePath.write_text(json.dumps({"cacheFormat": cacheFormat, "variants": self.variants}, indent=1), encoding="utf-8")


def selectVariants(clientRoot, zoneNames):
  variants = zoneSources.discoverZones(clientRoot)
  if zoneNames is None:
    return variants
  unknownZones = sorted(set(zoneNames) - {variant["zone"] for variant in variants.values()})
  if unknownZones:
    raise ToolError(f"Not zones in {clientRoot}: {unknownZones}")
  return {key: variant for key, variant in variants.items() if variant["zone"] in zoneNames}


def staleMeasuredGroups(entry, groupNames):
  return [groupName for groupName in groupNames if entry["measured"].get(groupName, {}).get("version") != surveyFields.measuredGroups[groupName][0]]


def validateMeasuredGroups(groupNames):
  unknownGroups = sorted(set(groupNames) - set(surveyFields.measuredGroups))
  if unknownGroups:
    raise ToolError(f"Unknown measured groups {unknownGroups}; available: {sorted(surveyFields.measuredGroups)}")


def surveyMeasured(clientRoot, toolingRoot, zoneNames, groupNames, reportProgress):
  """Technical lane: bring the named measured groups up to date for the named zones (all when None)."""
  validateMeasuredGroups(groupNames)
  variants = selectVariants(clientRoot, zoneNames)
  cache = SurveyCache(toolingRoot)
  fileHashCache = {}
  results = {}
  for index, (key, source) in enumerate(sorted(variants.items())):
    reportProgress(index, len(variants), f"surveying {key}")
    fileHashes = {}
    for filePath in zoneSources.sourceFiles(clientRoot, source):
      if filePath not in fileHashCache:
        fileHashCache[filePath] = hashFile(filePath)
      fileHashes[filePath.name.lower()] = fileHashCache[filePath]
    entry = cache.entry(key, fileHashes)
    staleGroups = staleMeasuredGroups(entry, groupNames)
    error = None
    if staleGroups:
      logger.info("measuring %s: %s", key, staleGroups)
      # A variant this parser cannot read is reported in its row, and retried next run, rather than aborting the others.
      try:
        entry["measured"].update(surveyFields.measureGroups(zoneGeometry.buildGeometry(clientRoot, source), staleGroups))
      except (ValueError, KeyError, struct.error) as parseError:
        logger.warning("survey of %s failed: %s", key, parseError)
        error = f"{type(parseError).__name__}: {parseError}"
    results[key] = {"zone": source["zone"], "format": source["format"]} | ({"error": error} if error else {groupName: entry["measured"][groupName]["value"] for groupName in groupNames})
  cache.save()
  return results


def readSurvey(toolingRoot, variantKeys):
  """Everything cached for the given variants, both lanes, with each group's version."""
  cache = SurveyCache(toolingRoot)
  return {key: cache.variants.get(key) for key in variantKeys}
