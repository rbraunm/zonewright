import io
import sys
from pathlib import Path

import numpy
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import planDrawing
from conftest import writePNG
from testModelsAndDressing import freshScene
from testWater import basin, environment, liquidMaterials, meshOf

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
    await session.expectSuccess("deleteObjects", {"names": built["built"]})
    await session.expectSuccess("editWater", {"name": "pool", "swimmable": False})
    refused = await session.expectError("buildSwimVolumes", {"body": "pool"})
    state = (await session.expectSuccess("getSwimVolumes", {}))["bodies"][0]["state"]
    return built, volumes, plain, boxed, plan, section, cut, refused, state

  built, volumes, plain, boxed, plan, section, cut, refused, state = stageBlenderServer.session(steps)
  # Without the hollow the boxes reach 4 under the bed, to -24 (the test above); over it they stop a unit over its ceiling at -22, and
  # the bed beyond it is never deeper than that.
  overCave = [volume for volume in volumes if all(abs(volume["minimum"][axis] + volume["maximum"][axis]) / 2 < 15 for axis in (0, 1))]
  assert overCave and min(volume["minimum"][2] for volume in volumes) == -21
  # Drawn, the boxes tint the view cyan over the water: more blue and green than red where they stand, and evenly where their tops
  # meet the surface, not in a mottle of the two fighting for depth.
  plainPixels = numpy.asarray(Image.open(io.BytesIO(plain)).convert("RGB"), dtype=numpy.int64)
  boxedPixels = numpy.asarray(Image.open(io.BytesIO(boxed)).convert("RGB"), dtype=numpy.int64)
  tint = (boxedPixels - plainPixels)[200:400, 300:660].mean(axis=(0, 1))
  assert tint[2] > tint[0] + 15 and tint[1] > tint[0] + 15
  overWater = (boxedPixels - plainPixels)[230:290, 330:630].reshape(-1, 3)
  assert overWater[:, 1].mean() > 30 and overWater[:, 1:].std(axis=0).max() < 6
  assert Image.open(io.BytesIO(plan)).size == (1440, 810)
  # The section along the x axis cuts the water at -5 and every box from the surface down, the middle box (over the hollow, 120 from
  # the section's start) stopping at -21 above the hollow's ceiling, which shows as ground below it.
  assert cut["water"] == [{"name": "pool", "levels": [-5.0, -5.0]}] and cut["length"] == 240
  middle = [box for box in cut["swim"] if box["s"][0] <= 120 <= box["s"][1]]
  assert len(middle) == 1 and middle[0]["z"] == [-21.0, -5.0] and all(box["z"][1] == -5.0 for box in cut["swim"])
  assert Image.open(io.BytesIO(section)).size == (1440, 810)
  assert "marked not swimmable" in refused and state == "notSwimmable"


markOlderNotSwimmable = """
import json
body = bpy.data.objects['pool']
definition = json.loads(body['zonewrightWater'])
definition['swimmable'] = False
body['zonewrightWater'] = json.dumps(definition)
result = definition['swimmable']
"""


def flagged(body, finding):
  return {item["box"] for item in body["findings"] if item["finding"] == finding}


