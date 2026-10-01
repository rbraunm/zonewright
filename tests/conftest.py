import base64
import hashlib
import json
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

import anyio
import pytest
from mcp import Client, StdioServerParameters

repositoryRoot = Path(__file__).resolve().parent.parent
downloadCachePath = repositoryRoot / "tests" / ".cache"
repositoryManifest = json.loads((repositoryRoot / "toolingManifest.json").read_text(encoding="ascii"))
pinnedBlender = repositoryManifest["blender"]
everquestClient = json.loads((repositoryRoot / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]["zonewright"]["env"]["EVERQUEST_CLIENT"]


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
    return json.loads(texts[0])

  async def expectImage(self, toolName, arguments=None):
    result, _ = await self.call(toolName, arguments)
    assert result.is_error is False, [content.text for content in result.content if content.type == "text"]
    assert [content.type for content in result.content] == ["image", "text"]
    assert result.content[0].mime_type == "image/png"
    return base64.b64decode(result.content[0].data), json.loads(result.content[1].text)

  async def expectError(self, toolName, arguments=None):
    result, _ = await self.call(toolName, arguments)
    assert result.is_error is True
    return result.content[0].text


class StagedServer:
  def __init__(self, rootPath, manifest, localAppData=None):
    self.repositoryPath = rootPath / "repository"
    self.localAppData = localAppData if localAppData is not None else rootPath / "localAppData"
    self.toolingRoot = self.localAppData / "zonewright"
    shutil.copytree(repositoryRoot / "server", self.repositoryPath / "server", ignore=shutil.ignore_patterns("__pycache__"))
    self.writeManifest(manifest)

  @property
  def manifestPath(self):
    return self.repositoryPath / "toolingManifest.json"

  def writeManifest(self, manifest):
    self.manifestPath.write_text(json.dumps(manifest), encoding="ascii")

  def readManifest(self):
    return json.loads(self.manifestPath.read_text(encoding="ascii"))

  def serverParameters(self, environment=None):
    return StdioServerParameters(
      command=sys.executable,
      args=[str(self.repositoryPath / "server" / "zonewrightServer.py")],
      env={"LOCALAPPDATA": str(self.localAppData), "EVERQUEST_CLIENT": everquestClient} if environment is None else environment,
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

    result = anyio.run(call)
    assert len(result.content) == 1
    return result, progressMessages

  def callToolExpectingSuccess(self, toolName, arguments=None):
    result, progressMessages = self.callTool(toolName, arguments)
    assert result.is_error is False, result.content[0].text
    return json.loads(result.content[0].text), progressMessages

  def callToolExpectingError(self, toolName, arguments=None, environment=None):
    result, _ = self.callTool(toolName, arguments, environment)
    assert result.is_error is True
    return result.content[0].text


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
  def stage(manifest):
    return StagedServer(tmp_path, manifest)
  return stage


@pytest.fixture(scope="session")
def installedLocalAppData(tmp_path_factory, blenderArchivePin):
  """A tooling root with the pinned Blender synced once per test session, shared by every bridge test."""
  rootPath = tmp_path_factory.mktemp("installed")
  server = StagedServer(rootPath, {"blender": blenderArchivePin, "extensions": {}})
  server.callToolExpectingSuccess("syncTooling")
  return server.localAppData


@pytest.fixture
def stageBlenderServer(tmp_path, blenderArchivePin, installedLocalAppData):
  return StagedServer(tmp_path, {"blender": blenderArchivePin, "extensions": {}}, installedLocalAppData)
