import math

import numpy

from conftest import writePNG
from testModelsAndDressing import freshScene
from testSurfaceBrushes import borderLength, shaped, stripedGround, surface, twoMaterials

uncovered = -1
readCenters = """
sceneObject = bpy.data.objects['ground']
evaluated = sceneObject.evaluated_get(bpy.context.evaluated_depsgraph_get())
mesh = evaluated.to_mesh()
result = [list(polygon.center) for polygon in mesh.polygons]
evaluated.to_mesh_clear()
"""


def pieceCount(faces, chosen):
  """How many pieces the chosen faces make, joined across shared edges."""
  owner = {}
  for index in chosen:
    for first, second in zip(faces[index], faces[index][1:] + faces[index][:1]):
      owner.setdefault((min(first, second), max(first, second)), []).append(index)
  parent = {index: index for index in chosen}

  def root(index):
    while parent[index] != index:
      index = parent[index]
    return index

  for sharing in owner.values():
    for other in sharing[1:]:
      parent[root(other)] = root(sharing[0])
  return len({root(index) for index in chosen})


def planArea(vertices, face):
  points = [vertices[index] for index in face]
  return abs(sum(x0 * y1 - x1 * y0 for (x0, y0, _), (x1, y1, _) in zip(points, points[1:] + points[:1]))) / 2


def borderSegments(vertices, faces, values, first, second):
  """The edges between a face holding one value and a face holding the other, as [start, end] positions."""
  owners = {}
  for index, face in enumerate(faces):
    for a, b in zip(face, face[1:] + face[:1]):
      owners.setdefault((min(a, b), max(a, b)), []).append(index)
  return numpy.array([[vertices[a], vertices[b]] for (a, b), sharing in owners.items()
    if len(sharing) == 2 and {values[sharing[0]], values[sharing[1]]} == {first, second}])


def distancesTo(points, segments):
  points = numpy.asarray(points)
  starts, spans = segments[:, 0], segments[:, 1] - segments[:, 0]
  along = numpy.clip(((points[:, None] - starts[None]) * spans[None]).sum(2) / (spans * spans).sum(1)[None], 0, 1)
  return numpy.linalg.norm(starts[None] + along[:, :, None] * spans[None] - points[:, None], axis=2).min(axis=1)


def cornerUVs(surfaceResult, face):
  start = surfaceResult["loopStarts"][face]
  return surfaceResult["uvs"][start:start + len(surfaceResult["faces"][face])]


async def threeMaterials(session, folder):
  for name, color in (("grass", (60, 120, 50, 255)), ("dirt", (120, 90, 60, 255)), ("sand", (200, 180, 120, 255))):
    await session.expectSuccess("createMaterial", {"name": name, "diffuseTexture": str(writePNG(folder / f"{name}.png", 4, 4, color))})


def testConformSurfaceEdgesKeepsANarrowStrokeWhole(stageBlenderServer, tmp_path):
  async def steps(session):
    await freshScene(session)
    await twoMaterials(session, tmp_path)
    await stripedGround(session, "ground", 200, 8)
    await session.expectSuccess("addSurfaceLayer", {"objectName": "ground", "name": "path"})
    await session.expectSuccess("paintSurface", {"objectName": "ground", "layer": "path", "material": "stone", "selector": {"nearPath": {"path": [[-95, -20, 0], [-50, 30, 0], [0, 60, 0], [60, 95, 0]], "radius": 7}}})
    before = (await session.expectSuccess("runPython", shaped("ground")))["result"]
    beforeSurface = (await session.expectSuccess("runPython", surface("ground", "path")))["result"]
    conformed = await session.expectSuccess("conformSurfaceEdges", {"objectName": "ground", "layer": "path", "smoothing": 12})
    after = (await session.expectSuccess("runPython", shaped("ground")))["result"]
    afterSurface = (await session.expectSuccess("runPython", surface("ground", "path")))["result"]
    return before, beforeSurface, conformed, after, afterSurface

  before, beforeSurface, conformed, after, afterSurface = stageBlenderServer.session(steps)
  painted = [index for index, value in enumerate(beforeSurface["values"]) if value != uncovered]
  kept = [index for index, value in enumerate(afterSurface["values"]) if value != uncovered]
  # A 14-wide stroke evened over 12 units keeps its faces and its area and stays one piece, its saw teeth gone.
  assert conformed["movedVertices"] > 20 and len(kept) >= 0.9 * len(painted)
  assert pieceCount(after["faces"], kept) == 1 == pieceCount(before["faces"], painted)
  areaBefore = sum(planArea(before["vertices"], before["faces"][index]) for index in painted)
  areaAfter = sum(planArea(after["vertices"], after["faces"][index]) for index in kept)
  assert abs(areaAfter - areaBefore) < 0.1 * areaBefore
  assert borderLength(after["vertices"], after["faces"], afterSurface["values"]) < 0.95 * borderLength(before["vertices"], before["faces"], beforeSurface["values"])


def testConformSurfaceEdgesLeavesABorderItEvenedAsItIsUntilPaintBesideItChanges(stageBlenderServer, tmp_path):
  trail = {"nearPath": {"path": [[-100, -75, 0], [-70, -45, 0], [-78, -10, 0], [-48, 20, 0], [-8, 25, 0]], "radius": 6}}

  async def steps(session):
    async def conform(smoothing):
      return await session.expectSuccess("conformSurfaceEdges", {"objectName": "ground", "layer": "trail", "smoothing": smoothing})

    await freshScene(session)
    await twoMaterials(session, tmp_path)
    await stripedGround(session, "ground", 240, 8)
    await session.expectSuccess("addSurfaceLayer", {"objectName": "ground", "name": "trail"})
    await session.expectSuccess("paintSurface", {"objectName": "ground", "layer": "trail", "material": "stone", "selector": trail})
    states = {"painted": (await session.expectSuccess("runPython", surface("ground", "trail")))["result"], "first": await conform(16)}
    states["once"] = (await session.expectSuccess("runPython", shaped("ground")))["result"]
    states["onceSurface"] = (await session.expectSuccess("runPython", surface("ground", "trail")))["result"]
    states["again"] = [await conform(smoothing) for smoothing in (16, 12, 8)]
    states["agains"] = (await session.expectSuccess("runPython", shaped("ground")))["result"]
    await session.expectSuccess("paintSurface", {"objectName": "ground", "layer": "trail", "material": "stone", "selector": {"nearPath": {"path": [[40, -90, 0], [90, 60, 0]], "radius": 6}}})
    states["layer"] = await conform(16)
    states["withNew"] = (await session.expectSuccess("runPython", shaped("ground")))["result"]
    await session.expectSuccess("paintSurface", {"objectName": "ground", "layer": "trail", "material": "stone", "selector": {"sphere": {"center": [-8, 25, 0], "radius": 10}}})
    states["beside"] = await conform(16)
    states["besideShape"] = (await session.expectSuccess("runPython", shaped("ground")))["result"]
    states["wider"] = await conform(24)
    return states

  states = stageBlenderServer.session(steps)
  stone = 0
  once, onceSurface = states["once"], states["onceSurface"]
  trailBorder = borderSegments(once["vertices"], once["faces"], onceSurface["values"], stone, uncovered)
  # A 12-wide closed trail evened over 16 stays one piece; run again over 16, 12, or 8 it is left exactly as it is, every one of its
  # border edges evened before.
  assert states["first"]["movedVertices"] > 20 and pieceCount(once["faces"], [index for index, value in enumerate(onceSurface["values"]) if value == stone]) == 1
  for again in states["again"]:
    assert again["movedVertices"] == 0 and again["changedFaces"] == 0 and again["alreadyEvened"] == len(trailBorder) > 30
  assert states["agains"]["vertices"] == once["vertices"]
  # Conforming the whole layer for a new stroke evens only the new stroke: the trail does not move.
  near = [index for index, point in enumerate(once["vertices"]) if distancesTo([point], trailBorder)[0] < 12]
  assert states["layer"]["movedVertices"] > 10 and states["layer"]["alreadyEvened"] == len(trailBorder)
  assert all(states["withNew"]["vertices"][index] == once["vertices"][index] for index in near)
  # Paint beside the trail's end makes that stretch new to even; the rest stays where it was.
  moved = [index for index, (old, new) in enumerate(zip(states["withNew"]["vertices"], states["besideShape"]["vertices"])) if old != new]
  assert states["beside"]["movedVertices"] == len(moved) > 0
  assert all(math.dist(states["withNew"]["vertices"][index][:2], (-8, 25)) < 40 for index in moved)
  # A larger smoothing evens it all again.
  assert states["wider"]["alreadyEvened"] == 0 and states["wider"]["movedVertices"] > 0


