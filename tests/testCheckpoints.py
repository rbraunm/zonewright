import datetime
from pathlib import Path

from conftest import writePNG

stateCode = """
import os
terrain = bpy.data.objects['terrain']
image = bpy.data.images['rock.png']
result = {
  'objects': sorted(sceneObject.name for sceneObject in bpy.data.objects),
  'crate': [list(vertex.co) for vertex in bpy.data.objects['crate'].data.vertices],
  'passes': [[block.name, block.value, block.mute, [list(point.co) for point in block.data]] for block in terrain.data.shape_keys.key_blocks],
  'terrainMaterials': sorted({terrain.data.materials[polygon.material_index].name for polygon in terrain.data.polygons}),
  'rockTexture': {'filePath': image.filepath, 'resolves': os.path.isfile(bpy.path.abspath(image.filepath)), 'size': list(image.size)},
}
"""


async def buildPlot(session, workFile, crateLocation=(20, 20, 0)):
  """A small plot saved as a work file: a terrain with a hill in its own pass, a textured material, and a crate."""
  textures = workFile.parent / "textures"
  textures.mkdir(parents=True, exist_ok=True)
  writePNG(textures / "rock.png", 8, 8, (120, 110, 100, 255))
  writePNG(textures / "grass.png", 8, 8, (60, 120, 50, 255))
  await session.expectSuccess("newFile", {"discardUnsavedChanges": True})
  await session.expectSuccess("createTerrainGrid", {"name": "terrain", "size": [64, 64], "spacing": 8, "location": [0, 0, 0]})
  await session.expectSuccess("addShapingPass", {"objectName": "terrain", "name": "hill"})
  await session.expectSuccess("sculptAtPoint", {"objectName": "terrain", "mode": "raise", "center": [0, 0, 0], "radius": 20, "strength": 10, "direction": [0, 0, 1]})
  await session.expectSuccess("createMaterial", {"name": "rock", "diffuseTexture": str(textures / "rock.png")})
  await session.expectSuccess("assignMaterial", {"objectName": "terrain", "materialName": "rock"})
  await session.expectSuccess("createPrimitive", {"kind": "cube", "name": "crate", "size": [4, 4, 4], "location": list(crateLocation)})
  await session.expectSuccess("saveFile", {"path": str(workFile)})


async def wreckPlot(session, workFile):
  """Unsaved changes a restore must undo: a moved mesh, a pass taken out, a new pass, another material."""
  await session.expectSuccess("moveVertices", {"objectName": "crate", "selector": {"all": True}, "offset": [0, 0, 9]})
  await session.expectSuccess("removeShapingPass", {"objectName": "terrain", "name": "hill"})
  await session.expectSuccess("addShapingPass", {"objectName": "terrain", "name": "spike"})
  await session.expectSuccess("sculptAtPoint", {"objectName": "terrain", "mode": "raise", "center": [-16, -16, 0], "radius": 12, "strength": 80, "direction": [0, 0, 1]})
  await session.expectSuccess("createMaterial", {"name": "grass", "diffuseTexture": str(workFile.parent / "textures" / "grass.png")})
  await session.expectSuccess("assignMaterial", {"objectName": "terrain", "materialName": "grass"})


async def sceneState(session):
  return (await session.expectSuccess("runPython", {"code": stateCode}))["result"]


def checkpointFile(toolingRoot, name):
  return toolingRoot / "checkpoints" / f"{name}.blend"


def stampOf(name):
  return name.split("/")[1].split("_")[0]


def filesUnder(folder):
  return sorted(str(path.relative_to(folder)) for path in folder.rglob("*") if path.is_file())


