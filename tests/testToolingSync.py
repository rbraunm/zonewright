import json
import subprocess
import urllib.request

from conftest import pinnedBlender


def blenderAction(action, version):
  return {"tool": "blender", "action": action, "version": version}


def extensionAction(action, extensionID, version):
  return {"tool": "extension", "action": action, "id": extensionID, "version": version}


def testSyncInstallsUpgradesAndRemoves(stageServer, blenderArchivePin, buildExtensionPin):
  version = blenderArchivePin["version"]
  probeV1 = buildExtensionPin("zonewrightProbe", "1.0.0")
  server = stageServer({"blender": blenderArchivePin, "extensions": {"zonewrightProbe": probeV1}})
  (server.toolingRoot / "blender" / "0.0.1").mkdir(parents=True)

  result, progressMessages = server.callToolExpectingSuccess("syncTooling")
  assert result["actions"] == [
    blenderAction("installed", version),
    blenderAction("removed", "0.0.1"),
    extensionAction("installed", "zonewrightProbe", "1.0.0"),
  ]
  assert f"downloading Blender {version}" in progressMessages
  assert f"extracting Blender {version}" in progressMessages
  installPath = server.toolingRoot / "blender" / version
  assert (installPath / "portable").is_dir()
  assert (installPath / "portable" / "extensions" / "user_default" / "zonewrightProbe" / "blender_manifest.toml").is_file()
  assert result["status"]["blender"]["state"] == "installed"
  assert result["status"]["blender"]["installedVersions"] == [version]
  assert result["status"]["extensions"] == {"pinned": {"zonewrightProbe": {"pinnedVersion": "1.0.0", "state": "installed"}}, "unpinned": {}}
  assert sorted(entry.name for entry in server.toolingRoot.iterdir()) == ["blender", "logs"]

  result, progressMessages = server.callToolExpectingSuccess("syncTooling")
  assert result["actions"] == []
  assert progressMessages == []

  probeV2 = buildExtensionPin("zonewrightProbe", "1.1.0")
  secondProbe = buildExtensionPin("zonewrightSecondProbe", "2.0.0")
  server.writeManifest({"blender": blenderArchivePin, "extensions": {"zonewrightProbe": probeV2, "zonewrightSecondProbe": secondProbe}})
  result, _ = server.callToolExpectingSuccess("syncTooling")
  assert result["actions"] == [
    extensionAction("removed", "zonewrightProbe", "1.0.0"),
    extensionAction("installed", "zonewrightProbe", "1.1.0"),
    extensionAction("installed", "zonewrightSecondProbe", "2.0.0"),
  ]

  result, _ = server.callToolExpectingSuccess("removeExtension", {"extensionID": "zonewrightSecondProbe"})
  assert result["actions"] == [extensionAction("removed", "zonewrightSecondProbe", "2.0.0")]
  assert server.readManifest()["extensions"] == {"zonewrightProbe": probeV2}
  assert result["status"]["extensions"] == {"pinned": {"zonewrightProbe": {"pinnedVersion": "1.1.0", "state": "installed"}}, "unpinned": {}}

  executablePath = installPath / "blender.exe"
  runningBlender = subprocess.Popen([str(executablePath), "--background", "--factory-startup", "--python-expr", "import time; time.sleep(600)"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
  try:
    errorText = server.callToolExpectingError("syncTooling")
  finally:
    runningBlender.kill()
    runningBlender.wait()
  assert "Blender is running from the tooling root" in errorText
  assert str(executablePath) in errorText


def testAddExtensionPinsCatalogRelease(stageServer, blenderArchivePin):
  request = urllib.request.Request(
    f"https://extensions.blender.org/api/v1/extensions/?blender_version={blenderArchivePin['version']}&platform=windows-x64",
    headers={"User-Agent": "zonewright"},
  )
  with urllib.request.urlopen(request) as response:
    catalog = json.loads(response.read())["data"]
  smallestAddon = min((entry for entry in catalog if entry["type"] == "add-on"), key=lambda entry: entry["archive_size"])
  extensionID = smallestAddon["id"]
  server = stageServer({"blender": blenderArchivePin, "extensions": {}})

  result, _ = server.callToolExpectingSuccess("addExtension", {"extensionID": extensionID})
  assert server.readManifest()["extensions"] == {
    extensionID: {"version": smallestAddon["version"], "url": smallestAddon["archive_url"], "sha256": smallestAddon["archive_hash"].removeprefix("sha256:")},
  }
  assert result["actions"][-1] == extensionAction("installed", extensionID, smallestAddon["version"])
  assert result["status"]["extensions"]["pinned"] == {extensionID: {"pinnedVersion": smallestAddon["version"], "state": "installed"}}

  errorText = server.callToolExpectingError("addExtension", {"extensionID": extensionID, "version": "0.0.0"})
  assert f"offers only {extensionID} {smallestAddon['version']}" in errorText


def testAddUnknownExtensionFails(stageServer):
  server = stageServer({"blender": pinnedBlender, "extensions": {}})
  errorText = server.callToolExpectingError("addExtension", {"extensionID": "zonewrightNoSuchExtension"})
  assert "lists 0 entries for 'zonewrightNoSuchExtension'" in errorText
  assert server.readManifest()["extensions"] == {}


def testRemoveUnpinnedExtensionFails(stageServer):
  server = stageServer({"blender": pinnedBlender, "extensions": {}})
  errorText = server.callToolExpectingError("removeExtension", {"extensionID": "zonewrightProbe"})
  assert "Extension 'zonewrightProbe' is not pinned" in errorText


def testHashMismatchFailsAndLeavesNothing(stageServer, buildExtensionPin):
  badPin = buildExtensionPin("zonewrightProbe", "1.0.0") | {"version": pinnedBlender["version"], "sha256": "0" * 64}
  server = stageServer({"blender": badPin, "extensions": {}})
  errorText = server.callToolExpectingError("syncTooling")
  assert "does not match pinned" in errorText
  assert sorted(entry.name for entry in server.toolingRoot.iterdir()) == ["blender", "logs"]
  assert list((server.toolingRoot / "blender").iterdir()) == []


def testDownloadFailureReportsReason(stageServer, tmp_path):
  missingPin = {"version": pinnedBlender["version"], "url": (tmp_path / "absent.zip").as_uri(), "sha256": "0" * 64}
  server = stageServer({"blender": missingPin, "extensions": {}})
  errorText = server.callToolExpectingError("syncTooling")
  assert "download failed" in errorText
  assert list((server.toolingRoot / "blender").iterdir()) == []


def testBrokenInstallFailsWithoutTouchingIt(stageServer):
  server = stageServer({"blender": pinnedBlender, "extensions": {}})
  brokenPath = server.toolingRoot / "blender" / pinnedBlender["version"]
  brokenPath.mkdir(parents=True)
  errorText = server.callToolExpectingError("syncTooling")
  assert "is broken" in errorText
  assert list(brokenPath.iterdir()) == []