def testConformSurfaceEdgesEvensAStrokeWhosePiecesTouchAtCornersAsOne(stageBlenderServer, tmp_path):
  async def steps(session):
    await freshScene(session)
    await threeMaterials(session, tmp_path)
    await stripedGround(session, "ground", 240, 8)
    for layer in ("thin", "cross"):
      await session.expectSuccess("addSurfaceLayer", {"objectName": "ground", "name": layer})
    await session.expectSuccess("paintSurface", {"objectName": "ground", "layer": "thin", "material": "sand", "selector": {"nearPath": {"path": [[15, -100, 0], [60, -75, 0], [105, -95, 0]], "radius": 5}}})
    await session.expectSuccess("paintSurface", {"objectName": "ground", "layer": "cross", "material": "dirt", "selector": {"nearPath": {"path": [[5, 25, 0], [115, 110, 0]], "radius": 10}}})
    await session.expectSuccess("paintSurface", {"objectName": "ground", "layer": "cross", "material": "sand", "selector": {"nearPath": {"path": [[10, 110, 0], [115, 20, 0]], "radius": 6}}})
    states = {"before": (await session.expectSuccess("runPython", shaped("ground")))["result"]}
    for layer in ("thin", "cross"):
      states[layer] = (await session.expectSuccess("runPython", surface("ground", layer)))["result"]
      await session.expectSuccess("conformSurfaceEdges", {"objectName": "ground", "layer": layer, "smoothing": 12})
      states[layer + "After"] = (await session.expectSuccess("runPython", surface("ground", layer)))["result"]
    states["after"] = (await session.expectSuccess("runPython", shaped("ground")))["result"]
    return states

  states = stageBlenderServer.session(steps)
  before, after = states["before"], states["after"]
  # The materials were made grass, dirt, sand; grass is never painted, so dirt and sand take the layers' first slots in paint order.
  sand, dirt = 0, 1

  def painted(surfaceResult, value):
    return [index for index, entry in enumerate(surfaceResult["values"]) if entry == value]

  # A 10-wide stroke painted as three pieces touching only at corners comes out one smooth stroke.
  assert pieceCount(before["faces"], painted(states["thin"], sand)) == 3 and pieceCount(after["faces"], painted(states["thinAfter"], sand)) == 1
  assert borderLength(after["vertices"], after["faces"], states["thinAfter"]["values"]) < 0.8 * borderLength(before["vertices"], before["faces"], states["thin"]["values"])
  # A sand trail painted across a dirt road in one layer: the trail stays one piece, the road it cuts in two stays two, and the borders
  # lose their stair-steps.
  assert pieceCount(after["faces"], painted(states["crossAfter"], sand)) == 1 == pieceCount(before["faces"], painted(states["cross"], sand))
  assert pieceCount(after["faces"], painted(states["crossAfter"], dirt)) == 2 == pieceCount(before["faces"], painted(states["cross"], dirt))
  assert borderLength(after["vertices"], after["faces"], states["crossAfter"]["values"]) < 0.85 * borderLength(before["vertices"], before["faces"], states["cross"]["values"])


def testConformSurfaceEdgesKeepsARoundAreaRound(stageBlenderServer, tmp_path):
  async def steps(session):
    await freshScene(session)
    await twoMaterials(session, tmp_path)
    await stripedGround(session, "ground", 200, 8)
    await session.expectSuccess("sculptAlongPath", {"objectName": "ground", "mode": "carve", "path": [[0, 0, -24]], "radius": 70, "strength": 1, "profile": [[0, 0], [0.5, 6], [1, 24]], "conformRim": False})
    await session.expectSuccess("addSurfaceLayer", {"objectName": "ground", "name": "floor"})
    await session.expectSuccess("paintSurface", {"objectName": "ground", "layer": "floor", "material": "stone", "selector": {"height": {"minimum": -100, "maximum": -14}}})
    before = (await session.expectSuccess("runPython", shaped("ground")))["result"]
    beforeSurface = (await session.expectSuccess("runPython", surface("ground", "floor")))["result"]
    await session.expectSuccess("conformSurfaceEdges", {"objectName": "ground", "layer": "floor", "smoothing": 16, "selector": {"cylinder": {"center": [0, 0], "radius": 80, "bottom": -100, "top": 100}}})
    after = (await session.expectSuccess("runPython", shaped("ground")))["result"]
    afterSurface = (await session.expectSuccess("runPython", surface("ground", "floor")))["result"]
    return before, beforeSurface, after, afterSurface

  before, beforeSurface, after, afterSurface = stageBlenderServer.session(steps)
  # The grid had no material; stone, the first painted, took its first slot.
  stone = 0

  def radii(shape, values):
    segments = borderSegments(shape["vertices"], shape["faces"], values, stone, uncovered)
    middles = segments.mean(axis=1)
    return numpy.linalg.norm(middles[:, :2], axis=1)

  def area(shape, values):
    return sum(planArea(shape["vertices"], face) for face, value in zip(shape["faces"], values) if value == stone)

  # The floor painted by height is a stair-stepped disc; evened on a grid whose diagonals turn, it comes out round, not a diamond,
  # and keeps its area.
  spread = radii(after, afterSurface["values"])
  assert (spread.max() - spread.min()) / spread.mean() < 0.15
  assert abs(area(after, afterSurface["values"]) - area(before, beforeSurface["values"])) < 0.05 * area(before, beforeSurface["values"])


async def oneCellWall(session, folder):
  """A straight wall 40 tall rising across the ground within one 8-unit cell, from y 0 to y 8: sand ground, stone wall."""
  await freshScene(session)
  await twoMaterials(session, folder)
  await stripedGround(session, "ground", 160, 8)
  await session.expectSuccess("sculptOutline", {"objectName": "ground", "mode": "fill", "outline": [[-300, 4], [300, 4], [300, 300], [-300, 300]], "base": 0, "profile": [[-4, 0], [0, 40], [40, 40]], "conformBreaks": False})
  await session.expectSuccess("projectUVs", {"objectName": "ground", "method": "box", "worldUnitsPerRepeat": 32})
  await session.expectSuccess("addSurfaceLayer", {"objectName": "ground", "name": "ground"})
  await session.expectSuccess("paintSurface", {"objectName": "ground", "layer": "ground", "material": "sand", "selector": {"all": True}})
  await session.expectSuccess("paintSurface", {"objectName": "ground", "layer": "ground", "material": "stone", "selector": {"slope": {"minimumDegrees": 40, "maximumDegrees": 180}}})
  await session.expectSuccess("addSurfaceLayer", {"objectName": "ground", "name": "blend"})


