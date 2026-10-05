import json
import re
from pathlib import Path

import pytest

import structurePlots
from conftest import everquestClient, pinnedBlender, writePNG, zoneSurveySkill
from testSpans import bridgeArguments, legs, ropes

pytestmark = pytest.mark.clientData("survey")
procedureVersionPattern = re.compile(r"^Interpretive procedure version: (\d+)$", re.MULTILINE)


def stageSurveyServer(stageServer):
  return stageServer({"blender": pinnedBlender, "extensions": {}})


def stagedSkillPath(server):
  return server.repositoryPath / zoneSurveySkill / "SKILL.md"


def procedureVersion(server):
  return int(procedureVersionPattern.search(stagedSkillPath(server).read_text(encoding="utf-8")).group(1))


def befallenInterpretation(screenshotFolder):
  """A complete interpretation of befallen; its screenshots are small PNGs, as the record only keeps them."""
  screenshotFolder.mkdir()
  paths = [str(writePNG(screenshotFolder / f"view{number}.png", 4, 4, (40 * number, 60, 90, 255))) for number in range(5)]
  return {
    "zoneType": "tightDungeon",
    "character": {"description": "A sunken keep of damp stone, its halls lit by guttering torches.", "tags": ["dark", "undead", "ruins"]},
    "areas": [
      {"name": "Entry halls", "center": [-40, 10, 0], "where": "The north end, at the door", "what": "Low stone halls", "connections": [
        {"to": "Lower crypts", "by": "a stair down"}, {"to": "zone:commons", "by": "the front door's zone line"},
      ]},
      {"name": "Lower crypts", "center": [-120, 60, -40], "where": "South of the halls, a level down", "what": "Burial vaults", "connections": [
        {"to": "Entry halls", "by": "the stair up"},
      ]},
    ],
    "landmarks": [{"name": "Iron gate", "location": [-35, 5, 0], "what": "A rusted portcullis", "significance": "The only way in"}],
    "definingCharacteristics": ["Short sight lines", "Two levels joined by one stair"],
    "screenshots": [
      {"path": paths[0], "view": "map", "caption": "The whole zone from above", "shows": []},
      {"path": paths[1], "view": "oblique", "caption": "The halls from above the gate", "shows": ["Entry halls", "Iron gate"]},
      {"path": paths[2], "view": "eyeLevel", "caption": "Inside the halls, facing south", "shows": ["Entry halls"]},
      {"path": paths[3], "view": "oblique", "caption": "The crypts from above the stair", "shows": ["Lower crypts"]},
      {"path": paths[4], "view": "eyeLevel", "caption": "In the crypts, facing the stair", "shows": ["Lower crypts"]},
    ],
    "structuredValues": {"stairDrop": {"value": 40, "unit": "units", "how": "measure from the hall floor to the crypt floor"}},
  }


def rowsByVariant(result):
  return {row["variant"]: row for row in result["rows"]}