def testBoxesLeftWithoutWaterAndBoxesOfABodyNoOneSwimsInAreReported(stageBlenderServer, tmp_path):
  async def steps(session):
    await pondScene(session, tmp_path)
    await session.expectSuccess("sculptAlongPath", {"objectName": "ground", "mode": "carve", "path": [[130, 130, -12]], "radius": 50, "strength": 1, "profile": [[0, 0], [1, 12]], "conformRim": False})
    built = await session.expectSuccess("buildSwimVolumes", {"body": "pool"})
    volumes = (await session.expectSuccess("getSwimVolumes", {}))["volumes"]
    await session.expectSuccess("editWater", {"name": "pool", "within": [[-200, -200], [10, -200], [10, 200], [-200, 200]]})
    halved = (await session.expectSuccess("getSwimVolumes", {}))["bodies"][0]
    await session.expectSuccess("editWater", {"name": "pool", "within": [], "seed": [130, 130]})
    moved = (await session.expectSuccess("getSwimVolumes", {}))["bodies"][0]
    await session.expectSuccess("editWater", {"name": "pool", "seed": [0, 0]})
    marking = await session.expectError("editWater", {"name": "pool", "swimmable": False})
    await session.expectSuccess("floodWater", {"name": "fountain", "seed": [130, 130], "level": -5, "material": "water"})
    await session.expectSuccess("editWater", {"name": "fountain", "swimmable": False})
    placing = await session.expectError("placeSwimVolume", {"name": "fountainPool", "liquid": "water", "minimum": [120, 120, -10], "maximum": [140, 140, -5], "body": "fountain"})
    await session.expectSuccess("runPython", {"code": markOlderNotSwimmable})
    older = await session.expectSuccess("getSwimVolumes", {})
    exporting = await session.expectError("exportZone", {"path": str(tmp_path / "swimtest.eqg")})
    return built, volumes, halved, moved, marking, placing, older, exporting

  built, volumes, halved, moved, marking, placing, older, exporting = stageBlenderServer.session(steps)
  east = {volume["name"] for volume in volumes if volume["minimum"][0] >= 10}
  west = {volume["name"] for volume in volumes if volume["maximum"][0] <= 10}
  assert east and west and built["findings"] == []
  # Cut back to the west of x = 10, the pool leaves the boxes east of the cut standing over a drained basin: each is reported, and none
  # still over the water is.
  stale = flagged(halved, "mostly without water over it")
  assert halved["state"] == "changed" and east <= stale and not west & stale
  # Seeded in the other basin, the pool leaves every box behind.
  assert flagged(moved, "mostly without water over it") == set(built["built"])
  # A body no one swims in has no swim volumes: marking one that has them is refused, naming them, as is tying a box to one; boxes of
  # one, as a file saved before could hold, are errors that export refuses.
  assert all(name in marking for name in built["built"]) and "deleteObjects" in marking
  assert "marked not swimmable" in placing
  pool = next(body for body in older["bodies"] if body["body"] == "pool")
  assert pool["state"] == "notSwimmable" and pool["boxes"] == built["built"]
  assert older["errors"] == [
    f"'{name}' belongs to 'pool', which is marked not swimmable; delete the box or mark the body swimmable (editWater swimmable true)" for name in built["built"]
  ]
  assert "marked not swimmable" in exporting


def testLavaTakesLavaSwimVolumesThatShowOverIt(stageBlenderServer, tmp_path):
  view = {"eye": [-120, -120, 30], "target": [0, 0, -15]}

  async def steps(session):
    await freshScene(session)
    await basin(session)
    diffuse = str(writePNG(tmp_path / "lava_c.png", 4, 4, (250, 90, 10, 255)))
    second = str(writePNG(tmp_path / "lava_d.png", 4, 4, (255, 200, 40, 255)))
    normal = str(writePNG(tmp_path / "lava_n.png", 4, 4, (128, 128, 255, 255)))
    noSecond = await session.expectError("createLiquidMaterial", {"name": "flatLava", "liquid": "lava", "diffuseTexture": diffuse, "normalTexture": normal})
    await session.expectSuccess("createLiquidMaterial", {"name": "lava", "liquid": "lava", "diffuseTexture": diffuse, "normalTexture": normal, "secondDiffuseTexture": second})
    await session.expectSuccess("floodWater", {"name": "pit", "seed": [0, 0], "level": -5, "material": "lava"})
    await session.expectSuccess("setZoneProperties", environment)
    built = await session.expectSuccess("buildSwimVolumes", {"body": "pit"})
    plain, _ = await session.expectImage("renderView", {"view": view})
    boxed, _ = await session.expectImage("renderView", {"view": view, "swimVolumes": True})
    return noSecond, built, plain, boxed

  noSecond, built, plain, boxed = stageBlenderServer.session(steps)
  assert "missing ['secondDiffuse']" in noSecond
  assert built["built"] and all(name.startswith("ALV_pit") for name in built["built"]) and built["findings"] == []
  # Over the lava's oranges the boxes draw magenta: bluer, and less green, than the lava alone.
  tint = (numpy.asarray(Image.open(io.BytesIO(boxed)).convert("RGB"), dtype=numpy.int64) - numpy.asarray(Image.open(io.BytesIO(plain)).convert("RGB"), dtype=numpy.int64))[230:290, 330:630].mean(axis=(0, 1))
  assert tint[2] > 30 and tint[2] > tint[1] + 40


