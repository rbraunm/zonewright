import io
import json
import math
import sys
from pathlib import Path

import numpy
import pytest
from PIL import Image

from claudelinkShots import actor, spawnRecord, writeShot, zoneDump
from conftest import serverRowValues, writePNG

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import clientPointLights
import eqCalibration
import previewFrame

# The camera of claudelink's Highpass Hold dump of 2026-10-11 (highpasshold_20261011-175509-160.json), as the client computed it.
highpassCamera = {
  "y": -447, "x": 273, "z": -11.310997, "heading": 238.933319, "pitch": -18.5, "roll": 0, "halfViewAngle": 45, "viewAngleRatio": 1.80663383,
  "renderWidth": 2560, "renderHeight": 1417, "projectionScaleX": 1.62289739, "projectionScaleY": 2.93198109,
}
highpassViewport = {"minX": 440, "minY": 0, "maxX": 3000, "maxY": 1417}


def smallDump(**changes):
  camera = {"y": 10.0, "x": 20.0, "z": 5.0, "heading": 128, "pitch": 0}
  light = {"type": 14, "tag": "redlight", "position": (10.0, 22.0, 5.31), "radius": 150}
  return zoneDump("highpasshold", 407, (48, 27), camera, (13, 31), light, 0.1, {"red": 200, "green": 200, "blue": 200, "start": 200, "end": 1500}, 1500, **changes)


def testAShotIsReadOnlyWhenItsFilesPairAndItRanUnthrottledAtTheClientArea(tmp_path):
  viewportImage = Image.new("RGB", (48, 27), (90, 60, 30))
  good = writeShot(tmp_path / "good", "shot", viewportImage, smallDump())
  sidecar, dump = eqCalibration.readShot(good)
  assert sidecar["dump"] == str(tmp_path / "good" / "highpasshold_shot.dump.json") and dump["zone"]["shortName"] == "highpasshold"
  slow = writeShot(tmp_path / "slow", "shot", viewportImage, smallDump(), fps=60.0)
  throttled = writeShot(tmp_path / "throttled", "shot", viewportImage, smallDump(), limiterActive=True)
  resized = writeShot(tmp_path / "resized", "shot", viewportImage, smallDump(), windowSize=(100, 27))
  refusals = {}
  for name, path in (("slow", slow), ("throttled", throttled), ("resized", resized)):
    with pytest.raises(ValueError) as refused:
      eqCalibration.readShot(path)
    refusals[name] = str(refused.value)
  assert "the client ran at 60.0 fps, under 100" in refusals["slow"]
  assert "frame limiter was throttling" in refusals["throttled"]
  assert "the PNG is 100x27, not the 188x27 client area the sidecar records" in refusals["resized"]
  # A dump taken at another moment, or for another shot, does not pair with the sidecar.
  sidecarPath = good.with_suffix(".json")
  dumpPath = Path(sidecar["dump"])
  dumpPath.write_text(json.dumps(dump | {"capturedAt": "2026-10-12T00:55:10.000Z", "origin": dump["origin"] | {"screenshot": str(tmp_path / "other.png")}}), encoding="ascii")
  with pytest.raises(ValueError) as unpaired:
    eqCalibration.readShot(good)
  assert "the dump was taken for" in str(unpaired.value) and "the sidecar was captured at 2026-10-12T00:55:09.082Z and the dump at 2026-10-12T00:55:10.000Z" in str(unpaired.value)
  sidecarPath.unlink()
  with pytest.raises(ValueError, match="no claudelink sidecar"):
    eqCalibration.readShot(good)


def testTheFrameIsTheClientsProjectionAtTheViewportsAspect():
  frame = eqCalibration.clientFrame(highpassCamera, highpassViewport)
  # Half the horizontal view is 45/512 of a turn, 31.64 degrees; the vertical half angle's tangent is the horizontal's over 2560/1417.
  assert frame["size"] == [1440, 797]
  assert abs(frame["verticalFieldOfViewDegrees"] - 37.6656) < 1e-4
  assert abs(previewFrame.verticalFieldOfView(45, 16 / 9) - previewFrame.verticalFieldOfViewDegrees) < 1e-12
  with pytest.raises(ValueError, match="projectionScaleY"):
    eqCalibration.clientFrame(highpassCamera | {"projectionScaleY": 2.9}, highpassViewport)
  with pytest.raises(ValueError, match="into a 2560x1400 viewport"):
    eqCalibration.clientFrame(highpassCamera, highpassViewport | {"maxY": 1400})


