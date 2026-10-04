import io
import math
from pathlib import Path

import numpy
from PIL import Image

from testModelsAndDressing import freshScene
from testReviewViews import crateScene, environment

longNote = "The crate from the south, low over the ground: does it sit on the grass, and does the grass run on behind it to the edge"


def pixelsOf(image):
  return numpy.asarray(Image.open(io.BytesIO(image)).convert("RGB"), dtype=numpy.int64)


def excludedFor(report, reason):
  return sorted(next(group["objects"] for group in report["excluded"] if group["reason"] == reason))


def testReviewCamerasReproduceTheirViewsAndRenderAsOneSheet(stageBlenderServer, tmp_path):
  views = {
    "high": {"eye": [0, -120, 80], "target": [40, 40, 5]},
    "standing": {"standAt": [0, 0], "headingDegrees": 45, "pitchDegrees": 0},
    "crateFrame": {"frame": {"objects": ["crate"], "headingDegrees": 200, "pitchDegrees": -25}},
  }

  async def steps(session):
    await crateScene(session, tmp_path)
    for name, view in views.items():
      await session.expectSuccess("saveReviewCamera", {"name": name, "view": view, "note": longNote if name == "high" else f"{name} view"})
    pairs = {name: (await session.expectImage("renderView", {"view": view}), await session.expectImage("renderView", {"view": {"camera": name}})) for name, view in views.items()}
    cameras = await session.expectSuccess("getReviewCameras")
    sheet = await session.expectImage("renderReviewSet", {}, mimeType="image/jpeg")
    refusals = [
      await session.expectError("renderReviewSet", {"names": ["standing", "nope"]}),
      await session.expectError("saveReviewCamera", {"name": "plan", "view": {"map": {"center": [0, 0], "width": 100}}, "note": "a map"}),
      await session.expectError("saveReviewCamera", {"name": "blank", "view": views["high"], "note": " "}),
      await session.expectError("saveReviewCamera", {"name": "crate", "view": views["high"], "note": "taken"}),
      await session.expectError("deleteReviewCameras", {"names": ["high", "nope"]}),
    ]
    moved = await session.expectSuccess("saveReviewCamera", {"name": "high", "view": {"eye": [0, -150, 90], "target": [40, 40, 5]}, "note": "farther back"})
    exported = await session.expectSuccess("checkExport", {"path": str(tmp_path / "cameras.eqg"), "purpose": "test"})
    deleted = await session.expectSuccess("deleteReviewCameras", {"names": ["high"]})
    return pairs, cameras, sheet, refusals, moved, exported, deleted

  pairs, cameras, sheet, refusals, moved, exported, deleted = stageBlenderServer.session(steps)
  # Each camera renders its view exactly, the scale figure standing again where she stood in the view from where a player stands.
  for name, ((viewImage, viewed), (cameraImage, camera)) in pairs.items():
    assert cameraImage == viewImage, name
  assert pairs["standing"][1][1]["figure"] == pairs["standing"][0][1]["figure"] and pairs["standing"][0][1]["figure"] is not None
  listed = {camera["name"]: camera for camera in cameras["cameras"]}
  assert list(listed) == ["crateFrame", "high", "standing"]
  assert listed["high"]["note"] == longNote and listed["high"]["view"] == views["high"]
  assert listed["high"]["headingDegrees"] == round(math.degrees(math.atan2(40, 160)), 2)
  assert listed["high"]["pitchDegrees"] == round(math.degrees(math.atan2(-75, math.hypot(40, 160))), 2)
  assert listed["standing"]["eye"] == [0.0, 0.0, 5.5] and listed["standing"]["headingDegrees"] == 45.0 and listed["standing"]["pitchDegrees"] == 0.0
  figure = listed["standing"]["figure"]
  assert figure[2] == 0.0 and 13 <= math.hypot(figure[0], figure[1]) <= 17 and figure[0] > 0 and figure[1] > 0
  # One sheet of the three in name order, each render as renderView gives it; the long note wraps to a second line under its cell.
  image, described = sheet
  assert Image.open(io.BytesIO(image)).size == (8 + 3 * (640 + 8), 8 + 360 + 20 + 17 + 8)
  assert [camera["name"] for camera in described["cameras"]] == ["crateFrame", "high", "standing"]
  for camera in described["cameras"]:
    assert Path(camera["outputPath"]).read_bytes() == pairs[camera["name"]][1][0]
  assert "nope" in refusals[0] and "{eye, target}" in refusals[1] and "note" in refusals[2] and "not a review camera" in refusals[3]
  assert "No review cameras ['nope']" in refusals[4] and "nothing deleted" in refusals[4]
  # Saved again under its name, a camera moves to the new view; export leaves the cameras out; deleting takes only the one named.
  assert moved["eye"] == [0.0, -150.0, 90.0] and moved["note"] == "farther back"
  assert excludedFor(exported, "a camera") == ["crateFrame", "high", "standing"]
  assert deleted == {"deleted": ["high"], "remaining": ["crateFrame", "standing"]}


