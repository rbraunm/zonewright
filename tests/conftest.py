import ast
import base64
import contextlib
import functools
import hashlib
import json
import msvcrt
import os
import shutil
import struct
import subprocess
import sys
import time
import urllib.request
import zipfile
import zlib
from pathlib import Path

import anyio
import anyio.from_thread
import pytest
from mcp import Client, StdioServerParameters

repositoryRoot = Path(__file__).resolve().parent.parent
downloadCachePath = repositoryRoot / "tests" / ".cache"
repositoryManifest = json.loads((repositoryRoot / "toolingManifest.json").read_text(encoding="ascii"))
pinnedBlender = repositoryManifest["blender"]
pinnedRecast = repositoryManifest["recast"]
everquestClient = os.environ.get("zonewrightEverQuestClient")
if not everquestClient:
  raise pytest.UsageError("the tests need zonewrightEverQuestClient, the EverQuest client folder, set in the shell that runs pytest")
# One install of each pinned Blender in the user's profile, shared by every test session in every worktree.
sharedLocalAppDataPath = Path(os.environ["LOCALAPPDATA"]) / "zonewrightTests" / pinnedBlender["sha256"][:16]
sharedInstallLockSeconds = 900
zoneSurveySkill = Path(".claude") / "skills" / "zone-survey"
readerFiles = {f"server/{name}.py" for name in (
  "eqAnimations", "eqArchive", "eqCubeMaps", "eqEmitterDefinitions", "eqEmitters", "eqLinks", "eqLooks", "eqModels", "eqRaces", "eqSkeletons",
  "eqTerrainTextures", "eqTextures", "eqWorldFile", "eqZones", "eqgFiles", "eqgSkeletons", "eqgTerrain", "loadTimeLight", "viewerLight", "zoneGeometry", "zoneSources",
  "bridgeModels", "emitterAssets", "emitterParticles", "bridgeEmitterDrawing", "eqClientZones", "serverRows",
)}
# What the readers import, and the client's light the imported zones are drawn in.
clientZoneFiles = readerFiles | {f"server/{name}.py" for name in ("bridgeClientLight", "eqAxes", "eqSky", "skyDrawing", "playerScale", "machineProfile", "toolingManifest")}
recastHelperSources = {f"recastHelper/{name}" for name in ("CMakeLists.txt", "main.cpp", "helperIO.h", "helperIO.cpp", "navMode.cpp", "inspectMode.cpp", "reachMode.cpp")}
# The Recast helper as the tests have it built: its sources, the Recast pin, and the sync that builds it.
recastHelperBuild = recastHelperSources | {"toolingManifest.json"} | {f"server/{name}.py" for name in ("recastHelper", "toolingSync", "toolingManifest", "machineProfile")}
serverReferenceFiles = {"tests/serverReference.py", "tests/serverReference.json"}
# The slow tiers, in groups by the files their tests depend on: the code they check and every server module it imports (but the
# plumbing every test runs: the Blender process, the bridge's protocol and command table, the tooling status, and the bridge's modules
# a group does not check), the references they compare against, and the Recast helper's build where they run it; and, found as the
# suite is collected (heavyTestFiles), the test modules holding the group's tests and the test helpers they import. A run of the whole
# suite takes a group only when one of its files changed since the branch left the last pushed claude, or is uncommitted; -m clientData
# or -m install runs a whole tier, and naming a test file runs it.
heavyGroups = {
  "survey": {f"server/{name}.py" for name in (
    "assetSurvey", "assetCatalog", "assetVocabulary", "assetSheets", "zoneSurvey", "surveyFields", "zoneInterpretation", "eqAnimations", "eqArchive",
    "eqCubeMaps", "eqEmitters", "eqLinks", "eqLooks", "eqModels", "eqRaces", "eqSkeletons", "eqTerrainTextures", "eqTextures", "eqWorldFile", "eqZones",
    "eqgFiles", "eqgSkeletons", "eqgTerrain", "loadTimeLight", "zoneGeometry", "zoneSources", "playerScale", "machineProfile", "toolingManifest",
  )} | {(zoneSurveySkill / "SKILL.md").as_posix()},
  "clientFiles": clientZoneFiles,
  "calibration": clientZoneFiles | {f"server/{name}.py" for name in ("eqCalibration", "previewFrame", "clientPointLights", "bridgeViews", "bridgePointLights")},
  "serverMaps": {f"server/{name}.py" for name in (
    "serverMapFiles", "serverMapDrawing", "eqAxes", "eqgFiles", "eqArchive", "playerReach", "laneDrawing", "planDrawing", "playerScale",
    "buildTolerances", "checkpoints", "serverNav", "viewSheets",
  )} | recastHelperBuild | serverReferenceFiles,
  "serverNav": {f"server/{name}.py" for name in ("serverNav", "serverMapFiles", "eqAxes", "eqgFiles", "eqArchive", "serverMapDrawing", "planDrawing", "viewSheets")}
    | recastHelperBuild | serverReferenceFiles,
  "install": {f"server/{name}.py" for name in (
    "toolingSync", "toolingManifest", "toolingStatus", "extensionCatalog", "machineProfile", "machineBenchmark", "blenderProcess",
  )} | recastHelperBuild,
}