def testCutContoursCutsALevelThatCrossesAnEdgeTwice(stageBlenderServer, tmp_path):
  async def steps(session):
    await oneCellWall(session, tmp_path)
    cut = await session.expectSuccess("cutContours", {"objectName": "ground", "levels": [12], "distanceFrom": {"material": "sand"}, "selector": {"material": "stone"}})
    shape = (await session.expectSuccess("runPython", shaped("ground")))["result"]
    painted = (await session.expectSuccess("runPython", surface("ground", "ground")))["result"]
    return cut, shape, painted

  cut, shape, painted = stageBlenderServer.session(steps)
  sand, stone = 0, 1
  # Every edge up the wall runs from the foot to the lip, both on the sand's border, and the distance from it passes 12 twice
  # between them: each such edge is split between its crossings, and then every stone face lies wholly on one side of 12.
  segments = borderSegments(shape["vertices"], shape["faces"], painted["values"], sand, stone)
  distances = distancesTo(shape["vertices"], segments)
  stoneFaces = [face for face, value in zip(shape["faces"], painted["values"]) if value == stone]
  assert len(stoneFaces) > 40
  for face in stoneFaces:
    corners = [distances[index] for index in face]
    assert min(corners) >= 12 - 1e-3 or max(corners) <= 12 + 1e-3
  assert cut["doubleCrossings"] >= 19
  # Each of the wall's 20 columns has a vertex on 12 from the foot and one on 12 from the lip.
  heights = [z for (_, _, z), distance in zip(shape["vertices"], distances) if abs(distance - 12) < 1e-3]
  assert sum(1 for z in heights if z < 20) >= 20 and sum(1 for z in heights if z > 20) >= 20


def testPaintTransitionRunsFromAOneCellWallsFootOnly(stageBlenderServer, tmp_path):
  transition = {"objectName": "ground", "layer": "blend", "material": "sand", "selector": {"material": "stone"}, "toward": {"material": "sand"}, "width": 12, "worldUnitsPerRepeat": 32, "onlyAbove": True}

  async def steps(session):
    await oneCellWall(session, tmp_path)
    uncut = await session.expectSuccess("paintTransition", transition)
    uncutSurface = (await session.expectSuccess("runPython", surface("ground", "blend")))["result"]
    await session.expectSuccess("eraseSurface", {"objectName": "ground", "layer": "blend", "selector": {"all": True}})
    await session.expectSuccess("cutContours", {"objectName": "ground", "levels": [12], "distanceFrom": {"material": "sand"}, "selector": {"material": "stone"}, "onlyAbove": True})
    strip = await session.expectSuccess("paintTransition", transition)
    stripSurface = (await session.expectSuccess("runPython", surface("ground", "blend")))["result"]
    shape = (await session.expectSuccess("runPython", shaped("ground")))["result"]
    lipless = await session.expectError("cutContours", {"objectName": "ground", "levels": [12], "onlyAbove": True})
    return uncut, uncutSurface, strip, stripSurface, shape, lipless

  uncut, uncutSurface, strip, stripSurface, shape, lipless = stageBlenderServer.session(steps)
  # The wall's lip is a stone-to-sand border too, but the strip measures only from the foot, where stone rises from sand. Uncut, every
  # wall face reaches from foot to lip, so none is painted and the faces the strip wants are counted.
  assert uncut["painted"] == 0 and uncut["straddlingFaces"] >= 40 and all(value == uncovered for value in uncutSurface["values"])
  # Cut 12 from the foot, the strip is the band up to the cut: its faces rise no more than 12 along the wall (11.77 up, the wall
  # leaning 8 in 40), none reaches the lip, and every face's mapping runs from v 0 at the foot, none smeared flat at v 0.
  assert strip["painted"] >= 40 and strip["straddlingFaces"] == 0
  stripFaces = [index for index, value in enumerate(stripSurface["values"]) if value != uncovered]
  for face in stripFaces:
    heights = [shape["vertices"][vertex][2] for vertex in shape["faces"][face]]
    vValues = [v for _, v in cornerUVs(stripSurface, face)]
    assert max(heights) <= 12 / math.hypot(1, 8 / 40) + 1e-3 and min(heights) >= -1e-3
    assert max(vValues) > 1e-3 and min(vValues) >= -1e-6 and max(vValues) <= 1 + 1e-6
  assert "it needs distanceFrom" in lipless