offLedge = [[120, 0, 70], [44, 0, 70], [36, 0, 0], [-100, 0, 0]]


def testReviewRoutesWalkByNameAndRenderAsAStripOfEyeLevelFrames(stageBlenderServer, tmp_path):
  async def steps(session):
    await freshScene(session)
    await session.expectSuccess("createTerrainGrid", {"name": "ground", "size": [304, 120], "spacing": 8, "location": [0, 0, 0], "collection": "terrain"})
    await session.expectSuccess("createPrimitive", {"kind": "cube", "name": "ledge", "size": [100, 80, 70], "location": [90, 0, 0]})
    await session.expectSuccess("setZoneProperties", environment)
    saved = await session.expectSuccess("saveReviewRoute", {"name": "offLedge", "path": offLedge})
    byName = await session.expectSuccess("walkRoute", {"route": "offLedge"})
    byPath = await session.expectSuccess("walkRoute", {"path": offLedge})
    both = await session.expectError("walkRoute", {"route": "offLedge", "path": offLedge})
    strip = await session.expectImage("renderRouteStrip", {"route": "offLedge", "spacing": 50}, mimeType="image/jpeg")
    firstView = await session.expectImage("renderView", {"view": strip[1]["frames"][0]["view"]})
    withFigure = await session.expectImage("compareRenders", {"before": strip[1]["frames"][0]["outputPath"], "after": firstView[1]["outputPath"]}, mimeType="image/jpeg")
    tooMany = await session.expectError("renderRouteStrip", {"route": "offLedge", "spacing": 5})
    exported = await session.expectSuccess("checkExport", {"path": str(tmp_path / "routes.eqg"), "purpose": "test"})
    deleted = await session.expectSuccess("deleteReviewRoutes", {"names": ["offLedge"]})
    gone = await session.expectError("walkRoute", {"route": "offLedge"})
    return saved, byName, byPath, both, strip, firstView, withFigure, tooMany, exported, deleted, gone

  saved, byName, byPath, both, strip, firstView, withFigure, tooMany, exported, deleted, gone = stageBlenderServer.session(steps)
  assert saved == {"name": "offLedge", "path": [[float(value) for value in point] for point in offLedge], "length": 220.0}
  # Walked by name, the route is walked exactly as its points are: off the ledge's sheer side, 70 down, is a drop.
  assert byName == byPath and [problem["kind"] for problem in byName["problems"]] == ["drop"]
  assert "Give path" in both
  image, planned = strip
  # Frames every 50 along the route where the walk stands, and one where it stops at the drop; all look along the route (west).
  frames = planned["frames"]
  assert [frame["distance"] for frame in frames if frame["problem"] is None] == [0.0, 50.0, 100.0, 150.0, 200.0]
  drop = [frame for frame in frames if frame["problem"] is not None]
  assert len(drop) == 1 and drop[0]["problem"]["kind"] == "drop" and 80 <= drop[0]["distance"] <= 81 and drop[0]["problem"]["resumesAtDistance"] <= 82
  assert [frame["view"]["standAt"][2] for frame in frames] == [70.0, 70.0, 70.0, 0.0, 0.0, 0.0]
  assert all(frame["view"]["headingDegrees"] == 270.0 for frame in frames)
  # Along the way a frame looks at where the walk stands 30 on (on flat ground 5.5 below the eye); the drop's frame looks past the brink
  # down to the ground below.
  assert abs(frames[0]["view"]["pitchDegrees"] - math.degrees(math.atan2(-5.5, 30))) < 0.02
  assert drop[0]["view"]["pitchDegrees"] < -60
  assert Image.open(io.BytesIO(image)).size == (8 + 4 * (640 + 8), 8 + 2 * (360 + 20 + 8))
  # A frame is the eye-level view of its standAt, heading, and pitch without the scale figure: the same view rendered by renderView
  # differs only where she stands, 15 ahead and a step aside: a figure-shaped box beside the middle of the picture.
  left, top, right, bottom = withFigure[1]["changedBounds"]
  assert 0 < withFigure[1]["changedShare"] < 0.04 and 320 < left < right < 640 and right - left < 120 and 90 < top < bottom < 400, withFigure[1]
  assert "more than 16 on a sheet" in tooMany and "spacing of at least" in tooMany
  assert excludedFor(exported, "a guide") == ["offLedge"]
  assert deleted == {"deleted": ["offLedge"], "remaining": []} and "No review route named 'offLedge'" in gone