def testTechnicalLaneMeasuresEachZoneFormat(stageServer):
  server = stageSurveyServer(stageServer)
  result, progressMessages = server.callToolExpectingSuccess("surveyZones", {
    "zones": ["gfaydark", "befallen", "arena2", "steamfontmts"],
    "groups": ["dimensions", "verticality", "regions", "content"],
    "sortBy": "dimensions.triangleCount",
  })
  assert [row["variant"] for row in result["rows"]] == ["steamfontmts:eqtzp", "gfaydark:wld", "arena2:eqgz:loose", "befallen:wld"]
  rows = rowsByVariant(result)
  assert rows["gfaydark:wld"]["dimensions"]["terrainSize"] == [5521.0, 5459.4, 900.3]
  assert rows["gfaydark:wld"]["dimensions"]["triangleCount"] == 74981
  assert rows["arena2:eqgz:loose"]["dimensions"]["terrainSize"] == [2392.8, 2255.2, 396.7]
  assert rows["arena2:eqgz:loose"]["dimensions"]["allGeometrySize"] == [5831.2, 7840.0, 737.3]
  assert rows["steamfontmts:eqtzp"]["dimensions"]["terrainSize"] == [4608.0, 5760.0, 472.7]
  assert rows["steamfontmts:eqtzp"]["dimensions"]["tileShape"] == {"quadsPerTile": 16, "unitsPerVertex": 12.0}
  assert rows["befallen:wld"]["verticality"]["enclosedShare"] > 0.95
  assert rows["gfaydark:wld"]["verticality"]["enclosedShare"] < 0.05
  assert rows["gfaydark:wld"]["regions"] == {"regionCount": 9, "regionsByKind": {"zoneLine": 9}, "zoneLines": None}
  assert rows["gfaydark:wld"]["content"]["topModels"][0] == {"model": "nekpine1_actordef", "count": 1247}
  assert rows["steamfontmts:eqtzp"]["content"]["topModels"][0] == {"model": "obp_stmfnt_pine.mod", "count": 194}
  assert progressMessages[0] == "checking zone file hashes"
  assert sorted(progressMessages[1:]) == ["measured arena2:eqgz:loose", "measured befallen:wld", "measured gfaydark:wld", "measured steamfontmts:eqtzp"]


def testCacheRecomputesOnlyStaleGroups(stageServer):
  server = stageSurveyServer(stageServer)
  server.callToolExpectingSuccess("surveyZones", {"zones": ["befallen"], "groups": ["dimensions", "regions"]})
  cachePath = server.toolingRoot / "survey" / "zoneSurvey.json"
  cache = json.loads(cachePath.read_text(encoding="utf-8"))
  entry = cache["variants"]["befallen:wld"]
  assert sorted(entry) == ["fileHashes", "measured"]
  assert sorted(entry["measured"]) == ["dimensions", "regions"]
  assert len(entry["fileHashes"]) >= 1
  entry["measured"]["dimensions"]["value"]["triangleCount"] = -1
  entry["measured"]["regions"] = {"version": 0, "value": {"regionCount": -1}}
  cachePath.write_text(json.dumps(cache), encoding="utf-8")

  result, _ = server.callToolExpectingSuccess("surveyZones", {"zones": ["befallen"], "groups": ["dimensions", "regions"]})
  row = rowsByVariant(result)["befallen:wld"]
  assert row["dimensions"]["triangleCount"] == -1
  assert row["regions"]["regionCount"] == 1

  cache = json.loads(cachePath.read_text(encoding="utf-8"))
  firstFile = next(iter(cache["variants"]["befallen:wld"]["fileHashes"]))
  cache["variants"]["befallen:wld"]["fileHashes"][firstFile] = "0" * 64
  cachePath.write_text(json.dumps(cache), encoding="utf-8")
  result, _ = server.callToolExpectingSuccess("surveyZones", {"zones": ["befallen"], "groups": ["dimensions"]})
  assert rowsByVariant(result)["befallen:wld"]["dimensions"]["triangleCount"] == 23314
  assert sorted(json.loads(cachePath.read_text(encoding="utf-8"))["variants"]["befallen:wld"]["measured"]) == ["dimensions"]


def testZoneSurveyReturnsEveryGroup(stageServer):
  server = stageSurveyServer(stageServer)
  survey, _ = server.callToolExpectingSuccess("getZoneSurvey", {"zone": "befallen"})
  variant = survey["variants"]["befallen:wld"]
  assert sorted(variant) == ["construction", "content", "dimensions", "format", "regions", "surfaces", "verticality", "zone"]
  assert survey["interpreted"] == {"state": "none", "variant": "befallen:wld", "procedureVersion": procedureVersion(server)}
  # A classic zone's placed objects are not built for the survey, so what share of steep area they hold is not measured.
  assert variant["construction"]["steepOnTerrainShare"] is None and variant["construction"]["terrainTextures"] > 0
  assert variant["surfaces"]["areaByKind"]["solid"] > 0
  assert survey["brewallLabelCount"] > 0