def testTheCameraLooksAlongItsHeadingAndPitchInBlendersAxes():
  # Heading 0 faces the server's +y, Blender's +X; 128 its +x, Blender's +Y; 256 Blender's -X. Pitch 64 is 45 degrees up.
  ahead = lambda camera: [round(target - eye, 6) for target, eye in zip(eqCalibration.cameraView(camera)["target"], eqCalibration.cameraView(camera)["eye"])]
  base = {"y": 10, "x": 20, "z": 5, "roll": 0}
  assert eqCalibration.cameraView(base | {"heading": 0, "pitch": 0})["eye"] == [10, 20, 5]
  assert ahead(base | {"heading": 0, "pitch": 0}) == [1, 0, 0]
  assert ahead(base | {"heading": 128, "pitch": 0}) == [0, 1, 0]
  assert ahead(base | {"heading": 256, "pitch": -64}) == [round(-math.sqrt(0.5), 6), 0, round(-math.sqrt(0.5), 6)]
  with pytest.raises(ValueError, match="roll"):
    eqCalibration.cameraView(base | {"heading": 0, "pitch": 0, "roll": 3})


def testTheDumpGivesTheZoneTheViewerAndTheLightTheClientDrew():
  dump = smallDump()
  viewed = eqCalibration.viewer(dump)
  # The vision floor 0.08 times one less the share 0.1, truncated to a byte: 18/255.
  assert viewed["specialAmbientColor"] == [18 / 255] * 3
  assert eqCalibration.zoneProperties(dump, viewed["specialAmbientColor"]) == {
    "sky": {"type": "highpasshold", "hour": 13, "minute": 31}, "fogStart": 200.0, "fogEnd": 1500.0, "fogDensity": 0.33, "fogOn": False,
    "minClip": 50.0, "maxClip": 1500.0, "newEngineZone": False, "shortName": "highpasshold", "zoneId": 407, "specialAmbientColor": [18 / 255] * 3,
  }
  carried = eqCalibration.carriedLight(dump)
  assert carried["position"] == [10.0, 22.0, 5.31] and carried["color"] == [0.5, 0.25999999046325684, 0.0] and carried["radius"] == 150
  assert eqCalibration.carriedLight(smallDump() | {"player": dump["player"] | {"light": dump["player"]["light"] | {"type": 0}}}) is None
  with pytest.raises(ValueError, match="radius of 100 in zone 407"):
    eqCalibration.carriedLight(dump | {"player": dump["player"] | {"light": dump["player"]["light"] | {"radius": 100}}})
  slider = dump | {"graphicsSettings": dump["graphicsSettings"] | {"memory": dump["graphicsSettings"]["memory"] | {"clipPlane": 10}}}
  with pytest.raises(ValueError, match="clip slider is 10"):
    eqCalibration.zoneProperties(slider, viewed["specialAmbientColor"])
  elf = dump | {"spawns": [dump["spawns"][0] | {"look": dump["spawns"][0]["look"] | {"race": 4}}]}
  with pytest.raises(ValueError, match="race 4"):
    eqCalibration.viewer(elf)


def testZoneDriftListsEachValueTheFileHoldsOtherwiseThanTheDump():
  dump = smallDump()
  updates = eqCalibration.zoneProperties(dump, eqCalibration.viewer(dump)["specialAmbientColor"])
  # The sky's hour is the artist's preview hour, not drift; the stored values' types are Blender's (ints where the dump holds floats).
  alike = {
    "sky": {"type": "highpasshold", "hour": 9, "minute": 0}, "fogStart": 200, "fogEnd": 1500, "fogDensity": 0.33, "fogOn": False, "minClip": 50,
    "maxClip": 1500, "newEngineZone": False, "shortName": "highpasshold", "zoneId": 407, "safePoint": [0, 0, 0, 0],
  }
  assert eqCalibration.zoneDrift(alike, updates) == {}
  drifted = {key: value for key, value in alike.items() if key != "fogDensity"} | {"sky": "none", "fogEnd": 2400, "minClip": 100, "zoneId": 788}
  assert eqCalibration.zoneDrift(drifted, updates) == {
    "skyType": {"stored": "none", "dump": "highpasshold"}, "fogEnd": {"stored": 2400, "dump": 1500.0}, "fogDensity": {"stored": None, "dump": 0.33},
    "minClip": {"stored": 100, "dump": 50.0}, "zoneId": {"stored": 788, "dump": 407},
  }
  assert eqCalibration.zoneDrift({}, updates)["skyType"] == {"stored": None, "dump": "highpasshold"}


