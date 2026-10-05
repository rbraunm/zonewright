import hashlib
import os
from pathlib import Path

from mcp.server.mcpserver.exceptions import ToolError

import eqArchive
import eqgFiles

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


def eqgVariants(archivePath):
  zoneName = archivePath.stem.lower()
  archive = eqArchive.EQArchive(archivePath)
  variants = {}
  for entryName in archive.names():
    if not entryName.endswith(".zon"):
      continue
    zonBytes = archive.read(entryName)
    if zonBytes[:5] == b"EQTZP" and entryName[:-4] + ".dat" in archive.entries:
      variants[f"{zoneName}:eqtzp"] = {"zone": zoneName, "format": "eqtzp", "archive": archivePath, "zon": entryName}
    elif zonBytes[:4] == b"EQGZ":
      variants[f"{zoneName}:eqgz"] = {"zone": zoneName, "format": "eqgz", "archive": archivePath, "zon": entryName}
  return variants


def looseVariants(clientRoot, zonPath):
  zoneName = zonPath.stem.lower()
  archivePath = clientRoot / f"{zonPath.stem}.eqg"
  if zonPath.read_bytes()[:4] == b"EQGZ" and archivePath.is_file():
    return {f"{zoneName}:eqgz:loose": {"zone": zoneName, "format": "eqgz", "archive": archivePath, "zonPath": zonPath}}
  return {}


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

  def animationTracks(self, resource):
    """An animation the archives register by resource name (an .ani entry's name without .ani): its tracks from the first archive
    holding it, or None."""
    entry = resource.lower() + ".ani"
    holder = next((archive for archive in self.archives if entry in archive.entries), None)
    return eqgFiles.parseAnimation(holder.read(entry), f"{holder.archivePath.name}:{entry}") if holder else None
