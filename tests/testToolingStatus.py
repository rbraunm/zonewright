import math
import os
import re

from conftest import pinnedBlender, pinnedRecast


def testMissingBlenderReportsMissing(stageServer):
  server = stageServer({"blender": pinnedBlender, "extensions": {}})
  status, _ = server.callToolExpectingSuccess("getToolingStatus")
  recastHelper = status.pop("recastHelper")
  assert sorted(recastHelper) == ["commit", "compiler", "executablePath", "fingerprint", "state"]
  assert (recastHelper["state"], recastHelper["commit"]) == ("missing", pinnedRecast["commit"])
  assert recastHelper["executablePath"] == str(server.toolingRoot / "recastHelper" / recastHelper["fingerprint"][:16] / "recastHelper.exe")
  assert re.fullmatch(r"[0-9a-f]{64}", recastHelper["fingerprint"])
  assert re.fullmatch(r"Microsoft \(R\) C/C\+\+ Optimizing Compiler Version [\d.]+ for x64", recastHelper["compiler"])
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
    "calibration": [],
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
