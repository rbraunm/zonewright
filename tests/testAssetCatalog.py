import io
import json
import shutil
import sys
from pathlib import Path

import numpy
from PIL import Image
import pytest

from conftest import pinnedBlender

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import eqArchive
import eqgFiles

pytestmark = pytest.mark.clientData("survey")
classicZone = "nektulos"
eqgZone = "nektulosa"


def stageCatalogServer(stageServer):
  return stageServer({"blender": pinnedBlender, "extensions": {}})


def catalogFile(server, sourceKey):
  return [path for path in (server.repositoryPath / "catalog" / "measured").glob("*.json") if json.loads(path.read_text(encoding="utf-8"))["source"] == sourceKey]


def testSurveyMeasuresAZonesTexturesLightsAndEmittersAndCachesThem(stageServer):
  server = stageCatalogServer(stageServer)
  surveyed, progressMessages = server.callToolExpectingSuccess("surveyAssets", {"zones": [classicZone]})
  measured = catalogFile(server, f"zone:{classicZone}")
  modified = measured[0].stat().st_mtime_ns
  again, _ = server.callToolExpectingSuccess("surveyAssets", {"zones": [classicZone]})
  lights, _ = server.callToolExpectingSuccess("findAssets", {"kind": "light", "source": classicZone})
  emitters, _ = server.callToolExpectingSuccess("findAssets", {"kind": "emitter", "source": classicZone})
  textures, _ = server.callToolExpectingSuccess("findAssets", {"kind": "texture", "source": classicZone, "sortBy": "areaShare", "limit": 500})
  tiling, _ = server.callToolExpectingSuccess("findAssets", {"kind": "texture", "source": classicZone, "tiles": True, "limit": 500})
  assert surveyed["source"] == f"zone:{classicZone}" and surveyed["format"] == "wld" and surveyed["problems"] == []
  assert progressMessages == ["checking zone file hashes", f"surveyed zone:{classicZone}"]
  # The second survey reads the cached measurement: same answer, file untouched.
  assert again == surveyed and len(measured) == 1 and measured[0].stat().st_mtime_ns == modified
  # Grouping lights into styles and emitters into definitions keeps every one the zone's files place.
  assert sum(style["count"] for style in lights["assets"]) == 11
  assert sum(zone["count"] for entry in emitters["assets"] for zone in entry["zones"].values()) == 37
  # The zone's textured area is shared out among its textures, and the most-used comes first.
  used = [entry["mainUse"]["areaShare"] for entry in textures["assets"] if "mainUse" in entry]
  assert abs(sum(used) - 1) < 0.002 and used == sorted(used, reverse=True)
  top = textures["assets"][0]
  assert surveyed["mostUsedTextures"][0]["id"] == top["id"] and Path(top["file"]).is_file()
  assert 0 < tiling["total"] < textures["total"] and all(entry["tiles"] for entry in tiling["assets"])
  # The kept measurement names no machine path and identifies its files by content; the images it lacks are extracted again unchanged.
  kept = measured[0].read_text(encoding="utf-8")
  assert str(server.toolingRoot) not in kept and str(server.repositoryPath) not in kept
  assert all(len(digest) == 64 for digest in json.loads(kept)["fileHashes"].values())
  shutil.rmtree(server.toolingRoot / "catalog" / "textures")
  server.callToolExpectingSuccess("surveyAssets", {"zones": [classicZone]})
  assert Path(top["file"]).is_file() and measured[0].read_text(encoding="utf-8") == kept


def testManyZonesAreSurveyedInParallelAndAnUnreadableOneIsReported(stageServer):
  server = stageCatalogServer(stageServer)
  first, progressMessages = server.callToolExpectingSuccess("surveyAssets", {"zones": [classicZone, "arena", eqgZone]})
  second, _ = server.callToolExpectingSuccess("surveyAssets", {"zones": [classicZone, "arena", eqgZone]})
  lights, _ = server.callToolExpectingSuccess("findAssets", {"kind": "light", "source": eqgZone})
  assert first["zones"] == 3 and first["surveyed"] == 2 and first["current"] == 0
  assert list(first["errors"]) == ["arena"] and "ships both a classic and an EQG version" in first["errors"]["arena"]
  assert progressMessages[0] == "checking zone file hashes" and sorted(progressMessages[1:]) == [f"surveyed zone:{classicZone}", f"surveyed zone:{eqgZone}"]
  assert second["surveyed"] == 0 and second["current"] == 2 and second["errors"] == first["errors"]
  assert sum(style["count"] for style in lights["assets"]) == 6


