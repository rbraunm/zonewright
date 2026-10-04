import io

import numpy
from PIL import Image

from testModelsAndDressing import freshScene
from testWater import basin, liquidMaterials

environment = {
  "ambientColor": [0.3, 0.3, 0.3], "specialAmbientColor": [0, 0, 0], "bounceColor": [0, 0, 0], "sunColor": [0.6, 0.6, 0.6],
  "sunAzimuthDegrees": 0, "sunElevationDegrees": 45, "fogColor": [0.5, 0.5, 0.5], "fogStart": 50, "fogEnd": 3000, "fogDensity": 0.1, "fogOn": True, "maxClip": 6000,
  "newEngineZone": False,
}
# Under the pool's middle (bed 20 down at its center) a hollow whose ceiling, at -22, lies within the 4 a box reaches under the bed.
caveCode = """
import bmesh
mesh = bpy.data.meshes.new('cave')
editor = bmesh.new()
bmesh.ops.create_cube(editor, size=1.0)
bmesh.ops.scale(editor, vec=(30, 30, 18), verts=editor.verts)
bmesh.ops.translate(editor, vec=(0, 0, -31), verts=editor.verts)
bmesh.ops.reverse_faces(editor, faces=editor.faces)
editor.to_mesh(mesh)
editor.free()
bpy.context.scene.collection.objects.link(bpy.data.objects.new('cave', mesh))
"""


async def pondScene(session, tmp_path):
  await freshScene(session)
  await basin(session)
  await liquidMaterials(session, tmp_path)
  await session.expectSuccess("floodWater", {"name": "pool", "seed": [0, 0], "level": -5, "material": "water"})
  await session.expectSuccess("setZoneProperties", environment)


def boxBottoms(volumes):
  return {volume["name"]: volume["minimum"][2] for volume in volumes}


def testSwimVolumesStartFromThePoolAndKeepWhatIsDoneByHand(stageBlenderServer, tmp_path):
  async def steps(session):
    await pondScene(session, tmp_path)
    undecided = await session.expectSuccess("getSwimVolumes", {})
    built = await session.expectSuccess("buildSwimVolumes", {"body": "pool"})
    listed = await session.expectSuccess("getSwimVolumes", {})
    widened = built["built"][0]
    await session.expectSuccess("transformObjects", {"names": [widened], "scale": [1.5, 1, 1]})
    kept = await session.expectError("buildSwimVolumes", {"body": "pool"})
    await session.expectSuccess("editWater", {"name": "pool", "level": -7})
    changed = await session.expectSuccess("getSwimVolumes", {})
    accepted = await session.expectSuccess("acceptSwimVolumes", {"body": "pool"})
    floating = await session.expectSuccess("placeSwimVolume", {"name": "skyPool", "liquid": "water", "minimum": [120, 120, 40], "maximum": [160, 150, 60]})
    clash = await session.expectError("placeSwimVolume", {"name": "SKYPOOL", "liquid": "water", "minimum": [0, 0, 0], "maximum": [1, 1, 1]})
    await session.expectSuccess("transformObjects", {"names": ["AWT_skyPool"], "rotateDegrees": [0, 0, 10]})
    turned = await session.expectSuccess("getSwimVolumes", {})
    return undecided, built, listed, widened, kept, changed, accepted, floating, clash, turned

  undecided, built, listed, widened, kept, changed, accepted, floating, clash, turned = stageBlenderServer.session(steps)
  assert undecided["bodies"] == [{"body": "pool", "state": "undecided", "boxes": [], "findings": []}]
  volumes = listed["volumes"]
  assert [volume["name"] for volume in volumes] == built["built"] and all(name.startswith("AWT_pool") for name in built["built"])
  # Tops at the surface (-5), bottoms 4 under the bed below them (20 down at the middle), every cell of the surface covered.
  assert all(volume["maximum"][2] == -5 for volume in volumes) and min(boxBottoms(volumes).values()) <= -24
  assert built["state"] == "boxed" and built["findings"] == [] and not any(volume["handEdited"] for volume in volumes)
  assert widened in kept and "edited or placed by hand" in kept
  # Lowered two units, the pool's boxes are stale; looked at and accepted, they are current again, the widened box kept as it was.
  assert changed["bodies"][0]["state"] == "changed"
  assert accepted["state"] == "boxed" and accepted["accepted"] == built["built"]
  assert any(finding["finding"] == "top away from the surface" for finding in accepted["findings"])
  # A pool in the air stands alone; a name that clashes once lowercased is refused; a turned box is an error a zone file cannot hold.
  assert floating["name"] == "AWT_skyPool" and floating["body"] is None and floating["minimum"] == [120.0, 120.0, 40.0]
  assert "already exists" in clash
  assert turned["errors"] == ["'AWT_skyPool' is turned; swim volumes stay square to the axes"]


