import json
import os
import re
import subprocess
from pathlib import Path

repositoryRoot = Path(__file__).resolve().parent.parent
manifestPath = repositoryRoot / "toolingManifest.json"
versionPattern = re.compile(r"\d+\.\d+\.\d+")
sha256Pattern = re.compile(r"[0-9a-f]{64}")
blenderVersionLinePattern = re.compile(r"^Blender (\d+\.\d+\.\d+)", re.MULTILINE)
blenderVersionTimeoutSeconds = 120


def resolveToolingRoot():
  localAppData = os.environ.get("LOCALAPPDATA")
  if not localAppData:
    raise RuntimeError("LOCALAPPDATA is not set; zonewright keeps its tooling under %LOCALAPPDATA%\\zonewright")
  return Path(localAppData) / "zonewright"


def loadManifest():
  manifest = json.loads(manifestPath.read_text(encoding="ascii"))
  blenderVersion = manifest["blender"]["version"]
  if not versionPattern.fullmatch(blenderVersion):
    raise ValueError(f"{manifestPath.name}: blender.version '{blenderVersion}' is not MAJOR.MINOR.PATCH")
  blenderUrl = manifest["blender"]["url"]
  if not isinstance(blenderUrl, str) or not blenderUrl:
    raise ValueError(f"{manifestPath.name}: blender.url must be a non-empty string")
  blenderSha256 = manifest["blender"]["sha256"]
  if not sha256Pattern.fullmatch(blenderSha256):
    raise ValueError(f"{manifestPath.name}: blender.sha256 '{blenderSha256}' is not 64 lowercase hex digits")
  return manifest


def readBlenderVersion(executablePath):
  completed = subprocess.run(
    [str(executablePath), "--version"],
    capture_output=True,
    encoding="utf-8",
    timeout=blenderVersionTimeoutSeconds,
  )
  if completed.returncode != 0:
    raise RuntimeError(f"{executablePath} --version exited {completed.returncode}: {completed.stderr.strip()}")
  versionMatch = blenderVersionLinePattern.search(completed.stdout)
  if versionMatch is None:
    raise RuntimeError(f"{executablePath} --version printed no 'Blender X.Y.Z' line: {completed.stdout.strip()[:200]}")
  return versionMatch.group(1)


def getBlenderStatus(toolingRoot, pinnedVersion):
  blenderRoot = toolingRoot / "blender"
  installedVersions = sorted(entry.name for entry in blenderRoot.iterdir() if entry.is_dir()) if blenderRoot.is_dir() else []
  executablePath = blenderRoot / pinnedVersion / "blender.exe"
  status = {
    "pinnedVersion": pinnedVersion,
    "executablePath": str(executablePath),
    "installedVersions": installedVersions,
  }
  if pinnedVersion not in installedVersions:
    return status | {"state": "missing"}
  if not executablePath.is_file():
    return status | {"state": "broken"}
  reportedVersion = readBlenderVersion(executablePath)
  if reportedVersion != pinnedVersion:
    return status | {"state": "versionMismatch", "reportedVersion": reportedVersion}
  return status | {"state": "installed"}


def getToolingStatus(toolingRoot):
  manifest = loadManifest()
  return {
    "toolingRoot": str(toolingRoot),
    "blender": getBlenderStatus(toolingRoot, manifest["blender"]["version"]),
  }