def testDescriptionsUseTheVocabularyAndAreFoundByWhatTheySay(stageServer):
  server = stageCatalogServer(stageServer)
  server.callToolExpectingSuccess("surveyAssets", {"zones": [eqgZone]})
  textures, _ = server.callToolExpectingSuccess("findAssets", {"kind": "texture", "source": eqgZone, "sortBy": "areaShare", "limit": 2})
  textureID = textures["assets"][0]["id"]
  base = {"id": textureID, "category": "grass", "tags": {"color": ["green"], "use": ["terrainFlat"]}, "description": "Dark mossy forest floor with roots.", "usage": "Flat shaded ground under trees; repeat about every 40 units."}
  badCategory = server.callToolExpectingError("describeAssets", {"descriptions": [base | {"category": "lawn"}]})
  badTerm = server.callToolExpectingError("describeAssets", {"descriptions": [base | {"tags": {"color": ["moss"]}}]})
  noUsage = server.callToolExpectingError("describeAssets", {"descriptions": [base | {"usage": " "}]})
  unknownAsset = server.callToolExpectingError("describeAssets", {"descriptions": [base | {"id": "texture/nothing.dds@00000000"}]})
  added, _ = server.callToolExpectingSuccess("extendAssetVocabulary", {"group": "material", "term": "loam", "meaning": "Dark rich soil."})
  addedAgain = server.callToolExpectingError("extendAssetVocabulary", {"group": "material", "term": "loam", "meaning": "Again."})
  described, _ = server.callToolExpectingSuccess("describeAssets", {"descriptions": [base | {"tags": {"color": ["green"], "material": ["loam", "moss"]}}]})
  byWords, _ = server.callToolExpectingSuccess("findAssets", {"text": "mossy roots"})
  byTags, _ = server.callToolExpectingSuccess("findAssets", {"tags": {"material": ["loam"]}, "categories": ["grass", "dirt"]})
  byOtherTags, _ = server.callToolExpectingSuccess("findAssets", {"tags": {"material": ["loam", "sand"]}})
  undescribed, _ = server.callToolExpectingSuccess("findAssets", {"kind": "texture", "source": eqgZone, "described": False, "limit": 500})
  entry, _ = server.callToolExpectingSuccess("getAsset", {"id": textureID})
  assert "category 'lawn' is not a texture category" in badCategory
  assert "color takes a non-empty list of" in badTerm and "unknown: ['moss']" in badTerm
  assert "usage must be written" in noUsage
  assert "is not in the catalog; survey the zone or folder that holds it" in unknownAsset
  assert added == {"group": "material", "term": "loam", "meaning": "Dark rich soil."} and "'loam' is already a material term" in addedAgain
  assert described == {"described": [textureID]}
  assert [asset["id"] for asset in byWords["assets"]] == [textureID] and [asset["id"] for asset in byTags["assets"]] == [textureID]
  assert byOtherTags["total"] == 0
  assert textureID not in [asset["id"] for asset in undescribed["assets"]] and undescribed["total"] == textures["total"] - 1
  assert entry["interpreted"]["tags"] == {"color": ["green"], "material": ["loam", "moss"]} and entry["measured"]["width"] > 0