def testRestoreBringsBackTheExactEarlierStateAndCanBeTakenBack(stageBlenderServer, tmp_path):
  workFile = tmp_path / "work" / "plot.blend"

  async def steps(session):
    await buildPlot(session, workFile)
    earlier = await sceneState(session)
    saved = await session.expectSuccess("saveCheckpoint", {"label": "hill and crate"})
    await wreckPlot(session, workFile)
    later = await sceneState(session)
    restored = await session.expectSuccess("restoreCheckpoint", {"name": saved["name"]})
    afterRestore = await sceneState(session)
    passesAfterRestore = (await session.expectSuccess("getObjectDetail", {"name": "terrain"}))["shapingPasses"]
    listed = await session.expectSuccess("listCheckpoints")
    takenBack = await session.expectSuccess("restoreCheckpoint", {"name": restored["beforeRestore"]})
    afterTakeBack = await sceneState(session)
    workFileBytes = workFile.read_bytes()
    listedAgain = await session.expectSuccess("listCheckpoints")
    return saved, earlier, later, restored, afterRestore, passesAfterRestore, listed, takenBack, afterTakeBack, workFileBytes, listedAgain

  saved, earlier, later, restored, afterRestore, passesAfterRestore, listed, takenBack, afterTakeBack, workFileBytes, listedAgain = stageBlenderServer.session(steps)
  toolingRoot = stageBlenderServer.toolingRoot
  assert saved["workFile"] == str(workFile)
  assert saved["label"] == "hill and crate"
  assert Path(saved["path"]) == checkpointFile(toolingRoot, saved["name"])
  assert saved["sizeBytes"] == Path(saved["path"]).stat().st_size
  stampTime = datetime.datetime.strptime(stampOf(saved["name"]), "%Y%m%dT%H%M%S.%fZ")
  assert saved["time"] == stampTime.isoformat(timespec="milliseconds") + "Z"
  assert later["crate"] != earlier["crate"]
  assert [block[0] for block in later["passes"]] == ["base", "spike"]
  assert later["terrainMaterials"] == ["grass"]
  assert afterRestore == earlier
  assert [block[0] for block in afterRestore["passes"]] == ["base", "hill"]
  assert afterRestore["rockTexture"] == {"filePath": "//textures\\rock.png", "resolves": True, "size": [8, 8]}
  assert passesAfterRestore == [{"name": "hill", "strength": 1.0, "muted": False, "active": True}]
  assert restored["filePath"] == str(workFile)
  assert restored["unsavedChanges"] is False
  assert restored["restored"] == saved["name"]
  assert [checkpoint["label"] for checkpoint in listed["checkpoints"]] == ["hill and crate", f"before restore {stampOf(saved['name'])}"]
  assert listed["checkpoints"][1]["name"] == restored["beforeRestore"]
  assert listed["totalSizeBytes"] == sum(checkpointFile(toolingRoot, checkpoint["name"]).stat().st_size for checkpoint in listed["checkpoints"])
  assert afterTakeBack == later
  assert workFileBytes == checkpointFile(toolingRoot, restored["beforeRestore"]).read_bytes()
  assert takenBack["restored"] == restored["beforeRestore"]
  assert [checkpoint["label"] for checkpoint in listedAgain["checkpoints"]] == [
    "hill and crate", f"before restore {stampOf(saved['name'])}", f"before restore {stampOf(restored['beforeRestore'])}",
  ]


def testRestoreRefusesACheckpointOfAnotherWorkFile(stageBlenderServer, tmp_path):
  plotFile = tmp_path / "plot" / "plot.blend"
  otherFile = tmp_path / "other" / "other.blend"

  async def steps(session):
    await buildPlot(session, plotFile)
    saved = await session.expectSuccess("saveCheckpoint", {"label": "plot"})
    await buildPlot(session, otherFile, crateLocation=(-20, -20, 0))
    otherBefore = otherFile.read_bytes()
    refusal = await session.expectError("restoreCheckpoint", {"name": saved["name"]})
    otherListed = await session.expectSuccess("listCheckpoints")
    plotListed = await session.expectSuccess("listCheckpoints", {"path": str(plotFile)})
    crate = (await session.expectSuccess("getObjectDetail", {"name": "crate"}))["location"]
    return saved, otherBefore, refusal, otherListed, plotListed, crate

  saved, otherBefore, refusal, otherListed, plotListed, crate = stageBlenderServer.session(steps)
  assert f"Checkpoint '{saved['name']}' was saved from '{plotFile}', but the open work file is '{otherFile}'" in refusal
  assert otherFile.read_bytes() == otherBefore
  assert otherListed["checkpoints"] == []
  assert [checkpoint["name"] for checkpoint in plotListed["checkpoints"]] == [saved["name"]]
  assert crate == [-20.0, -20.0, 0.0]