def testRegionsListAnEQGZonesZoneLinesInZoneNameOrder(stageServer):
  server = stageSurveyServer(stageServer)
  result, _ = server.callToolExpectingSuccess("surveyZones", {"zones": ["draniksscar", "Crescent", "zonewrightnowhere"], "groups": ["regions"]})
  assert result["unknownZones"] == ["zonewrightnowhere"]
  assert [row["variant"] for row in result["rows"]] == ["crescent:eqgz:loose", "draniksscar:eqgz"]
  rows = rowsByVariant(result)
  # The files write the turns in radians (-pi/2, pi/2, pi), which read as 512ths of a turn are a degree or two.
  assert rows["crescent:eqgz:loose"]["regions"]["zoneLines"] == [
    {"name": "ATP_1_", "number": 1, "center": [-842.0, -2765.0, 87.1], "size": [111.8, 340.8, 307.2], "headingDegrees": -1.1},
  ]
  assert rows["crescent:eqgz:loose"]["regions"]["regionsByKind"] == {"water": 57, "zoneLine": 1}
  assert rows["draniksscar:eqgz"]["regions"]["zoneLines"] == [
    {"name": "ATP_3_", "number": 3, "center": [1337.0, -2045.8, 430.1], "size": [120.0, 60.0, 130.0], "headingDegrees": 1.1},
    {"name": "ATP_2_", "number": 2, "center": [2110.7, -729.4, -207.0], "size": [120.0, 60.0, 130.0], "headingDegrees": -2.21},
    {"name": "ATP_1_", "number": 1, "center": [1383.7, 2058.3, 482.9], "size": [120.0, 60.0, 130.0], "headingDegrees": -1.1},
  ]


def testSurveyRefusesUnknownGroupsAndSortsByUnrequestedGroups(stageServer):
  server = stageSurveyServer(stageServer)
  assert "Unknown measured groups ['mood']" in server.callToolExpectingError("surveyZones", {"zones": ["gfaydark"], "groups": ["mood"]})
  refusal = server.callToolExpectingError("surveyZones", {"zones": ["gfaydark"], "groups": ["regions"], "sortBy": "dimensions.footprint"})
  assert "sortBy 'dimensions.footprint' sorts by the dimensions group, which this call does not request (groups ['regions'])" in refusal


def testSurveyWithoutClientFails(stageServer):
  server = stageSurveyServer(stageServer)
  errorText = server.callToolExpectingError("surveyZones", {"zones": ["gfaydark"]}, environment={"LOCALAPPDATA": str(server.localAppData)})
  assert "EVERQUEST_CLIENT is not set" in errorText


def testZoneNotesListBrewallLabelsOnTheZoneByLayerAndTheRestByName(stageServer):
  server = stageSurveyServer(stageServer)
  notes, _ = server.callToolExpectingSuccess("getZoneNotes", {"zone": "gfaydark"})
  assert notes["variant"] == "gfaydark:wld" and list(notes["labels"]) == ["gfaydark_1"] and len(notes["labels"]["gfaydark_1"]) == 144
  # The map's file holds (-2657.4233, 1641.3786, 0.0008): x and y swap and turn negative in the scene.
  assert {"text": "to Butcherblock Mountains", "scenePosition": [-1641, 2657, 0]} in notes["labels"]["gfaydark_1"]
  assert notes["offZone"] == {"count": 4, "texts": [
    "Original Map: EverQuest Default", "Revised Map: Brewall Rainsinger (Cazic-Thule)", "http://www.eqmaps.info", "Return of the Exiled (www.roteguild.org)",
  ]}


