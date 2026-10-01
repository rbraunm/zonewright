import os
from pathlib import Path

from mcp.server.mcpserver.exceptions import ToolError

import eqArchive
import eqgFiles


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
