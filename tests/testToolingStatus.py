import math
import os

from conftest import pinnedBlender


def testMissingBlenderReportsMissing(stageServer):
  server = stageServer({"blender": pinnedBlender, "extensions": {}})
  status, _ = server.callToolExpectingSuccess("getToolingStatus")
  assert status == {
    "toolingRoot": str(server.toolingRoot),
    "blender": {
      "pinnedVersion": pinnedBlender["version"],
      "executablePath": str(server.toolingRoot / "blender" / pinnedBlender["version"] / "blender.exe"),
      "installedVersions": [],
      "state": "missing",
    },
    "extensions": {"pinned": {}, "unpinned": {}},
    "machineProfile": {"state": "missing", "problems": ["no machine profile"], "workers": max(1, math.floor(os.cpu_count() * 0.8))},
    "bridge": {"state": "notStarted"},
    "runPython": {"calls": 0, "logFiles": [str(server.toolingRoot / "logs" / "runPython.log")]},
  }


def testVersionFolderWithoutExecutableReportsBroken(stageServer):
  server = stageServer({"blender": pinnedBlender, "extensions": {}})
  (server.toolingRoot / "blender" / pinnedBlender["version"]).mkdir(parents=True)
  blenderStatus = server.callToolExpectingSuccess("getToolingStatus")[0]["blender"]
  assert blenderStatus["state"] == "broken"
  assert blenderStatus["reason"] == "blender.exe is missing"
  assert blenderStatus["installedVersions"] == [pinnedBlender["version"]]


def testPinnedExtensionWithoutBlenderReportsMissing(stageServer, buildExtensionPin):
  probePin = buildExtensionPin("zonewrightProbe", "1.0.0")
  server = stageServer({"blender": pinnedBlender, "extensions": {"zonewrightProbe": probePin}})
  extensionsStatus = server.callToolExpectingSuccess("getToolingStatus")[0]["extensions"]
  assert extensionsStatus == {"pinned": {"zonewrightProbe": {"pinnedVersion": "1.0.0", "state": "missing"}}, "unpinned": {}}