class WholeResult(dict):
  """A tool's whole result as its report file holds it, where the tool returned a compact view naming one; `inline` is that view."""

  def __init__(self, whole, inline):
    super().__init__(whole)
    self.inline = inline


def wholeResult(text):
  """A tool result's JSON text as the tool's whole result: the report file it names (resultReports), or itself where it names none."""
  inline = json.loads(text)
  if not isinstance(inline, dict) or "report" not in inline:
    return inline
  return WholeResult(json.loads(Path(inline["report"]).read_text(encoding="utf-8")), inline)


class ToolSession:
  """One MCP client session against a staged server, so state (like the Blender bridge) persists across calls."""

  def __init__(self, client):
    self.client = client

  async def call(self, toolName, arguments=None):
    progressMessages = []

    async def recordProgress(progress, total, message):
      progressMessages.append(message)

    result = await self.client.call_tool(toolName, arguments, progress_callback=recordProgress)
    return result, progressMessages

  async def expectSuccess(self, toolName, arguments=None):
    result, _ = await self.call(toolName, arguments)
    texts = [content.text for content in result.content if content.type == "text"]
    assert result.is_error is False, texts
    assert len(texts) == 1
    return wholeResult(texts[0])

  async def expectImage(self, toolName, arguments=None, mimeType="image/png"):
    result, _ = await self.call(toolName, arguments)
    assert result.is_error is False, [content.text for content in result.content if content.type == "text"]
    assert [content.type for content in result.content] == ["image", "text"]
    assert result.content[0].mime_type == mimeType
    return base64.b64decode(result.content[0].data), wholeResult(result.content[1].text)

  async def expectError(self, toolName, arguments=None):
    result, _ = await self.call(toolName, arguments)
    assert result.is_error is True
    return result.content[0].text


