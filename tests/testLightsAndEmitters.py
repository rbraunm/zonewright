import collections
import json
import math
import struct
import sys
from pathlib import Path

import numpy
import pytest
from PIL import Image

from conftest import writePNG

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import clientPointLights
import emitterParticles
import eqEmitterDefinitions
import eqEmitters

repositoryRoot = Path(__file__).resolve().parent.parent
everquestClient = Path(json.loads((repositoryRoot / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]["zonewright"]["env"]["EVERQUEST_CLIENT"])
texture = (200, 150, 100, 255)
darkEnvironment = {
  "ambientColor": [0.1, 0.1, 0.1], "specialAmbientColor": [0, 0, 0], "bounceColor": [0, 0, 0], "sunColor": [0, 0, 0], "sunAzimuthDegrees": 0,
  "sunElevationDegrees": 30, "fogColor": [0, 0, 0], "fogStart": 0, "fogEnd": 1000, "fogDensity": 0, "fogOn": False, "maxClip": 2000,
  "newEngineZone": False, "sky": "none",
}


def light(name, position, color, radius):
  return {"name": name, "position": position, "color": color, "radius": radius}


def testAPointLightFallsOffWithTheSquareOfItsDistanceOverItsRadius():
  lamp = light("LIB_lamp", (0.0, 0.0, 0.0), (1.0, 0.5, 0.25), 20.0)
  positions = [(10, 0, 0), (10, 0, 0), (6, 8, 0), (20, 0, 0), (30, 0, 0)]
  normals = [(-1, 0, 0), (1, 0, 0), (0, -1, 0), (-1, 0, 0), (-1, 0, 0)]
  added = clientPointLights.addedLight(positions, normals, [lamp])
  # Half the radius away and facing the light: 1 - (1/2)^2 of its color. Facing away: none. At 10, turned 0.8 toward it: 0.8 of that.
  # At the radius and beyond: none.
  expected = [[0.75 * channel for channel in lamp["color"]], [0, 0, 0], [0.6 * channel for channel in lamp["color"]], [0, 0, 0], [0, 0, 0]]
  assert numpy.allclose(added, expected, atol=1e-12)


def testADrawTakesTheThreeLightsScoringHighestAtItsCenter():
  white, red = (1.0, 1.0, 1.0), (1.0, 0.0, 0.0)
  lights = [
    light("near", (5, 0, 0), white, 10), light("middle", (0, 6, 0), white, 10), light("redder", (0, 0, 7), red, 10),
    light("far", (-8, 0, 0), white, 10), light("outOfReach", (20, 0, 0), white, 10),
  ]
  chosen = clientPointLights.selectForDraw(lights, (0, 0, 0), numpy.array([-1, -1, -1]), numpy.array([1, 1, 1]), True)
  # Scores are 2 * lightness * radius^2 / distance^2: 8, 5.6, 2.0 (red's lightness is half white's), and 3.1; the last light does not
  # reach the draw's box.
  assert [entry["name"] for entry in chosen] == ["near", "middle", "far"]


def testGeometryWithBakedLightTakesOnlyTheLightsMarkedForIt():
  lights = [light(name, (0, 0, 0), (1, 1, 1), 10) for name in ("LIB_torch", "lib_lamp", "LIT_torch", "LI", "xxB")]
  assert [entry["name"] for entry in clientPointLights.eligible(lights, False)] == ["LIB_torch", "lib_lamp", "xxB"]
  assert [entry["name"] for entry in clientPointLights.eligible(lights, True)] == ["LIB_torch", "lib_lamp", "LIT_torch", "LI", "xxB"]


def testACarriedLightTakesItsTypesColorAndReachAndStandsByTheFeet():
  torch = clientPointLights.carriedLight(2, [10.0, 20.0, 30.0], 90.0, 383)
  # Facing +X the heading is 0 of 512, so the client's tables put the light exactly 2 toward +X and 4 above the feet.
  assert torch == {
    "name": "carried torch", "position": [12.0, 20.0, 34.0], "color": [0.9959999918937683, 0.9412000179290771, 0.6273999810218811],
    "radius": 100.0, "carriedByViewer": True,
  }
  # Facing -Y (heading 384 of 512, 4.71 once turned to radians) the offset turns by 4 of the table's 512 steps, not by three quarters.
  turned = clientPointLights.carriedLight(14, [0.0, 0.0, 0.0], 180.0, 383)
  assert numpy.allclose(turned["position"], [2 * math.cos(2 * math.pi * 4 / 512), 2 * math.sin(2 * math.pi * 4 / 512), 4.0], atol=1e-12)
  assert turned["color"] == [0.5, 0.25999999046325684, 0.0] and turned["radius"] == 150.0
  # Zones 121 and 158 cap the reach at 25 and 100; a lantern of type 8 reaches 400 elsewhere.
  assert [clientPointLights.carriedLight(8, [0, 0, 0], 0.0, zone)["radius"] for zone in (121, 158, 383)] == [25.0, 100.0, 400.0]
  with pytest.raises(ValueError, match="carried light type"):
    clientPointLights.carriedLight(0, [0, 0, 0], 0.0, 383)


def testTheViewersLightOutranksTheZonesAndLightsBakedGeometry():
  white = (1.0, 1.0, 1.0)
  carried = clientPointLights.carriedLight(2, [-50.0, 0.0, -4.0], 90.0, 383)
  zoneLights = [light("LIB_a", (1, 0, 0), white, 10), light("LIB_b", (0, 1, 0), white, 10), light("LIB_c", (0, 0, 1), white, 10)]
  # Each zone light scores 2 * 1 * 100 / 1 = 200 at the center; the carried torch, 48 away, 2 * 0.81 * 100^2 * 100 / 48^2 = 703.
  chosen = clientPointLights.selectForDraw(zoneLights + [carried], (0, 0, 0), numpy.array([-1, -1, -1]), numpy.array([1, 1, 1]), False)
  assert [entry["name"] for entry in chosen] == ["carried torch", "LIB_a", "LIB_b"]
  assert clientPointLights.eligible([carried, light("LIT_x", (0, 0, 0), white, 10)], False) == [carried]


def testEachVertexOfARegionTakesTheThreeLightsScoringHighestAtIt():
  white = (1.0, 1.0, 1.0)
  first, second = (0.0, 0.0, 0.0), (100.0, 0.0, 0.0)
  lights = [
    light("a", (0, 0, 4), white, 10), light("b", (0, 0, 5), white, 10), light("c", (0, 0, 6), white, 10), light("d", (0, 0, 9), white, 10),
    light("e", (100, 0, 5), (0.0, 1.0, 1.0), 10),
  ]
  added = clientPointLights.addedLightPerVertex([first, second], [(0, 0, 1), (0, 0, 1)], lights)
  # The first vertex takes a, b, and c, the nearest of its four; d, farthest, scores lowest. The second takes only e.
  assert numpy.allclose(added[0], clientPointLights.addedLight([first], [(0, 0, 1)], lights[:3])[0], atol=1e-12)
  assert numpy.allclose(added[1], [0, 0.75, 0.75], atol=1e-12)
  assert added[0][0] < clientPointLights.addedLight([first], [(0, 0, 1)], lights[:4])[0][0]


def definitionWith(**values):
  """One definition read back through the .edd reader from a record holding the given fields (zero elsewhere)."""
  record = bytearray(eqEmitterDefinitions.recordBytes)
  record[:4] = b"test"
  record[eqEmitterDefinitions.textureOffset:eqEmitterDefinitions.textureOffset + 8] = b"test.dds"
  offsets = {name: (offset, kind) for name, offset, kind in eqEmitterDefinitions.fields}
  for name, value in values.items():
    offset, kind = offsets[name]
    struct.pack_into("<i" if kind == "i" else "<f", record, offset, value)
  return eqEmitterDefinitions.parseDefinitions(eqEmitterDefinitions.magic + eqEmitterDefinitions.version + bytes(record), "test.edd")[0]


steadyFields = {"particleLife": 2.0, "eventRate": 10.0, "perEvent": 3, "maximumAlpha": 1.0, "sizeScale": 1.0, "widthMinimum": 1.0, "widthMaximum": 1.0, "heightMinimum": 1.0, "heightMaximum": 1.0}


def testASteadyEmitterHoldsEveryParticleItsEventsLeaveAlive():
  definition = definitionWith(**steadyFields)
  particles = emitterParticles.steadyParticles(definition, "steady", (0, 0, 0), 4000000, (0, -5, 0))
  ages = sorted(particle["age"] for particle in particles)
  # Ten events a second of three particles each, every particle living two seconds: twenty events' worth alive at any moment.
  assert emitterParticles.capacityFor(definition, 4000000) == 63
  assert len(particles) == 60
  assert ages[-1] < 2.0
  assert numpy.allclose(numpy.diff(ages[::3]), 0.1)


def testParticlesTravelByTheirVelocityAccelerationGravityAndDrift():
  definition = definitionWith(**steadyFields, velocityAMinimum=2.0, velocityAMaximum=2.0, accelerationA=1.0, gravity=4.0, driftX=0.5)
  particles = emitterParticles.steadyParticles(definition, "moving", (10, 20, 30), 4000000, (10, 15, 30))
  for particle in particles:
    age = particle["age"]
    expected = (10 + 0.5 * age, 20, 30 + 2 * age + 0.5 * age * age - 0.5 * 4 * age * age)
    assert numpy.allclose(particle["center"], expected, atol=1e-9)


def testParticleOpacityFadesInAndOutAndStopsAtItsMaximum():
  definition = definitionWith(**(steadyFields | {"maximumAlpha": 0.8}), alphaFadeIn=0.5, alphaFadeOut=0.5)
  particles = emitterParticles.steadyParticles(definition, "fading", (0, 0, 0), 4000000, (0, -5, 0))
  for particle in particles:
    age = particle["age"]
    opacity = min(min(age / 0.5, 1.0) * min((2.0 - age) / 0.5, 1.0), 0.8)
    assert particle["color"][3] == round(opacity * 255) / 255
  assert max(particle["color"][3] for particle in particles) == 204 / 255


def testFramesStepThroughTheTexturesCellsAtTheirRate():
  # Frames are the rate times the age rounded as the FPU rounds (half to even): 4.8 is frame 5 of 16; 2.5 is frame 2 of 4; 2 of 8.
  assert emitterParticles.frameCell(definitionWith(frames=16, framesPerSecond=16.0), 0.3) == (0.25, 0.25, 0.25, 0.25)
  assert emitterParticles.frameCell(definitionWith(frames=4, framesPerSecond=10.0), 0.25) == (0.0, 0.5, 0.5, 0.5)
  assert emitterParticles.frameCell(definitionWith(frames=8, framesPerSecond=1.0), 2.0) == (0.5, 0.0, 0.25, 0.5)


def testBillboardsWhoseTurnIsNotTracedAreRefused():
  with pytest.raises(ValueError, match="billboard mode 3"):
    emitterParticles.steadyParticles(definitionWith(**steadyFields, billboard=3), "odd", (0, 0, 0), 4000000, (0, -5, 0))
  with pytest.raises(ValueError, match="velocity"):
    emitterParticles.steadyParticles(definitionWith(**steadyFields, velocityAligned=1), "aligned", (0, 0, 0), 4000000, (0, -5, 0))


def clientDefinitions():
  path = eqEmitterDefinitions.environmentDefinitionsPath(everquestClient)
  return eqEmitterDefinitions.parseDefinitions(path.read_bytes(), path.name)


@pytest.mark.clientData("clientFiles")
def testTheClientsEmitterDefinitionsReadAsItsParticleCodeReadsThem():
  definitions = clientDefinitions()
  assert len(definitions) == 626
  assert (definitions[0]["name"], definitions[0]["texture"]) == ("None", "nul")
  torch, mist, smoke = definitions[57], definitions[99], definitions[65]
  assert {key: torch[key] for key in ("name", "texture", "additive", "noDepthWrite", "billboard", "shape", "particleLife", "eventRate", "perEvent", "burst", "frames", "framesPerSecond", "velocityAMinimum", "widthMinimum", "heightMaximum", "alphaFadeOut", "sizeFadeOut", "lodDistance")} == {
    "name": "new_torch_flame", "texture": "new_flame.dds", "additive": 1, "noDepthWrite": 1, "billboard": 0, "shape": 5, "particleLife": 1.0,
    "eventRate": 12.0, "perEvent": 1, "burst": 1, "frames": 16, "framesPerSecond": 16.0, "velocityAMinimum": 3.5, "widthMinimum": 2.0,
    "heightMaximum": 2.0, "alphaFadeOut": pytest.approx(0.4), "sizeFadeOut": pytest.approx(0.8), "lodDistance": 80.0,
  }
  assert {key: mist[key] for key in ("name", "texture", "additive", "shape", "shapeRadius", "gravity", "accelerationA", "maximumAlpha", "widthMaximum", "spinMinimum")} == {
    "name": "geyserbottom", "texture": "mist_white.dds", "additive": 0, "shape": 4, "shapeRadius": 30.0, "gravity": 150.0, "accelerationA": 50.0,
    "maximumAlpha": pytest.approx(0.9), "widthMaximum": 60.0, "spinMinimum": -200.0,
  }
  assert [smoke[key] for key in ("startRed", "endRed", "emitterScaled", "particleLife")] == [125, 255, 1, 7.0]


@pytest.mark.clientData("clientFiles")
def testTheClientsEmitterListsReadEachFieldByPosition():
  lifespans, visible = collections.Counter(), collections.Counter()
  for path in sorted(everquestClient.iterdir()):
    if path.name.lower().endswith(eqEmitters.listSuffix):
      text = path.read_text(encoding="latin-1")
      if tuple(field.strip() for field in text.splitlines()[0].split("^")) == eqEmitters.longHeader:
        for emitter in eqEmitters.parseEmitters(text, path.name):
          lifespans[emitter["lifespan"]] += 1
          visible[emitter["alwaysVisible"]] += 1
  # The seven-field lists label their sixth field AlwaysVisible and their seventh Lifespan; the client reads the sixth as the lifespan,
  # which holds the lifespans the six-field lists hold, and the seventh as a switch.
  assert lifespans == {4000000: 9763, 9999999: 1186, 999999: 266, 0: 230, 225: 2, 28: 1}
  assert visible == {0: 10843, 1: 605}


@pytest.mark.clientData("clientFiles")
def testTheEmitterDefinitionsTheClientsZonesPlaceFindTheirTextures():
  definitions = clientDefinitions()
  used = set()
  for path in everquestClient.iterdir():
    if path.name.lower().endswith(eqEmitters.listSuffix):
      used |= {emitter["definition"] for emitter in eqEmitters.parseEmitters(path.read_text(encoding="latin-1"), path.name)}
  files = eqEmitterDefinitions.textureFiles(everquestClient)
  missing = {(index, definitions[index]["texture"]) for index in used if index < len(definitions) and definitions[index]["texture"].lower() not in files}
  # Found ignoring case in SpellEffects, EnvEmitterEffects, or ActorEffects; these few name textures the client does not hold.
  assert missing == {(0, "nul"), (154, "wisp_grey01.dds"), (400, "fire01_gray.dds"), (401, "fire01_gray.dds"), (402, "fire01_gray.dds"), (495, "shadow04.dds")}
  assert sorted(index for index in used if index >= len(definitions)) == [966, 1003, 1015, 4442777]


async def centerPixel(session, view):
  _, description = await session.expectImage("renderView", {"view": view})
  pixels = numpy.asarray(Image.open(description["outputPath"]).convert("RGB"), dtype=numpy.float64) / 255
  height, width = pixels.shape[:2]
  return pixels[height // 2, width // 2], description


def testPointLightsLightWhatTheyReachAsTheClientDoes(stageBlenderServer, tmp_path):
  texturePath = writePNG(tmp_path / "stone.png", 8, 8, texture)
  lights = [
    light("LIB_lamp", [-8, 0, 10], [1, 0.5, 0.25], 20), light("LIT_skipped", [12, 0, 10], [0, 1, 0], 20),
    light("LIT_slab", [40, 0, 10], [0.5, 0.5, 1], 20), light("LIT_left", [40, 12, 10], [1, 1, 1], 20), light("LIT_right", [40, -12, 10], [1, 1, 1], 20),
    light("LIT_fourth", [40, 0, 18], [1, 1, 1], 20),
  ]

  async def steps(session):
    await session.expectSuccess("newFile", {"discardUnsavedChanges": True})
    await session.expectSuccess("createTerrainGrid", {"name": "ground", "size": [40, 40], "spacing": 4, "location": [0, 0, 0], "collection": "terrain"})
    await session.expectSuccess("createPrimitive", {"kind": "grid", "name": "slab", "size": [8, 8, 0], "location": [40, 0, 0], "divisions": [2, 2]})
    await session.expectSuccess("createMaterial", {"name": "stone", "diffuseTexture": str(texturePath)})
    for name in ("ground", "slab"):
      await session.expectSuccess("assignMaterial", {"objectName": name, "materialName": "stone"})
    await session.expectSuccess("setZoneProperties", darkEnvironment)
    await session.expectSuccess("placeLights", {"lights": lights})
    return [await centerPixel(session, {"eye": [x, 0, 30], "target": [x, 0.001, 0]}) for x in (-8, 12, 40)]

  (lamp, lampDescription), (skipped, _), (slab, _) = stageBlenderServer.session(steps)
  assert lampDescription["pointLights"] == {"lights": 6, "litObjects": 2, "notLit": []}

  def expected(light):
    return [channel / 255 * min(1.0, value) for channel, value in zip(texture, light)]

  # The terrain under the LIB_ light, half its radius below it, takes 1 - (1/2)^2 of its color over the ambient 0.1.
  assert numpy.allclose(lamp, expected([0.1 + 0.75 * channel for channel in (1, 0.5, 0.25)]), atol=1.5 / 255)
  # Terrain carries baked light in the client, so it takes no LIT_ light: only the ambient.
  assert numpy.allclose(skipped, expected([0.1, 0.1, 0.1]), atol=1.5 / 255)
  # The slab is a placed object and takes every light, but only the three scoring highest at its origin: the one over it, and the two
  # beside it (each 10 down and 12 across: 10 / sqrt(244) facing, 1 - 244/400 reach); the fourth, farthest, adds nothing though it reaches.
  beside = 2 * (10 / math.sqrt(244)) * (1 - 244 / 400)
  assert numpy.allclose(slab, expected([0.1 + 0.75 * channel + beside for channel in (0.5, 0.5, 1)]), atol=1.5 / 255)


def testTheViewersCarriedLightLightsTheTerrainAsTheClientDraws(stageBlenderServer, tmp_path):
  texturePath = writePNG(tmp_path / "stone.png", 8, 8, texture)
  view = {"eye": [0, 0, 30], "target": [0, 0.001, 0]}
  # A torch carried 2 toward +X and 4 above feet at (-2, 0, 6) stands 10 over the ground's middle.
  torch = {"lightType": 2, "at": [-2, 0, 6], "headingDegrees": 90}

  async def steps(session):
    await session.expectSuccess("newFile", {"discardUnsavedChanges": True})
    await session.expectSuccess("createTerrainGrid", {"name": "ground", "size": [40, 40], "spacing": 4, "location": [0, 0, 0], "collection": "terrain"})
    await session.expectSuccess("createMaterial", {"name": "stone", "diffuseTexture": str(texturePath)})
    await session.expectSuccess("assignMaterial", {"objectName": "ground", "materialName": "stone"})
    await session.expectSuccess("setZoneProperties", darkEnvironment)
    withoutZoneId = await session.expectError("renderView", {"view": view, "carriedLight": torch})
    await session.expectSuccess("setZoneProperties", {"zoneId": 383})
    unlit = await centerPixel(session, view)
    _, description = await session.expectImage("renderView", {"view": view, "carriedLight": torch})
    pixels = numpy.asarray(Image.open(description["outputPath"]).convert("RGB"), dtype=numpy.float64) / 255
    return withoutZoneId, unlit, pixels[pixels.shape[0] // 2, pixels.shape[1] // 2], description

  withoutZoneId, (unlit, _), lit, description = stageBlenderServer.session(steps)
  assert "set zoneId with setZoneProperties" in withoutZoneId
  carried = clientPointLights.carriedLight(2, [-2, 0, 6], 90, 383)
  assert description["carriedLight"] == carried and carried["position"] == [0.0, 0.0, 10.0]
  assert description["pointLights"] == {"lights": 1, "litObjects": 1, "notLit": []}
  assert numpy.allclose(unlit, [channel / 255 * 0.1 for channel in texture[:3]], atol=1.5 / 255)
  # Terrain carries baked light and takes only the lights marked for it, which a carried light is: 10 below it, facing it, 1 - (10/100)^2
  # of its color over the ambient 0.1, clamped to 1.
  light = [min(1.0, 0.1 + 0.99 * channel) for channel in carried["color"]]
  assert numpy.allclose(lit, [channel / 255 * value for channel, value in zip(texture, light)], atol=1.5 / 255)


@pytest.mark.clientData("clientFiles")
def testATextureDrawnBlendedAndAddedDrawsAgain(stageBlenderServer):
  # Definitions 50 (added) and 204 (blended), both Guardian of the Gears' smoke, share pt_add_smoke.dds: one image, two materials.
  definitionsPath = eqEmitterDefinitions.environmentDefinitionsPath(everquestClient)
  definitions = eqEmitterDefinitions.parseDefinitions(definitionsPath.read_bytes(), definitionsPath.name)
  assert [(definitions[index]["texture"].lower(), definitions[index]["additive"]) for index in (50, 204)] == [("pt_add_smoke.dds", 1), ("pt_add_smoke.dds", 0)]
  view = {"eye": [0, -30, 3], "target": [0, 0, 3]}
  emitters = [
    {"name": "added", "position": [-3, 0, 0], "definition": 50, "lifespan": 4000000},
    {"name": "blended", "position": [3, 0, 0], "definition": 204, "lifespan": 4000000},
  ]

  async def steps(session):
    await session.expectSuccess("newFile", {"discardUnsavedChanges": True})
    await session.expectSuccess("setZoneProperties", darkEnvironment)
    await session.expectSuccess("placeEmitters", {"emitters": emitters})
    _, first = await session.expectImage("renderView", {"view": view})
    _, second = await session.expectImage("renderView", {"view": view})
    return first, second

  first, second = stageBlenderServer.session(steps)
  assert first["emitters"]["emitters"] == second["emitters"]["emitters"] == 2 and first["emitters"]["notDrawn"] == []


@pytest.mark.clientData("clientFiles")
def testEmittersDrawTheirParticlesWhereTheyStand(stageBlenderServer, tmp_path):
  texturePath = writePNG(tmp_path / "stone.png", 8, 8, texture)
  view = {"eye": [0, -8, 3], "target": [0, 0, 3]}
  emitters = [
    {"name": "flame", "position": [0, 0, 2], "definition": 57, "lifespan": 4000000},
    {"name": "neverMade", "position": [10, 0, 2], "definition": 57, "lifespan": 0},
    {"name": "unknown", "position": [-10, 0, 2], "definition": 9999, "lifespan": 4000000},
    {"name": "negative", "position": [-12, 0, 2], "definition": -7, "lifespan": 0},
  ]

  async def steps(session):
    await session.expectSuccess("newFile", {"discardUnsavedChanges": True})
    await session.expectSuccess("createPrimitive", {"kind": "cube", "name": "wall", "size": [60, 2, 30], "location": [0, 20, -5]})
    await session.expectSuccess("createMaterial", {"name": "stone", "diffuseTexture": str(texturePath)})
    await session.expectSuccess("assignMaterial", {"objectName": "wall", "materialName": "stone"})
    await session.expectSuccess("setZoneProperties", darkEnvironment)
    await session.expectSuccess("placeEmitters", {"emitters": emitters})
    _, drawn = await session.expectImage("renderView", {"view": view})
    await session.expectSuccess("deleteObjects", {"names": ["flame"]})
    _, without = await session.expectImage("renderView", {"view": view})
    return drawn, without

  drawn, without = stageBlenderServer.session(steps)
  # new_torch_flame makes 12 events a second of one particle living one second: 12 alive, all drawn this near (its detail distance is 80).
  assert drawn["emitters"] | {"notDrawn": None} == {"emitters": 1, "particles": 12, "notDrawn": None}
  # The client checks the index before the lifespan: a negative index with a lifespan of 0 is refused for its index.
  assert sorted(drawn["emitters"]["notDrawn"], key=lambda group: group["reason"]) == [
    {"reason": "definition -7: the client makes no emitter for a negative definition index", "count": 1, "emitters": ["negative"]},
    {"reason": "definition 9999 is past the client's 626 environment emitter definitions", "count": 1, "emitters": ["unknown"]},
    {"reason": "lifespan 0: the client makes an emitter only for a lifespan above 0", "count": 1, "emitters": ["neverMade"]},
  ]
  assert without["emitters"]["emitters"] == 0
  # The flames add their light (they are additive) over the emitter, which stands in the middle of the view a unit below the eye, 8 away:
  # its row is a quarter of the way from the middle to the bottom over the tangent of half the 46.5-degree view. They rise from it.
  added = (numpy.asarray(Image.open(drawn["outputPath"]).convert("RGB"), dtype=numpy.float64) - numpy.asarray(Image.open(without["outputPath"]).convert("RGB"), dtype=numpy.float64)).sum(2)
  assert added.min() >= -3
  rows, columns = numpy.nonzero(added > 30)
  weights = added[rows, columns]
  height, width = added.shape
  emitterRow = height / 2 + height / 2 * (1 / 8) / math.tan(math.radians(46.5 / 2))
  assert abs(numpy.average(columns, weights=weights) - width / 2) < 0.02 * width
  assert numpy.average(rows, weights=weights) < emitterRow
  assert added.max() > 200