def testWorkFilesOfOneNameInDifferentFoldersKeepSeparateCheckpoints(stageBlenderServer, tmp_path):
  eastFile = tmp_path / "east" / "plot.blend"
  westFile = tmp_path / "west" / "plot.blend"

  async def steps(session):
    await buildPlot(session, eastFile)
    east = await session.expectSuccess("saveCheckpoint", {"label": "start"})
    await buildPlot(session, westFile, crateLocation=(-20, -20, 0))
    west = await session.expectSuccess("saveCheckpoint", {"label": "start"})
    await session.expectSuccess("moveVertices", {"objectName": "crate", "selector": {"all": True}, "offset": [0, 0, 9]})
    restored = await session.expectSuccess("restoreCheckpoint", {"name": west["name"]})
    crate = (await session.expectSuccess("getObjectDetail", {"name": "crate"}))["worldMinimum"]
    eastListed = await session.expectSuccess("listCheckpoints", {"path": str(eastFile)})
    westListed = await session.expectSuccess("listCheckpoints")
    return east, west, restored, crate, eastListed, westListed

  east, west, restored, crate, eastListed, westListed = stageBlenderServer.session(steps)
  eastFolder, westFolder = east["name"].split("/")[0], west["name"].split("/")[0]
  assert eastFolder.startswith("plot-") and westFolder.startswith("plot-")
  assert eastFolder != westFolder
  assert [checkpoint["name"] for checkpoint in eastListed["checkpoints"]] == [east["name"]]
  assert [checkpoint["name"] for checkpoint in westListed["checkpoints"]] == [west["name"], restored["beforeRestore"]]
  assert eastListed["workFile"] == str(eastFile) and westListed["workFile"] == str(westFile)
  assert checkpointFile(stageBlenderServer.toolingRoot, east["name"]).read_bytes() == eastFile.read_bytes()
  assert crate == [-22.0, -22.0, 0.0]


def testCheckpointsWriteNothingInTheWorkRepository(stageBlenderServer, tmp_path):
  workRepository = tmp_path / "workRepository"
  workFile = workRepository / "zones" / "plot" / "plot.blend"

  async def steps(session):
    await buildPlot(session, workFile)
    before = filesUnder(workRepository)
    saved = await session.expectSuccess("saveCheckpoint", {"label": "start"})
    await wreckPlot(session, workFile)
    restored = await session.expectSuccess("restoreCheckpoint", {"name": saved["name"]})
    listed = await session.expectSuccess("listCheckpoints")
    await session.expectSuccess("deleteCheckpoints", {"names": [restored["beforeRestore"]]})
    return before, saved, listed, filesUnder(workRepository)

  before, saved, listed, after = stageBlenderServer.session(steps)
  checkpointsRoot = stageBlenderServer.toolingRoot / "checkpoints"
  assert before == ["zones\\plot\\plot.blend", "zones\\plot\\plot.blend1", "zones\\plot\\textures\\grass.png", "zones\\plot\\textures\\rock.png"]
  assert after == before
  assert Path(saved["path"]).parent.parent == checkpointsRoot
  assert Path(listed["folder"]).parent == checkpointsRoot
  assert len(listed["checkpoints"]) == 2


