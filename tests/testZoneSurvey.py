import json

from conftest import pinnedBlender


def stageSurveyServer(stageServer):
  return stageServer({"blender": pinnedBlender, "extensions": {}})


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
  assert rows["arena2:eqgz:loose"]["dimensions"]["terrainSize"] == [2255.2, 2392.8, 396.7]
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
  assert sorted(entry["measured"]) == ["dimensions", "regions"]
  assert entry["interpreted"] == {}
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
  assert sorted(variant) == ["construction", "content", "dimensions", "format", "interpreted", "regions", "surfaces", "verticality", "zone"]
  assert variant["interpreted"] == {}
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
  assert butcherblock["layer"] == "gfaydark_1"


def testZoneNotesForUnknownZoneFail(stageServer):
  server = stageSurveyServer(stageServer)
  errorText = server.callToolExpectingError("getZoneNotes", {"zone": "zonewrightnowhere"})
  assert "No Brewall map files for zone 'zonewrightnowhere'" in errorText
