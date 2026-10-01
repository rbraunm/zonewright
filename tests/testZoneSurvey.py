from conftest import pinnedBlender


def stageSurveyServer(stageServer):
  return stageServer({"blender": pinnedBlender, "extensions": {}})


def testSurveyMeasuresEachZoneFormat(stageServer):
  server = stageSurveyServer(stageServer)
  result, progressMessages = server.callToolExpectingSuccess("surveyZones", {"zones": ["gfaydark", "arena2", "anguish", "neighborhood"]})
  rows = {(row["zone"], row["format"]): row for row in result["zones"]}
  assert sorted(rows) == [("anguish", "eqgz"), ("arena2", "eqgz"), ("gfaydark", "wld"), ("neighborhood", "eqtzp")]
  assert [row["zone"] for row in result["zones"]] == ["anguish", "neighborhood", "gfaydark", "arena2"]
  assert rows[("gfaydark", "wld")] | {"brewallLabels": None} == {
    "zone": "gfaydark", "format": "wld", "brewallLabels": None,
    "terrainSize": [5521.0, 5459.4, 900.3], "allGeometrySize": [5521.0, 5459.4, 900.3],
    "triangles": 74981, "textures": 32, "placements": 0,
  }
  assert rows[("arena2", "eqgz")]["terrainSize"] == [2255.2, 2392.8, 396.7]
  assert rows[("arena2", "eqgz")]["allGeometrySize"] == [5831.2, 7840.0, 737.3]
  assert rows[("arena2", "eqgz")]["triangles"] == 41882
  assert rows[("anguish", "eqgz")]["terrainSize"] == [5020.6, 8968.2, 1305.1]
  assert rows[("anguish", "eqgz")]["placements"] == 696
  assert rows[("neighborhood", "eqtzp")]["terrainSize"] == [6528.0, 5376.0, 681.1]
  assert rows[("neighborhood", "eqtzp")]["triangles"] == 2636770
  assert rows[("neighborhood", "eqtzp")]["textures"] == 21
  assert rows[("gfaydark", "wld")]["brewallLabels"] > 0
  assert len(progressMessages) == 4
  assert (server.toolingRoot / "survey" / "zoneSurvey.json").is_file()

  _, progressMessages = server.callToolExpectingSuccess("surveyZones", {"zones": ["gfaydark"]})
  assert progressMessages == []


def testSurveyOfUnknownZoneFails(stageServer):
  server = stageSurveyServer(stageServer)
  errorText = server.callToolExpectingError("surveyZones", {"zones": ["gfaydark", "zonewrightnowhere"]})
  assert "Not zones in" in errorText
  assert "zonewrightnowhere" in errorText


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
