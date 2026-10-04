import hashlib
import json
import os
import time
from pathlib import Path

import pytest

from conftest import everquestClient, pinnedBlender


def touch(path):
  """Change a file's modification time the way an edit would, without changing its content."""
  later = time.time_ns() + 2_000_000_000
  os.utime(path, ns=(later, later))


def testProfileChoosesTheFastestHardwareBackend(freshBlenderServer):
  profile = json.loads((freshBlenderServer.toolingRoot / "machineProfile.json").read_text(encoding="utf-8"))
  usable = [benchmark for benchmark in profile["benchmarks"] if benchmark["usable"]]
  assert [benchmark["backend"] for benchmark in profile["benchmarks"]] == ["vulkan", "opengl"]
  assert usable
  assert profile["gpuBackend"] == min(usable, key=lambda benchmark: benchmark["totalRenderSeconds"])["backend"]
  assert all(benchmark["deviceType"] != "SOFTWARE" for benchmark in usable)
  status = freshBlenderServer.callToolExpectingSuccess("getToolingStatus")[0]["machineProfile"]
  assert status["state"] == "current"
  assert status["gpuBackend"] == profile["gpuBackend"]


def testStaleProfileBlocksBlenderUntilSynced(freshBlenderServer):
  profilePath = freshBlenderServer.toolingRoot / "machineProfile.json"
  original = profilePath.read_text(encoding="utf-8")
  profile = json.loads(original)
  profile["blenderVersion"] = "0.0.1"
  profilePath.write_text(json.dumps(profile), encoding="utf-8")
  try:
    errorText = freshBlenderServer.callToolExpectingError("runPython", {"code": "result = 1"})
    status = freshBlenderServer.callToolExpectingSuccess("getToolingStatus")[0]["machineProfile"]
  finally:
    profilePath.write_text(original, encoding="utf-8")
  assert f"Machine profile is missing or stale (profiled Blender 0.0.1, pinned {pinnedBlender['version']}); run syncTooling" in errorText
  assert status["state"] == "stale"


def testServerCodeChangeFailsUntilReconnect(freshBlenderServer):
  async def steps(session):
    before = await session.expectSuccess("getToolingStatus")
    touch(freshBlenderServer.repositoryPath / "server" / "zoneSurvey.py")
    return before, await session.expectError("getToolingStatus")

  before, errorText = freshBlenderServer.session(steps)
  assert before["bridge"] == {"state": "notStarted"}
  assert "zonewright server code changed on disk since this server started (zoneSurvey.py); reconnect with /mcp to load it" in errorText


def testBridgeCodeChangeRestartsBlenderAndReopensTheFile(freshBlenderServer, tmp_path):
  blendPath = tmp_path / "kept.blend"

  async def steps(session):
    await session.expectSuccess("newFile", {"discardUnsavedChanges": True})
    await session.expectSuccess("createPrimitive", {"kind": "cube", "name": "keeper", "size": [2, 2, 2], "location": [0, 0, 0]})
    await session.expectSuccess("saveFile", {"path": str(blendPath)})
    before = (await session.expectSuccess("getToolingStatus"))["bridge"]
    touch(freshBlenderServer.repositoryPath / "server" / "bridgeObjects.py")
    stale = (await session.expectSuccess("getToolingStatus"))["bridge"]
    detail = await session.expectSuccess("getObjectDetail", {"name": "keeper"})
    after = (await session.expectSuccess("getToolingStatus"))["bridge"]
    return before, stale, detail, after

  before, stale, detail, after = freshBlenderServer.session(steps)
  assert before["codeCurrent"] is True
  assert stale["codeCurrent"] is False
  assert stale["pid"] == before["pid"]
  assert detail["dimensions"] == [2.0, 2.0, 2.0]
  assert after["pid"] != before["pid"]
  assert after["codeCurrent"] is True
  assert after["filePath"] == str(blendPath)
  assert after["unsavedChanges"] is False


def testBridgeCodeChangeWithUnsavedChangesWaitsForSave(freshBlenderServer, tmp_path):
  blendPath = tmp_path / "pending.blend"

  async def steps(session):
    await session.expectSuccess("newFile", {"discardUnsavedChanges": True})
    await session.expectSuccess("createPrimitive", {"kind": "cube", "name": "pending", "size": [2, 2, 2], "location": [0, 0, 0]})
    before = (await session.expectSuccess("getToolingStatus"))["bridge"]
    touch(freshBlenderServer.repositoryPath / "server" / "bridgeObjects.py")
    refused = await session.expectError("getObjectDetail", {"name": "pending"})
    await session.expectSuccess("saveFile", {"path": str(blendPath)})
    detail = await session.expectSuccess("getObjectDetail", {"name": "pending"})
    after = (await session.expectSuccess("getToolingStatus"))["bridge"]
    return before, refused, detail, after

  before, refused, detail, after = freshBlenderServer.session(steps)
  assert "Blender bridge code changed on disk (bridgeObjects.py) and the open file has unsaved changes" in refused
  assert detail["name"] == "pending"
  assert after["pid"] != before["pid"]
  assert after["filePath"] == str(blendPath)


@pytest.mark.clientData("survey")
def testSurveyReusesHashesAndDiscoveryUntilFilesChange(stageServer):
  server = stageServer({"blender": pinnedBlender, "extensions": {}})
  befallenArchive = Path(everquestClient) / "befallen.s3d"
  server.callToolExpectingSuccess("surveyZones", {"zones": ["befallen"]})
  cachePath = server.toolingRoot / "survey" / "zoneSurvey.json"
  cache = json.loads(cachePath.read_text(encoding="utf-8"))
  realHash = hashlib.sha256(befallenArchive.read_bytes()).hexdigest()
  assert cache["fileHashes"][str(befallenArchive)]["sha256"] == realHash
  assert cache["variants"]["befallen:wld"]["fileHashes"]["befallen.s3d"] == realHash
  cache["fileHashes"][str(befallenArchive)]["sha256"] = "f" * 64
  del cache["discovery"]["variants"]["unrest:wld"]
  cachePath.write_text(json.dumps(cache), encoding="utf-8")

  server.callToolExpectingSuccess("surveyZones", {"zones": ["befallen"]})
  memoUsed = json.loads(cachePath.read_text(encoding="utf-8"))["variants"]["befallen:wld"]["fileHashes"]["befallen.s3d"]
  discoveryUsed, _ = server.callToolExpectingSuccess("surveyZones", {"zones": ["unrest"]})

  server.callToolExpectingSuccess("surveyZones", {"zones": ["befallen"], "verifyHashes": True})
  verified = json.loads(cachePath.read_text(encoding="utf-8"))
  verified["discovery"]["listingFingerprint"] = "changed"
  cachePath.write_text(json.dumps(verified), encoding="utf-8")
  rediscovered, _ = server.callToolExpectingSuccess("surveyZones", {"zones": ["unrest"]})

  assert memoUsed == "f" * 64
  assert discoveryUsed["unknownZones"] == ["unrest"] and discoveryUsed["rows"] == []
  assert verified["variants"]["befallen:wld"]["fileHashes"]["befallen.s3d"] == realHash
  assert [row["variant"] for row in rediscovered["rows"]] == ["unrest:wld"]