def testTransitionStraddlingCountsOnlyFacesTheStripWantsAndItsMappingIsItsOwn(stageBlenderServer, tmp_path):
  transition = {"objectName": "cliff", "layer": "blend", "material": "sand", "selector": {"material": "stone"}, "toward": {"material": "sand"}, "width": 12, "worldUnitsPerRepeat": 32}

  async def steps(session):
    await freshScene(session)
    await twoMaterials(session, tmp_path)
    await stripedGround(session, "cliff", 160, 4)
    await session.expectSuccess("addShapingPass", {"objectName": "cliff", "name": "wall"})
    await session.expectSuccess("sculptOutline", {"objectName": "cliff", "mode": "fill", "outline": [[-80, 4], [80, 4], [80, 80], [-80, 80]], "base": 0, "profile": [[-24, 0], [0, 60], [40, 60]], "conformBreaks": False})
    await session.expectSuccess("projectUVs", {"objectName": "cliff", "method": "box", "worldUnitsPerRepeat": 32})
    await session.expectSuccess("addSurfaceLayer", {"objectName": "cliff", "name": "ground"})
    await session.expectSuccess("paintSurface", {"objectName": "cliff", "layer": "ground", "material": "sand", "selector": {"all": True}})
    await session.expectSuccess("paintSurface", {"objectName": "cliff", "layer": "ground", "material": "stone", "selector": {"slope": {"minimumDegrees": 40, "maximumDegrees": 180}}})
    await session.expectSuccess("addSurfaceLayer", {"objectName": "cliff", "name": "blend"})
    await session.expectSuccess("cutContours", {"objectName": "cliff", "levels": [12], "distanceFrom": {"material": "sand"}, "selector": {"material": "stone"}})
    states = {"plain": (await session.expectSuccess("runPython", surface("cliff", "blend")))["result"]}
    states["foot"] = await session.expectSuccess("paintTransition", transition | {"onlyAbove": True})
    states["painted"] = (await session.expectSuccess("runPython", surface("cliff", "blend")))["result"]
    await session.expectSuccess("eraseSurface", {"objectName": "cliff", "layer": "blend", "selector": {"all": True}})
    states["erased"] = (await session.expectSuccess("runPython", surface("cliff", "blend")))["result"]
    states["both"] = await session.expectSuccess("paintTransition", transition)
    states["bothPainted"] = (await session.expectSuccess("runPython", surface("cliff", "blend")))["result"]
    await session.expectSuccess("setSurfaceLayer", {"objectName": "cliff", "name": "blend", "muted": True})
    states["muted"] = (await session.expectSuccess("runPython", surface("cliff", "blend")))["result"]
    await session.expectSuccess("setSurfaceLayer", {"objectName": "cliff", "name": "blend", "muted": False})
    states["unmuted"] = (await session.expectSuccess("runPython", surface("cliff", "blend")))["result"]
    await session.expectSuccess("projectUVs", {"objectName": "cliff", "method": "planar", "direction": [0, 0, 1], "worldUnitsPerRepeat": 16})
    states["projected"] = (await session.expectSuccess("runPython", surface("cliff", "blend")))["result"]
    await session.expectSuccess("removeSurfaceLayer", {"objectName": "cliff", "name": "blend"})
    states["removed"] = (await session.expectSuccess("runPython", surface("cliff", "ground")))["result"]
    states["shape"] = (await session.expectSuccess("runPython", shaped("cliff")))["result"]
    return states

  states = stageBlenderServer.session(steps)
  # After the cut, the strip at the foot and the strips at foot and lip both end on it: no face is left straddling either way.
  assert states["foot"]["straddlingFaces"] == 0 and states["both"]["straddlingFaces"] == 0 and states["both"]["painted"] == 2 * states["foot"]["painted"] > 0
  plain = states["plain"]["uvs"]
  stripLoops = {start + corner for face, value in enumerate(states["painted"]["values"]) if value != uncovered
    for start in [states["painted"]["loopStarts"][face]] for corner in range(len(states["painted"]["faces"][face]))}
  assert any(states["painted"]["uvs"][loop] != plain[loop] for loop in stripLoops)
  # Erased or muted, the transition leaves the box projection beneath exactly as it was; unmuted, its own mapping is back.
  assert states["erased"]["uvs"] == plain and states["muted"]["uvs"] == plain and states["unmuted"]["uvs"] == states["bothPainted"]["uvs"]
  # A new projection goes beneath the transition: its faces keep their mapping, every other corner takes the projection (x and y over 16).
  bothLoops = {start + corner for face, value in enumerate(states["bothPainted"]["values"]) if value != uncovered
    for start in [states["bothPainted"]["loopStarts"][face]] for corner in range(len(states["bothPainted"]["faces"][face]))}
  vertices = states["shape"]["vertices"]
  for loop, (u, v) in enumerate(states["projected"]["uvs"]):
    x, y, _ = vertices[states["projected"]["loopVertices"][loop]]
    if loop in bothLoops:
      assert (u, v) == tuple(states["bothPainted"]["uvs"][loop])
    else:
      assert abs(u - x / 16) < 1e-4 and abs(v - y / 16) < 1e-4
  # Removed, it leaves the projection everywhere.
  for loop, (u, v) in enumerate(states["removed"]["uvs"]):
    x, y, _ = vertices[states["removed"]["loopVertices"][loop]]
    assert abs(u - x / 16) < 1e-4 and abs(v - y / 16) < 1e-4


def testPaintTransitionRunsOverNotchesInAWallsFootWithoutSmearing(stageBlenderServer, tmp_path):
  async def steps(session):
    await freshScene(session)
    await twoMaterials(session, tmp_path)
    await stripedGround(session, "ground", 240, 8)
    await session.expectSuccess("addShapingPass", {"objectName": "ground", "name": "mesa"})
    await session.expectSuccess("sculptOutline", {"objectName": "ground", "mode": "fill", "outline": [[-70, -30], [-20, -62], [50, -42], [62, 28], [0, 70], [-62, 42]], "base": 0, "profile": [[-6, 0], [0, 36], [40, 36]]})
    await session.expectSuccess("projectUVs", {"objectName": "ground", "method": "box", "worldUnitsPerRepeat": 64})
    await session.expectSuccess("addSurfaceLayer", {"objectName": "ground", "name": "ground"})
    await session.expectSuccess("paintSurface", {"objectName": "ground", "layer": "ground", "material": "sand", "selector": {"all": True}})
    await session.expectSuccess("paintSurface", {"objectName": "ground", "layer": "ground", "material": "stone", "selector": {"slope": {"minimumDegrees": 40, "maximumDegrees": 180}}})
    await session.expectSuccess("addSurfaceLayer", {"objectName": "ground", "name": "blend"})
    await session.expectSuccess("cutContours", {"objectName": "ground", "levels": [14], "distanceFrom": {"material": "sand"}, "selector": {"material": "stone"}, "onlyAbove": True})
    ground = (await session.expectSuccess("runPython", surface("ground", "ground")))["result"]
    strip = await session.expectSuccess("paintTransition", {"objectName": "ground", "layer": "blend", "material": "sand", "selector": {"material": "stone"}, "toward": {"material": "sand"}, "width": 14, "worldUnitsPerRepeat": 32, "onlyAbove": True})
    stripSurface = (await session.expectSuccess("runPython", surface("ground", "blend")))["result"]
    shape = (await session.expectSuccess("runPython", shaped("ground")))["result"]
    return ground, strip, stripSurface, shape

  ground, strip, stripSurface, shape = stageBlenderServer.session(steps)
  sand, stone = 0, 1
  vertices, faces = shape["vertices"], shape["faces"]

  def middle(face):
    return numpy.mean([vertices[index] for index in face], axis=0)

  # The foot has notches: sand faces poking up the wall, standing higher than the stone beside them.
  owners = {}
  for index, face in enumerate(faces):
    for a, b in zip(face, face[1:] + face[:1]):
      owners.setdefault((min(a, b), max(a, b)), []).append(index)
  notchSides = 0
  for sharing in owners.values():
    if len(sharing) == 2 and {ground["values"][face] for face in sharing} == {sand, stone}:
      sandFace, stoneFace = sorted(sharing, key=lambda face: ground["values"][face] != sand)
      notchSides += middle(faces[sandFace])[2] > middle(faces[stoneFace])[2] + 1
  assert notchSides >= 4
  # The foot runs on whole over them, so every face the strip wants is painted; and no face's mapping collapses along the wall (every
  # face spans along the texture about as far as it spans along the wall), which is what smeared one column of it over a notch.
  assert strip["straddlingFaces"] == 0 and strip["painted"] > 150
  for face, value in enumerate(stripSurface["values"]):
    if value == uncovered:
      continue
    corners = numpy.array([vertices[index] for index in faces[face]])
    normal = numpy.cross(corners[1] - corners[0], corners[2] - corners[0])
    across = numpy.cross(normal, [0, 0, 1])
    extent = numpy.ptp(corners @ (across / numpy.linalg.norm(across)))
    mapping = cornerUVs(stripSurface, face)
    assert numpy.ptp([u for u, _ in mapping]) * 32 > 0.3 * extent
    assert all(-1e-6 <= v <= 1 + 1e-6 for _, v in mapping)


