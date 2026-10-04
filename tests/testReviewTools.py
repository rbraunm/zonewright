import io
import math
from pathlib import Path

from PIL import Image

from testReviewViews import crateScene, environment

longNote = "The crate from the south, low over the ground: does it sit on the grass, and does the grass run on behind it to the edge"


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