def testBoxesStopAboveACaveUnderTheBedAndDrawInViews(stageBlenderServer, tmp_path):
  view = {"eye": [-120, -120, 30], "target": [0, 0, -15]}

  async def steps(session):
    await pondScene(session, tmp_path)
    await session.expectSuccess("runPython", {"code": caveCode})
    built = await session.expectSuccess("buildSwimVolumes", {"body": "pool"})
    volumes = (await session.expectSuccess("getSwimVolumes", {}))["volumes"]
    plain, _ = await session.expectImage("renderView", {"view": view})
    boxed, _ = await session.expectImage("renderView", {"view": view, "swimVolumes": True})
    plan, _ = await session.expectImage("renderSketch", {"center": [0, 0], "width": 300, "layers": ["water", "swim"]})
    section, cut = await session.expectImage("renderSection", {"start": [-120, 0], "end": [120, 0], "bottom": -45, "top": 20})
    await session.expectSuccess("editWater", {"name": "pool", "swimmable": False})
    refused = await session.expectError("buildSwimVolumes", {"body": "pool"})
    state = (await session.expectSuccess("getSwimVolumes", {}))["bodies"][0]["state"]
    return built, volumes, plain, boxed, plan, section, cut, refused, state

  built, volumes, plain, boxed, plan, section, cut, refused, state = stageBlenderServer.session(steps)
  # Without the hollow the boxes reach 4 under the bed, to -24 (the test above); over it they stop a unit over its ceiling at -22, and
  # the bed beyond it is never deeper than that.
  overCave = [volume for volume in volumes if all(abs(volume["minimum"][axis] + volume["maximum"][axis]) / 2 < 15 for axis in (0, 1))]
  assert overCave and min(volume["minimum"][2] for volume in volumes) == -21
  # Drawn, the boxes tint the view cyan over the water: more blue and green than red where they stand.
  plainPixels = numpy.asarray(Image.open(io.BytesIO(plain)).convert("RGB"), dtype=numpy.int64)
  boxedPixels = numpy.asarray(Image.open(io.BytesIO(boxed)).convert("RGB"), dtype=numpy.int64)
  tint = (boxedPixels - plainPixels)[200:400, 300:660].mean(axis=(0, 1))
  assert tint[2] > tint[0] + 15 and tint[1] > tint[0] + 15
  assert Image.open(io.BytesIO(plan)).size == (1440, 810)
  # The section along the x axis cuts the water at -5 and every box from the surface down, the middle box (over the hollow, 120 from
  # the section's start) stopping at -21 above the hollow's ceiling, which shows as ground below it.
  assert cut["water"] == [{"name": "pool", "levels": [-5.0, -5.0]}] and cut["length"] == 240
  middle = [box for box in cut["swim"] if box["s"][0] <= 120 <= box["s"][1]]
  assert len(middle) == 1 and middle[0]["z"] == [-21.0, -5.0] and all(box["z"][1] == -5.0 for box in cut["swim"])
  assert Image.open(io.BytesIO(section)).size == (1440, 810)
  assert "marked not swimmable" in refused and state == "notSwimmable"