def testTextureSheetIsNumberedInTheOrderAsked(stageBlenderServer):
  async def steps(session):
    await session.expectSuccess("surveyAssets", {"zones": [eqgZone]})
    textures = await session.expectSuccess("findAssets", {"kind": "texture", "source": eqgZone, "sortBy": "name", "limit": 3})
    ids = [asset["id"] for asset in textures["assets"]]
    image, description = await session.expectImage("viewTextures", {"ids": ids, "columns": 2, "tiled": True}, "image/jpeg")
    large, largeDescription = await session.expectImage("viewTextures", {"ids": ids[:1], "columns": 1, "cellSide": 256}, "image/jpeg")
    return ids, image, description, large, largeDescription

  ids, image, description, large, largeDescription = stageBlenderServer.session(steps)
  assert description["cells"] == [{"number": number, "id": assetID} for number, assetID in enumerate(ids, start=1)]
  # Two columns of 128-pixel cells with 28-pixel labels and 4-pixel gaps: three cells take two rows.
  assert Image.open(io.BytesIO(image)).size == (description["width"], description["height"]) == (4 + 2 * 132, 4 + 2 * 160)
  assert Image.open(io.BytesIO(large)).size == (4 + 260, 4 + 288) and Path(largeDescription["outputPath"]).is_file()


def testZoneImportBringsTheZonesLightsAndEmitters(stageBlenderServer):
  readEnvironment = """
lights = bpy.data.collections['nektulosa lights'].objects
emitters = bpy.data.collections['nektulosa emitters'].objects
result = {'lights': len(lights), 'emitters': len(emitters), 'radii': sorted({round(o.data['eqRadius'], 1) for o in lights}),
  'definitions': sorted({o['eqEmitterDefinition'] for o in emitters})}
"""

  async def steps(session):
    await session.expectSuccess("newFile", {"discardUnsavedChanges": True})
    imported = await session.expectSuccess("importZone", {"zone": eqgZone})
    placed = await session.expectSuccess("runPython", {"code": readEnvironment})
    await session.expectSuccess("surveyAssets", {"zones": [eqgZone]})
    styles = await session.expectSuccess("findAssets", {"kind": "light", "source": eqgZone})
    definitions = await session.expectSuccess("findAssets", {"kind": "emitter", "source": eqgZone})
    return imported, placed["result"], styles, definitions

  imported, placed, styles, definitions = stageBlenderServer.session(steps)
  assert imported["lights"] == placed["lights"] == 6 and imported["emitters"] == placed["emitters"] == 6
  assert placed["radii"] == sorted({radius for style in styles["assets"] for radius in style["radii"]})
  assert placed["definitions"] == sorted(int(entry["id"].split("/")[1]) for entry in definitions["assets"])