def testDeleteCheckpointsDeletesOnlyTheNamedAndTheLastTakesItsFolder(stageBlenderServer, tmp_path):
  workFile = tmp_path / "work" / "plot.blend"

  async def steps(session):
    await buildPlot(session, workFile)
    names = [(await session.expectSuccess("saveCheckpoint", {"label": label}))["name"] for label in ("one", "two", "three")]
    refusal = await session.expectError("deleteCheckpoints", {"names": [names[0], "plot-00000000/20260101T000000.000Z_missing"]})
    afterRefusal = await session.expectSuccess("listCheckpoints")
    deleted = await session.expectSuccess("deleteCheckpoints", {"names": [names[0], names[2]]})
    afterDelete = await session.expectSuccess("listCheckpoints")
    folder = Path(afterDelete["folder"])
    folderFiles = sorted(path.name for path in folder.iterdir())
    keptSize = (folder / folderFiles[0]).stat().st_size
    await session.expectSuccess("deleteCheckpoints", {"names": [names[1]]})
    afterLast = await session.expectSuccess("listCheckpoints")
    return names, refusal, afterRefusal, deleted, afterDelete, folder, folderFiles, keptSize, afterLast

  names, refusal, afterRefusal, deleted, afterDelete, folder, folderFiles, keptSize, afterLast = stageBlenderServer.session(steps)
  toolingRoot = stageBlenderServer.toolingRoot
  assert "No checkpoint is named 'plot-00000000/20260101T000000.000Z_missing'" in refusal
  assert [checkpoint["name"] for checkpoint in afterRefusal["checkpoints"]] == names
  assert deleted == {"deleted": [names[0], names[2]]}
  assert [checkpoint["name"] for checkpoint in afterDelete["checkpoints"]] == [names[1]]
  assert afterDelete["totalSizeBytes"] == keptSize
  assert not checkpointFile(toolingRoot, names[0]).exists() and not checkpointFile(toolingRoot, names[2]).exists()
  assert folderFiles == [f"{names[1].split('/')[1]}.blend", "workFile.json"]
  assert not folder.exists()
  assert afterLast["checkpoints"] == [] and afterLast["totalSizeBytes"] == 0


def testCheckpointsRefuseWhatTheyCannotKeep(stageBlenderServer, tmp_path):
  workFile = tmp_path / "work" / "plot.blend"
  packCode = f"""
image = bpy.data.images.new('baked', 4, 4)
image.filepath_raw = r'{tmp_path / "baked.png"}'
image.file_format = 'PNG'
image.save()
image.source = 'FILE'
image.pack()
bpy.data.materials['rock'].node_tree.nodes.new('ShaderNodeTexImage').image = image
"""

  async def steps(session):
    await session.expectSuccess("newFile", {"discardUnsavedChanges": True})
    neverSaved = await session.expectError("saveCheckpoint", {"label": "start"})
    neverSavedList = await session.expectError("listCheckpoints")
    await buildPlot(session, workFile)
    badLabel = await session.expectError("saveCheckpoint", {"label": "east/west"})
    saved = await session.expectSuccess("saveCheckpoint", {"label": "start"})
    await session.expectSuccess("runPython", {"code": packCode})
    unsaveable = await session.expectError("saveCheckpoint", {"label": "packed"})
    restoreRefusal = await session.expectError("restoreCheckpoint", {"name": saved["name"]})
    listed = await session.expectSuccess("listCheckpoints")
    await session.expectSuccess("openFile", {"path": str(workFile), "discardUnsavedChanges": True})
    restored = await session.expectSuccess("restoreCheckpoint", {"name": saved["name"]})
    return neverSaved, neverSavedList, badLabel, saved, unsaveable, restoreRefusal, listed, restored

  neverSaved, neverSavedList, badLabel, saved, unsaveable, restoreRefusal, listed, restored = stageBlenderServer.session(steps)
  assert "saveCheckpoint works on the open work file, and the open scene has never been saved" in neverSaved
  assert "listCheckpoints works on the open work file, and the open scene has never been saved" in neverSavedList
  assert "A checkpoint label is part of a file name" in badLabel and "['/']" in badLabel
  assert "Cannot save: image 'baked' is packed into the .blend" in unsaveable
  assert "restoreCheckpoint first keeps the current state as a checkpoint, and that failed" in restoreRefusal
  assert "Cannot save: image 'baked' is packed into the .blend" in restoreRefusal
  assert [checkpoint["name"] for checkpoint in listed["checkpoints"]] == [saved["name"]]
  assert restored["restored"] == saved["name"]