def testWhatTheDumpPlacesStandsWhereItsActorsStand():
  spawns = [spawnRecord(7, "a_guard000", "0x10", (100, 200, -4), 64), spawnRecord(8, "ghost000", "0x0", (100, 210, -4), 0)]
  doors = [
    {"id": 3, "name": "OBJ_GATE", "visible": True, "actor": "0x11"}, {"id": 4, "name": "OBJ_HIDDEN", "visible": False, "actor": "0x12"},
  ]
  groundItems = [{"dropId": 5, "name": "IT69_ACTORDEF", "actor": "0x13"}, {"dropId": 6, "name": "IT70_ACTORDEF", "actor": "0x14"}]
  actors = [
    actor("0x10", "HHM_ACTORDEF", (100, 200, -4), 64, 1.2), actor("0x11", "OBJ_GATE_ACTORDEF", (50, 60, 1), 128, 1.5),
    actor("0x12", "OBJ_HIDDEN_ACTORDEF", (0, 0, 0), 0, 1), actor("0x13", "IT69_ACTORDEF", (5, 6, 7), 300, 1), actor("0x14", "IT70_ACTORDEF", (1, 2, 3), 0, 1, pitch=20),
  ]
  placements, notPlaced = eqCalibration.dumpPlacements(smallDump(spawns=spawns, doors=doors, groundItems=groundItems, actors=actors))
  assert [(entry["kind"], entry["name"], entry["model"], entry["position"], entry["turnDegrees"], entry["scale"]) for entry in placements] == [
    ("spawn", "spawn7_a_guard000", "HHM_ACTORDEF", [100, 200, -4], 45.0, 1.2), ("door", "door3_OBJ_GATE", "OBJ_GATE_ACTORDEF", [50, 60, 1], 90.0, 1.5),
    ("groundItem", "groundItem5_IT69_ACTORDEF", "IT69_ACTORDEF", [5, 6, 7], 300 * 360 / 512, 1),
  ]
  assert placements[0]["appearance"] == {
    "variation": 0, "headType": 0, "faceStyle": 0, "hairStyle": 0, "hairColor": 0, "facialHair": 255, "facialHairColor": 0, "eyeColor1": 0,
    "heritage": 0, "tattoo": 0, "details": 0, "textureSet": 0,
  }
  assert [(entry["name"], entry["reason"].split(":")[0]) for entry in notPlaced] == [
    ("spawn8_ghost000", "no actor in the client's scene graph (0x0)"), ("door4_OBJ_HIDDEN", "hidden"), ("groundItem6_IT70_ACTORDEF", "tilted (pitch 20, roll 0)"),
  ]


def testOnlyWhatCouldShowInTheFrameIsKept():
  view = {"eye": [0, 0, 0], "target": [1, 0, 0]}
  frame = {"size": [960, 540], "verticalFieldOfViewDegrees": previewFrame.verticalFieldOfViewDegrees}
  points = [[500, 0, 0], [-50, 0, 0], [-500, 0, 0], [100, 400, 0], [100, 150, 0], [1700, 0, 0], [1500, 0, 0]]
  # Ahead, just behind, far behind, far off to the side, just outside the field, past the far clip and its margin, just inside the clip.
  assert eqCalibration.withinView(points, view, frame, 1500).tolist() == [True, True, False, False, True, False, True]


def testTheComparisonMeasuresTheDrawnPixelsTheSkyAndEachBlock():
  screen = numpy.full((64, 96, 3), 0.5)
  render = screen.copy()
  render[:32, :32] += 40 / 255
  drawn = numpy.zeros((64, 96), dtype=bool)
  drawn[:32, :] = True
  numbers, blocks, pictures = eqCalibration.comparison(screen, render, drawn)
  # A third of the drawn half differs by 40; the sky half not at all; the top left block alone, by 40.
  assert numbers["meanPixelDifference"] == round(40 / 3, 2) and numbers["meanSkyDifference"] == 0.0 and numbers["meanDifference"] == round(40 / 6, 2)
  assert numpy.round(blocks, 6).tolist() == [[40.0, 0.0, 0.0], [0.0, 0.0, 0.0]]
  assert pictures["sheet"].size == (eqCalibration.sheetWidth, round(eqCalibration.sheetWidth * 64 / 96))
  assert numpy.asarray(pictures["difference"])[0, 0].tolist() == [160, 160, 160] and numpy.asarray(pictures["difference"])[40, 40].tolist() == [0, 0, 0]


