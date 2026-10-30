import math
import re
import sys
from pathlib import Path

import numpy
import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import eqSky
import skyDrawing
from conftest import everquestClient

clientRoot = Path(everquestClient)
skyFolder = clientRoot / "Resources" / "Sky"
# A zone's view values a sky does not supply, and the light a zone drawing no sky sets by hand.
viewValues = {"specialAmbientColor": [0, 0, 0], "fogStart": 30, "fogEnd": 2000, "fogDensity": 0.33, "fogOn": True, "maxClip": 4000, "newEngineZone": False}
handLight = {
  "ambientColor": [0.3, 0.3, 0.35], "bounceColor": [0, 0, 0], "sunColor": [0.6, 0.57, 0.5], "sunAzimuthDegrees": 135, "sunElevationDegrees": 45,
  "fogColor": [0.55, 0.6, 0.7],
}


def colorMapPixel(fileName, row, column):
  with Image.open(skyFolder / fileName) as image:
    return numpy.asarray(image.convert("RGBA"), dtype=numpy.int64)[row, column]


def testAZoneWithoutItsOwnSkyGetsTheDefaultSkyAndItsLight(tmp_path):
  state = eqSky.skyState(clientRoot, tmp_path, {"type": "highpasshold", "hour": 13, "minute": 0})
  assert state["chain"] == [
    "sky.ini has no sky type 'highpasshold', so the client uses 'default'",
    "SkySetting-default -> WeatherPattern-DefaultClear (its DefaultWeather)",
    "ColorSet-DefaultClear -> ColorMap-DefaultClearDay",
  ]
  light = [[round(int(value) / 255, 4) for value in colorMapPixel("ColorMap-DefaultDay.dds", row, 31)[:3]] for row in (0, 2, 3, 28)]
  assert state["lightFrom"] == "sun"
  assert state["environment"] == {
    "sunColor": light[0], "fogColor": light[1], "ambientColor": light[2], "bounceColor": light[3],
    "sunAzimuthDegrees": 0.0, "sunElevationDegrees": 75.0,
  }
  # The scene light comes from the 13:00 sun, the sky an hour ahead: its dome turns about the 14:00 sun.
  assert state["dome"]["axis"] == [0.0, 0.5, 0.866025]
  assert state["horizon"]["minAngle"] == 0.067612 and state["horizon"]["maxWidth"] == 0.6
  assert [satellite["name"] for satellite in state["satellites"]] == ["ClearSun", "ClearMoon"]
  sun = state["satellites"][0]
  assert sun["halfExtent"] == 0.3 and [texture["state"] for texture in sun["textures"]] == ["ClearSunDay"]
  assert numpy.load(sun["textures"][0]["path"]).shape == (256, 256, 4)


def testTheMoonLightsTheSceneBefore555AndAfter1820WithTheAmbientRaisedToTheFloor(tmp_path):
  assert [eqSky.lightsByMoon(hour, minute) for hour, minute in ((5, 54), (5, 55), (18, 20), (18, 21))] == [True, False, False, True]
  night = eqSky.skyState(clientRoot, tmp_path, {"type": "default", "hour": 1, "minute": 0})
  assert night["lightFrom"] == "moon"
  assert night["environment"]["sunColor"] == [round(int(value) / 255, 4) for value in colorMapPixel("ColorMap-DefaultNight.dds", 1, 31)[:3]]
  assert night["environment"]["bounceColor"] == [round(int(value) / 255, 4) for value in colorMapPixel("ColorMap-DefaultNight.dds", 29, 31)[:3]]
  # The night map's ambient is black; eqgame.exe raises it to the 0.08 of a character without infravision or ultravision.
  assert colorMapPixel("ColorMap-DefaultNight.dds", 3, 31)[:3].tolist() == [0, 0, 0]
  assert night["environment"]["ambientColor"] == [0.08, 0.08, 0.08]
  # The moon stands opposite the sun, which at 1:00 is 15 degrees past the nadir toward -Y.
  assert night["environment"]["sunAzimuthDegrees"] == 0.0 and night["environment"]["sunElevationDegrees"] == 75.0
  # At 18:10 the sun, below the horizon, still lights the scene.
  dusk = eqSky.skyState(clientRoot, tmp_path, {"type": "default", "hour": 18, "minute": 10})
  assert dusk["lightFrom"] == "sun" and dusk["environment"]["sunElevationDegrees"] == -2.5