class StagedServer:
  """A copy of the server, and of the zone-survey skill it reads its interpretive procedure from, with its own tooling root. Its tool
  calls share one server process, as a client's calls do, until close; each session gets a process of its own. Its zonewrightEverQuestClient is
  the real client's unless clientFolder names another."""

  def __init__(self, rootPath, manifest, localAppData=None, clientFolder=None):
    self.repositoryPath = rootPath / "repository"
    self.localAppData = localAppData if localAppData is not None else rootPath / "localAppData"
    self.toolingRoot = self.localAppData / "zonewright"
    self.clientFolder = str(clientFolder) if clientFolder is not None else everquestClient
    shutil.copytree(repositoryRoot / "server", self.repositoryPath / "server", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(repositoryRoot / "recastHelper", self.repositoryPath / "recastHelper")
    shutil.copytree(repositoryRoot / zoneSurveySkill, self.repositoryPath / zoneSurveySkill)
    self.writeManifest(manifest)
    self.openContexts = contextlib.ExitStack()
    self.portal = None
    self.client = None

  def connectedClient(self):
    if self.client is None:
      self.portal = self.openContexts.enter_context(anyio.from_thread.start_blocking_portal())
      self.client = self.openContexts.enter_context(self.portal.wrap_async_context_manager(Client(self.serverParameters())))
    return self.client

  def close(self):
    self.openContexts.close()
    self.portal = self.client = None

  @property
  def manifestPath(self):
    return self.repositoryPath / "toolingManifest.json"

  def writeManifest(self, manifest):
    """The manifest, with the repository's Recast pin unless it names one of its own."""
    self.manifestPath.write_text(json.dumps({"recast": pinnedRecast} | manifest), encoding="ascii")

  def readManifest(self):
    return json.loads(self.manifestPath.read_text(encoding="ascii"))

  def serverParameters(self, environment=None):
    """The front (zonewrightFront.py) over a worker of this staged server, as Claude Code runs zonewright."""
    return StdioServerParameters(
      command=sys.executable,
      args=[str(self.repositoryPath / "server" / "zonewrightFront.py")],
      env={"LOCALAPPDATA": str(self.localAppData), "zonewrightEverQuestClient": self.clientFolder} if environment is None else environment,
    )

  def session(self, steps):
    async def run():
      async with Client(self.serverParameters()) as client:
        return await steps(ToolSession(client))
    return anyio.run(run)

  def callTool(self, toolName, arguments=None, environment=None):
    progressMessages = []

    async def recordProgress(progress, total, message):
      progressMessages.append(message)

    async def call():
      async with Client(self.serverParameters(environment)) as client:
        return await client.call_tool(toolName, arguments, progress_callback=recordProgress)

    if environment is None:
      client = self.connectedClient()
      result = self.portal.call(functools.partial(client.call_tool, toolName, arguments, progress_callback=recordProgress))
    else:
      result = anyio.run(call)
    assert len(result.content) == 1
    return result, progressMessages

  def callToolExpectingSuccess(self, toolName, arguments=None):
    result, progressMessages = self.callTool(toolName, arguments)
    assert result.is_error is False, result.content[0].text
    return wholeResult(result.content[0].text), progressMessages

  def callToolExpectingError(self, toolName, arguments=None, environment=None):
    result, _ = self.callTool(toolName, arguments, environment)
    assert result.is_error is True
    return result.content[0].text


def walkOf(result, group="medium"):
  """One body group's walk from a walkRoute result (or a structure's, a graded route's, a prefab entrance's walk)."""
  return result["groups"][group]


def sha256OfFile(filePath):
  digest = hashlib.sha256()
  with filePath.open("rb") as source:
    while chunk := source.read(1024 * 1024):
      digest.update(chunk)
  return digest.hexdigest()


@pytest.fixture(scope="session")
def blenderArchivePin():
  archivePath = downloadCachePath / pinnedBlender["url"].rsplit("/", 1)[1]
  if not archivePath.is_file():
    downloadCachePath.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(pinnedBlender["url"], headers={"User-Agent": "zonewright"})
    with urllib.request.urlopen(request) as response, archivePath.open("wb") as destination:
      shutil.copyfileobj(response, destination)
  assert sha256OfFile(archivePath) == pinnedBlender["sha256"]
  return {"version": pinnedBlender["version"], "url": archivePath.as_uri(), "sha256": pinnedBlender["sha256"]}


@pytest.fixture
def buildExtensionPin(tmp_path):
  def build(extensionID, version):
    archivePath = tmp_path / "extensionArchives" / f"{extensionID}-{version}.zip"
    archivePath.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(archivePath, "w") as archive:
      archive.writestr("blender_manifest.toml", "\n".join([
        'schema_version = "1.0.0"',
        f'id = "{extensionID}"',
        f'version = "{version}"',
        f'name = "{extensionID}"',
        'tagline = "zonewright test extension"',
        'maintainer = "zonewright"',
        'type = "add-on"',
        'blender_version_min = "4.2.0"',
        'license = ["SPDX:GPL-3.0-or-later"]',
        "",
      ]))
      archive.writestr("__init__.py", "def register():\n  pass\n\n\ndef unregister():\n  pass\n")
    return {"version": version, "url": archivePath.as_uri(), "sha256": sha256OfFile(archivePath)}
  return build


@pytest.fixture
def stageServer(tmp_path):
  staged = []

  def stage(manifest):
    staged.append(StagedServer(tmp_path, manifest))
    return staged[-1]
  yield stage
  for server in staged:
    server.close()


@contextlib.contextmanager
def exclusiveLock(lockPath):
  """Held by one process at a time; the system releases it if the holder dies."""
  deadline = time.monotonic() + sharedInstallLockSeconds
  with lockPath.open("a+b") as lockFile:
    while True:
      try:
        msvcrt.locking(lockFile.fileno(), msvcrt.LK_NBLCK, 1)
        break
      except OSError:
        if time.monotonic() > deadline:
          raise TimeoutError(f"Another test session held {lockPath} for {sharedInstallLockSeconds} s")
        time.sleep(1)
    try:
      yield
    finally:
      msvcrt.locking(lockFile.fileno(), msvcrt.LK_UNLCK, 1)


def junction(link, target):
  subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True)


def linkSharedRecast(toolingRoot, sharedToolingRoot):
  for name in ("recast", "recastHelper"):
    junction(toolingRoot / name, sharedToolingRoot / name)


@pytest.fixture(scope="session")
def sharedRecastHelper():
  """The shared install's tooling root, holding the Recast helper built from this worktree's sources: built only when it lacks that
  fingerprint, and never removing the builds of others, which other worktrees use."""
  sys.path.insert(0, str(repositoryRoot / "server"))
  import toolingSync
  sharedLocalAppDataPath.mkdir(parents=True, exist_ok=True)
  with exclusiveLock(sharedLocalAppDataPath / "install.lock"):
    toolingSync.syncRecastHelper(sharedLocalAppDataPath / "zonewright", pinnedRecast, lambda done, of, message: None)
  return sharedLocalAppDataPath / "zonewright"