@pytest.mark.clientData("calibration")
def testCalibrationPredictsAClaudelinkShotOfAnEQGZoneFromItsDump(stageBlenderServer, tmp_path):
  # High over Highpass Hold at 13:31, looking west (heading 256, Blender's -X) and down (pitch -14) at the zone under the sky, the viewer
  # a human carrying a red light, with a guard and a dropped item standing in the air ahead. The shot is what renderView draws of the same scene, set up by hand: the dump has to
  # bring calibrateShot to the same picture by its own path. A dump's (y, x, z) are Blender's (x, y, z).
  camera = {"y": -447.0, "x": 273.0, "z": 300.0, "heading": 256, "pitch": -14}
  feet = [-447.0, 273.0, camera["z"] - 7.44]
  light = clientPointLights.carriedLight(14, feet, 270, 407)
  pitch = math.radians(-14 * 360 / 512)
  eye = [camera["y"], camera["x"], camera["z"]]
  view = {"eye": eye, "target": [eye[0] - math.cos(pitch), eye[1], eye[2] + math.sin(pitch)]}
  guard, item = (-487.0, 275.0, 293.0), (-477.0, 270.0, 294.8)
  spawns = [spawnRecord(7, "a_guard000", "0x10", guard, 64), spawnRecord(8, "unseen000", "0x0", guard, 64)]
  doors = [{"id": 3, "name": "OBJ_HIDDEN", "visible": False, "actor": "0x12"}]
  groundItems = [{"dropId": 5, "name": "IT69_ACTORDEF", "actor": "0x13"}]
  actors = [actor("0x10", "HUM_ACTORDEF", guard, 64, 1.2), actor("0x12", "OBJ_HIDDEN_ACTORDEF", item, 0, 1), actor("0x13", "IT69_ACTORDEF", item, 100, 1)]
  fogSet = {"red": 200, "green": 200, "blue": 200, "start": 200, "end": 1500}
  dump = zoneDump(
    "highpasshold", 407, (960, 540), camera, (13, 31), {"type": 14, "tag": "redlight", "position": tuple(light["position"]), "radius": 150}, 0.1, fogSet, 1500,
    spawns=spawns, doors=doors, groundItems=groundItems, actors=actors,
  )
  zone = eqCalibration.zoneProperties(dump, eqCalibration.viewer(dump)["specialAmbientColor"])

  async def steps(session):
    slowShot = writeShot(tmp_path / "slow", "slow", Image.new("RGB", (960, 540)), dump, fps=80.0)
    slow = await session.expectError("calibrateShot", {"screenshotPath": str(slowShot), "discardUnsavedChanges": True})
    await session.expectSuccess("newFile", {"discardUnsavedChanges": True})
    await session.expectSuccess("importZone", {"zone": "highpasshold"})
    await session.expectSuccess("setZoneProperties", zone)
    await session.expectSuccess("placeSpawn", {
      "zone": "highpasshold", "model": "HUM", "name": "guard", "height": 6, "y": guard[0], "x": guard[1], "z": guard[2], "heading": 64, "snapToGround": False,
    })
    await session.expectSuccess("placeObject", {"zone": "highpasshold", "model": "IT69_ACTORDEF", "name": "item", "y": item[0], "x": item[1], "z": item[2], "heading": 100})
    image, _ = await session.expectImage("renderView", {"view": view, "guides": False, "carriedLight": {"lightType": 14, "at": feet, "headingDegrees": 270}})
    shot = writeShot(tmp_path / "shots", "shot", Image.open(io.BytesIO(image)).convert("RGB"), dump)
    nearItem = await session.expectSuccess("placeDump", {"dumpPath": json.loads(shot.with_suffix(".json").read_text(encoding="ascii"))["dump"], "near": list(item), "radius": 5})
    predicted = await session.expectImage("calibrateShot", {"screenshotPath": str(shot), "discardUnsavedChanges": True}, mimeType="image/jpeg")
    fitted = await session.expectImage("calibrateShot", {"screenshotPath": str(shot), "fitLight": True, "discardUnsavedChanges": True}, mimeType="image/jpeg")
    return slow, nearItem, predicted[1], fitted[1], await session.expectSuccess("getToolingStatus")

  slow, nearItem, predicted, fitted, status = stageBlenderServer.session(steps)
  assert "the client ran at 80.0 fps, under 100" in slow
  # placeDump within 5 of the item places it alone, the guard 11 away left out; what the dump cannot place is listed either way.
  assert nearItem["placed"] == [{"kind": "groundItem", "name": "groundItem5_IT69_ACTORDEF", "model": "IT69_ACTORDEF"}]
  assert sorted(entry["name"] for entry in nearItem["notPlaced"]) == ["door3_OBJ_HIDDEN", "spawn8_unseen000"]
  # The dump's camera, frame, sky, header, light, viewer, and placements draw the shot again.
  assert predicted["frame"] == {"size": [960, 540], "verticalFieldOfViewDegrees": previewFrame.verticalFieldOfViewDegrees}
  assert predicted["comparison"]["meanPixelDifference"] < 0.5 and predicted["comparison"]["meanSkyDifference"] < 0.5
  assert 0.3 < predicted["comparison"]["drawnShare"] < 1
  assert predicted["placed"] == 2 and predicted["outOfView"] == 0
  assert sorted(entry["name"] for entry in predicted["notPlaced"]) == ["door3_OBJ_HIDDEN", "spawn8_unseen000"]
  assert predicted["conditions"]["gameTime"] == "13:31" and predicted["conditions"]["carriedLight"]["position"] == light["position"]
  assert predicted["fitLight"] is None and Path(predicted["sheetPath"]).is_file()
  # The sky's own light explains the shot under the fit's formula but for the liquids' added light (water's reflection), and the fit
  # finds the sky's ambient; its noon light clamps on most sunlit faces, which leaves the sun and bounce colors unmeasured.
  fit = fitted["fitLight"]
  assert fit["predicted"]["residual"] < 0.02 and fit["fitted"]["residual"] < 0.02 and fit["unclampedPixels"] < fit["pixels"]
  assert max(abs(measured - expected) for measured, expected in zip(fit["fitted"]["ambientColor"], fit["predicted"]["ambientColor"])) < 0.02
  latest = [entry for entry in status["calibration"] if entry["shot"] == "shot.png"]
  assert [(entry["runs"], entry["meanPixelDifference"], entry["gameTime"], entry["source"]) for entry in latest] == [
    (2, fitted["comparison"]["meanPixelDifference"], "13:31", {"kind": "clientZone", "path": None}),
  ]
  assert predicted["source"] == {"kind": "clientZone", "path": None, "zoneDrift": None}


