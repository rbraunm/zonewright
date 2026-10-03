from testModelsAndDressing import freshScene


def testWalkRouteCrossesADeckAndFindsDropsAndLowCeilings(stageBlenderServer):
  async def steps(session):
    await freshScene(session)
    await session.expectSuccess("createTerrainGrid", {"name": "ground", "size": [400, 200], "spacing": 8, "location": [0, 0, 0], "collection": "terrain"})
    await session.expectSuccess("deleteFaces", {"objectName": "ground", "selector": {"box": {"minimum": [-62, -200, -10], "maximum": [62, 200, 10]}}})
    await session.expectSuccess("createPrimitive", {"kind": "cube", "name": "deck", "size": [200, 24, 15], "location": [0, 0, -15.5]})
    await session.expectSuccess("createPrimitive", {"kind": "cube", "name": "ledge", "size": [70, 20, 4], "location": [-145, 70, 5]})
    across = await session.expectSuccess("walkRoute", {"path": [[-150, 0, 0], [150, 0, 0]]})
    beside = await session.expectSuccess("walkRoute", {"path": [[-150, 40, 0], [150, 40, 0]]})
    under = await session.expectSuccess("walkRoute", {"path": [[-190, 70, 0], [-100, 70, 0]]})
    return across, beside, under

  across, beside, under = stageBlenderServer.session(steps)
  # Over the chasm the footing is the 24-wide deck, half a unit below the ground it runs under at each end.
  assert across["walkable"] and across["problems"] == [] and across["lowestHeadroom"] is None
  assert abs(across["length"] - 300) < 1 and across["steepest"]["slopeDegrees"] < 10
  assert 20 <= across["narrowest"]["width"] <= 24 and abs(across["narrowest"]["at"][0]) < 64
  # Beside the deck the ground ends at the chasm's edge, 64 from the middle.
  assert not beside["walkable"] and len(beside["problems"]) == 1 and beside["problems"][0].startswith("drop")
  assert -68 <= beside["length"] - 150 <= -60
  # Under the ledge, whose underside is 5 above the ground, there is not room for a player 6 tall.
  assert not under["walkable"] and under["lowestHeadroom"]["headroom"] == 5.0
  assert all(problem.startswith("headroom 5.0") for problem in under["problems"])
