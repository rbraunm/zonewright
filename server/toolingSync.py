import hashlib
import http.client
import json
import logging
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from mcp.server.mcpserver.exceptions import ToolError

import blenderProcess
import machineProfile
import recastHelper
import toolingManifest
import toolingStatus

logger = logging.getLogger(__name__)
downloadChunkBytes = 1024 * 1024
downloadProgressSteps = 50
# download.blender.org answers 403 to urllib's default Python-urllib User-Agent.
httpUserAgent = "zonewright"
# Each connect and each read; without it a stalled server holds a tool call for as long as the connection lives.
networkTimeoutSeconds = 60
networkAttempts = 3
networkRetryPauseSeconds = 2
ninjaStepPattern = re.compile(r"^\[(\d+)/(\d+)\]")
buildOutputTailLines = 40


def isTransient(error):
  """A reset, a stall, a cut-off body, or a server error; not a missing file, a refused request, or a bad address."""
  if isinstance(error, urllib.error.HTTPError):
    return error.code >= 500
  if isinstance(error, urllib.error.URLError):
    return isinstance(error.reason, (ConnectionError, TimeoutError))
  return isinstance(error, (ConnectionError, TimeoutError, http.client.IncompleteRead))


def fetchWithRetries(url, attempt):
  """attempt(response) on a fresh response, again after a transient network failure, networkAttempts times in all."""
  request = urllib.request.Request(url, headers={"User-Agent": httpUserAgent})
  for number in range(1, networkAttempts + 1):
    try:
      with urllib.request.urlopen(request, timeout=networkTimeoutSeconds) as response:
        return attempt(response)
    except (urllib.error.URLError, ConnectionError, TimeoutError, http.client.IncompleteRead) as error:
      if not isTransient(error) or number == networkAttempts:
        raise ToolError(f"{url}: download failed{f' after {number} attempts' if number > 1 else ''}: {error!r}") from error
      logger.warning("%s: attempt %s of %s failed (%r); trying again", url, number, networkAttempts, error)
      time.sleep(networkRetryPauseSeconds * number)


def downloadVerified(url, expectedSha256, destinationPath, reportProgress, label):
  logger.info("downloading %s from %s", label, url)

  def attempt(response):
    digest = hashlib.sha256()
    totalBytes = int(response.headers["Content-Length"])
    receivedBytes = 0
    nextReportBytes = 0
    with destinationPath.open("wb") as destination:
      while chunk := response.read(downloadChunkBytes):
        digest.update(chunk)
        destination.write(chunk)
        receivedBytes += len(chunk)
        if receivedBytes >= nextReportBytes:
          reportProgress(receivedBytes, totalBytes, f"downloading {label}")
          nextReportBytes += totalBytes // downloadProgressSteps
    if receivedBytes != totalBytes:
      raise http.client.IncompleteRead(b"", totalBytes - receivedBytes)
    return digest.hexdigest()

  actualSha256 = fetchWithRetries(url, attempt)
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


def downloadUnpinned(url, destinationPath, reportProgress, label):
  """A download no SHA-256 pins, because its server does not promise stable bytes; the caller verifies what it extracts."""
  logger.info("downloading %s from %s", label, url)

  def attempt(response):
    receivedBytes = 0
    with destinationPath.open("wb") as destination:
      while chunk := response.read(downloadChunkBytes):
        destination.write(chunk)
        receivedBytes += len(chunk)
        reportProgress(receivedBytes, None, f"downloading {label}")
    return receivedBytes

  fetchWithRetries(url, attempt)


def fetchRecastTree(toolingRoot, pin, stagingPath, reportProgress):
  """The pinned recastnavigation commit from codeload, only the parts the helper builds from, verified by the tree digest."""
  url = pin["repository"].replace(toolingManifest.githubPrefix, "https://codeload.github.com/", 1) + "/zip/" + pin["commit"]
  archivePath = stagingPath / "recast.zip"
  downloadUnpinned(url, archivePath, reportProgress, f"Recast {pin['commit'][:12]}")
  reportProgress(0, None, "extracting Recast")
  extractedPath = stagingPath / "recast"
  with zipfile.ZipFile(archivePath) as archive:
    names = [name for name in archive.namelist() if not name.endswith("/")]
    tops = {name.split("/", 1)[0] for name in names}
    if len(tops) != 1:
      raise ToolError(f"{url}: expected one top-level folder, found {sorted(tops)}")
    prefix = tops.pop() + "/"
    for name in names:
      relative = name[len(prefix):]
      if any(relative == part or relative.startswith(part + "/") for part in recastHelper.treeParts):
        target = extractedPath / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(archive.read(name))
  digest = recastHelper.treeDigest(extractedPath)
  if digest != pin["treeSha256"]:
    raise ToolError(f"{url}: the extracted tree's digest {digest} does not match pinned treeSha256 {pin['treeSha256']}")
  destination = recastHelper.treePath(toolingRoot, pin)
  destination.parent.mkdir(parents=True, exist_ok=True)
  extractedPath.rename(destination)


