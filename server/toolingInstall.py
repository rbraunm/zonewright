import hashlib
import shutil
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from mcp.server.mcpserver.exceptions import ToolError

import toolingStatus

downloadChunkBytes = 1024 * 1024
# download.blender.org answers 403 to urllib's default Python-urllib User-Agent.
downloadUserAgent = "zonewright"


def downloadVerified(url, expectedSha256, destinationPath):
  digest = hashlib.sha256()
  request = urllib.request.Request(url, headers={"User-Agent": downloadUserAgent})
  try:
    with urllib.request.urlopen(request) as response, destinationPath.open("wb") as destination:
      while chunk := response.read(downloadChunkBytes):
        digest.update(chunk)
        destination.write(chunk)
  except urllib.error.URLError as error:
    raise ToolError(f"{url}: download failed: {error}") from error
  actualSha256 = digest.hexdigest()
  if actualSha256 != expectedSha256:
    raise ToolError(f"{url}: SHA-256 {actualSha256} does not match pinned {expectedSha256}")


def extractSingleTopFolder(archivePath, stagingPath):
  with zipfile.ZipFile(archivePath) as archive:
    archive.extractall(stagingPath)
  topEntries = list(stagingPath.iterdir())
  if len(topEntries) != 1 or not topEntries[0].is_dir():
    raise ToolError(f"{archivePath.name}: expected one top-level folder, found {sorted(entry.name for entry in topEntries)}")
  return topEntries[0]


def installBlender(toolingRoot):
  blenderManifest = toolingStatus.loadManifest()["blender"]
  pinnedVersion = blenderManifest["version"]
  status = toolingStatus.getBlenderStatus(toolingRoot, pinnedVersion)
  if status["state"] == "installed":
    return {"action": "none", "removedVersions": [], "blender": status}
  if status["state"] != "missing":
    raise ToolError(f"Blender {pinnedVersion} at {status['executablePath']} is {status['state']}; remove it by hand and rerun")
  blenderRoot = toolingRoot / "blender"
  blenderRoot.mkdir(parents=True, exist_ok=True)
  with tempfile.TemporaryDirectory(dir=toolingRoot, prefix="staging-") as stagingDirectory:
    stagingPath = Path(stagingDirectory)
    archivePath = stagingPath / "blender.zip"
    downloadVerified(blenderManifest["url"], blenderManifest["sha256"], archivePath)
    extractedPath = extractSingleTopFolder(archivePath, stagingPath / "extracted")
    extractedPath.rename(blenderRoot / pinnedVersion)
  status = toolingStatus.getBlenderStatus(toolingRoot, pinnedVersion)
  if status["state"] != "installed":
    raise ToolError(f"Blender {pinnedVersion} is {status['state']} after install: {status}")
  removedVersions = [version for version in status["installedVersions"] if version != pinnedVersion]
  for version in removedVersions:
    shutil.rmtree(blenderRoot / version)
  return {"action": "installed", "removedVersions": removedVersions, "blender": toolingStatus.getBlenderStatus(toolingRoot, pinnedVersion)}