@pytest.fixture(scope="session")
def recastToolingRoot(tmp_path_factory, sharedRecastHelper):
  """A tooling root of this session's own whose Recast tree and helper builds are the shared install's."""
  toolingRoot = tmp_path_factory.mktemp("recast") / "zonewright"
  toolingRoot.mkdir()
  linkSharedRecast(toolingRoot, sharedRecastHelper)
  return toolingRoot


@pytest.fixture(scope="session")
def sharedLocalAppData(request, tmp_path_factory, sharedRecastHelper):
  """The pinned Blender, this machine's profile, and the Recast helper, synced into the shared install only when it lacks them."""
  sharedLocalAppDataPath.mkdir(parents=True, exist_ok=True)
  with exclusiveLock(sharedLocalAppDataPath / "install.lock"):
    probe = StagedServer(tmp_path_factory.mktemp("sharedProbe"), {"blender": pinnedBlender, "extensions": {}}, sharedLocalAppDataPath)
    status = probe.callToolExpectingSuccess("getToolingStatus")[0]
    probe.close()
    if status["blender"]["state"] != "installed" or status["machineProfile"]["state"] != "current":
      installer = StagedServer(tmp_path_factory.mktemp("sharedInstall"), {"blender": request.getfixturevalue("blenderArchivePin"), "extensions": {}}, sharedLocalAppDataPath)
      installer.callToolExpectingSuccess("syncTooling")
      installer.close()
  return sharedLocalAppDataPath


@pytest.fixture(scope="session")
def installedLocalAppData(tmp_path_factory, sharedLocalAppData):
  """A tooling root of this session's own, so logs and counters stay apart, whose Blender, Recast tree, and helper builds are the shared
  install's."""
  localAppData = tmp_path_factory.mktemp("installed") / "localAppData"
  toolingRoot = localAppData / "zonewright"
  toolingRoot.mkdir(parents=True)
  shutil.copy(sharedLocalAppData / "zonewright" / "machineProfile.json", toolingRoot / "machineProfile.json")
  junction(toolingRoot / "blender", sharedLocalAppData / "zonewright" / "blender")
  linkSharedRecast(toolingRoot, sharedLocalAppData / "zonewright")
  return localAppData


class WarmServer:
  """The session's one staged server, kept running with its Blender between tests."""

  def __init__(self, staged, portal, client):
    self.staged = staged
    self.portal = portal
    self.client = client

  @property
  def toolingRoot(self):
    return self.staged.toolingRoot

  def session(self, steps):
    return self.portal.call(steps, ToolSession(self.client))


@pytest.fixture(scope="session")
def warmServer(tmp_path_factory, installedLocalAppData):
  staged = StagedServer(tmp_path_factory.mktemp("warmServer"), {"blender": pinnedBlender, "extensions": {}}, installedLocalAppData)
  with anyio.from_thread.start_blocking_portal() as portal, portal.wrap_async_context_manager(Client(staged.serverParameters())) as client:
    yield WarmServer(staged, portal, client)


@pytest.fixture
def stageBlenderServer(warmServer):
  """The warm server, its Blender on an empty file as a fresh one starts."""
  warmServer.session(lambda session: session.expectSuccess("newFile", {"discardUnsavedChanges": True}))
  return warmServer


@pytest.fixture
def freshBlenderServer(tmp_path, installedLocalAppData):
  """A server and Blender of the test's own, for what a new process or its files decide: crashes, code changes, the profile."""
  server = StagedServer(tmp_path, {"blender": pinnedBlender, "extensions": {}}, installedLocalAppData)
  yield server
  server.close()


def serverRowValues(shortName, zoneID=901):
  """The zone row's identity, gate, and template, which a game export needs: an id the client does not register unless one is given, with
  the id of a client string the client's eqstr_us.txt does not hold for its entry."""
  return {
    "shortName": shortName, "zoneId": zoneID, "longName": f"The {shortName} test", "timeType": "outdoor", "entryGate": "open", "serverTemplate": "qeynos2",
    "eqstrID": 40001,
  }


def writePNG(path, width, height, rgba):
  """A solid-color RGBA PNG, for test textures."""
  row = b"\x00" + bytes(rgba) * width
  def chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
  header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
  path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(row * height)) + chunk(b"IEND", b""))
  return path


