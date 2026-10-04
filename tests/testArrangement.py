import math

import numpy

from testModelsAndDressing import freshScene
from testWalkRoute import instancedStepCode

slope = math.radians(20)


def rampHeight(y, rampY=0.0):
  """The top of a 4-thick cube ramp built at the origin of rampY and turned 20 degrees about X: up toward +y."""
  return 4 / math.cos(slope) + (y - rampY) * math.tan(slope)


def rotationMatrix(rotationDegrees):
  """Blender's XYZ euler: X applied first, then Y, then Z."""
  x, y, z = (math.radians(value) for value in rotationDegrees)
  aboutX = numpy.array([[1, 0, 0], [0, math.cos(x), -math.sin(x)], [0, math.sin(x), math.cos(x)]])
  aboutY = numpy.array([[math.cos(y), 0, math.sin(y)], [0, 1, 0], [-math.sin(y), 0, math.cos(y)]])
  aboutZ = numpy.array([[math.cos(z), -math.sin(z), 0], [math.sin(z), math.cos(z), 0], [0, 0, 1]])
  return aboutZ @ aboutY @ aboutX


def byName(result):
  return {copy["name"]: copy for copy in result["copies" if "copies" in result else "settled"]}


def testCopiesSinkToTheLowestGroundUnderTheirFootprintAndFollowItWhenItMoves(stageBlenderServer):
  async def steps(session):
    await freshScene(session)
    await session.expectSuccess("createPrimitive", {"kind": "cube", "name": "ramp", "size": [60, 60, 4], "location": [0, 0, 0], "rotationDegrees": [20, 0, 0]})
    await session.expectSuccess("createPrimitive", {"kind": "cube", "name": "crate", "size": [4, 4, 4], "location": [500, 0, 0]})
    placed = await session.expectSuccess("placeCopies", {"source": "crate", "settle": True, "depth": 0.5, "copies": [
      {"name": "square", "location": [0, 0]}, {"name": "turned", "location": [10, 5], "rotationDegrees": [0, 0, 45]},
    ]})
    await session.expectSuccess("transformObjects", {"names": ["ramp"], "translate": [0, 0, 5]})
    again = await session.expectSuccess("settleObjects", {"names": ["square", "turned"], "depth": 0.5})
    return placed, again

  placed, again = stageBlenderServer.session(steps)
  # A crate's footprint reaches 2 downhill when square and 2*sqrt(2) when turned 45 degrees; its base sinks to the ground there and half a
  # unit more, so its uphill edge is buried and no edge floats.
  for result, rise in ((byName(placed), 0), (byName(again), 5)):
    square, turned = result["square"], result["turned"]
    assert abs(square["location"][2] - (rampHeight(-2) + rise - 0.5)) < 0.005
    assert abs(turned["location"][2] - (rampHeight(5 - 2 * math.sqrt(2)) + rise - 0.5)) < 0.005
    assert square["under"] == [round(rampHeight(-2) + rise, 2), round(rampHeight(2) + rise, 2)]
    assert square["rotationDegrees"] == [0.0, 0.0, 0.0] and turned["rotationDegrees"] == [0.0, 0.0, 45.0]
    assert square["location"][:2] == [0.0, 0.0] and turned["location"][:2] == [10.0, 5.0]


def testCopiesRestOnAnObjectAndLieFlushOnARampKeepingTheirHeading(stageBlenderServer):
  async def steps(session):
    await freshScene(session)
    await session.expectSuccess("createTerrainGrid", {"name": "ground", "size": [300, 300], "spacing": 10, "location": [0, 0, 0]})
    await session.expectSuccess("createPrimitive", {"kind": "cube", "name": "table", "size": [20, 10, 6], "location": [60, 80, 0]})
    await session.expectSuccess("createPrimitive", {"kind": "cube", "name": "ramp", "size": [30, 40, 4], "location": [40, -60, 0], "rotationDegrees": [20, 0, 0]})
    await session.expectSuccess("createPrimitive", {"kind": "cube", "name": "crate", "size": [4, 4, 4], "location": [0, 0, -100]})
    await session.expectSuccess("placeCopies", {"source": "crate", "copies": [
      {"name": "onTable", "location": [54, 80, 50], "rotationDegrees": [0, 0, 15]},
      {"name": "big", "location": [60, 81, 50], "rotationDegrees": [0, 0, -10], "scale": 1.3},
      {"name": "overhanging", "location": [69, 80, 50]},
      {"name": "upRamp", "location": [40, -58, 60]},
      {"name": "turnedOnRamp", "location": [36, -64, 60], "rotationDegrees": [0, 0, 30], "scale": 1.5},
    ]})
    table = await session.expectSuccess("settleObjects", {"names": ["onTable", "big", "overhanging"], "onto": "table"})
    ramp = await session.expectSuccess("settleObjects", {"names": ["upRamp", "turnedOnRamp"], "onto": "ramp", "tiltShare": 1})
    rampAgain = await session.expectSuccess("settleObjects", {"names": ["upRamp", "turnedOnRamp"], "onto": "ramp", "tiltShare": 1})
    return table, ramp, rampAgain

  table, ramp, rampAgain = stageBlenderServer.session(steps)
  # The table's top is 6 up; every crate's base rests there, the one hanging half off the edge too, each still turned as placed.
  table = byName(table)
  assert [copy["location"][2] for copy in table.values()] == [6.0, 6.0, 6.0]
  assert [copy["spans"][0] for copy in table.values()] == [6.0, 6.0, 6.0]
  assert [copy["rotationDegrees"] for copy in table.values()] == [[0.0, 0.0, 15.0], [0.0, 0.0, -10.0], [0.0, 0.0, 0.0]]
  # Leaned all the way, each crate stands square to the ramp (turned 20 degrees about X) with its base on the ramp's top, and keeps the
  # turn it had about its own up; settling again changes nothing.
  ramp = byName(ramp)
  for name, turn in (("upRamp", 0), ("turnedOnRamp", 30)):
    x, y, z = ramp[name]["location"]
    assert abs(z - rampHeight(y, -60)) < 0.005
    expected = rotationMatrix([20, 0, 0]) @ rotationMatrix([0, 0, turn])
    assert numpy.abs(rotationMatrix(ramp[name]["rotationDegrees"]) - expected).max() < 1e-3
  assert ramp["upRamp"]["rotationDegrees"] == [20.0, 0.0, 0.0]
  assert byName(rampAgain) == ramp