labelScene = [
  ("crate", "cube", [10, 10, 10], [40, 20, 0]), ("pillar", "cylinder", [4, 4, 30], [-45, 30, 0]), ("wall", "cube", [40, 2, 20], [0, -10, 0]),
  ("hidden", "cube", [6, 6, 6], [0, 10, 0]),
]
southLow = {"eye": [0, -60, 6], "target": [0, 40, 6]}


def testLabelsNameOnlyWhatTheViewShowsAndTheObjectsShadingColorsEachObject(stageBlenderServer, tmp_path):
  async def steps(session):
    await freshScene(session)
    await session.expectSuccess("createTerrainGrid", {"name": "ground", "size": [200, 200], "spacing": 8, "location": [0, 0, 0], "collection": "terrain"})
    for name, kind, size, location in labelScene:
      await session.expectSuccess("createPrimitive", {"kind": kind, "name": name, "size": size, "location": location} | ({"segments": 12} if kind == "cylinder" else {}))
    await session.expectSuccess("createRegion", {"name": "yard", "outline": [[-50, -50], [50, -50], [50, 50], [-50, 50]], "bottom": -5, "top": 40, "intent": "a yard"})
    await session.expectSuccess("setZoneProperties", environment)
    labelled = await session.expectImage("renderView", {"view": southLow, "labels": ["crate", "pillar", "hidden", "wall"]})
    plain = await session.expectImage("renderView", {"view": southLow})
    picks = {place["object"]: await session.expectSuccess("pick", {"view": southLow, "pixel": place["at"]}) for place in labelled[1]["labels"]["shown"]}
    objects = await session.expectImage("renderView", {"view": southLow, "shading": "objects"})
    unknown = await session.expectError("renderView", {"view": southLow, "labels": ["nope"]})
    region = await session.expectError("renderView", {"view": southLow, "labels": ["yard"]})
    return labelled, plain, picks, objects, unknown, region

  labelled, plain, picks, objects, unknown, region = stageBlenderServer.session(steps)
  places = labelled[1]["labels"]
  # The box behind the wall is not shown, so it gets no name; each name is written by a mark on its own object.
  assert [place["object"] for place in places["shown"]] == ["crate", "pillar", "wall"] and places["notVisible"] == ["hidden"]
  for place in places["shown"]:
    assert picks[place["object"]]["object"] == place["object"]
    x, y = place["at"]
    assert pixelsOf(labelled[0])[y, x].tolist() == [255, 255, 255] and pixelsOf(plain[0])[y, x].tolist() != [255, 255, 255]
  assert "No object named 'nope'" in unknown and "'yard' draws nothing in views" in region
  # Each object the view shows takes its own color, the largest first, and the crate's face toward the camera (south, away from the
  # northwest light) draws its color at the shading's ambient share.
  legend = objects[1]["objects"]["legend"]
  assert [entry["object"] for entry in legend][0] == "ground" and {entry["object"] for entry in legend} == {"ground", "crate", "pillar", "wall"}
  assert len({entry["color"] for entry in legend}) == 4 and [entry["share"] for entry in legend] == sorted([entry["share"] for entry in legend], reverse=True)
  crate = next(entry for entry in legend if entry["object"] == "crate")
  x, y = next(place["at"] for place in places["shown"] if place["object"] == "crate")
  expected = numpy.array([int(crate["rgb"][index:index + 2], 16) for index in (1, 3, 5)]) * 0.6
  assert numpy.abs(pixelsOf(objects[0])[y, x] - expected).max() <= 2, (pixelsOf(objects[0])[y, x], expected)