def buildRecastHelper(toolingRoot, pin, studio, tree, reportProgress):
  """One cmd /c: vcvars64, then CMake with Ninja, building recastHelper.exe into the install folder of its fingerprint."""
  compiler = recastHelper.compilerLine(studio["compiler"])
  fingerprint = recastHelper.helperFingerprint(pin, compiler)
  root = recastHelper.helpersRoot(toolingRoot)
  root.mkdir(parents=True, exist_ok=True)
  installPath = recastHelper.installPath(toolingRoot, fingerprint)
  started = time.perf_counter()
  with tempfile.TemporaryDirectory(dir=root, prefix="build-") as buildDirectory:
    outputPath = Path(buildDirectory) / "out"
    scriptPath = Path(buildDirectory) / "build.bat"
    scriptPath.write_text("\r\n".join([
      "@echo off",
      f'call "{studio["vcvars"]}" >nul',
      "if errorlevel 1 exit /b 1",
      f'"{studio["cmake"]}" {" ".join(recastHelper.configureArguments)} "-DCMAKE_MAKE_PROGRAM={studio["ninja"]}" "-DRECAST_ROOT={tree}"'
      f' -S "{recastHelper.sourcesPath}" -B "{outputPath}"',
      "if errorlevel 1 exit /b 1",
      f'"{studio["cmake"]}" --build "{outputPath}"',
      "",
    ]), encoding="ascii")
    reportProgress(0, None, "building the Recast helper")
    process = subprocess.Popen(["cmd", "/c", str(scriptPath)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
    output = []
    for rawLine in process.stdout:
      line = rawLine.decode("utf-8", errors="replace").rstrip()
      output.append(line)
      step = ninjaStepPattern.match(line)
      if step:
        reportProgress(int(step.group(1)), int(step.group(2)), "building the Recast helper")
    exitCode = process.wait()
    if exitCode != 0:
      raise ToolError(f"Building the Recast helper failed with exit {exitCode}:\n" + "\n".join(output[-buildOutputTailLines:]))
    compileSeconds = round(time.perf_counter() - started, 1)
    installPath.mkdir(parents=True, exist_ok=True)
    shutil.copy2(outputPath / recastHelper.executableName, installPath / recastHelper.executableName)
  record = {
    "fingerprint": fingerprint, "commit": pin["commit"], "treeSha256": pin["treeSha256"], "compiler": compiler,
    "configureArguments": list(recastHelper.configureArguments), "compileSeconds": compileSeconds,
  }
  (installPath / recastHelper.buildRecordName).write_text(json.dumps(record, indent=2), encoding="utf-8")
  logger.info("built the Recast helper %s in %.1f s with %s", fingerprint[:16], compileSeconds, compiler)
  return {"tool": "recastHelper", "action": "built", "fingerprint": fingerprint, "compileSeconds": compileSeconds}


def syncRecastHelper(toolingRoot, pin, reportProgress):
  """Fetch the pinned Recast tree when it is missing and build the helper when its current fingerprint has no build. Builds of other
  fingerprints stay: worktrees sharing one install must not delete each other's."""
  if recastHelper.helperStatus(toolingRoot, pin)["state"] == "built":
    return []
  studio = recastHelper.visualStudio()
  actions = []
  tree = recastHelper.treePath(toolingRoot, pin)
  if tree.is_dir():
    digest = recastHelper.treeDigest(tree)
    if digest != pin["treeSha256"]:
      raise ToolError(f"{tree} holds a tree whose digest {digest} does not match pinned treeSha256 {pin['treeSha256']}; remove it by hand and rerun")
  else:
    toolingRoot.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=toolingRoot, prefix="staging-") as stagingDirectory:
      fetchRecastTree(toolingRoot, pin, Path(stagingDirectory), reportProgress)
    actions.append({"tool": "recast", "action": "fetched", "commit": pin["commit"]})
  actions.append(buildRecastHelper(toolingRoot, pin, studio, tree, reportProgress))
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
  actions += syncRecastHelper(toolingRoot, manifest["recast"], reportProgress)
  problems = machineProfile.profileProblems(toolingRoot)
  if problems:
    profile = machineProfile.profileMachine(toolingRoot, reportProgress)
    actions.append({"tool": "machineProfile", "action": "profiled", "reasons": problems, "gpuBackend": profile["gpuBackend"], "gpu": profile["gpu"]})
  for action in actions:
    logger.info("sync %s", action)
  return {"actions": actions, "status": toolingStatus.getToolingStatus(toolingRoot)}
