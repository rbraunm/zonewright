import hashlib
import os
from pathlib import Path

from mcp.server.mcpserver.exceptions import ToolError

import eqArchive
import eqgFiles
import eqgTerrain

listingExtensions = (".s3d", ".eqg", ".zon")


def resolveClientRoot():
  clientRoot = os.environ.get("EVERQUEST_CLIENT")
  if not clientRoot:
    raise ToolError("EVERQUEST_CLIENT is not set; point it at the EverQuest client folder in .mcp.json")
  clientPath = Path(clientRoot)
  if not (clientPath / "eqgame.exe").is_file():
    raise ToolError(f"EVERQUEST_CLIENT '{clientPath}' has no eqgame.exe")
  return clientPath


def clientListingFingerprint(clientRoot):
  """Name, size, and modification time of every archive, zone file, and asset list; any added, removed, or changed file changes it."""
  digest = hashlib.sha256()
  for entry in sorted(os.scandir(clientRoot), key=lambda candidate: candidate.name.lower()):
    name = entry.name.lower()
    if entry.is_file() and (name.endswith(listingExtensions) or name.endswith("_assets.txt")):
      status = entry.stat()
      digest.update(f"{name}|{status.st_size}|{status.st_mtime_ns}\n".encode("utf-8"))
  return digest.hexdigest()


def wldVariants(archivePath):
  zoneName = archivePath.stem.lower()
  # Character, object, and light archives (name_chr.s3d and so on) never hold a zone; some have malformed directories.
  if "_" in zoneName:
    return {}
  entries = eqArchive.EQArchive(archivePath).entries
  # Equipment and sky archives also carry a same-named .wld; only zones pair it with objects.wld or lights.wld.
  if f"{zoneName}.wld" in entries and ("objects.wld" in entries or "lights.wld" in entries):
    return {f"{zoneName}:wld": {"zone": zoneName, "format": "wld", "archive": archivePath}}
  return {}


def terrainDataFile(zonBytes, archive, sourceName):
  """The .dat an EQ terrain .zon loads from the archive, or None when the archive lacks it and the project cannot load."""
  dataName = eqgTerrain.dataFileName(zonBytes.decode("latin1"), sourceName)
  return dataName if dataName in archive.entries else None


def eqgVariants(archivePath):
  zoneName = archivePath.stem.lower()
  archive = eqArchive.EQArchive(archivePath)
  variants = {}
  for entryName in archive.names():
    if not entryName.endswith(".zon"):
      continue
    zonBytes = archive.read(entryName)
    if zonBytes[:5] == b"EQTZP":
      dataName = terrainDataFile(zonBytes, archive, f"{archivePath.name}:{entryName}")
      if dataName is not None:
        variants[f"{zoneName}:eqtzp"] = {"zone": zoneName, "format": "eqtzp", "archive": archivePath, "zon": entryName, "dat": dataName}
    elif zonBytes[:4] == b"EQGZ":
      variants[f"{zoneName}:eqgz"] = {"zone": zoneName, "format": "eqgz", "archive": archivePath, "zon": entryName}
  return variants


def looseVariants(clientRoot, zonPath):
  """The zone a loose <archive>.zon makes, which the client loads over the archive's own (EQGraphicsDX9.dll 0x10066230): an EQG zone
  (EQGZ), or an EQ terrain project (EQTZP) whose .dat its *NAME names in the archive."""
  zoneName = zonPath.stem.lower()
  archivePath = clientRoot / f"{zonPath.stem}.eqg"
  if not archivePath.is_file():
    return {}
  zonBytes = zonPath.read_bytes()
  if zonBytes[:4] == b"EQGZ":
    return {f"{zoneName}:eqgz:loose": {"zone": zoneName, "format": "eqgz", "archive": archivePath, "zonPath": zonPath}}
  if zonBytes[:5] == b"EQTZP":
    dataName = terrainDataFile(zonBytes, eqArchive.EQArchive(archivePath), zonPath.name)
    if dataName is not None:
      return {f"{zoneName}:eqtzp:loose": {"zone": zoneName, "format": "eqtzp", "archive": archivePath, "zonPath": zonPath, "dat": dataName}}
  return {}


def terrainFiles(source):
  """An EQ terrain variant's .zon text, the loose one when the client has it, and its .dat bytes."""
  archive = eqArchive.EQArchive(source["archive"])
  zonBytes = source["zonPath"].read_bytes() if "zonPath" in source else archive.read(source["zon"])
  return zonBytes.decode("latin1"), archive.read(source["dat"])


def discoverZones(clientRoot):
  """Every zone variant in the client, keyed zone:format; a zone can ship both a classic and an EQG version."""
  variants = {}
  for archivePath in sorted(clientRoot.glob("*.s3d")):
    variants |= wldVariants(archivePath)
  for archivePath in sorted(clientRoot.glob("*.eqg")):
    variants |= eqgVariants(archivePath)
  for zonPath in sorted(clientRoot.glob("*.zon")):
    variants |= looseVariants(clientRoot, zonPath)
  return variants


def zoneVariants(clientRoot, zoneName):
  """The variants of one zone, reading only its own files."""
  variants = {}
  for finder, path in ((wldVariants, clientRoot / f"{zoneName}.s3d"), (eqgVariants, clientRoot / f"{zoneName}.eqg")):
    if path.is_file():
      variants |= finder(path)
  zonPath = clientRoot / f"{zoneName}.zon"
  if zonPath.is_file():
    variants |= looseVariants(clientRoot, zonPath)
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


def sourceFiles(clientRoot, source):
  """Every file a variant's survey reads."""
  paths = list(assetArchivePaths(clientRoot, source)[0])
  if "zonPath" in source:
    paths.append(source["zonPath"])
  if source["format"] == "wld":
    objectArchive = clientRoot / f"{source['zone']}_obj.s3d"
    if objectArchive.is_file():
      paths.append(objectArchive)
  return paths


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