def testAColorSetCrossFadesInEightBitsInsideATransition(tmp_path):
  # DefaultClear's day map starts at 0.279999 of a day and fades in over 0.019989: 6:50 is 310 of its 1309 65536ths in, weight 60. The
  # sky runs an hour ahead of the clock, so the clock's 5:50 draws it.
  state = eqSky.skyState(clientRoot, tmp_path, {"type": "default", "weather": "DefaultClear", "hour": 5, "minute": 50})
  assert state["chain"][-1] == "ColorSet-DefaultClear -> ColorMap-DefaultClearDawn fading to ColorMap-DefaultClearDay"
  dawn = colorMapPixel("ColorMap-DefaultDawn.dds", 0, 0)
  day = colorMapPixel("ColorMap-DefaultDay.dds", 0, 0)
  assert state["dome"]["colors"][0] == [round(int(value) / 255, 4) for value in ((day * 60 + dawn * 195) >> 8)[:3]]
  assert dawn[:3].tolist() != day[:3].tolist()


def testAnUnknownWeatherIsRefusedNamingIt(tmp_path):
  try:
    eqSky.skyState(clientRoot, tmp_path, {"type": "default", "weather": "NoSuchWeather", "hour": 12, "minute": 0})
  except ValueError as error:
    assert "WeatherPattern 'NoSuchWeather'" in str(error)
  else:
    raise AssertionError("an unknown weather resolved")


def testARendersSkyTakesAnHourAndAWeatherOverTheZonesAndRefusesTheRest():
  stored = {"type": "highpasshold", "weather": "DefaultClear", "hour": 13, "minute": 0}
  assert skyDrawing.skyAt(stored, {"hour": 1, "minute": 0}) == {"type": "highpasshold", "weather": "DefaultClear", "hour": 1, "minute": 0}
  assert skyDrawing.skyAt(stored, {"hour": 21, "minute": 30, "weather": "DefaultRain"}) == {"type": "highpasshold", "weather": "DefaultRain", "hour": 21, "minute": 30}
  assert skyDrawing.skyAt({"type": "default", "hour": 13, "minute": 0}, {"hour": 0, "minute": 59}) == {"type": "default", "hour": 0, "minute": 59}
  refusals = {
    "hour alone": ({"hour": 1}, "hour and minute both given, got {'hour': 1}"),
    "a stray key": ({"hour": 1, "minute": 0, "type": "default"}, "takes only hour, minute, and weather, not ['type']"),
    "hour 24": ({"hour": 24, "minute": 0}, "sky hour must be 0-23 and minute 0-59, got 24:0"),
    "minute 60": ({"hour": 1, "minute": 60}, "sky hour must be 0-23 and minute 0-59, got 1:60"),
    "a bool hour": ({"hour": True, "minute": 0}, "sky hour must be a int, got True"),
    "an empty weather": ({"hour": 1, "minute": 0, "weather": ""}, "sky weather must be a str, got ''"),
    "not a dict": ([1, 0], "hour and minute both given, got [1, 0]"),
  }
  for override, message in refusals.values():
    with pytest.raises(ValueError, match=re.escape(message)):
      skyDrawing.skyAt(stored, override)
  with pytest.raises(ValueError, match=re.escape("sky must be {type, weather (optional), hour, minute}, got {'type': 'default', 'hour': 1}")):
    skyDrawing.validateSky({"type": "default", "hour": 1})


def flatSky():
  return {
    "environment": {"fogColor": [0.2, 0.3, 0.4]},
    "dome": {"axis": [0.0, 0.0, 1.0], "colors": [[row / 29, row / 29, row / 29] for row in range(30)]},
    "horizon": {"color": [1.0, 1.0, 1.0], "alpha": 0.0, "minAngle": 0.1, "maxAngle": 0.1, "minWidth": 0.5, "maxWidth": 0.5, "minCameraZ": 0.0, "maxCameraZ": 1.0},
    "satellites": [],
  }


