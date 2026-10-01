import concurrent.futures
import hashlib
import json
import logging
import multiprocessing
import os
import struct
from pathlib import Path

from mcp.server.mcpserver.exceptions import ToolError

import machineProfile
import surveyFields
import zoneGeometry
import zoneSources

logger = logging.getLogger(__name__)
cacheFormat = 2
discoveryExtensions = (".s3d", ".eqg", ".zon")
sourcePathKeys = ("archive", "zonPath")
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
  """Per zone variant: each source file's SHA-256 and each field group's value with the version that produced it; plus a git-style hash memo and the cached zone discovery."""

  def __init__(self, toolingRoot):
    self.cachePath = toolingRoot / "survey" / "zoneSurvey.json"
    cache = json.loads(self.cachePath.read_text(encoding="utf-8")) if self.cachePath.is_file() else {"cacheFormat": cacheFormat}
    if cache["cacheFormat"] != cacheFormat:
      logger.warning("survey cache format %s is not %s; rebuilding it", cache["cacheFormat"], cacheFormat)
      cache = {"cacheFormat": cacheFormat}
    self.variants = cache.get("variants", {})
    self.fileHashes = cache.get("fileHashes", {})
    self.discovery = cache.get("discovery")

  def entry(self, key, fileHashes):
    entry = self.variants.get(key)
    if entry is None or entry["fileHashes"] != fileHashes:
      entry = {"fileHashes": fileHashes, "measured": {}, "interpreted": {}}
      self.variants[key] = entry
    return entry

  def save(self):
    self.cachePath.parent.mkdir(parents=True, exist_ok=True)
    self.cachePath.write_text(json.dumps({"cacheFormat": cacheFormat, "variants": self.variants, "fileHashes": self.fileHashes, "discovery": self.discovery}, indent=1), encoding="utf-8")


def clientListingFingerprint(clientRoot):
  """Name, size, and modification time of every file zone discovery reads; any added, removed, or changed archive changes it."""
  digest = hashlib.sha256()
  for entry in sorted(os.scandir(clientRoot), key=lambda candidate: candidate.name.lower()):
    name = entry.name.lower()
    if entry.is_file() and (name.endswith(discoveryExtensions) or name.endswith("_assets.txt")):
      status = entry.stat()
      digest.update(f"{name}|{status.st_size}|{status.st_mtime_ns}\n".encode("utf-8"))
  return digest.hexdigest()


def discoveredVariants(clientRoot, cache):
  fingerprint = clientListingFingerprint(clientRoot)
  if cache.discovery is not None and cache.discovery["clientRoot"] == str(clientRoot) and cache.discovery["listingFingerprint"] == fingerprint:
    return {key: {name: Path(value) if name in sourcePathKeys else value for name, value in source.items()} for key, source in cache.discovery["variants"].items()}
  variants = zoneSources.discoverZones(clientRoot)
  cache.discovery = {
    "clientRoot": str(clientRoot),
    "listingFingerprint": fingerprint,
    "variants": {key: {name: str(value) if name in sourcePathKeys else value for name, value in source.items()} for key, source in variants.items()},
  }
  return variants


def selectVariants(clientRoot, cache, zoneNames):
  variants = discoveredVariants(clientRoot, cache)
  if zoneNames is None:
    return variants
  unknownZones = sorted(set(zoneNames) - {variant["zone"] for variant in variants.values()})
  if unknownZones:
    raise ToolError(f"Not zones in {clientRoot}: {unknownZones}")
  return {key: variant for key, variant in variants.items() if variant["zone"] in zoneNames}


