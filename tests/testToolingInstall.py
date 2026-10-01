import hashlib
import io
import json
import shutil
import sys
import zipfile
from pathlib import Path

import anyio
import pip
from mcp import Client, StdioServerParameters

repositoryRoot = Path(__file__).resolve().parent.parent
pinnedBlenderVersion = json.loads((repositoryRoot / "toolingManifest.json").read_text(encoding="ascii"))["blender"]["version"]
launcherPath = Path(pip.__file__).parent / "_vendor" / "distlib" / "t64.exe"


def buildFakeBlenderExecutable(reportedVersion):
  scriptArchive = io.BytesIO()
  with zipfile.ZipFile(scriptArchive, "w") as archive:
    archive.writestr("__main__.py", f"print('Blender {reportedVersion}')\n")
  shebang = f'#!"{sys.executable}"\n'.encode("utf-8")
  return launcherPath.read_bytes() + shebang + scriptArchive.getvalue()


def buildBlenderArchive(archivePath, reportedVersion):
  with zipfile.ZipFile(archivePath, "w") as archive:
    archive.writestr("blender-fake-windows-x64/blender.exe", buildFakeBlenderExecutable(reportedVersion))
    archive.writestr("blender-fake-windows-x64/readme.txt", "fake\n")
  return hashlib.sha256(archivePath.read_bytes()).hexdigest()


def stageServer(tmp_path, url, sha256):
  serverRoot = tmp_path / "repository"
  shutil.copytree(repositoryRoot / "server", serverRoot / "server", ignore=shutil.ignore_patterns("__pycache__"))
  manifest = {"blender": {"version": pinnedBlenderVersion, "url": url, "sha256": sha256}}
  (serverRoot / "toolingManifest.json").write_text(json.dumps(manifest), encoding="ascii")
  return serverRoot / "server" / "zonewrightServer.py"


async def callTool(serverPath, localAppData, toolName):
  serverParameters = StdioServerParameters(
    command=sys.executable,
    args=[str(serverPath)],
    env={"LOCALAPPDATA": str(localAppData)},
  )
  async with Client(serverParameters) as client:
    return await client.call_tool(toolName)


def callInstallBlender(serverPath, localAppData):
  result = anyio.run(callTool, serverPath, localAppData, "installBlender")
  assert len(result.content) == 1
  return result


def testInstallFromVerifiedArchive(tmp_path):
  archivePath = tmp_path / "blender.zip"
  sha256 = buildBlenderArchive(archivePath, pinnedBlenderVersion)
  serverPath = stageServer(tmp_path, archivePath.as_uri(), sha256)
  localAppData = tmp_path / "localAppData"
  result = callInstallBlender(serverPath, localAppData)
  assert result.is_error is False
  installResult = json.loads(result.content[0].text)
  assert installResult["action"] == "installed"
  assert installResult["removedVersions"] == []
  assert installResult["blender"]["state"] == "installed"
  toolingRoot = localAppData / "zonewright"
  assert (toolingRoot / "blender" / pinnedBlenderVersion / "readme.txt").read_text() == "fake\n"
  assert sorted(entry.name for entry in toolingRoot.iterdir()) == ["blender"]


def testUpgradeRemovesOtherVersions(tmp_path):
  archivePath = tmp_path / "blender.zip"
  sha256 = buildBlenderArchive(archivePath, pinnedBlenderVersion)
  serverPath = stageServer(tmp_path, archivePath.as_uri(), sha256)
  localAppData = tmp_path / "localAppData"
  blenderRoot = localAppData / "zonewright" / "blender"
  (blenderRoot / "0.0.1").mkdir(parents=True)
  (blenderRoot / "0.0.1" / "blender.exe").write_bytes(buildFakeBlenderExecutable("0.0.1"))
  installResult = json.loads(callInstallBlender(serverPath, localAppData).content[0].text)
  assert installResult["action"] == "installed"
  assert installResult["removedVersions"] == ["0.0.1"]
  assert installResult["blender"]["installedVersions"] == [pinnedBlenderVersion]
  assert sorted(entry.name for entry in blenderRoot.iterdir()) == [pinnedBlenderVersion]


def testAlreadyInstalledDoesNothing(tmp_path):
  missingArchive = tmp_path / "absent.zip"
  serverPath = stageServer(tmp_path, missingArchive.as_uri(), "0" * 64)
  localAppData = tmp_path / "localAppData"
  installedPath = localAppData / "zonewright" / "blender" / pinnedBlenderVersion
  installedPath.mkdir(parents=True)
  (installedPath / "blender.exe").write_bytes(buildFakeBlenderExecutable(pinnedBlenderVersion))
  result = callInstallBlender(serverPath, localAppData)
  assert result.is_error is False
  installResult = json.loads(result.content[0].text)
  assert installResult["action"] == "none"
  assert installResult["blender"]["state"] == "installed"


def testHashMismatchFailsAndLeavesNothing(tmp_path):
  archivePath = tmp_path / "blender.zip"
  buildBlenderArchive(archivePath, pinnedBlenderVersion)
  serverPath = stageServer(tmp_path, archivePath.as_uri(), "0" * 64)
  localAppData = tmp_path / "localAppData"
  result = callInstallBlender(serverPath, localAppData)
  assert result.is_error is True
  assert "does not match pinned" in result.content[0].text
  assert sorted(entry.name for entry in (localAppData / "zonewright").iterdir()) == ["blender"]
  assert list((localAppData / "zonewright" / "blender").iterdir()) == []


def testBrokenInstallFailsWithoutTouchingIt(tmp_path):
  archivePath = tmp_path / "blender.zip"
  sha256 = buildBlenderArchive(archivePath, pinnedBlenderVersion)
  serverPath = stageServer(tmp_path, archivePath.as_uri(), sha256)
  localAppData = tmp_path / "localAppData"
  brokenPath = localAppData / "zonewright" / "blender" / pinnedBlenderVersion
  brokenPath.mkdir(parents=True)
  result = callInstallBlender(serverPath, localAppData)
  assert result.is_error is True
  assert "is broken" in result.content[0].text
  assert list(brokenPath.iterdir()) == []


def testDownloadFailureReportsReason(tmp_path):
  missingArchive = tmp_path / "absent.zip"
  serverPath = stageServer(tmp_path, missingArchive.as_uri(), "0" * 64)
  localAppData = tmp_path / "localAppData"
  result = callInstallBlender(serverPath, localAppData)
  assert result.is_error is True
  assert "download failed" in result.content[0].text
  assert list((localAppData / "zonewright" / "blender").iterdir()) == []
