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


class StagedServer:
  def __init__(self, rootPath, manifest):
    self.repositoryPath = rootPath / "repository"
    self.localAppData = rootPath / "localAppData"
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

  def callTool(self, toolName, arguments=None, environment=None):
    progressMessages = []

    async def recordProgress(progress, total, message):
      progressMessages.append(message)

    async def call():
      serverParameters = StdioServerParameters(
        command=sys.executable,
        args=[str(self.repositoryPath / "server" / "zonewrightServer.py")],
        env={"LOCALAPPDATA": str(self.localAppData), "EVERQUEST_CLIENT": everquestClient} if environment is None else environment,
      )
      async with Client(serverParameters) as client:
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