def testZoneNoteScenePositionsLieOnTheZone(stageServer):
  server = stageSurveyServer(stageServer)
  notes, _ = server.callToolExpectingSuccess("getZoneNotes", {"zone": "highpasshold"})
  rows, _ = server.callToolExpectingSuccess("surveyZones", {"zones": ["highpasshold"], "groups": ["dimensions"]})
  minimum, maximum = rows["rows"][0]["dimensions"]["terrainMinimum"], rows["rows"][0]["dimensions"]["terrainMaximum"]
  assert notes["extent"] == {"minimum": minimum[:2], "maximum": maximum[:2]}
  # The first layer names the places; the second holds the map's credits and its hunters in a column off the zone. Highpass Hold runs
  # about 3000 units along x and 1550 along y, so its places past 840 along x fall off its ground unless the axes swap.
  places = notes["labels"]["highpasshold_1"]
  assert len(places) == 81 and max(abs(place["scenePosition"][0]) for place in places) > maximum[1]
  assert {"text": "to Kithicor Forest", "scenePosition": [-1361, 198, -115]} in places
  assert notes["offZone"] == {"count": 7, "texts": [
    "http://www.eqmaps.info", "Return of the Exiled (www.roteguild.org)", "Grenix Mucktail", "Hagnis Shralok", "Recfek Shralok", "Vexven Mucktail", "Vopuk Shralok",
  ]}


def testZoneNotesKeepTheLabelsHoldingEveryWord(stageServer):
  server = stageSurveyServer(stageServer)
  exits, _ = server.callToolExpectingSuccess("getZoneNotes", {"zone": "crescent", "text": "to"})
  causeway, _ = server.callToolExpectingSuccess("getZoneNotes", {"zone": "draniksscar", "text": "Nobles` CAUSEWAY"})
  credits, _ = server.callToolExpectingSuccess("getZoneNotes", {"zone": "crescent", "text": "map brewall"})
  # "To" is a word of "Bag-To-Token", but only part of "Skeleton", "Touch", and "Elevator", which stay out.
  assert exits["labels"] == {"crescent_1": [
    {"text": "to Bixie Warfront", "scenePosition": [-1326, -2559, -160]}, {"text": "to Blightfire Moors", "scenePosition": [-1022, -2783, -73]},
    {"text": "Realnyna (Bag-To-Token)", "scenePosition": [-1340, -1350, -91]},
  ]}
  assert exits["offZone"] == {"count": 0, "texts": []}
  assert causeway["labels"] == {"draniksscar_1": [{"text": "to Nobles` Causeway", "scenePosition": [2039, -711, -260]}]}
  assert credits["labels"] == {} and credits["offZone"] == {"count": 1, "texts": ["Revised Map: Brewall Rainsinger (Cazic-Thule)"]}


def testInterpretationIsKeptWithItsScreenshotsAndReadBackCurrent(stageServer, tmp_path):
  server = stageSurveyServer(stageServer)
  interpretation = befallenInterpretation(tmp_path / "views")
  recorded, _ = server.callToolExpectingSuccess("recordZoneInterpretation", {"zone": "Befallen", "interpretation": interpretation})
  survey, _ = server.callToolExpectingSuccess("getZoneSurvey", {"zone": "befallen"})

  keptPaths = [Path(path) for path in recorded["screenshots"]]
  assert {key: recorded[key] for key in ("zone", "variant", "state", "areas", "landmarks")} == {"zone": "befallen", "variant": "befallen:wld", "state": "current", "areas": 2, "landmarks": 1}
  assert recorded["procedureVersion"] == procedureVersion(server)
  assert [path.parent for path in keptPaths] == [server.toolingRoot / "survey" / "interpretations" / "befallen"] * 5
  assert [path.read_bytes() for path in keptPaths] == [Path(screenshot["path"]).read_bytes() for screenshot in interpretation["screenshots"]]
  assert survey["interpreted"] == {
    "state": "current", "variant": "befallen:wld", "procedureVersion": procedureVersion(server),
    "interpretation": interpretation | {"screenshots": [screenshot | {"path": str(path)} for screenshot, path in zip(interpretation["screenshots"], keptPaths)]},
  }