def testAZoneRoundTripsThroughExportImportAndTheCatalog(stageBlenderServer, tmp_path):
  texturePath = tmp_path / "checker.png"
  checker = Image.new("RGBA", (64, 64), (180, 90, 40, 255))
  for row in range(0, 64, 8):
    for column in range(0, 64, 8):
      if (row + column) // 8 % 2:
        checker.paste((90, 45, 20, 255), (column, row, column + 8, row + 8))
  checker.save(texturePath)
  archivePath = tmp_path / "roundtrip.eqg"
  readEnvironment = """
light = bpy.data.collections['roundtrip lights'].objects[0]
emitter = bpy.data.collections['roundtrip emitters'].objects[0]
result = {'light': [light.name, [round(value, 4) for value in light.location], [round(value, 4) for value in light.data.color], light.data['eqRadius']],
  'emitter': [emitter.name, [round(value, 4) for value in emitter.location], emitter['eqEmitterDefinition'], emitter['eqEmitterLifespan']]}
"""

  async def steps(session):
    await session.expectSuccess("newFile", {"discardUnsavedChanges": True})
    await session.expectSuccess("createTerrainGrid", {"name": "ground", "size": [128, 128], "spacing": 16, "location": [0, 0, 0], "collection": "terrain"})
    await session.expectSuccess("createMaterial", {"name": "checker", "diffuseTexture": str(texturePath)})
    await session.expectSuccess("assignMaterial", {"objectName": "ground", "materialName": "checker"})
    await session.expectSuccess("projectUVs", {"objectName": "ground", "method": "planar", "worldUnitsPerRepeat": 32, "direction": [0, 0, 1]})
    await session.expectSuccess("placeLights", {"lights": [{"name": "LIT_torch01", "position": [10, -20, 8], "color": [1, 0.5, 0.25], "radius": 60}]})
    await session.expectSuccess("placeEmitters", {"emitters": [{"name": "campfire01", "position": [5, 5, 1], "definition": 259, "lifespan": 4000000}]})
    await session.expectSuccess("saveFile", {"path": str(tmp_path / "roundtrip.blend")})
    exported = await session.expectSuccess("exportZone", {"path": str(archivePath), "purpose": "test"})
    surveyed = await session.expectSuccess("surveyAssets", {"path": str(archivePath)})
    texture = await session.expectSuccess("findAssets", {"kind": "texture", "source": f"file:{archivePath}"})
    light = await session.expectSuccess("findAssets", {"kind": "light", "source": f"file:{archivePath}"})
    await session.expectSuccess("newFile", {"discardUnsavedChanges": True})
    imported = await session.expectSuccess("importZoneFile", {"path": str(archivePath)})
    environment = await session.expectSuccess("runPython", {"code": readEnvironment})
    return exported, surveyed, texture, light, imported, environment["result"]

  exported, surveyed, texture, light, imported, environment = stageBlenderServer.session(steps)
  assert exported["lights"] == 1 and exported["emitters"] == 1 and exported["emitterList"] == str(tmp_path / "roundtrip_EnvironmentEmitters.txt")
  assert eqgFiles.parseZone(eqArchive.EQArchive(archivePath).read("roundtrip.zon"), "roundtrip.zon")["lights"] == [{"name": "LIT_torch01", "position": (10.0, -20.0, 8.0), "color": (1.0, 0.5, 0.25), "radius": 60.0}]
  assert (tmp_path / "roundtrip_EnvironmentEmitters.txt").read_text(encoding="latin1").splitlines() == ["Name^EmitterDefIdx^X^Y^Z^Lifespan", "campfire01^259^5.000000^5.000000^1.000000^4000000"]
  assert surveyed["kinds"]["texture"]["assets"] == 1 and surveyed["kinds"]["light"]["assets"] == 1 and surveyed["kinds"]["emitter"]["assets"] == 1
  # The catalog measures the exported terrain as it was textured: one repeat every 32 units over all of the flat ground.
  [entry] = texture["assets"]
  assert entry["mainUse"]["unitsPerRepeat"] == 32.0 and entry["mainUse"]["areaShare"] == 1.0 and entry["mainUse"]["slopeShares"]["flat"] == 1.0
  assert entry["size"] == "64x64" and entry["tiles"] is True
  assert all(abs(measured - expected) <= 1 for measured, expected in zip(entry["meanColor"], (135, 67.5, 30)))
  assert light["assets"][0]["colors"] == [{"rgb": [1.0, 0.5, 0.25], "count": 1}] and light["assets"][0]["radii"] == [60.0]
  assert imported["lights"] == 1 and imported["emitters"] == 1
  assert environment == {"light": ["LIT_torch01", [10.0, -20.0, 8.0], [1.0, 0.5, 0.25], 60.0], "emitter": ["campfire01", [5.0, 5.0, 1.0], 259, 4000000]}


def testModelSheetDrawsEachModel(stageBlenderServer):
  async def steps(session):
    await session.expectSuccess("newFile", {"discardUnsavedChanges": True})
    await session.expectSuccess("surveyAssets", {"zones": [eqgZone]})
    models = await session.expectSuccess("findAssets", {"kind": "model", "source": eqgZone, "text": "obj_tree", "sortBy": "name", "limit": 2})
    image, description = await session.expectImage("viewModels", {"ids": [asset["id"] for asset in models["assets"]], "columns": 2}, "image/jpeg")
    summary = await session.expectSuccess("getSceneSummary")
    return models, image, description, summary

  models, image, description, summary = stageBlenderServer.session(steps)
  assert [cell["id"] for cell in description["cells"]] == [asset["id"] for asset in models["assets"]] and len(models["assets"]) == 2
  sheet = Image.open(io.BytesIO(image)).convert("RGB")
  assert sheet.size == (4 + 2 * 260, 4 + 288)
  # Each 256-pixel cell shows its model over the fog color, not an empty frame; the scene is left as it was.
  for left in (4, 264):
    cell = sheet.crop((left, 4, left + 256, 260))
    assert len(numpy.unique(numpy.asarray(cell).reshape(-1, 3), axis=0)) > 200
  assert summary["objectCount"] == 0