def direction(azimuthDegrees, elevationDegrees):
  azimuth, elevation = math.radians(azimuthDegrees), math.radians(elevationDegrees)
  return [math.cos(elevation) * math.sin(azimuth), math.cos(elevation) * math.cos(azimuth), math.sin(elevation)]


def domeShade(angleFromAxisDegrees):
  return numpy.interp(math.radians(angleFromAxisDegrees), skyDrawing.domeRingAngles, skyDrawing.domeRingRows / 29)


def testTheHorizonBandHidesTheDomeBelowItAndFadesAcrossIt():
  sky = flatSky()
  sky["horizon"]["color"] = sky["environment"]["fogColor"]
  # Below the band's foot (0.1 radians) the fog color is opaque; above its top (0.6) only the dome shows; across the band the fog
  # thins to the color map's alpha at its top, 0 here.
  views = numpy.array([direction(0, -10), direction(90, 2), direction(0, 40), direction(0, 89.9), direction(0, 10), direction(0, 20), direction(0, 30)], dtype=numpy.float32)
  colors = skyDrawing.skyColors(views, sky, 0.0, {})
  assert numpy.allclose(colors[0], [0.2, 0.3, 0.4]) and numpy.allclose(colors[1], [0.2, 0.3, 0.4])
  assert numpy.allclose(colors[2], [domeShade(50)] * 3, atol=1e-5)
  assert numpy.allclose(colors[3], [0, 0, 0], atol=2e-3)
  domeShares = [(colors[index, 0] - 0.2) / (domeShade(90 - elevation) - 0.2) for index, elevation in ((4, 10), (5, 20), (6, 30))]
  assert 0 < domeShares[0] < domeShares[1] < domeShares[2] < 1


def testASatelliteIsAQuadSquareToItsDirectionBlendedByItsAlpha():
  sky = flatSky() | {"horizon": None}
  red = numpy.zeros((4, 4, 4), dtype=numpy.uint8)
  red[..., 0] = 255
  red[..., 3] = 255
  sky["satellites"] = [{"direction": [0.0, 1.0, 0.0], "up": [0.0, 0.0, 1.0], "right": [1.0, 0.0, 0.0], "halfExtent": 0.1, "textures": [{"path": "red", "weight": 0.5}]}]
  # The quad spans atan(0.1) = 5.7 degrees each way from its direction.
  views = numpy.array([direction(0, 0), direction(5, 0), direction(7, 0)], dtype=numpy.float32)
  colors = skyDrawing.skyColors(views, sky, 0.0, {"red": red})
  dome = domeShade(90)
  assert numpy.allclose(colors[0], [0.5 * dome + 0.5, 0.5 * dome, 0.5 * dome], atol=1e-5)
  assert numpy.allclose(colors[1], colors[0])
  assert numpy.allclose(colors[2], [dome] * 3, atol=1e-5)


def renderedPixel(path, column, row):
  with Image.open(path) as image:
    return numpy.asarray(image.convert("RGB"), dtype=numpy.float64)[row, column] / 255