def testRecordingRefusesBadFieldsAndKeepsNothing(stageServer, tmp_path):
  server = stageSurveyServer(stageServer)
  good = befallenInterpretation(tmp_path / "views")
  notAnImage = tmp_path / "notes.png"
  notAnImage.write_text("not an image", encoding="ascii")
  halls, crypts = good["areas"]
  bad = good | {
    "zoneType": "spookyDungeon",
    "character": {"description": " ", "tags": ["Dark halls"]},
    "areas": [halls | {"connections": [{"to": "Nowhere", "by": "a door"}, {"to": "zone:zonewrightnowhere", "by": "a zone line"}]}, crypts | {"center": [0, 0]}],
    "screenshots": [good["screenshots"][0] | {"view": "aerial"}, good["screenshots"][1] | {"path": str(notAnImage), "shows": ["Entry halls"]}] + good["screenshots"][3:],
    "structuredValues": {"stairDrop": {"value": "forty", "unit": "units", "how": "measured"}},
    "mood": "grim",
  }
  refusal = server.callToolExpectingError("recordZoneInterpretation", {"zone": "befallen", "interpretation": bad})
  missingLandmarks = server.callToolExpectingError("recordZoneInterpretation", {"zone": "befallen", "interpretation": {key: value for key, value in good.items() if key != "landmarks"}})
  unknownZone = server.callToolExpectingError("recordZoneInterpretation", {"zone": "zonewrightnowhere", "interpretation": good})
  survey, _ = server.callToolExpectingSuccess("getZoneSurvey", {"zone": "befallen"})

  for problem in (
    "unknown fields ['mood']",
    "zoneType 'spookyDungeon' is not one of the zone-survey skill's zone types: ['bigDungeon',",
    "character.description must be written",
    "character.tags is a non-empty list of camelCase terms, got ['Dark halls']",
    "area 'Entry halls': connection to 'Nowhere' is neither another area nor zone:<zone short name>",
    "area 'Entry halls': connection to 'zone:zonewrightnowhere' names no zone in the client",
    "area 'Lower crypts': center is [x, y, z] in the scene's coordinates",
    "screenshots[0]: view 'aerial' is one of ['map', 'oblique', 'eyeLevel', 'section']",
    f"screenshots[1]: {notAnImage} is not a PNG",
    "no map view: the procedure starts from a map of the whole zone",
    "area 'Entry halls' has no eyeLevel view",
    "landmark 'Iron gate' has no oblique or eyeLevel view",
    "structuredValues 'stairDrop': value is a number",
  ):
    assert problem in refusal
  assert refusal.count("\n- ") == 13
  assert "landmarks is missing" in missingLandmarks
  assert "'zonewrightnowhere' is not a zone in" in unknownZone
  assert survey["interpreted"]["state"] == "none"
  assert not (server.toolingRoot / "survey" / "interpretations").exists()


def testRaisingTheProcedureVersionMakesTheInterpretationStale(stageServer, tmp_path):
  server = stageSurveyServer(stageServer)
  interpretation = befallenInterpretation(tmp_path / "views")
  server.callToolExpectingSuccess("recordZoneInterpretation", {"zone": "befallen", "interpretation": interpretation})
  version = procedureVersion(server)
  skillPath = stagedSkillPath(server)
  skillPath.write_text(procedureVersionPattern.sub(f"Interpretive procedure version: {version + 1}", skillPath.read_text(encoding="utf-8")), encoding="utf-8")
  stale, _ = server.callToolExpectingSuccess("getZoneSurvey", {"zone": "befallen"})
  readAgain, _ = server.callToolExpectingSuccess("getZoneSurvey", {"zone": "befallen"})
  server.callToolExpectingSuccess("recordZoneInterpretation", {"zone": "befallen", "interpretation": interpretation})
  recordedAgain, _ = server.callToolExpectingSuccess("getZoneSurvey", {"zone": "befallen"})

  assert {key: stale["interpreted"][key] for key in ("state", "procedureVersion", "staleBecause")} == {
    "state": "stale", "procedureVersion": version + 1, "staleBecause": {"procedureChanged": {"recorded": version, "current": version + 1}},
  }
  assert stale["interpreted"]["interpretation"]["areas"] == interpretation["areas"]
  assert readAgain["interpreted"]["state"] == "stale"
  assert recordedAgain["interpreted"]["state"] == "current" and "staleBecause" not in recordedAgain["interpreted"]