def testCleanLiftsOrFillsWithTheLayersOwnPaintOnly(stageBlenderServer, tmp_path):
  async def steps(session):
    await freshScene(session)
    await threeMaterials(session, tmp_path)
    await stripedGround(session, "ground", 160, 8)
    await session.expectSuccess("assignMaterial", {"objectName": "ground", "materialName": "grass"})
    await session.expectSuccess("addSurfaceLayer", {"objectName": "ground", "name": "ground"})
    await session.expectSuccess("addSurfaceLayer", {"objectName": "ground", "name": "path"})
    await session.expectSuccess("paintSurface", {"objectName": "ground", "layer": "ground", "material": "dirt", "selector": {"sphere": {"center": [-45, 30, 0], "radius": 7}}})
    await session.expectSuccess("paintSurface", {"objectName": "ground", "layer": "path", "material": "sand", "selector": {"nearPath": {"path": [[-80, -5, 0], [80, 5, 0]], "radius": 12}}})
    await session.expectSuccess("paintSurface", {"objectName": "ground", "layer": "path", "material": "sand", "selector": {"sphere": {"center": [40, -33, 0], "radius": 6}}})
    await session.expectSuccess("paintSurface", {"objectName": "ground", "layer": "path", "material": "sand", "selector": {"sphere": {"center": [-45, 30, 0], "radius": 4}}})
    await session.expectSuccess("eraseSurface", {"objectName": "ground", "layer": "path", "selector": {"sphere": {"center": [0, 2, 0], "radius": 5}}})
    states = {"before": (await session.expectSuccess("runPython", surface("ground", "path")))["result"]}
    states["cleaned"] = await session.expectSuccess("editSurface", {"objectName": "ground", "layer": "path", "operation": "clean", "minimumArea": 300})
    states["after"] = (await session.expectSuccess("runPython", surface("ground", "path")))["result"]
    await session.expectSuccess("paintSurface", {"objectName": "ground", "layer": "ground", "material": "dirt", "selector": {"sphere": {"center": [40, -33, 0], "radius": 14}}})
    states["repainted"] = (await session.expectSuccess("runPython", surface("ground", "path")))["result"]
    await session.expectSuccess("editSurface", {"objectName": "ground", "layer": "path", "operation": "grow"})
    states["grown"] = (await session.expectSuccess("runPython", surface("ground", "path")))["result"]
    states["centers"] = (await session.expectSuccess("runPython", {"code": readCenters}))["result"]
    return states

  states = stageBlenderServer.session(steps)
  grass, dirt, sand = 0, 1, 2
  centers = states["centers"]
  before, after = states["before"], states["after"]
  island = [index for index, (x, y, _) in enumerate(centers) if math.dist((x, y), (-45, 30)) <= 4]
  speck = [index for index, (x, y, _) in enumerate(centers) if math.dist((x, y), (40, -33)) <= 6]
  hole = [index for index, (x, y, _) in enumerate(centers) if math.dist((x, y), (0, 2)) <= 5]
  # The path layer keeps only its own sand: its specks over the dirt island and on the grass are lifted off, its hole filled with sand.
  assert set(after["values"]) == {uncovered, sand} and states["cleaned"]["changed"] > 0
  assert all(after["values"][index] == uncovered and after["shown"][index] == dirt for index in island)
  assert all(after["values"][index] == uncovered and after["shown"][index] == grass for index in speck)
  assert all(before["values"][index] == uncovered and after["values"][index] == sand for index in hole)
  # The dirt island, a lower layer's speck amid grass beneath, is left for cleaning that layer.
  assert all(before["shown"][index] == sand and after["shown"][index] == dirt for index in island)
  # Dirt painted beneath where the speck was shows there.
  assert all(states["repainted"]["shown"][index] == dirt for index in speck)
  # Growing the path spreads only its sand: every face that shows something new shows sand the path layer now covers.
  changed = [index for index, (old, new) in enumerate(zip(states["repainted"]["shown"], states["grown"]["shown"])) if old != new]
  assert changed and all(states["grown"]["shown"][index] == sand and states["grown"]["values"][index] == sand for index in changed)


def testEdgeNoiseKeepsStrokesWholeAndEdgesWithinItsAmplitude(stageBlenderServer, tmp_path):
  stroke = {"nearPath": {"path": [[-95, -60, 0], [-40, -20, 0], [0, 30, 0], [50, 50, 0], [95, 90, 0]], "radius": 7}}

  async def steps(session):
    await freshScene(session)
    await twoMaterials(session, tmp_path)
    await stripedGround(session, "ground", 200, 8)
    await session.expectSuccess("addSurfaceLayer", {"objectName": "ground", "name": "paint"})
    strokes = []
    for amplitude, seed in [(3.5, seed) for seed in range(1, 7)] + [(4, 2)]:
      await session.expectSuccess("paintSurface", {"objectName": "ground", "layer": "paint", "material": "stone", "selector": stroke, "edgeNoise": {"featureSize": 24, "amplitude": amplitude, "seed": seed}})
      strokes.append((await session.expectSuccess("runPython", surface("ground", "paint")))["result"])
      await session.expectSuccess("eraseSurface", {"objectName": "ground", "layer": "paint", "selector": {"all": True}})
    for seed, y in ((1, 20), (2, 50), (3, 80)):
      narrow = {"nearPath": {"path": [[-95, y, 0], [-30, y + 18, 0], [30, y - 10, 0], [95, y + 12, 0]], "radius": 6}}
      await session.expectSuccess("paintSurface", {"objectName": "ground", "layer": "paint", "material": "stone", "selector": narrow, "edgeNoise": {"featureSize": 14, "amplitude": 3, "seed": seed}})
      strokes.append((await session.expectSuccess("runPython", surface("ground", "paint")))["result"])
      await session.expectSuccess("eraseSurface", {"objectName": "ground", "layer": "paint", "selector": {"all": True}})
    await session.expectSuccess("paintSurface", {"objectName": "ground", "layer": "paint", "material": "stone", "selector": {"box": {"minimum": [-200, -200, -1], "maximum": [0, 200, 1]}}, "edgeNoise": {"featureSize": 30, "amplitude": 12, "seed": 1}})
    half = (await session.expectSuccess("runPython", surface("ground", "paint")))["result"]
    centers = (await session.expectSuccess("runPython", {"code": readCenters}))["result"]
    return strokes, half, centers

  strokes, half, centers = stageBlenderServer.session(steps)
  stone = 0
  # A stroke under noise stays in one piece whatever the seed: 14 wide under noise half its radius, the stroke the sweep saw break
  # into four (amplitude 4, seed 2), and 12-wide strokes under noise as fine as the stroke is wide, where it pinched them to a corner.
  for painted in strokes:
    assert pieceCount(painted["faces"], [index for index, value in enumerate(painted["values"]) if value == stone]) == 1
  # The half plane's edge at x = 0 wanders both ways, and no face farther than the amplitude from it changes.
  moved = [x for (x, _, _), value in zip(centers, half["values"]) if (value == stone) != (x < 0)]
  assert any(x > 0 for x in moved) and any(x < 0 for x in moved)
  assert max(abs(x) for x in moved) <= 12 and max(abs(x) for x in moved) > 6


