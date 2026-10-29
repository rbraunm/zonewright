import json
import sys
from pathlib import Path

import anyio
import pytest

from conftest import touch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
from zonewrightFront import Front, WorkerError

standInPath = Path(__file__).resolve().parent / "standInWorker.py"


class StandIn:
  """A front over tests/standInWorker.py, whose tools come from a JSON definition rewritten between reloads."""

  def __init__(self, folder):
    self.definitionPath = folder / "worker.json"
    self.adoptedPath = folder / "adopted.json"
    self.notifications = 0
    self.progress = []

  def define(self, version, tools, instructions="first", failToStart=False, record=None):
    self.definitionPath.write_text(json.dumps({
      "version": version, "tools": tools, "instructions": instructions, "failToStart": failToStart, "record": record,
      "adoptedPath": str(self.adoptedPath),
    }), encoding="utf-8")

  async def notify(self):
    self.notifications += 1

  async def recordProgress(self, progress, total, message):
    self.progress.append((progress, total, message))

  def withFront(self, work):
    async def run():
      async with anyio.create_task_group() as taskGroup:
        front = Front(taskGroup, [str(standInPath), str(self.definitionPath)])
        await front.start()
        try:
          return await work(front)
        finally:
          await front.close()
          taskGroup.cancel_scope.cancel()
    return anyio.run(run)

  async def text(self, front, name, arguments=None):
    result = await front.call(name, arguments or {}, self.notify, self.recordProgress)
    return result.is_error, result.content[0].text


@pytest.fixture
def standIn(tmp_path):
  return StandIn(tmp_path)


def testCallsReachTheWorkerAndReloadServesTheNewCodeWithTheFrontsToolsHidden(standIn):
  standIn.define(1, ["alpha", "beta"])

  async def work(front):
    before = await standIn.text(front, "alpha", {"text": "hi"})
    hidden = await standIn.text(front, "handOverBridge")
    standIn.define(2, ["alpha", "gamma"], instructions="second")
    reloaded = await front.call("reloadZonewright", {}, standIn.notify)
    after = await standIn.text(front, "alpha")
    gone = await standIn.text(front, "beta")
    return before, hidden, reloaded.structured_content, after, gone, sorted(tool.name for tool in front.tools())

  before, hidden, changes, after, gone, names = standIn.withFront(work)
  assert before == (False, "alpha 1 hi")
  assert hidden == (True, "handOverBridge is the front's own; reloadZonewright uses it")
  assert changes == {"added": ["gamma"], "removed": ["beta"], "changed": ["alpha"], "instructionsChanged": True,
    "blender": {"handedOver": False, "reason": "no Blender was running"}}
  assert after == (False, "alpha 2")
  assert gone == (True, "zonewright has no tool beta")
  assert names == ["alpha", "gamma", "progressing", "reloadZonewright", "slow"]
  assert standIn.notifications == 1


def testReloadHandsBlenderFromTheOldWorkerToTheNew(standIn):
  record = {"pid": 4242, "port": 50001, "token": "t", "codeFingerprint": {"bridgeMain.py": [10, 20]}, "logPath": "blender.log", "logOffset": 0}
  standIn.define(1, ["alpha"], record=record)

  async def work(front):
    reloaded = await front.call("reloadZonewright", {}, standIn.notify)
    return reloaded.structured_content["blender"]

  blender = standIn.withFront(work)
  assert blender == {"handedOver": True, "adopted": record}
  assert json.loads(standIn.adoptedPath.read_text(encoding="utf-8")) == record


def testProgressNotificationsAreForwarded(standIn):
  standIn.define(1, ["alpha"])
  result = standIn.withFront(lambda front: standIn.text(front, "progressing"))
  assert result == (False, "progressed")
  assert standIn.progress == [(1.0, 3.0, "one"), (2.0, 3.0, "two")]


def testABuildThatFailsToStartLeavesTheRunningOne(standIn):
  standIn.define(1, ["alpha"])

  async def work(front):
    standIn.define(2, ["alpha"], failToStart=True)
    refused = await standIn.text(front, "reloadZonewright")
    return refused, await standIn.text(front, "alpha")

  refused, still = standIn.withFront(work)
  assert refused[0] is True
  assert refused[1].startswith("zonewright did not start: ")
  assert refused[1].endswith("; its stderr ends: this build does not start; the running zonewright stays")
  assert still == (False, "alpha 1")
  assert standIn.notifications == 0


def testReloadIsRefusedWhileACallRuns(standIn, tmp_path):
  standIn.define(1, ["alpha"])
  release = tmp_path / "release"

  async def work(front):
    results = {}

    async def slowCall():
      results["slow"] = await standIn.text(front, "slow", {"release": str(release)})

    async with anyio.create_task_group() as calls:
      calls.start_soon(slowCall)
      while front.running == 0:
        await anyio.sleep(0.01)
      results["reload"] = await standIn.text(front, "reloadZonewright")
      release.touch()
    return results

  results = standIn.withFront(work)
  assert results["reload"] == (True, "1 zonewright calls are running; reload when they finish")
  assert results["slow"] == (False, "released")


def testAWorkerThatFailsAtStartupStopsTheFront(standIn):
  standIn.define(1, ["alpha"], failToStart=True)
  with pytest.raises(ExceptionGroup) as caught:
    standIn.withFront(lambda front: anyio.sleep(0))
  assert [type(error) for error in caught.value.exceptions] == [WorkerError]
  assert str(caught.value.exceptions[0]).startswith("zonewright did not start: ")


def testReloadKeepsBlenderAndItsUnsavedScene(freshBlenderServer):
  async def steps(session):
    await session.expectSuccess("newFile", {"discardUnsavedChanges": True})
    await session.expectSuccess("createPrimitive", {"kind": "cube", "name": "kept", "size": [2, 2, 2], "location": [0, 0, 0]})
    before = (await session.expectSuccess("getToolingStatus"))["bridge"]
    touch(freshBlenderServer.repositoryPath / "server" / "zoneSurvey.py")
    refused = await session.expectError("getObjectDetail", {"name": "kept"})
    reloaded = await session.expectSuccess("reloadZonewright")
    detail = await session.expectSuccess("getObjectDetail", {"name": "kept"})
    after = (await session.expectSuccess("getToolingStatus"))["bridge"]
    return before, refused, reloaded, detail, after

  before, refused, reloaded, detail, after = freshBlenderServer.session(steps)
  assert before["state"] == "running"
  assert "zonewright server code changed on disk since this server started (zoneSurvey.py); reloadZonewright loads it" in refused
  assert reloaded["added"] == [] and reloaded["removed"] == [] and reloaded["changed"] == []
  assert reloaded["blender"]["handedOver"] is True
  assert reloaded["blender"]["pid"] == before["pid"]
  assert reloaded["blender"]["unsavedChanges"] is True
  assert detail["dimensions"] == [2.0, 2.0, 2.0]
  assert after["pid"] == before["pid"]
  assert after["unsavedChanges"] is True
  assert after["codeCurrent"] is True