def writeCubeDDS(path, side, faceColors):
  """An uncompressed (A8R8G8B8) DDS cube map of one level, each face one RGBA color, in DDS face order (+X, -X, +Y, -Y, +Z, -Z), as
  the client's env_noswap_day.dds is stored."""
  pixelFormat = struct.pack("<8I", 32, 0x41, 0, 32, 0x00FF0000, 0x0000FF00, 0x000000FF, 0xFF000000)
  header = b"DDS " + struct.pack("<7I", 124, 0x1 | 0x2 | 0x4 | 0x8 | 0x1000, side, side, side * 4, 0, 0) + bytes(44) + pixelFormat
  header += struct.pack("<4I", 0x1000 | 0x8, 0xFE00, 0, 0) + bytes(4)
  path.write_bytes(header + b"".join(bytes((blue, green, red, alpha)) * side * side for red, green, blue, alpha in faceColors))
  return path


def touch(path):
  """Change a file's modification time the way an edit would, without changing its content."""
  later = time.time_ns() + 2_000_000_000
  os.utime(path, ns=(later, later))


def gitOutput(*arguments):
  return subprocess.run(["git", "-C", str(repositoryRoot), *arguments], check=True, capture_output=True, encoding="utf-8").stdout


def heavyGroupOf(item):
  if item.get_closest_marker("install"):
    return "install"
  marker = item.get_closest_marker("clientData")
  if marker is None:
    return None
  if len(marker.args) != 1 or marker.args[0] not in heavyGroups or marker.args[0] == "install":
    raise pytest.UsageError(f"{item.nodeid}: clientData takes one of {sorted(set(heavyGroups) - {'install'})}, got {marker.args!r}")
  return marker.args[0]


@functools.cache
def testHelpersImported(modulePath):
  """The test helpers a test module imports (modules in tests/), but conftest, the plumbing every test runs."""
  helpers = []
  for node in ast.walk(ast.parse(modulePath.read_text(encoding="utf-8"))):
    names = [alias.name for alias in node.names] if isinstance(node, ast.Import) else [node.module] if isinstance(node, ast.ImportFrom) and node.level == 0 else []
    helpers += [repositoryRoot / "tests" / f"{name}.py" for name in names if name != "conftest" and (repositoryRoot / "tests" / f"{name}.py").is_file()]
  return helpers


@functools.cache
def testFilesOf(modulePath):
  """A test module and the test helpers it imports, and those they import in turn, as repository paths."""
  found, waiting = set(), [modulePath]
  while waiting:
    module = waiting.pop()
    if module.relative_to(repositoryRoot).as_posix() not in found:
      found.add(module.relative_to(repositoryRoot).as_posix())
      waiting += testHelpersImported(module)
  return frozenset(found)


def heavyTestFiles(items, groups):
  """Each heavy group's files with the test modules holding its tests and the test helpers they import (testFilesOf)."""
  files = {group: set(named) for group, named in heavyGroups.items()}
  for item in items:
    if groups[item.nodeid] is not None:
      files[groups[item.nodeid]] |= testFilesOf(Path(item.path))
  return files


def pytest_addoption(parser):
  parser.addoption("--everyTier", action="store_true", help="run the client data and install tiers whatever changed")


def pytest_collection_modifyitems(config, items):
  groups = {item.nodeid: heavyGroupOf(item) for item in items}
  wholeSuite = all((config.invocation_params.dir / argument.split("::")[0]).is_dir() for argument in config.args)
  if config.option.markexpr or config.option.everyTier or not wholeSuite:
    return
  base = gitOutput("merge-base", "HEAD", "origin/claude").strip()
  changed = set(gitOutput("diff", "--name-only", base).splitlines()) | set(gitOutput("ls-files", "--others", "--exclude-standard").splitlines())
  groupFiles = heavyTestFiles(items, groups)
  config.heavyGroupReport = []
  for group, files in groupFiles.items():
    touched = sorted(changed & files)
    count = sum(1 for value in groups.values() if value == group)
    config.heavyGroupReport.append(f"{group} ({count} tests): " + (f"run, its code changed: {', '.join(touched)}" if touched else f"left out, its code is as at {base[:8]} (origin/claude)"))
  leftOut = {group for group, files in groupFiles.items() if not changed & files}
  deselected = [item for item in items if groups[item.nodeid] in leftOut]
  if deselected:
    config.hook.pytest_deselected(items=deselected)
    items[:] = [item for item in items if groups[item.nodeid] not in leftOut]


def pytest_terminal_summary(terminalreporter, config):
  for line in getattr(config, "heavyGroupReport", []):
    terminalreporter.write_line(f"heavy tests, {line}")
