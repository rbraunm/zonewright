import math
from pathlib import Path

groundSceneCode = """
import bpy
mesh = bpy.data.meshes.new('ground')
mesh.from_pydata([(-200, -200, 0), (200, -200, 0), (200, 200, 0), (-200, 200, 0)], [], [(0, 1, 2, 3)])
ground = bpy.data.objects.new('ground', mesh)
bpy.context.scene.collection.objects.link(ground)
material = bpy.data.materials.new('grass')
material.use_nodes = True
material.node_tree.nodes['Principled BSDF'].inputs['Base Color'].default_value = (0.2, 0.45, 0.15, 1)
mesh.materials.append(material)
"""
zoneProperties = {
  "fogColor": [0.55, 0.6, 0.7], "fogStart": 30, "fogEnd": 200,
  "sunAzimuthDegrees": 135, "sunElevationDegrees": 45, "sunColor": [1, 0.95, 0.85], "sunStrength": 3,
  "ambientColor": [0.3, 0.3, 0.35], "newEngineZone": False,
}
eyeLevelView = {"standAt": [0, 0, 0], "headingDegrees": 0, "pitchDegrees": 0}
eyeHeight = 5.5
renderHeight = 540
verticalFieldOfViewDegrees = 52


def linearToSRGB(component):
  return 12.92 * component if component <= 0.0031308 else 1.055 * component ** (1 / 2.4) - 0.055


async def buildGroundScene(session):
  await session.expectSuccess("newFile", {"discardUnsavedChanges": True})
  await session.expectSuccess("runPython", {"code": groundSceneCode})
  await session.expectSuccess("setZoneProperties", zoneProperties)


def testRunPythonKeepsNamespaceAndReturnsOutput(stageBlenderServer):
  async def steps(session):
    first = await session.expectSuccess("runPython", {"code": "width = 6\nprint('hello from blender')"})
    second = await session.expectSuccess("runPython", {"code": "result = {'area': width * 7, 'version': bpy.app.version_string}"})
    return first, second

  first, second = stageBlenderServer.session(steps)
  assert first == {"output": "hello from blender\n", "result": None}
  assert second["result"]["area"] == 42
  assert second["result"]["version"].startswith("5.2.2")


def testRunPythonErrorCarriesTraceback(stageBlenderServer):
  async def steps(session):
    return await session.expectError("runPython", {"code": "def explode():\n  raise ValueError('boom')\nexplode()"})

  errorText = stageBlenderServer.session(steps)
  assert "ValueError: boom" in errorText
  assert "Traceback (most recent call last)" in errorText
  assert 'File "<runPython>", line 2, in explode' in errorText


def testCrashIsReportedAndNextCallStartsFresh(stageBlenderServer):
  async def steps(session):
    await session.expectSuccess("runPython", {"code": "survivor = 1"})
    crashText = await session.expectError("runPython", {"code": "import os\nos._exit(7)"})
    crashedStatus = await session.expectSuccess("getToolingStatus")
    afterCrash = await session.expectSuccess("runPython", {"code": "result = 'survivor' in globals()"})
    runningStatus = await session.expectSuccess("getToolingStatus")
    return crashText, crashedStatus["bridge"], afterCrash, runningStatus["bridge"]

  crashText, crashedBridge, afterCrash, runningBridge = stageBlenderServer.session(steps)
  assert "Blender exited with code 7 during runPython" in crashText
  assert crashedBridge["state"] == "crashed"
  assert crashedBridge["exitCode"] == 7
  assert afterCrash["result"] is False
  assert runningBridge["state"] == "running"


def testUnsavedChangesBlockNewFileUntilDiscarded(stageBlenderServer):
  async def steps(session):
    await session.expectSuccess("newFile", {"discardUnsavedChanges": True})
    await session.expectSuccess("runPython", {"code": "bpy.data.objects.new('marker', None)"})
    refusal = await session.expectError("newFile")
    discarded = await session.expectSuccess("newFile", {"discardUnsavedChanges": True})
    summary = await session.expectSuccess("getSceneSummary")
    return refusal, discarded, summary

  refusal, discarded, summary = stageBlenderServer.session(steps)
  assert "Refusing to start a new file: the open file has unsaved changes" in refusal
  assert discarded == {"filePath": None, "unsavedChanges": False, "scene": "Scene"}
  assert summary["objectCount"] == 0


def testSaveStoresTexturesAsRelativePathsAndReopens(stageBlenderServer, tmp_path):
  workFolder = tmp_path / "work"
  (workFolder / "textures").mkdir(parents=True)
  texturePath = workFolder / "textures" / "grass.png"
  blendPath = workFolder / "scratch.blend"
  textureCode = f"""
image = bpy.data.images.new('grassTexture', 8, 8)
image.filepath_raw = r'{texturePath}'
image.file_format = 'PNG'
image.save()
image.source = 'FILE'
material = bpy.data.materials['grass']
texture = material.node_tree.nodes.new('ShaderNodeTexImage')
texture.image = image
material.node_tree.links.new(texture.outputs['Color'], material.node_tree.nodes['Principled BSDF'].inputs['Base Color'])
"""

  async def steps(session):
    await buildGroundScene(session)
    await session.expectSuccess("runPython", {"code": textureCode})
    saved = await session.expectSuccess("saveFile", {"path": str(blendPath)})
    await session.expectSuccess("newFile")
    reopened = await session.expectSuccess("openFile", {"path": str(blendPath)})
    summary = await session.expectSuccess("getSceneSummary")
    return saved, reopened, summary

  saved, reopened, summary = stageBlenderServer.session(steps)
  assert saved == {"filePath": str(blendPath), "unsavedChanges": False, "scene": "Scene"}
  assert reopened["filePath"] == str(blendPath)
  images = {image["name"]: image for image in summary["images"]}
  assert images["grassTexture"]["filePath"] == "//textures\\grass.png"
  assert images["grassTexture"]["packed"] is False
  assert {"name": "grass", "textures": ["grassTexture"]} in summary["materials"]
  assert summary["zoneProperties"]["fogEnd"] == 200
  assert summary["zoneProperties"]["newEngineZone"] is False
  assert [sceneObject["name"] for sceneObject in summary["objects"]] == ["ground"]
  assert summary["objects"][0]["triangles"] == 2


