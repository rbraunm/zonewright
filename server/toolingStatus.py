import os
import tomllib
from pathlib import Path

import blenderProcess
import recastHelper
import toolingManifest


def resolveToolingRoot():
  localAppData = os.environ.get("LOCALAPPDATA")
  if not localAppData:
    raise RuntimeError("LOCALAPPDATA is not set; zonewright keeps its tooling under %LOCALAPPDATA%\\zonewright")
  return Path(localAppData) / "zonewright"


def blenderInstallPath(toolingRoot, version):
  return toolingRoot / "blender" / version


def extensionsPath(toolingRoot, blenderVersion):
  return blenderInstallPath(toolingRoot, blenderVersion) / "portable" / "extensions" / "user_default"


def getBlenderStatus(toolingRoot, pinnedVersion):
  blenderRoot = toolingRoot / "blender"
  installedVersions = sorted(entry.name for entry in blenderRoot.iterdir() if entry.is_dir()) if blenderRoot.is_dir() else []
  installPath = blenderInstallPath(toolingRoot, pinnedVersion)
  executablePath = installPath / "blender.exe"
  status = {
    "pinnedVersion": pinnedVersion,
    "executablePath": str(executablePath),
    "installedVersions": installedVersions,
  }
  if pinnedVersion not in installedVersions:
    return status | {"state": "missing"}
  if not executablePath.is_file():
    return status | {"state": "broken", "reason": "blender.exe is missing"}
  if not (installPath / "portable").is_dir():
    return status | {"state": "broken", "reason": "portable folder is missing, so Blender would use the user's own config"}
  reportedVersion = blenderProcess.readBlenderVersion(executablePath)
  if reportedVersion != pinnedVersion:
    return status | {"state": "versionMismatch", "reportedVersion": reportedVersion}
  return status | {"state": "installed"}


def readInstalledExtensions(toolingRoot, blenderVersion):
  repositoryPath = extensionsPath(toolingRoot, blenderVersion)
  if not repositoryPath.is_dir():
    return {}
  installedExtensions = {}
  for entry in repositoryPath.iterdir():
    if entry.name.startswith("."):
      continue
    extensionManifest = tomllib.loads((entry / "blender_manifest.toml").read_text(encoding="utf-8"))
    if extensionManifest["id"] != entry.name:
      raise RuntimeError(f"{entry}: folder name does not match manifest id '{extensionManifest['id']}'")
    installedExtensions[entry.name] = extensionManifest["version"]
  return installedExtensions


def getExtensionsStatus(toolingRoot, blenderVersion, extensionPins):
  installedExtensions = readInstalledExtensions(toolingRoot, blenderVersion)
  pinnedStatus = {}
  for extensionID, pin in sorted(extensionPins.items()):
    status = {"pinnedVersion": pin["version"]}
    if extensionID not in installedExtensions:
      pinnedStatus[extensionID] = status | {"state": "missing"}
    elif installedExtensions[extensionID] != pin["version"]:
      pinnedStatus[extensionID] = status | {"state": "versionMismatch", "installedVersion": installedExtensions[extensionID]}
    else:
      pinnedStatus[extensionID] = status | {"state": "installed"}
  return {
    "pinned": pinnedStatus,
    "unpinned": {extensionID: version for extensionID, version in sorted(installedExtensions.items()) if extensionID not in extensionPins},
  }


def getToolingStatus(toolingRoot):
  manifest = toolingManifest.loadManifest()
  blenderVersion = manifest["blender"]["version"]
  return {
    "toolingRoot": str(toolingRoot),
    "blender": getBlenderStatus(toolingRoot, blenderVersion),
    "extensions": getExtensionsStatus(toolingRoot, blenderVersion, manifest["extensions"]),
    "recastHelper": recastHelper.helperStatus(toolingRoot, manifest["recast"]),
  }