def testPatternsPlaceAndFaceCopiesAndJitterRepeatsForASeed(stageBlenderServer):
  jittered = {
    "source": "post", "pattern": {"row": {"from": [0, 50], "to": [60, 50], "count": 6}}, "settle": False,
    "jitter": {"turnDegrees": [-20, 20], "tiltDegrees": [2, 8], "scale": [0.8, 1.2], "position": 3},
  }

  async def steps(session):
    await freshScene(session)
    await session.expectSuccess("createPrimitive", {"kind": "cylinder", "name": "post", "size": [1, 1, 8], "location": [500, 500, 0], "segments": 6})
    results = {}
    for name, pattern in (
      ("out", {"ring": {"center": [10, 20], "radius": 30, "count": 4, "startDegrees": 45, "facing": "out"}}),
      ("in", {"ring": {"center": [10, 20], "radius": 30, "count": 4, "startDegrees": 45, "facing": "in"}}),
      ("along", {"row": {"from": [0, 0], "to": [0, 30], "count": 3, "facing": "along"}}),
      ("spaced", {"row": {"from": [0, 0], "to": [30, 0], "spacing": 12}}),
      ("grid", {"grid": {"center": [0, 0], "size": [20, 10], "spacing": [10, 10], "turnDegrees": 90}}),
      ("route", {"route": {"path": [[0, 0], [20, 0], [20, 20]], "spacing": 10, "offset": 2, "facing": "along"}}),
    ):
      results[name] = (await session.expectSuccess("generateCopies", {"source": "post", "pattern": pattern, "settle": False}))["copies"]
    results["seeded"] = [(await session.expectSuccess("generateCopies", jittered | {"seed": seed}))["copies"] for seed in (7, 7, 8)]
    refused = await session.expectError("generateCopies", jittered | {"settle": True, "tiltShare": 0.5})
    return results, refused

  results, refused = stageBlenderServer.session(steps)

  def places(name):
    return [[round(value, 3) for value in copy["location"]] for copy in results[name]]

  def turns(name):
    return [copy["rotationDegrees"][2] for copy in results[name]]

  corner = round(30 * math.sqrt(0.5), 3)
  assert places("out") == [[round(10 + x, 3), round(20 + y, 3), 0.0] for x, y in ((corner, corner), (-corner, corner), (-corner, -corner), (corner, -corner))]
  assert turns("out") == [45.0, 135.0, -135.0, -45.0]
  assert turns("in") == [-135.0, -45.0, 45.0, 135.0]
  assert places("along") == [[0.0, 0.0, 0.0], [0.0, 15.0, 0.0], [0.0, 30.0, 0.0]] and turns("along") == [90.0, 90.0, 90.0]
  assert places("spaced") == [[0.0, 0.0, 0.0], [12.0, 0.0, 0.0], [24.0, 0.0, 0.0]] and turns("spaced") == [0.0, 0.0, 0.0]
  assert sorted(places("grid")) == [[x, y, 0.0] for x in (-5.0, 5.0) for y in (-10.0, 0.0, 10.0)]
  # The route's copies sit 2 to the right of it, facing along it: east on the first leg, north on the second.
  assert places("route") == [[0.0, -2.0, 0.0], [10.0, -2.0, 0.0], [22.0, 0.0, 0.0], [22.0, 10.0, 0.0], [22.0, 20.0, 0.0]]
  assert turns("route") == [0.0, 0.0, 90.0, 90.0, 90.0]
  first, repeated, other = ([[copy[key] for key in ("location", "rotationDegrees", "scale")] for copy in copies] for copies in results["seeded"])
  assert first == repeated and first != other
  for (x, y, _), (tiltX, tiltY, turn), scale in first:
    assert abs(y - 50) <= 3 and 2 <= abs(tiltX) <= 8 and 2 <= abs(tiltY) <= 8 and -20 <= turn <= 20 and 0.8 <= scale <= 1.2
  assert "jitter tiltDegrees and tiltShare both set each copy's tilt" in refused


def testAKitInstanceSettlesOnTheGroundNotOnItself(stageBlenderServer):
  async def steps(session):
    await freshScene(session)
    await session.expectSuccess("createTerrainGrid", {"name": "ground", "size": [400, 400], "spacing": 10, "location": [0, 0, 0]})
    await session.expectSuccess("runPython", {"code": instancedStepCode})
    return await session.expectSuccess("generateCopies", {"source": "stepInstance", "pattern": {"row": {"from": [-100, -100], "to": [100, -100], "count": 3}}})

  generated = stageBlenderServer.session(steps)
  # The kit's step is 1.5 tall from its base; each copy stands on the flat ground, not on its own step or the one it was copied from.
  assert [copy["location"][2] for copy in generated["copies"]] == [0.0, 0.0, 0.0]
  assert [copy["spans"] for copy in generated["copies"]] == [[0.0, 1.5]] * 3