def testPlanNamesEachSwimVolumeInsideItClearOfTheOthers(stageBlenderServer, tmp_path):
  plan = {"center": [0, 0], "width": 260}

  async def steps(session):
    await pondScene(session, tmp_path)
    await session.expectSuccess("buildSwimVolumes", {"body": "pool"})
    volumes = (await session.expectSuccess("getSwimVolumes", {}))["volumes"]
    _, drawn = await session.expectImage("renderSketch", plan | {"layers": ["water", "swim"], "spotHeights": False})
    return volumes, drawn

  volumes, drawn = stageBlenderServer.session(steps)
  frame = planDrawing.PlanFrame(plan["center"], plan["width"], (1440, 810))
  boxes = [{"name": volume["name"], "liquid": volume["liquid"], "corners": [volume["minimum"], volume["maximum"]]} for volume in volumes]
  board = planDrawing.LabelBoard(ImageDraw.Draw(Image.new("RGBA", frame.size)), frame.size)
  labels, unnamed = planDrawing.swimLabels(board, frame, boxes)
  bounds = {name: board.textBox(position, text, planDrawing.swimLabelSize) for name, text, position in labels}
  rectangles = {}
  for box in boxes:
    (left, bottom), (right, top) = frame.pixel(box["corners"][0]), frame.pixel(box["corners"][1])
    rectangles[box["name"]] = (left, top, right, bottom)
  # Each box is named inside itself, in full where its name fits and by its number in the narrow strips where it does not, and no name
  # overlaps another; the drawing reports any box it left unnamed.
  assert sorted(list(bounds) + unnamed) == sorted(rectangles)
  assert {text for _, text, _ in labels} >= {"AWT_pool01"} and any(text == name[-2:] for name, text, _ in labels)
  for name, (left, top, right, bottom) in bounds.items():
    boxLeft, boxTop, boxRight, boxBottom = rectangles[name]
    assert boxLeft <= left and right <= boxRight and boxTop <= top and bottom <= boxBottom
  named = list(bounds.values())
  for index, a in enumerate(named):
    for b in named[index + 1:]:
      assert a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1]
  assert drawn["unnamedSwimVolumes"] == unnamed


def pixelsOf(image):
  return numpy.asarray(Image.open(io.BytesIO(image)).convert("RGB"), dtype=numpy.int64)


hideRiver = "bpy.data.objects['river'].hide_render = {hidden}\nresult = bpy.data.objects['river'].hide_render"


def testSwimVolumesTintASlopingRiverEvenlyAndNoHigherThanTheyStand(stageBlenderServer, tmp_path):
  near = {"eye": [-120, -70, 24], "target": [-20, 0, -8]}
  far = {"eye": [-1300, -1700, 900], "target": [0, 0, -6]}

  async def steps(session):
    await freshScene(session)
    await session.expectSuccess("createTerrainGrid", {"name": "ground", "size": [400, 160], "spacing": 8, "location": [0, 0, 0], "collection": "terrain"})
    await session.expectSuccess("sculptAlongPath", {"objectName": "ground", "mode": "carve", "path": [[-200, 0, -10], [200, 0, -10]], "radius": 34, "strength": 1, "profile": [[0, 0], [0.55, 0], [1, 10]], "conformRim": False})
    await liquidMaterials(session, tmp_path)
    await session.expectSuccess("runWater", {"name": "river", "path": [[-150, 0, -4], [150, 0, -8]], "reach": 30, "material": "water"})
    await session.expectSuccess("setZoneProperties", environment)
    built = await session.expectSuccess("buildSwimVolumes", {"body": "river"})
    images = {}
    for name, view in (("near", near), ("far", far)):
      images[name] = (await session.expectImage("renderView", {"view": view}))[0]
      images[name + "Swim"] = (await session.expectImage("renderView", {"view": view, "swimVolumes": True}))[0]
    await session.expectSuccess("runPython", {"code": hideRiver.format(hidden=True)})
    images["farDry"] = (await session.expectImage("renderView", {"view": far}))[0]
    await session.expectSuccess("runPython", {"code": hideRiver.format(hidden=False)})
    river = await meshOf(session, "river")
    return built, images, river

  built, images, river = stageBlenderServer.session(steps)
  # The river falls 4 over its length, so its boxes step down a unit at a time with their tops up to a unit under its surface; drawn,
  # they tint the river evenly, more blue and green than red, where boxes and a sloping surface once broke it into a sawtooth.
  levels = numpy.array(river["vertices"])[:, 2]
  assert len(built["built"]) >= 4 and levels.max() - levels.min() > 3.9
  tint = (pixelsOf(images["nearSwim"]) - pixelsOf(images["near"]))[380:440, 20:260].reshape(-1, 3)
  assert tint[:, 1].mean() > 30 and tint[:, 2].mean() > tint[:, 0].mean() + 30 and tint[:, 1:].std(axis=0).max() < 6
  # From far off the tint rises no higher in the view than the water does: no box is drawn above where it stands (lifted by the square
  # of the distance, they once stood 4 rows of pixels above it here).
  water = numpy.abs(pixelsOf(images["far"]) - pixelsOf(images["farDry"])).sum(axis=2) > 12
  tinted = numpy.abs(pixelsOf(images["farSwim"]) - pixelsOf(images["far"])).sum(axis=2) > 12
  columns = numpy.flatnonzero(water.any(axis=0) & tinted.any(axis=0))
  assert len(columns) > 40 and (tinted.argmax(axis=0)[columns] >= water.argmax(axis=0)[columns] - 1).all()