def testSkillWithoutAProcedureVersionIsRefused(stageServer, tmp_path):
  server = stageSurveyServer(stageServer)
  skillPath = stagedSkillPath(server)
  skillPath.write_text(procedureVersionPattern.sub("", skillPath.read_text(encoding="utf-8")), encoding="utf-8")
  readRefusal = server.callToolExpectingError("getZoneSurvey", {"zone": "befallen"})
  recordRefusal = server.callToolExpectingError("recordZoneInterpretation", {"zone": "befallen", "interpretation": befallenInterpretation(tmp_path / "views")})

  for refusal in (readRefusal, recordRefusal):
    assert "must state the interpretive procedure's version once, as 'Interpretive procedure version: <n>' (found 0)" in refusal
  assert not (server.toolingRoot / "survey" / "interpretations").exists()


def testChangedZoneFileMakesTheInterpretationStale(stageServer, tmp_path):
  server = stageSurveyServer(stageServer)
  server.callToolExpectingSuccess("recordZoneInterpretation", {"zone": "befallen", "interpretation": befallenInterpretation(tmp_path / "views")})
  cachePath = server.toolingRoot / "survey" / "zoneSurvey.json"
  cache = json.loads(cachePath.read_text(encoding="utf-8"))
  # The hash memo stands in for an edited file: its size and modification time are unchanged, so the survey takes the memo's hash.
  cache["fileHashes"][str(Path(everquestClient) / "befallen.s3d")]["sha256"] = "f" * 64
  cachePath.write_text(json.dumps(cache), encoding="utf-8")
  survey, _ = server.callToolExpectingSuccess("getZoneSurvey", {"zone": "befallen"})

  assert survey["interpreted"]["state"] == "stale"
  assert survey["interpreted"]["staleBecause"] == {"zoneFilesChanged": ["befallen.s3d"]}


def testComparingAZoneWithStructuresSetsItsPlacedTrianglesBesideTheClients(stageBlenderServer, tmp_path):
  async def steps(session):
    kitPath = str(await structurePlots.testPrefab(session, tmp_path))
    await structurePlots.testPlot(session, tmp_path)
    await session.expectSuccess("placeKitPiece", {"name": "stripWall", "kitPath": kitPath, "piece": "testKitWall25", "location": [-10, 60, 0], "facingDegrees": 90})
    await session.expectSuccess("buildBridge", bridgeArguments(kitPath, posts=legs, rails=ropes))
    await session.expectSuccess("placePrefab", {"name": "housePlaza", "kitPath": kitPath, "prefab": "testKitHouse", "location": [60, -14], "facingDegrees": 0})
    await session.expectSuccess("saveFile", {})
    checked = await session.expectSuccess("checkExport", {"path": str(tmp_path / "structplot.eqg"), "purpose": "test"})
    return checked, await session.expectSuccess("compareWithClientZones", {"zones": ["highpasshold"]})

  checked, compared = stageBlenderServer.session(steps)
  placed = compared["measures"]["construction.placedTriangles"]
  # The loose wall, the bridge, and the house's three parts, each placed once, and the house's parts holding their nested pieces.
  assert placed["zone"] == checked["placedTriangles"] > 0
  assert compared["measures"]["content.placementCount"]["zone"] == 5
  assert placed["clientZones"] == 1 and placed["median"] > placed["zone"]


def testZoneNotesForUnknownZoneFail(stageServer):
  server = stageSurveyServer(stageServer)
  errorText = server.callToolExpectingError("getZoneNotes", {"zone": "zonewrightnowhere"})
  assert "No Brewall map files for zone 'zonewrightnowhere'" in errorText
