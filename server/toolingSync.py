import hashlib
import logging
import shutil
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from mcp.server.mcpserver.exceptions import ToolError

import blenderProcess
import machineProfile
import toolingManifest
import toolingStatus

logger = logging.getLogger(__name__)
downloadChunkBytes = 1024 * 1024
downloadProgressSteps = 50
# download.blender.org answers 403 to urllib's default Python-urllib User-Agent.
httpUserAgent = "zonewright"


def openURL(url):
  request = urllib.request.Request(url, headers={"User-Agent": httpUserAgent})
  try:
    return urllib.request.urlopen(request)
  except urllib.error.URLError as error:
    raise ToolError(f"{url}: download failed: {error}") from error


def downloadVerified(url, expectedSha256, destinationPath, reportProgress, label):
  digest = hashlib.sha256()
  logger.info("downloading %s from %s", label, url)
  with openURL(url) as response, destinationPath.open("wb") as destination:
    totalBytes = int(response.headers["Content-Length"])
    receivedBytes = 0
    nextReportBytes = 0
    while chunk := response.read(downloadChunkBytes):
      digest.update(chunk)
      destination.write(chunk)
      receivedBytes += len(chunk)
      if receivedBytes >= nextReportBytes:
        reportProgress(receivedBytes, totalBytes, f"downloading {label}")
        nextReportBytes += totalBytes // downloadProgressSteps
  if receivedBytes != totalBytes:
    raise ToolError(f"{url}: received {receivedBytes} bytes, expected {totalBytes}")
  actualSha256 = digest.hexdigest()
  if actualSha256 != expectedSha256:
    raise ToolError(f"{url}: SHA-256 {actualSha256} does not match pinned {expectedSha256}")


def extractSingleTopFolder(archivePath, extractPath):
  with zipfile.ZipFile(archivePath) as archive:
    archive.extractall(extractPath)
  topEntries = list(extractPath.iterdir())
  if len(topEntries) != 1 or not topEntries[0].is_dir():
    raise ToolError(f"{archivePath.name}: expected one top-level folder, found {sorted(entry.name for entry in topEntries)}")
  return topEntries[0]


def installBlender(toolingRoot, blenderPin, stagingPath, reportProgress):
  archivePath = stagingPath / "blender.zip"
  downloadVerified(blenderPin["url"], blenderPin["sha256"], archivePath, reportProgress, f"Blender {blenderPin['version']}")
  reportProgress(0, None, f"extracting Blender {blenderPin['version']}")
  extractedPath = extractSingleTopFolder(archivePath, stagingPath / "blender")
  (extractedPath / "portable").mkdir()
  extractedPath.rename(toolingStatus.blenderInstallPath(toolingRoot, blenderPin["version"]))


def syncBlender(toolingRoot, blenderPin, stagingPath, reportProgress):
  pinnedVersion = blenderPin["version"]
  actions = []
  status = toolingStatus.getBlenderStatus(toolingRoot, pinnedVersion)
  if status["state"] == "missing":
    installBlender(toolingRoot, blenderPin, stagingPath, reportProgress)
    actions.append({"tool": "blender", "action": "installed", "version": pinnedVersion})
    status = toolingStatus.getBlenderStatus(toolingRoot, pinnedVersion)
  if status["state"] != "installed":
    raise ToolError(f"Blender {pinnedVersion} at {status['executablePath']} is {status['state']} ({status}); remove it by hand and rerun")
  for version in status["installedVersions"]:
    if version != pinnedVersion:
      shutil.rmtree(toolingStatus.blenderInstallPath(toolingRoot, version))
      actions.append({"tool": "blender", "action": "removed", "version": version})
  return actions


def removeExtension(executablePath, extensionID, version):
  blenderProcess.runBlender(executablePath, ["--command", "extension", "remove", f"user_default.{extensionID}"])
  return {"tool": "extension", "action": "removed", "id": extensionID, "version": version}


def installExtension(executablePath, extensionID, pin, stagingPath, reportProgress):
  archivePath = stagingPath / f"{extensionID}-{pin['version']}.zip"
  downloadVerified(pin["url"], pin["sha256"], archivePath, reportProgress, f"extension {extensionID} {pin['version']}")
  reportProgress(0, None, f"installing extension {extensionID} {pin['version']}")
  blenderProcess.runBlender(executablePath, ["--command", "extension", "install-file", "--repo", "user_default", str(archivePath)])
  return {"tool": "extension", "action": "installed", "id": extensionID, "version": pin["version"]}


def syncExtensions(toolingRoot, blenderVersion, extensionPins, stagingPath, reportProgress):
  executablePath = toolingStatus.blenderInstallPath(toolingRoot, blenderVersion) / "blender.exe"
  status = toolingStatus.getExtensionsStatus(toolingRoot, blenderVersion, extensionPins)
  actions = [removeExtension(executablePath, extensionID, version) for extensionID, version in status["unpinned"].items()]
  for extensionID, extensionStatus in status["pinned"].items():
    if extensionStatus["state"] == "installed":
      continue
    if extensionStatus["state"] == "versionMismatch":
      actions.append(removeExtension(executablePath, extensionID, extensionStatus["installedVersion"]))
    actions.append(installExtension(executablePath, extensionID, extensionPins[extensionID], stagingPath, reportProgress))
  status = toolingStatus.getExtensionsStatus(toolingRoot, blenderVersion, extensionPins)
  unsettled = {extensionID: extensionStatus for extensionID, extensionStatus in status["pinned"].items() if extensionStatus["state"] != "installed"}
  if unsettled or status["unpinned"]:
    raise ToolError(f"Extensions did not settle after sync: pinned {unsettled}, unpinned {status['unpinned']}")
  return actions


def syncTooling(toolingRoot, reportProgress):
  manifest = toolingManifest.loadManifest()
  blenderRoot = toolingRoot / "blender"
  runningBlenders = blenderProcess.findRunningBlenders(blenderRoot)
  if runningBlenders:
    raise ToolError(f"Blender is running from the tooling root; close it before syncing: {', '.join(runningBlenders)}")
  blenderRoot.mkdir(parents=True, exist_ok=True)
  with tempfile.TemporaryDirectory(dir=toolingRoot, prefix="staging-") as stagingDirectory:
    stagingPath = Path(stagingDirectory)
    actions = syncBlender(toolingRoot, manifest["blender"], stagingPath, reportProgress)
    actions += syncExtensions(toolingRoot, manifest["blender"]["version"], manifest["extensions"], stagingPath, reportProgress)
  problems = machineProfile.profileProblems(toolingRoot)
  if problems:
    profile = machineProfile.profileMachine(toolingRoot, reportProgress)
    actions.append({"tool": "machineProfile", "action": "profiled", "reasons": problems, "gpuBackend": profile["gpuBackend"], "gpu": profile["gpu"]})
  for action in actions:
    logger.info("sync %s", action)
  return {"actions": actions, "status": toolingStatus.getToolingStatus(toolingRoot)}