# The compared zone: a short name the client does not register, so its sky type resolves to the client's default and its models come
# from the startup archives. The file's view values differ from the dump's header in fogEnd and minClip alone.
comparedZone = "compared"
comparedFileValues = {
  "sky": {"type": comparedZone, "hour": 9, "minute": 0}, "fogStart": 200.0, "fogEnd": 3000.0, "fogDensity": 0.33, "fogOn": True, "minClip": 50.0,
  "maxClip": 4000.0, "newEngineZone": False,
}
comparedFogSet = {"red": 200, "green": 200, "blue": 200, "start": 200, "end": 2400}
comparedDrift = {"fogEnd": {"stored": 3000.0, "dump": 2400.0}, "minClip": {"stored": 50.0, "dump": 500.0}}
# From 20 up at Blender (-60, 0), looking along +X (heading 0) and 14 degrees down at the crate, the guard, and the dropped kiln.
comparedCamera = {"y": -60.0, "x": 0.0, "z": 20.0, "heading": 0, "pitch": -20}
guardAt, kilnAt = (15.0, 0.0, 0.0), (20.0, 10.0, 0.0)


def comparedDump():
  """A dump of the compared zone at noon with a fogged header: the viewer carries no light; the kiln lies dropped in view."""
  noLight = {"type": 0, "tag": "", "position": (comparedCamera["y"], comparedCamera["x"], comparedCamera["z"] - 3.69), "radius": 0}
  return zoneDump(
    comparedZone, serverRowValues(comparedZone)["zoneId"], (960, 540), comparedCamera, (12, 0), noLight, 0.1, comparedFogSet, 4000,
    groundItems=[{"dropId": 5, "name": "IT10800_ACTORDEF", "actor": "0x13"}], actors=[actor("0x13", "IT10800_ACTORDEF", kilnAt, 100, 1)],
    fogOnOff=255, minClip=500,
  )