def testSmoothRoundsRaggedEdgesAndKeepsStraightOnes(stageBlenderServer, tmp_path):
  async def steps(session):
    await freshScene(session)
    await twoMaterials(session, tmp_path)
    await stripedGround(session, "ground", 200, 8)
    await session.expectSuccess("addSurfaceLayer", {"objectName": "ground", "name": "blotches"})
    await session.expectSuccess("addSurfaceLayer", {"objectName": "ground", "name": "disc"})
    await session.expectSuccess("addSurfaceLayer", {"objectName": "ground", "name": "half"})
    await session.expectSuccess("paintSurface", {"objectName": "ground", "layer": "blotches", "material": "stone", "selector": {"noise": {"featureSize": 24, "share": 0.3, "seed": 11}}})
    await session.expectSuccess("paintSurface", {"objectName": "ground", "layer": "disc", "material": "stone", "selector": {"sphere": {"center": [0, 0, 0], "radius": 48}}, "edgeNoise": {"featureSize": 16, "amplitude": 8, "seed": 3}})
    await session.expectSuccess("paintSurface", {"objectName": "ground", "layer": "half", "material": "sand", "selector": {"box": {"minimum": [-200, -200, -1], "maximum": [0, 200, 1]}}})
    shape = (await session.expectSuccess("runPython", shaped("ground")))["result"]
    states = {}
    for layer in ("blotches", "disc"):
      states[layer] = (await session.expectSuccess("runPython", surface("ground", layer)))["result"]
      states[layer + "Smoothed"] = await session.expectSuccess("editSurface", {"objectName": "ground", "layer": layer, "operation": "smooth", "steps": 2})
      states[layer + "After"] = (await session.expectSuccess("runPython", surface("ground", layer)))["result"]
    states["straight"] = await session.expectSuccess("editSurface", {"objectName": "ground", "layer": "half", "operation": "smooth", "steps": 2})
    return shape, states

  shape, states = stageBlenderServer.session(steps)
  vertices, faces = shape["vertices"], shape["faces"]

  def covered(surfaceResult):
    return [index for index, value in enumerate(surfaceResult["values"]) if value != uncovered]

  # Noise blotches lose their ragged teeth, notches, and one-face specks: fewer pieces and much less border.
  assert states["blotchesSmoothed"]["changed"] > 20
  assert pieceCount(faces, covered(states["blotchesAfter"])) < pieceCount(faces, covered(states["blotches"]))
  assert borderLength(vertices, faces, states["blotchesAfter"]["values"]) < 0.8 * borderLength(vertices, faces, states["blotches"]["values"])
  # A large disc with a ragged edge comes out rounder with its paint kept: rounding takes only the teeth and fills only the notches.
  assert borderLength(vertices, faces, states["discAfter"]["values"]) < 0.9 * borderLength(vertices, faces, states["disc"]["values"])
  assert abs(len(covered(states["discAfter"])) - len(covered(states["disc"]))) < 0.1 * len(covered(states["disc"]))
  # A straight edge along the grid is already as even as the faces allow, and stays.
  assert states["straight"]["changed"] == 0


def testSurfaceLayersMoveAndRemoveExactly(stageBlenderServer, tmp_path):
  async def steps(session):
    await freshScene(session)
    await twoMaterials(session, tmp_path)
    await stripedGround(session, "ground", 64, 8)
    for name in ("floor", "middle", "top"):
      await session.expectSuccess("addSurfaceLayer", {"objectName": "ground", "name": name})
    await session.expectSuccess("paintSurface", {"objectName": "ground", "layer": "floor", "material": "sand", "selector": {"all": True}})
    await session.expectSuccess("paintSurface", {"objectName": "ground", "layer": "top", "material": "sand", "selector": {"box": {"minimum": [-40, -40, -1], "maximum": [40, 0, 1]}}})
    states = {"beforeMiddle": (await session.expectSuccess("runPython", surface("ground", "floor")))["result"]}
    await session.expectSuccess("paintSurface", {"objectName": "ground", "layer": "middle", "material": "stone", "selector": {"box": {"minimum": [-40, -40, -1], "maximum": [0, 40, 1]}}})
    states["painted"] = (await session.expectSuccess("runPython", surface("ground", "floor")))["result"]
    await session.expectSuccess("setSurfaceLayer", {"objectName": "ground", "name": "top", "position": 0})
    states["topDown"] = (await session.expectSuccess("runPython", surface("ground", "floor")))["result"]
    await session.expectSuccess("setSurfaceLayer", {"objectName": "ground", "name": "top", "position": 2})
    states["topBack"] = (await session.expectSuccess("runPython", surface("ground", "floor")))["result"]
    states["outOfRange"] = await session.expectError("setSurfaceLayer", {"objectName": "ground", "name": "top", "position": 3})
    await session.expectSuccess("removeSurfaceLayer", {"objectName": "ground", "name": "middle"})
    states["removed"] = (await session.expectSuccess("runPython", surface("ground", "floor")))["result"]
    states["centers"] = (await session.expectSuccess("runPython", {"code": readCenters}))["result"]
    return states

  states = stageBlenderServer.session(steps)
  sand, stone = 0, 1
  overlap = [index for index, (x, y, _) in enumerate(states["centers"]) if x < 0 and y < 0]
  assert overlap and all(states["painted"]["shown"][index] == sand for index in overlap)
  # The top layer moved under the others lets the middle's stone show where they overlap; moved back, every face is as it was.
  assert all(states["topDown"]["shown"][index] == stone for index in overlap)
  assert states["topBack"]["shown"] == states["painted"]["shown"] and "position is 0 (bottom) to 2 (top), got 3" in states["outOfRange"]
  # Removing the middle layer shows exactly what showed before it was painted.
  assert states["removed"]["shown"] == states["beforeMiddle"]["shown"] and states["removed"]["shown"] != states["painted"]["shown"]


def testPaintSurfaceMasksStayInsideTheRegion(stageBlenderServer, tmp_path):
  async def steps(session):
    await freshScene(session)
    await twoMaterials(session, tmp_path)
    await stripedGround(session, "ground", 160, 8)
    await session.expectSuccess("sculptOutline", {"objectName": "ground", "mode": "fill", "outline": [[-60, -40], [60, -40], [60, 40], [-60, 40]], "base": 0, "profile": [[-8, 0], [0, 30], [40, 30]], "conformBreaks": False})
    await session.expectSuccess("createRegion", {"name": "west", "outline": [[-84, -84], [-4, -84], [-4, 84], [-84, 84]], "bottom": -100, "top": 100, "intent": "the mesa's west half", "access": "play"})
    await session.expectSuccess("addSurfaceLayer", {"objectName": "ground", "name": "rock"})
    await session.expectSuccess("addSurfaceLayer", {"objectName": "ground", "name": "band"})
    await session.expectSuccess("paintSurface", {"objectName": "ground", "layer": "rock", "material": "stone", "selector": {"and": [{"region": "west"}, {"slope": {"minimumDegrees": 40, "maximumDegrees": 180}}]}})
    await session.expectSuccess("paintSurface", {"objectName": "ground", "layer": "band", "material": "sand", "selector": {"and": [{"region": "west"}, {"height": {"minimum": 12, "maximum": 25}}]}})
    rock = (await session.expectSuccess("runPython", surface("ground", "rock")))["result"]
    band = (await session.expectSuccess("runPython", surface("ground", "band")))["result"]
    shape = (await session.expectSuccess("runPython", shaped("ground")))["result"]
    return rock, band, shape

  rock, band, shape = stageBlenderServer.session(steps)
  vertices = shape["vertices"]

  def center(face):
    return [sum(vertices[index][axis] for index in face) / len(face) for axis in range(3)]

  def slope(face):
    a, b, c = (numpy.array(vertices[index]) for index in face[:3])
    normal = numpy.cross(b - a, c - a)
    return math.degrees(math.acos(abs(normal[2]) / numpy.linalg.norm(normal)))

  # Rock goes on the mesa's steep walls inside the west region only, and the band on faces whose middles lie 12 to 25 up there.
  for face, value in zip(shape["faces"], rock["values"]):
    x, _, _ = center(face)
    assert (value != uncovered) == (x < -4 and slope(face) >= 40)
  for face, value in zip(shape["faces"], band["values"]):
    x, _, z = center(face)
    assert (value != uncovered) == (x < -4 and 12 <= z <= 25)
  assert any(value != uncovered for value in rock["values"]) and any(value != uncovered for value in band["values"])