def testPreviewsDrawTheZonesSkyAndTakeItsLight(stageBlenderServer, tmp_path):
  handSet = handLight | viewValues
  sky = {"type": "highpasshold", "hour": 13, "minute": 0}
  views = {"towardSun": [0, 100, 100], "awayFromSun": [0, -100, 100], "level": [100, 0, 0]}

  async def steps(session):
    await session.expectSuccess("newFile", {"discardUnsavedChanges": True})
    await session.expectSuccess("setZoneProperties", handSet)
    withSky = await session.expectSuccess("setZoneProperties", {"sky": sky})
    refused = await session.expectError("setZoneProperties", {"ambientColor": [1, 1, 1]})
    rendered = {}
    for name, target in views.items():
      _, description = await session.expectImage("renderView", {"view": {"eye": [0, 0, 0], "target": target}})
      rendered[name] = description["outputPath"]
    removed = await session.expectSuccess("setZoneProperties", {"sky": "none"})
    unlit = await session.expectError("renderView", {"view": {"eye": [0, 0, 0], "target": [0, 100, 0]}})
    return withSky, refused, rendered, removed, unlit

  withSky, refused, rendered, removed, unlit = stageBlenderServer.session(steps)
  assert withSky["replacedBySky"] == ["ambientColor", "bounceColor", "fogColor", "sunAzimuthDegrees", "sunColor", "sunElevationDegrees"]
  assert withSky["zone"]["sky"] == sky and "ambientColor" not in withSky["zone"]
  assert withSky["sky"]["chain"][0] == "sky.ini has no sky type 'highpasshold', so the client uses 'default'"
  assert "The zone's sky supplies ['ambientColor']" in refused
  state = eqSky.skyState(clientRoot, tmp_path, sky)
  textures = {texture["path"]: numpy.load(texture["path"]) for satellite in state["satellites"] for texture in satellite["textures"]}
  # The middle of a 960 x 540 render looks along the view; the camera stands at height 0, the horizon band's lowest.
  expected = skyDrawing.skyColors(numpy.array([direction(0, 45), direction(180, 45), direction(90, 0)], dtype=numpy.float32), state, 0.0, textures)
  measured = [renderedPixel(rendered[name], 480, 270) for name in views]
  for measuredColor, expectedColor in zip(measured, expected):
    assert numpy.abs(measuredColor - expectedColor).max() <= 2 / 255
  assert numpy.abs(measured[0] - measured[1]).max() > 20 / 255
  assert numpy.allclose(measured[2], state["environment"]["fogColor"], atol=1 / 255)
  assert removed["zone"]["sky"] == "none" and removed["sky"] is None
  assert "Zone properties missing" in unlit and "ambientColor" in unlit


def testARenderTakesAnotherHourWithoutChangingTheZonesSky(stageBlenderServer, tmp_path):
  sky = {"type": "highpasshold", "hour": 13, "minute": 0}
  night = {"hour": 1, "minute": 0}
  view = {"eye": [0, 0, 0], "target": [0, 100, 100]}

  async def steps(session):
    await session.expectSuccess("newFile", {"discardUnsavedChanges": True})
    await session.expectSuccess("setZoneProperties", viewValues | {"sky": sky})
    before = await session.expectImage("renderView", {"view": view})
    atNight = await session.expectImage("renderView", {"view": view, "sky": night})
    properties = (await session.expectSuccess("getSceneSummary"))["zoneProperties"]
    after = await session.expectImage("renderView", {"view": view})
    refusals = [
      await session.expectError("renderView", {"view": view, "sky": night | {"weather": "NoSuchWeather"}}),
      await session.expectError("renderView", {"view": view, "sky": {"hour": 1}}),
      await session.expectError("renderView", {"view": view, "sky": night, "shading": "layout"}),
    ]
    await session.expectSuccess("setZoneProperties", {"sky": "none"})
    await session.expectSuccess("setZoneProperties", handLight)
    noSky = await session.expectError("renderView", {"view": view, "sky": night})
    return before, atNight, properties, after, refusals, noSky

  before, atNight, properties, after, refusals, noSky = stageBlenderServer.session(steps)
  # Asked for 1:00, the render draws the 1:00 sky, lit by the moon, and says so; the zone keeps 13:00, and the next render is the
  # 13:00 one again, byte for byte.
  state = eqSky.skyState(clientRoot, tmp_path, sky | night)
  textures = {texture["path"]: numpy.load(texture["path"]) for satellite in state["satellites"] for texture in satellite["textures"]}
  expected = skyDrawing.skyColors(numpy.array([direction(0, 45)], dtype=numpy.float32), state, 0.0, textures)[0]
  nightPixel, dayPixel = renderedPixel(atNight[1]["outputPath"], 480, 270), renderedPixel(before[1]["outputPath"], 480, 270)
  assert state["lightFrom"] == "moon" and numpy.abs(nightPixel - expected).max() <= 2 / 255
  assert numpy.abs(nightPixel - dayPixel).max() > 20 / 255
  assert atNight[1]["sky"] == {"hour": 1, "minute": 0, "weather": "DefaultClear", "lightFrom": "moon"}
  assert "sky" not in before[1] and "sky" not in after[1]
  assert properties["sky"] == sky
  assert after[0] == before[0]
  assert "WeatherPattern 'NoSuchWeather'" in refusals[0]
  assert "hour and minute both given" in refusals[1]
  assert "A sky lights only client shading, not 'layout'" in refusals[2]
  assert "The zone draws no sky, so no hour can light it; setZoneProperties sets one" in noSky