async def comparedWorkFile(session, folder):
  """Flat ground with a crate and a placed guard spawn (the server's data, which a client draws only from its own rows), the zone row's
  values, saved as compared.blend in folder; returns its path."""
  await session.expectSuccess("newFile", {"discardUnsavedChanges": True})
  await session.expectSuccess("createTerrainGrid", {"name": "ground", "size": [200, 200], "spacing": 10, "location": [0, 0, 0], "collection": "terrain"})
  await session.expectSuccess("createMaterial", {"name": "grass", "diffuseTexture": str(writePNG(folder / "grass.png", 4, 4, (90, 120, 60, 255)))})
  await session.expectSuccess("createMaterial", {"name": "stone", "diffuseTexture": str(writePNG(folder / "stone.png", 4, 4, (120, 110, 100, 255)))})
  await session.expectSuccess("assignMaterial", {"objectName": "ground", "materialName": "grass"})
  await session.expectSuccess("projectUVs", {"objectName": "ground", "method": "planar", "worldUnitsPerRepeat": 64, "direction": [0, 0, 1]})
  await session.expectSuccess("createPrimitive", {"kind": "cube", "name": "crate", "size": [8, 8, 8], "location": [14, -6, 0]})
  await session.expectSuccess("assignMaterial", {"objectName": "crate", "materialName": "stone"})
  await session.expectSuccess("projectUVs", {"objectName": "crate", "method": "box", "worldUnitsPerRepeat": 8})
  await session.expectSuccess("setZoneProperties", comparedFileValues | serverRowValues(comparedZone))
  await session.expectSuccess("placeSpawn", {"zone": None, "model": "HUM", "name": "guard", "height": 6, "location": list(guardAt), "headingDegrees": 270})
  await session.expectSuccess("saveFile", {"path": str(folder / f"{comparedZone}.blend")})
  return folder / f"{comparedZone}.blend"