def testRegionEditsAreValidated(stageBlenderServer):
  async def steps(session):
    await freshScene(session)
    await session.expectSuccess("createRegion", {"name": "camp", "outline": [[0, 0], [10, 0], [10, 10]], "bottom": -10, "top": 10, "intent": "a camp", "access": "play"})
    return [
      await session.expectError("editRegion", {"name": "camp"}),
      await session.expectError("editRegion", {"name": "camp", "bottom": 10}),
      await session.expectError("createRegion", {"name": "camp", "outline": [[0, 0], [10, 0], [10, 10]], "bottom": -10, "top": 10, "intent": "again", "access": "play"}),
      await session.expectError("createRegion", {"name": "field", "outline": [[0, 0], [10, 0], [10, 10]], "bottom": -10, "top": 10, "intent": "  ", "access": "play"}),
    ]

  empty, inverted, duplicate, blank = stageBlenderServer.session(steps)
  assert "editRegion needs an outline, bottom, top, intent, or access" in empty
  assert "A region's bottom must lie below its top, got 10.0 and 10.0" in inverted
  assert "camp" in duplicate and "A region needs its intent" in blank


def testCutContoursByHeightCarriesTheProjection(stageBlenderServer, tmp_path):
  readMapping = """
import numpy
mesh = bpy.data.objects['ground'].data
uvs = numpy.empty(len(mesh.loops) * 2)
mesh.uv_layers['UVMap'].data.foreach_get('uv', uvs)
loopVertices = numpy.empty(len(mesh.loops), dtype=numpy.int64)
mesh.loops.foreach_get('vertex_index', loopVertices)
result = {'uvs': uvs.reshape(-1, 2).tolist(), 'loopVertices': loopVertices.tolist()}
"""

  async def steps(session):
    await freshScene(session)
    await stripedGround(session, "ground", 128, 16)
    await session.expectSuccess("moveVertices", {"objectName": "ground", "selector": {"box": {"minimum": [0, -100, -1], "maximum": [100, 100, 1]}}, "offset": [0, 0, 40]})
    await session.expectSuccess("projectUVs", {"objectName": "ground", "method": "planar", "direction": [0, 0, 1], "worldUnitsPerRepeat": 32})
    before = (await session.expectSuccess("runPython", shaped("ground")))["result"]
    cut = await session.expectSuccess("cutContours", {"objectName": "ground", "levels": [10, 30]})
    shape = (await session.expectSuccess("runPython", shaped("ground")))["result"]
    mapping = (await session.expectSuccess("runPython", {"code": readMapping}))["result"]
    return before, cut, shape, mapping

  before, cut, shape, mapping = stageBlenderServer.session(steps)
  # The ramp's new vertices on 10 and 30 carry the planar projection with them: every corner, old and new, maps x and y over 32.
  assert cut["splitEdges"] > 0 and len(shape["vertices"]) > len(before["vertices"])
  for (u, v), vertex in zip(mapping["uvs"], mapping["loopVertices"]):
    x, y, _ = shape["vertices"][vertex]
    assert abs(u - x / 32) < 1e-4 and abs(v - y / 32) < 1e-4


def testRebuildRegionLaysVerticesOutAgainAndSpansAPlaneEvenly(stageBlenderServer, tmp_path):
  tilt = """
mesh = bpy.data.objects['ground'].data
for vertex in mesh.vertices:
  vertex.co.z = 0.5 * vertex.co.x
mesh.update()
"""

  async def steps(session):
    await freshScene(session)
    await stripedGround(session, "ground", 160, 8)
    await session.expectSuccess("runPython", {"code": tilt})
    base = (await session.expectSuccess("runPython", shaped("ground")))["result"]
    await session.expectSuccess("createRegion", {"name": "mound", "outline": [[-36, -36], [36, -36], [36, 36], [-36, 36]], "bottom": -500, "top": 500, "intent": "a mound to take back", "access": "play"})
    await session.expectSuccess("addShapingPass", {"objectName": "ground", "name": "mound"})
    await session.expectSuccess("sculptAtPoint", {"objectName": "ground", "mode": "raise", "center": [0, 0, 0], "radius": 36, "strength": 25, "direction": [0, 0, 1]})
    turned = await session.expectSuccess("followContours", {"objectName": "ground", "selector": {"region": "mound"}})
    await session.expectSuccess("addShapingPass", {"objectName": "ground", "name": "rough"})
    await session.expectSuccess("warp", {"objectName": "ground", "featureSize": 12, "amplitude": 3, "seed": 3, "plane": "horizontal", "selector": {"box": {"minimum": [-25, -25, -500], "maximum": [25, 25, 500]}}})
    rebuilt = await session.expectSuccess("rebuildRegion", {"objectName": "ground", "selector": {"region": "mound"}, "mode": "surroundings"})
    after = (await session.expectSuccess("runPython", shaped("ground")))["result"]
    return base, turned, rebuilt, after

  base, turned, rebuilt, after = stageBlenderServer.session(steps)
  inside = [index for index, (x, y, _) in enumerate(base["vertices"]) if abs(x) < 36 and abs(y) < 36]
  # The warp moved the mound's vertices sideways and followContours turned its diagonals around the mound; the rebuild lays the
  # vertices out where the base has them and spans the tilted plane across, evenly whatever way the diagonals run.
  assert turned["turnedDiagonals"] > 0 and rebuilt["affectedVertices"] == len(inside) == 81
  for index in inside:
    x, y, z = after["vertices"][index]
    assert abs(x - base["vertices"][index][0]) < 1e-4 and abs(y - base["vertices"][index][1]) < 1e-4
    assert abs(z - 0.5 * x) < 1e-3


def testRebuildRegionSplitsItsQuadsAndTurnsTheirDiagonalsToTheNewGround(stageBlenderServer):
  async def steps(session):
    await freshScene(session)
    await session.expectSuccess("createTerrainGrid", {"name": "ground", "size": [160, 160], "spacing": 8, "location": [0, 0, 0], "collection": "terrain"})
    await session.expectSuccess("createRegion", {"name": "terrace", "outline": [[0, -40], [40, 0], [0, 40], [-40, 0]], "bottom": -100, "top": 100, "intent": "a diamond terrace", "access": "play"})
    rebuilt = await session.expectSuccess("rebuildRegion", {"objectName": "ground", "selector": {"region": "terrace"}, "mode": "height", "height": 12, "fadeDistance": 16})
    return rebuilt, (await session.expectSuccess("runPython", shaped("ground")))["result"]

  rebuilt, shape = stageBlenderServer.session(steps)
  vertices, faces = shape["vertices"], shape["faces"]

  def inside(index):
    x, y, _ = vertices[index]
    return abs(x) + abs(y) < 40

  # The grid's quads in the terrace become triangles, and diagonals turn to run along its sloping edges; the quads far outside stay.
  assert all(len(face) == 3 for face in faces if all(inside(index) for index in face))
  assert all(len(face) == 4 for face in faces if all(abs(vertices[index][0]) + abs(vertices[index][1]) > 60 for index in face))
  assert rebuilt["turnedDiagonals"] > 0


