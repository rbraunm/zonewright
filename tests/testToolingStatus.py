import json
import sys
from pathlib import Path

import anyio
from mcp import Client, StdioServerParameters

repositoryRoot = Path(__file__).resolve().parent.parent
serverPath = repositoryRoot / "server" / "zonewrightServer.py"
pinnedBlenderVersion = json.loads((repositoryRoot / "toolingManifest.json").read_text(encoding="ascii"))["blender"]["version"]


async def callGetToolingStatus(localAppData):
  serverParameters = StdioServerParameters(
    command=sys.executable,
    args=[str(serverPath)],
    env={"LOCALAPPDATA": str(localAppData)},
  )
  async with Client(serverParameters) as client:
    result = await client.call_tool("getToolingStatus")
  assert result.is_error is False
  assert len(result.content) == 1
  return json.loads(result.content[0].text)


def testMissingBlenderReportsMissing(tmp_path):
  toolingRoot = tmp_path / "zonewright"
  assert anyio.run(callGetToolingStatus, tmp_path) == {
    "toolingRoot": str(toolingRoot),
    "blender": {
      "pinnedVersion": pinnedBlenderVersion,
      "executablePath": str(toolingRoot / "blender" / pinnedBlenderVersion / "blender.exe"),
      "installedVersions": [],
      "state": "missing",
    },
  }


def testVersionFolderWithoutExecutableReportsBroken(tmp_path):
  (tmp_path / "zonewright" / "blender" / pinnedBlenderVersion).mkdir(parents=True)
  blenderStatus = anyio.run(callGetToolingStatus, tmp_path)["blender"]
  assert blenderStatus["state"] == "broken"
  assert blenderStatus["installedVersions"] == [pinnedBlenderVersion]