@pytest.mark.clientData("calibration")
def testCompareShotMeasuresAShotOfTheOpenWorkFileAndOfItsExportFromItsDump(stageBlenderServer, tmp_path):
  dump = comparedDump()
  zone = eqCalibration.zoneProperties(dump, eqCalibration.viewer(dump)["specialAmbientColor"])
  pitch = math.radians(comparedCamera["pitch"] * 360 / 512)
  eye = [comparedCamera["y"], comparedCamera["x"], comparedCamera["z"]]
  view = {"eye": eye, "target": [eye[0] + math.cos(pitch), eye[1], eye[2] + math.sin(pitch)]}
  export = tmp_path / "export" / f"{comparedZone}.eqg"
  export.parent.mkdir()

  async def steps(session):
    blend = await comparedWorkFile(session, tmp_path)
    await session.expectSuccess("exportZone", {"path": str(export), "purpose": "test"})
    # The shot is what the client would draw of the shipped zone under the dump's header, with the kiln it dropped: the scene set up by
    # hand with the dump's values, the guard gone, and the kiln placed; then the saved file is opened again, as the artist left it.
    await session.expectSuccess("setZoneProperties", zone)
    await session.expectSuccess("deleteObjects", {"names": ["guard"]})
    await session.expectSuccess("placeObject", {"zone": None, "model": "IT10800_ACTORDEF", "name": "kiln", "y": kilnAt[0], "x": kilnAt[1], "z": kilnAt[2], "heading": 100})
    image, _ = await session.expectImage("renderView", {"view": view, "guides": False})
    shot = writeShot(tmp_path / "shots", comparedZone, Image.open(io.BytesIO(image)).convert("RGB"), dump)
    await session.expectSuccess("openFile", {"path": str(blend), "discardUnsavedChanges": True})
    before = await session.expectSuccess("getSceneSummary", {})
    predicted = await session.expectImage("compareShot", {"screenshotPath": str(shot)}, mimeType="image/jpeg")
    after = await session.expectSuccess("getSceneSummary", {})
    archived = await session.expectImage("compareShot", {"screenshotPath": str(shot), "archivePath": str(export)}, mimeType="image/jpeg")
    status = await session.expectSuccess("getToolingStatus")
    otherShot = writeShot(tmp_path / "other", "other", Image.new("RGB", (48, 27)), smallDump())
    refusals = {
      "otherZone": await session.expectError("compareShot", {"screenshotPath": str(otherShot)}),
      "missingArchive": await session.expectError("compareShot", {"screenshotPath": str(shot), "archivePath": str(tmp_path / "missing" / f"{comparedZone}.eqg")}),
    }
    await session.expectSuccess("transformObjects", {"names": ["crate"], "translate": [0, 0, 1]})
    refusals["unsaved"] = await session.expectError("compareShot", {"screenshotPath": str(shot)})
    await session.expectSuccess("newFile", {"discardUnsavedChanges": True})
    refusals["neverSaved"] = await session.expectError("compareShot", {"screenshotPath": str(shot)})
    return blend, before, predicted[1], after, archived[1], status, refusals

  blend, before, predicted, after, archived, status, refusals = stageBlenderServer.session(steps)
  # The work file's own route: the dump's header drives the render, the file's fogEnd and minClip reported as drift, never applied; the
  # guard (left out of the export) is hidden and the dump's kiln kept, so the render is the shot.
  assert predicted["source"] == {"kind": "workFile", "path": str(blend), "zoneDrift": comparedDrift}
  assert predicted["zoneProperties"] == zone and predicted["conditions"]["gameTime"] == "12:00" and predicted["conditions"]["carriedLight"] is None
  assert predicted["placed"] == 1 and predicted["notPlaced"] == [] and predicted["outOfView"] == 0
  assert predicted["comparison"]["meanPixelDifference"] < 0.5 and predicted["comparison"]["meanSkyDifference"] < 0.5
  assert 0.3 < predicted["comparison"]["drawnShare"] < 1 and predicted["fitLight"] is None
  runFolder = Path(predicted["runFolder"])
  assert runFolder.parent == stageBlenderServer.toolingRoot / "calibration" / comparedZone and runFolder.name == predicted["run"]
  assert sorted(path.name for path in runFolder.iterdir()) == ["blocks.npy", "blocks.png", "difference.png", "render.png", "result.json", "sheet.jpg", "shot.png"]
  recorded = json.loads((runFolder / "result.json").read_text(encoding="utf-8"))
  assert recorded["source"] == predicted["source"] and recorded["comparison"] == predicted["comparison"] and [entry["name"] for entry in recorded["placed"]] == ["groundItem5_IT10800_ACTORDEF"]
  # Afterwards the saved work file is open again as it was: no dump collection, no placement, the file's own values, nothing unsaved.
  assert after == before and after["status"] == {"filePath": str(blend), "unsavedChanges": False, "scene": "Scene"}
  assert f"{comparedZone} dump" not in after["collections"] and "guard" in [entry["name"] for entry in after["objects"]]
  # The archive route draws the export in a new file, as verifyExport's archive views do, and comes back to the work file too.
  assert archived["source"] == {"kind": "archive", "path": str(export), "zoneDrift": comparedDrift}
  assert archived["placed"] == 1 and archived["comparison"]["meanPixelDifference"] < 0.5 and archived["comparison"]["meanSkyDifference"] < 0.5
  latest = [entry for entry in status["calibration"] if entry["shot"] == f"{comparedZone}.png"]
  assert [(entry["zone"], entry["runs"], entry["source"], entry["meanPixelDifference"]) for entry in latest] == [
    (comparedZone, 2, {"kind": "archive", "path": str(export)}, archived["comparison"]["meanPixelDifference"]),
  ]
  assert f"The shot is of zone 'highpasshold', but the open work file {blend} has the short name '{comparedZone}'" in refusals["otherZone"]
  assert f"'{tmp_path / 'missing' / f'{comparedZone}.eqg'}' is not an absolute path to an existing .eqg file" in refusals["missingArchive"]
  assert "The open file has unsaved changes; compareShot measures the preview of the work file as saved" in refusals["unsaved"]
  assert "this scene was never saved: save it as a work file (saveFile with a path)" in refusals["neverSaved"]