def testClearRegionTakesBackSpilledPaintWithTheSameEdgeNoiseAndResetNamesPassesStillShapingIt(stageBlenderServer, tmp_path):
  noise = {"featureSize": 20, "amplitude": 8, "seed": 4}

  async def steps(session):
    await freshScene(session)
    await twoMaterials(session, tmp_path)
    await stripedGround(session, "ground", 160, 8)
    await session.expectSuccess("createRegion", {"name": "camp", "outline": [[-30, -30], [30, -30], [30, 30], [-30, 30]], "bottom": -100, "top": 200, "intent": "a camp clearing", "access": "play"})
    await session.expectSuccess("addShapingPass", {"objectName": "ground", "name": "hill"})
    await session.expectSuccess("sculptAtPoint", {"objectName": "ground", "mode": "raise", "center": [0, 0, 0], "radius": 70, "strength": 20, "direction": [0, 0, 1]})
    await session.expectSuccess("addShapingPass", {"objectName": "ground", "name": "terrace"})
    await session.expectSuccess("rebuildRegion", {"objectName": "ground", "selector": {"region": "camp"}, "mode": "height", "height": 25})
    await session.expectSuccess("addSurfaceLayer", {"objectName": "ground", "name": "ground"})
    await session.expectSuccess("paintSurface", {"objectName": "ground", "layer": "ground", "material": "stone", "selector": {"region": "camp"}, "edgeNoise": noise})
    plain = await session.expectSuccess("clearRegion", {"region": "camp", "terrainObject": "ground", "surfacing": True})
    spilled = (await session.expectSuccess("runPython", surface("ground", "ground")))["result"]
    await session.expectSuccess("paintSurface", {"objectName": "ground", "layer": "ground", "material": "stone", "selector": {"region": "camp"}, "edgeNoise": noise})
    cleared = await session.expectSuccess("clearRegion", {"region": "camp", "terrainObject": "ground", "surfacing": True, "shaping": "reset", "edgeNoise": noise})
    after = (await session.expectSuccess("runPython", surface("ground", "ground")))["result"]
    centers = (await session.expectSuccess("runPython", {"code": readCenters}))["result"]
    noSurfacing = await session.expectError("clearRegion", {"region": "camp", "terrainObject": "ground", "edgeNoise": noise})
    return plain, spilled, cleared, after, centers, noSurfacing

  plain, spilled, cleared, after, centers, noSurfacing = stageBlenderServer.session(steps)
  stone = 0
  # Without edgeNoise only the region's faces are erased and the paint the noise spilled past its edge stays; with the edgeNoise it
  # was painted with, all of it goes.
  left = [index for index, value in enumerate(spilled["values"]) if value == stone]
  assert left and all(abs(centers[index][0]) > 30 or abs(centers[index][1]) > 30 for index in left)
  assert cleared["surfacing"]["erasedFaces"] == plain["surfacing"]["erasedFaces"] + len(left)
  assert all(value == uncovered for value in after["values"])
  assert cleared["shaping"]["passes"] == ["hill", "terrace"] and cleared["shaping"]["stillShapedBy"] == []
  assert "it needs surfacing" in noSurfacing


def testClearRegionTakesBackTheSpillOnGroundReshapedSincePainting(stageBlenderServer, tmp_path):
  noise = {"featureSize": 20, "amplitude": 6, "seed": 4}

  async def steps(session):
    await freshScene(session)
    await twoMaterials(session, tmp_path)
    await stripedGround(session, "ground", 200, 8)
    await session.expectSuccess("createRegion", {"name": "camp", "outline": [[-40, -40], [40, -40], [40, 40], [-40, 40]], "bottom": -50, "top": 100, "intent": "a camp clearing", "access": "play"})
    await session.expectSuccess("addShapingPass", {"objectName": "ground", "name": "mound"})
    await session.expectSuccess("sculptAtPoint", {"objectName": "ground", "mode": "raise", "center": [0, 0, 0], "radius": 60, "strength": 14, "direction": [0, 0, 1]})
    await session.expectSuccess("addSurfaceLayer", {"objectName": "ground", "name": "ground"})
    await session.expectSuccess("paintSurface", {"objectName": "ground", "layer": "ground", "material": "stone", "selector": {"region": "camp"}, "edgeNoise": noise})
    await session.expectSuccess("paintSurface", {"objectName": "ground", "layer": "ground", "material": "sand", "selector": {"nearPath": {"path": [[44, -20, 0], [96, -20, 0]], "radius": 6}}})
    painted = (await session.expectSuccess("runPython", surface("ground", "ground")))["result"]
    paintedCenters = (await session.expectSuccess("runPython", {"code": readCenters}))["result"]
    await session.expectSuccess("addShapingPass", {"objectName": "ground", "name": "terrace"})
    leveled = await session.expectSuccess("rebuildRegion", {"objectName": "ground", "selector": {"region": "camp"}, "mode": "height", "height": 6, "fadeDistance": 12})
    await session.expectSuccess("clearRegion", {"region": "camp", "terrainObject": "ground", "surfacing": True, "edgeNoise": noise})
    after = (await session.expectSuccess("runPython", surface("ground", "ground")))["result"]
    centers = (await session.expectSuccess("runPython", {"code": readCenters}))["result"]
    return painted, paintedCenters, leveled, after, centers

  painted, paintedCenters, leveled, after, centers = stageBlenderServer.session(steps)
  stone, sand = 0, 1
  # The camp was painted with a noisy edge spilling past the region, then leveled (its edge vertices moved, its diagonals turned):
  # cleared with the same edgeNoise, none of its paint is left, while the sand path that runs on beyond the noise's reach keeps its
  # far end.
  assert leveled["turnedDiagonals"] > 0
  assert any(value == stone and (abs(x) > 40 or abs(y) > 40) for value, (x, y, _) in zip(painted["values"], paintedCenters))
  assert stone not in after["values"]
  assert all(value == sand for value, (x, _, _) in zip(after["values"], centers) if x > 60 and value != uncovered)
  assert sum(value == sand for value, (x, _, _) in zip(after["values"], centers) if x > 60) > 10


def testResetRegionNamesThePassesThatStillShapeTheArea(stageBlenderServer):
  async def steps(session):
    await freshScene(session)
    await stripedGround(session, "ground", 160, 8)
    await session.expectSuccess("createRegion", {"name": "butte", "outline": [[-40, -40], [40, -40], [40, 40], [-40, 40]], "bottom": -100, "top": 200, "intent": "a butte", "access": "play"})
    await session.expectSuccess("addShapingPass", {"objectName": "ground", "name": "hill"})
    await session.expectSuccess("sculptAtPoint", {"objectName": "ground", "mode": "raise", "center": [0, 0, 0], "radius": 70, "strength": 20, "direction": [0, 0, 1]})
    await session.expectSuccess("addShapingPass", {"objectName": "ground", "name": "plateau"})
    await session.expectSuccess("rebuildRegion", {"objectName": "ground", "selector": {"region": "butte"}, "mode": "height", "height": 40})
    reset = await session.expectSuccess("resetRegion", {"objectName": "ground", "selector": {"region": "butte"}, "passes": ["hill"]})
    return reset, (await session.expectSuccess("runPython", shaped("ground")))["result"]

  reset, shape = stageBlenderServer.session(steps)
  # The plateau pass holds what lifted the hill to 40, not 40 itself: with the hill taken back, the plateau dishes by the hill's
  # height under it, and the reset names the pass that still shapes the area.
  assert reset["stillShapedBy"] == ["plateau"]
  plateau = [z for x, y, z in shape["vertices"] if abs(x) < 40 and abs(y) < 40]
  assert max(plateau) - min(plateau) > 5