def hashFiles(paths, cache, verifyHashes):
  """SHA-256 per file, reused from the memo while a file's size and modification time are unchanged (as git does) unless verifyHashes."""
  hashes = {}
  pending = []
  for path in sorted(set(paths)):
    status = path.stat()
    memo = cache.fileHashes.get(str(path))
    if not verifyHashes and memo is not None and memo["size"] == status.st_size and memo["modifiedNanoseconds"] == status.st_mtime_ns:
      hashes[path] = memo["sha256"]
    else:
      pending.append((path, status))
  with concurrent.futures.ThreadPoolExecutor(machineProfile.workerCount()) as pool:
    for (path, status), digest in zip(pending, pool.map(hashFile, [path for path, _ in pending])):
      cache.fileHashes[str(path)] = {"size": status.st_size, "modifiedNanoseconds": status.st_mtime_ns, "sha256": digest}
      hashes[path] = digest
  return hashes


def staleMeasuredGroups(entry, groupNames):
  return [groupName for groupName in groupNames if entry["measured"].get(groupName, {}).get("version") != surveyFields.measuredGroups[groupName][0]]


def validateMeasuredGroups(groupNames):
  unknownGroups = sorted(set(groupNames) - set(surveyFields.measuredGroups))
  if unknownGroups:
    raise ToolError(f"Unknown measured groups {unknownGroups}; available: {sorted(surveyFields.measuredGroups)}")


def measureVariant(clientRoot, source, groupNames):
  """Runs in a worker process. A variant this parser cannot read is reported in its row, and retried next run, rather than aborting the others."""
  try:
    return {"groups": surveyFields.measureGroups(zoneGeometry.buildGeometry(clientRoot, source), groupNames)}
  except (ValueError, KeyError, struct.error) as parseError:
    return {"error": f"{type(parseError).__name__}: {parseError}"}


def surveyMeasured(clientRoot, toolingRoot, zoneNames, groupNames, reportProgress, verifyHashes=False):
  """Technical lane: bring the named measured groups up to date for the named zones (all when None), measuring stale zones in parallel."""
  validateMeasuredGroups(groupNames)
  cache = SurveyCache(toolingRoot)
  variants = selectVariants(clientRoot, cache, zoneNames)
  sourcePaths = {key: zoneSources.sourceFiles(clientRoot, source) for key, source in variants.items()}
  reportProgress(0, None, "checking zone file hashes")
  hashes = hashFiles([path for paths in sourcePaths.values() for path in paths], cache, verifyHashes)
  entries = {key: cache.entry(key, {path.name.lower(): hashes[path] for path in sourcePaths[key]}) for key in variants}
  stale = {key: staleMeasuredGroups(entries[key], groupNames) for key in variants}
  stale = {key: groups for key, groups in stale.items() if groups}
  errors = {}

  def recordOutcome(key, outcome):
    if "error" in outcome:
      logger.warning("survey of %s failed: %s", key, outcome["error"])
      errors[key] = outcome["error"]
    else:
      entries[key]["measured"].update(outcome["groups"])

  # Starting worker processes costs about a second, more than measuring one zone in place.
  if len(stale) == 1:
    key, groups = next(iter(stale.items()))
    recordOutcome(key, measureVariant(clientRoot, variants[key], groups))
  elif stale:
    workers = min(machineProfile.workerCount(), len(stale))
    with concurrent.futures.ProcessPoolExecutor(workers, mp_context=multiprocessing.get_context("spawn")) as pool:
      futures = {pool.submit(measureVariant, clientRoot, variants[key], groups): key for key, groups in stale.items()}
      for done, future in enumerate(concurrent.futures.as_completed(futures), start=1):
        recordOutcome(futures[future], future.result())
        reportProgress(done, len(stale), f"measured {futures[future]}")
  cache.save()
  return {
    key: {"zone": source["zone"], "format": source["format"]} | ({"error": errors[key]} if key in errors else {groupName: entries[key]["measured"][groupName]["value"] for groupName in groupNames})
    for key, source in sorted(variants.items())
  }


def readSurvey(toolingRoot, variantKeys):
  """Everything cached for the given variants, both lanes, with each group's version."""
  cache = SurveyCache(toolingRoot)
  return {key: cache.variants.get(key) for key in variantKeys}
