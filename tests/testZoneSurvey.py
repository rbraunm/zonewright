import json
import re
from pathlib import Path

import pytest

from conftest import everquestClient, pinnedBlender, writePNG, zoneSurveySkill

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
  assert rows["gfaydark:wld"]["regions"] == {"regionCount": 9, "regionsByKind": {"zoneLine": 9}}
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


def testSurveyRejectsUnknownZonesGroupsAndSorts(stageServer):
  server = stageSurveyServer(stageServer)
  assert "zonewrightnowhere" in server.callToolExpectingError("surveyZones", {"zones": ["gfaydark", "zonewrightnowhere"]})
  assert "Unknown measured groups ['mood']" in server.callToolExpectingError("surveyZones", {"zones": ["gfaydark"], "groups": ["mood"]})
  assert "sortBy 'surfaces.totalArea' must start with one of the requested groups" in server.callToolExpectingError("surveyZones", {"zones": ["gfaydark"], "sortBy": "surfaces.totalArea"})


def testSurveyWithoutClientFails(stageServer):
  server = stageSurveyServer(stageServer)
  errorText = server.callToolExpectingError("surveyZones", {"zones": ["gfaydark"]}, environment={"LOCALAPPDATA": str(server.localAppData)})
  assert "EVERQUEST_CLIENT is not set" in errorText


def testZoneNotesListBrewallLabels(stageServer):
  server = stageSurveyServer(stageServer)
  notes, _ = server.callToolExpectingSuccess("getZoneNotes", {"zone": "gfaydark"})
  labelTexts = [label["text"] for label in notes["labels"]]
  assert "to Butcherblock Mountains" in labelTexts
  butcherblock = notes["labels"][labelTexts.index("to Butcherblock Mountains")]
  assert butcherblock["mapPosition"] == [-2657.4233, 1641.3786, 0.0008]
  assert butcherblock["scenePosition"] == [-1641.3786, 2657.4233, 0.0008]
  assert butcherblock["layer"] == "gfaydark_1"


def testZoneNoteScenePositionsLieOnTheZone(stageServer):
  server = stageSurveyServer(stageServer)
  notes, _ = server.callToolExpectingSuccess("getZoneNotes", {"zone": "highpasshold"})
  rows, _ = server.callToolExpectingSuccess("surveyZones", {"zones": ["highpasshold"], "groups": ["dimensions"]})
  minimum, maximum = rows["rows"][0]["dimensions"]["terrainMinimum"], rows["rows"][0]["dimensions"]["terrainMaximum"]
  # The first layer names the places; the second holds the map's credits in a column off the zone. Highpass Hold runs about 3000 units
  # along x and 1550 along y, so its places at map y past 840 fall off its ground unless the axes swap.
  places = [label for label in notes["labels"] if label["layer"] == "highpasshold_1"]
  offTheZone = [label["text"] for label in places if not all(minimum[axis] <= label["scenePosition"][axis] <= maximum[axis] for axis in (0, 1))]
  assert len(places) == 81 and offTheZone == []
  assert max(abs(label["mapPosition"][1]) for label in places) > maximum[1]
  kithicor = next(label for label in notes["labels"] if label["text"] == "to Kithicor Forest")
  assert kithicor["scenePosition"] == [-1361.0, 198.0, -114.5187]


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


def testZoneNotesForUnknownZoneFail(stageServer):
  server = stageSurveyServer(stageServer)
  errorText = server.callToolExpectingError("getZoneNotes", {"zone": "zonewrightnowhere"})
  assert "No Brewall map files for zone 'zonewrightnowhere'" in errorText
