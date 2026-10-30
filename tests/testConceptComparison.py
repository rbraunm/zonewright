import io
import math
import shutil
import sys
from pathlib import Path

import numpy
from PIL import Image, ImageDraw, ImageEnhance

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import conceptComparison
import previewFrame
from conftest import writePNG
from testReviewViews import crateScene

view = {"eye": [0, -120, 60], "target": [40, 40, 5]}
nightSky = {"hour": 21, "minute": 0}
nightDrawn = {"hour": 21, "minute": 0, "weather": "DefaultClear", "lightFrom": "moon"}


def paintedScene(path):
  """A small painting: a sky shading from blue to orange over a dark cliff on the left, a sand bank on the right, and a red rock."""
  image = Image.new("RGB", (480, 200))
  draw = ImageDraw.Draw(image)
  for row in range(200):
    draw.line([(0, row), (480, row)], fill=(int(90 + row * 0.8), int(130 + row * 0.3), int(220 - row * 0.6)))
  draw.polygon([(0, 40), (150, 70), (210, 200), (0, 200)], fill=(60, 35, 25))
  draw.ellipse([260, 110, 520, 260], fill=(225, 190, 140))
  draw.rectangle([300, 60, 360, 130], fill=(170, 60, 40))
  image.save(path)
  return image


def testTheComparisonTellsAMatchFromADriftInColorOrInPlace(tmp_path):
  concept = paintedScene(tmp_path / "concept.png")
  concept.save(tmp_path / "same.png")
  ImageEnhance.Color(ImageEnhance.Brightness(concept).enhance(0.6)).enhance(0.3).save(tmp_path / "drab.png")
  shifted = Image.new("RGB", concept.size, (0, 0, 0))
  shifted.paste(concept, (48, 0))
  shifted.paste(concept.crop((0, 0, 48, 200)), (0, 0))
  shifted.save(tmp_path / "shifted.png")
  forward = [0.0, 1.0, 0.0]
  same, drab, moved = (
    conceptComparison.compare(tmp_path / "concept.png", tmp_path / f"{name}.png", tmp_path / f"{name}.jpg", forward, previewFrame.verticalFieldOfViewDegrees)
    for name in ("same", "drab", "shifted")
  )
  # The same picture matches on every measure.
  assert same["masses"] == {"sameThird": 1.0, "correlation": 1.0}
  assert same["edges"] == {"conceptEdgesMet": 1.0, "renderEdgesMet": 1.0}
  assert same["palette"]["conceptDistance"] == 0.0 and same["palette"]["renderDistance"] == 0.0
  # Darker and greyer, it keeps its masses and edges, each picture split at its own thirds, and drifts in palette and color.
  assert drab["masses"]["sameThird"] > 0.95 and drab["edges"]["conceptEdgesMet"] > 0.9
  assert drab["palette"]["conceptDistance"] > 15
  assert drab["color"]["render"]["lightness"] < drab["color"]["concept"]["lightness"] - 10
  assert drab["color"]["render"]["chroma"] < drab["color"]["concept"]["chroma"] / 2
  # Moved a tenth of the way across, it keeps its palette and loses its masses and edges.
  assert moved["palette"]["conceptDistance"] < 5
  assert moved["masses"]["sameThird"] < 0.85 and moved["edges"]["conceptEdgesMet"] < 0.6
  # Straight ahead, eye level runs through the middle of the picture.
  assert same["eyeLevelRow"] == 100.0
  assert Image.open(tmp_path / "same.jpg").size == (8 + 3 * (640 + 8), 8 + 3 * (267 + 20 + 8))