def testSaveRefusesPackedImage(stageBlenderServer, tmp_path):
  async def steps(session):
    await buildGroundScene(session)
    await session.expectSuccess("runPython", {"code": f"""
image = bpy.data.images.new('baked', 4, 4)
image.filepath_raw = r'{tmp_path / "baked.png"}'
image.file_format = 'PNG'
image.save()
image.source = 'FILE'
image.pack()
texture = bpy.data.materials['grass'].node_tree.nodes.new('ShaderNodeTexImage')
texture.image = image
"""})
    return await session.expectError("saveFile", {"path": str(tmp_path / "packed.blend")})

  errorText = stageBlenderServer.session(steps)
  assert "Cannot save: image 'baked' is packed into the .blend" in errorText
  assert not (tmp_path / "packed.blend").exists()


def testRenderWithoutZonePropertiesFails(stageBlenderServer):
  async def steps(session):
    await session.expectSuccess("newFile", {"discardUnsavedChanges": True})
    await session.expectSuccess("runPython", {"code": groundSceneCode})
    return await session.expectError("renderView", {"view": eyeLevelView})

  assert "Zone properties missing: ['fogColor', 'fogStart', 'fogEnd', 'sunAzimuthDegrees', 'sunElevationDegrees', 'sunColor', 'sunStrength', 'ambientColor', 'newEngineZone']" in stageBlenderServer.session(steps)


def testRenderIsDeterministicFoggedAndFiled(stageBlenderServer):
  async def steps(session):
    await buildGroundScene(session)
    firstImage, firstDescription = await session.expectImage("renderView", {"view": eyeLevelView})
    secondImage, secondDescription = await session.expectImage("renderView", {"view": eyeLevelView})
    sky = await session.expectSuccess("runPython", {"code": f"""
image = bpy.data.images.load(r'{firstDescription['outputPath']}')
width, height = image.size
index = ((height - 1) * width + width // 2) * 4
result = list(image.pixels[index:index + 3])
"""})
    return firstImage, firstDescription, secondImage, secondDescription, sky["result"]

  firstImage, firstDescription, secondImage, secondDescription, skyPixel = stageBlenderServer.session(steps)
  assert firstImage == secondImage
  assert Path(firstDescription["outputPath"]).read_bytes() == firstImage
  assert firstDescription["outputPath"] != secondDescription["outputPath"]
  assert firstImage[:8] == b"\x89PNG\r\n\x1a\n"
  assert (firstDescription["width"], firstDescription["height"]) == (960, 540)
  for measured, fogComponent in zip(skyPixel, zoneProperties["fogColor"]):
    assert abs(measured - linearToSRGB(fogComponent)) <= 2 / 255


def testEyeLevelViewPlacesFigureAndPickMeasuresTheGround(stageBlenderServer):
  pixel = [480, 500]

  async def steps(session):
    await buildGroundScene(session)
    _, description = await session.expectImage("renderView", {"view": eyeLevelView})
    picked = await session.expectSuccess("pick", {"view": eyeLevelView, "pixel": pixel})
    sky = await session.expectSuccess("pick", {"view": eyeLevelView, "pixel": [480, 10]})
    return description, picked, sky

  description, picked, sky = stageBlenderServer.session(steps)
  assert description["eye"] == [0.0, 0.0, eyeHeight]
  assert description["figure"] == [1.5, 15.0, 0.0]
  depression = math.atan((pixel[1] + 0.5 - renderHeight / 2) / (renderHeight / 2) * math.tan(math.radians(verticalFieldOfViewDegrees / 2)))
  assert picked["hit"] is True
  assert picked["object"] == "ground"
  assert picked["material"] == "grass"
  assert picked["normal"] == [0.0, 0.0, 1.0]
  assert abs(picked["position"][1] - eyeHeight / math.tan(depression)) < 0.01
  assert abs(picked["distance"] - eyeHeight / math.sin(depression)) < 0.01
  assert sky["hit"] is False


def testSyncStopsAnIdleBridgeButRefusesUnsavedChanges(stageBlenderServer):
  async def steps(session):
    await session.expectSuccess("runPython", {"code": "bpy.data.objects.new('marker', None)"})
    refusal = await session.expectError("syncTooling")
    await session.expectSuccess("newFile", {"discardUnsavedChanges": True})
    synced = await session.expectSuccess("syncTooling")
    status = await session.expectSuccess("getToolingStatus")
    return refusal, synced, status

  refusal, synced, status = stageBlenderServer.session(steps)
  assert "unsaved changes; save it or open another file with discardUnsavedChanges before syncing" in refusal
  assert synced["actions"] == []
  assert status["bridge"] == {"state": "notStarted"}