def testAConceptCameraRendersAtTheArtsFrameAndKeepsItsArtWhereTheWorkMoves(stageBlenderServer, tmp_path):
  workA, workB = tmp_path / "workA", tmp_path / "workB"
  (workA / "ref").mkdir(parents=True)
  wide = writePNG(tmp_path / "wide.png", 400, 200, (128, 128, 128, 255))

  async def steps(session):
    await crateScene(session, tmp_path)
    await session.expectSuccess("setZoneProperties", {"sky": {"type": "default", "hour": 13, "minute": 0}})
    tried = await session.expectImage("compareToConcept", {"concept": str(wide), "view": view}, mimeType="image/jpeg")
    shutil.copy(tried[1]["renderPath"], workA / "ref" / "art.png")
    await session.expectSuccess("saveFile", {"path": str(workA / "plot.blend")})
    kept = await session.expectImage("compareToConcept", {"concept": str(workA / "ref" / "art.png"), "view": view, "saveAs": "match", "note": "the crate as the art shows it"}, mimeType="image/jpeg")
    nightKept = await session.expectImage("compareToConcept", {"concept": str(workA / "ref" / "art.png"), "view": view, "saveAs": "nightMatch", "note": "the crate as the art shows it, by night", "sky": nightSky}, mimeType="image/jpeg")
    again = await session.expectImage("compareToConcept", {"camera": "match"}, mimeType="image/jpeg")
    nightAgain = await session.expectImage("compareToConcept", {"camera": "nightMatch"}, mimeType="image/jpeg")
    _, rendered = await session.expectImage("renderView", {"view": {"camera": "match"}})
    nightRendered = await session.expectImage("renderView", {"view": {"camera": "nightMatch"}})
    dayCameraAtNight = await session.expectImage("renderView", {"view": {"camera": "match"}, "sky": nightSky})
    centre = await session.expectSuccess("pick", {"view": {"camera": "match"}, "pixel": [480, 240]})
    refusals = [
      await session.expectError("pick", {"view": {"camera": "match"}, "pixel": [100, 500]}),
      await session.expectError("compareToConcept", {"camera": "nope"}),
      await session.expectError("compareToConcept", {"camera": "match", "view": view}),
      await session.expectError("compareToConcept", {"concept": str(tmp_path / "missing.png"), "view": view}),
      await session.expectError("compareToConcept", {"concept": str(wide), "view": {"map": {"center": [0, 0], "width": 100}}}),
      await session.expectError("compareToConcept", {"concept": str(wide), "view": view, "saveAs": "unnoted"}),
      await session.expectError("compareToConcept", {"camera": "match", "sky": {"hour": 1}}),
    ]
    sheet = await session.expectImage("renderReviewSet", {"names": ["match"]}, mimeType="image/jpeg")
    await session.expectSuccess("saveFile", {})
    shutil.copytree(workA, workB)
    (workA / "ref" / "art.png").unlink()
    await session.expectSuccess("openFile", {"path": str(workB / "plot.blend")})
    moved = await session.expectSuccess("getReviewCameras")
    movedAgain = await session.expectImage("compareToConcept", {"camera": "match"}, mimeType="image/jpeg")
    return tried, kept, nightKept, again, nightAgain, rendered, nightRendered, dayCameraAtNight, centre, refusals, sheet, moved, movedAgain

  tried, kept, nightKept, again, nightAgain, rendered, nightRendered, dayCameraAtNight, centre, refusals, sheet, moved, movedAgain = stageBlenderServer.session(steps)
  # The zone is drawn at the art's aspect, 2 to 1, as the RoF2 client's camera draws it: half the horizontal view 45/512 of a turn, the
  # vertical following the aspect.
  assert tried[1]["frameSize"] == [960, 480] and Image.open(tried[1]["renderPath"]).size == (960, 480)
  assert tried[1]["horizontalFieldOfViewDegrees"] == round(2 * 45 * 360 / 512, 2)
  assert abs(tried[1]["verticalFieldOfViewDegrees"] - math.degrees(2 * math.atan(math.tan(math.radians(45 * 360 / 512)) / 2))) < 1e-9
  # Art that is the render itself matches on every measure, and the kept camera draws that render again exactly.
  assert kept[1]["masses"] == {"sameThird": 1.0, "correlation": 1.0}
  assert kept[1]["edges"] == {"conceptEdgesMet": 1.0, "renderEdgesMet": 1.0} and kept[1]["palette"]["conceptDistance"] == 0.0
  assert kept[1]["keptCamera"]["concept"] == {"path": str(workA / "ref" / "art.png"), "size": [960, 480], "verticalFieldOfViewDegrees": tried[1]["verticalFieldOfViewDegrees"]}
  assert Path(again[1]["renderPath"]).read_bytes() == Path(kept[1]["renderPath"]).read_bytes()
  assert again[1]["masses"] == kept[1]["masses"] and again[1]["keptCamera"] is None
  # A camera kept at 21:00 renders the art's frame by moonlight, kept and compared again alike, as the day camera does when asked
  # for 21:00, and unlike the day camera at the zone's hour; each result says the hour it drew.
  assert kept[1]["keptCamera"]["sky"] is None and "sky" not in kept[1] and "sky" not in again[1]
  assert nightKept[1]["keptCamera"]["sky"] == nightSky and nightKept[1]["sky"] == nightDrawn and nightAgain[1]["sky"] == nightDrawn
  nightBytes = Path(nightAgain[1]["renderPath"]).read_bytes()
  assert nightBytes == Path(nightKept[1]["renderPath"]).read_bytes() and nightBytes != Path(again[1]["renderPath"]).read_bytes()
  assert nightRendered[0] == nightBytes and dayCameraAtNight[0] == nightBytes
  assert nightRendered[1]["sky"] == nightDrawn and (nightRendered[1]["width"], nightRendered[1]["height"]) == (960, 480)
  # renderView and pick take the kept camera's frame.
  assert (rendered["width"], rendered["height"]) == (960, 480)
  assert centre["hit"] is True and centre["object"] == "crate"
  assert "outside the 960x480 render" in refusals[0]
  assert "not a review camera matched to concept art" in refusals[1] and "['match', 'nightMatch']" in refusals[1]
  assert "kept camera alone" in refusals[2] and "cannot be read" in refusals[3] and "{frame}" in refusals[4] and "note" in refusals[5]
  assert "hour and minute both given" in refusals[6]
  assert sheet[1]["cameras"][0]["sky"] is None
  # On the review sheet the wide render keeps its shape, with the sheet's background above and below it.
  pixels = numpy.asarray(Image.open(io.BytesIO(sheet[0])).convert("RGB"), dtype=numpy.int64)
  assert numpy.abs(pixels[8 + 10, 8 + 320] - [40, 40, 40]).max() <= 4
  assert numpy.abs(pixels[8 + 350, 8 + 320] - [40, 40, 40]).max() <= 4
  assert numpy.abs(pixels[8 + 180, 8 + 320] - [40, 40, 40]).max() > 20
  # Kept relative to the saved .blend, the art is found again where the work moved with it.
  listed = {camera["name"]: camera for camera in moved["cameras"]}
  assert listed["match"]["concept"]["path"] == str(workB / "ref" / "art.png")
  assert listed["match"]["sky"] is None and listed["nightMatch"]["sky"] == nightSky and listed["nightMatch"]["concept"]["path"] == str(workB / "ref" / "art.png")
  assert movedAgain[1]["concept"] == str(workB / "ref" / "art.png") and movedAgain[1]["masses"]["sameThird"] == 1.0
