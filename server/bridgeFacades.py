"""Dressed faces at cave mouths: a defined pass, "facade <cave> <end>", that presses the cliff at a cave's open end into a flat vertical
face square to its path over a level apron, so a hall (cutCave with wallShare 1) reads as carved into the rock rather than as a cave
mouth. Its definition keeps the face itself, so it is graded again (bridgeGrading) whether the cave is cut or taken back: the ground it
reads is every face no cave made and every cave's plug faces on their vertices, as the ground stands with the caves taken back. Runs
under Blender's Python."""
import math

import numpy

import bridgeCaveData
import bridgeCaves
import bridgeMeshAccess

facadeKind = "facade"
facadePassPrefix = "facade "
# A vertex this close to the face's line counts as on it, in front of the face: a grid's single precision places a row on a whole
# coordinate only to about 1e-5.
lineTolerance = 1e-3
movedTolerance = 1e-9


def passName(cave, end):
  return f"{facadePassPrefix}{cave} {end}"


def featureName(definition):
  return f"{definition['cave']} {definition['end']}"


def uncutGround(sceneObject):
  """The ground with every cave taken back, as vertex index triangles and edges: the faces no cave made and each cave's plug faces."""
  mesh = sceneObject.data
  loopTotals, loopVertices = bridgeMeshAccess.faceLoops(sceneObject)
  kept = ~bridgeCaveData.caveFaceMask(sceneObject)
  starts = numpy.concatenate([[0], numpy.cumsum(loopTotals)[:-1]])
  polygons = [loopVertices[start:start + total] for start, total, keep in zip(starts.tolist(), loopTotals.tolist(), kept.tolist()) if keep]
  for name, record in bridgeCaveData.caves(sceneObject).items():
    byIdentifier = bridgeCaveData.plugIndices(bridgeCaveData.attributeValues(mesh, bridgeCaveData.vertexTagPrefix + name))
    polygons += [byIdentifier[numpy.array(plugFace["vertices"])] for plugFace in record["plug"]]
  triangles = numpy.array([[polygon[0], polygon[index], polygon[index + 1]] for polygon in polygons for index in range(1, len(polygon) - 1)], dtype=numpy.int64)
  edges = numpy.array([[polygon[index], polygon[(index + 1) % len(polygon)]] for polygon in polygons for index in range(len(polygon))], dtype=numpy.int64)
  return triangles, numpy.unique(numpy.sort(edges, axis=1), axis=0)


def facingDirection(facingDegrees):
  """The plan direction a face looks out along: 0 is +Y, turning clockwise."""
  radians = math.radians(facingDegrees)
  return numpy.array([math.sin(radians), math.cos(radians)])


def facadeDefinition(sceneObject, cave, end, faceAt, width, height, apron, blend):
  """A facade's definition at a cave's open end, refusing what cannot be dressed there."""
  record = bridgeCaves.requireCave(sceneObject, cave)
  if end not in ("start", "end"):
    raise ValueError(f"end is the end of the cave's path the face stands at, start or end, got {end!r}")
  if faceAt <= 0:
    raise ValueError(f"faceAt is how far in from the cave's {end} along its path the face stands, positive, got {faceAt}")
  if apron < 0 or blend <= 0:
    raise ValueError(f"apron is at least 0 and blend positive, got {apron} and {blend}")
  caveDefinition = bridgeCaves.caveDefinition(**record["definition"])
  line = bridgeCaves.CaveLine(caveDefinition)
  straight = straightFrom(line, end)
  if faceAt > straight + 1e-9:
    raise ValueError(f"faceAt {faceAt:g} is past the cave's first bend from its {end}: its path runs straight only {straight:.1f} in from there; stand the face within it")
  along = faceAt if end == "start" else line.length - faceAt
  floors, directions, widths, heights = line.at(numpy.array([0.0 if end == "start" else line.length, along]))
  kind = endKindOnUncutGround(sceneObject, caveDefinition, floors[0], directions[0], widths[0], heights[0], end)
  if kind != "open":
    raise ValueError(f"The cave's {end} at {bridgeCaves.roundedPoint(floors[0])} is {kind}, not open: a face is dressed at a mouth that opens on the ground in front of it")
  if width < widths[1] + 2:
    raise ValueError(f"width {width:g} does not frame the cave, {widths[1]:.1f} wide where the face stands: give at least {widths[1] + 2:.1f}")
  if height < heights[1] + 1:
    raise ValueError(f"height {height:g} does not frame the cave, {heights[1]:.1f} tall where the face stands: give at least {heights[1] + 1:.1f}")
  inward = directions[1] if end == "start" else -directions[1]
  outward = -inward
  return {
    "kind": facadeKind, "cave": cave, "end": end, "center": [float(value) for value in floors[1]],
    "facingDegrees": math.degrees(math.atan2(outward[0], outward[1])) % 360.0, "width": float(width), "height": float(height),
    "apron": float(apron), "blend": float(blend),
  }


def straightFrom(line, end):
  """How far a cave's path runs straight in from one end before its first bend."""
  start, finish, along = line.lines[0] if end == "start" else line.lines[-1]
  length = float(numpy.linalg.norm(finish - start))
  if end == "start":
    return length if along == 0 else 0.0
  return length if math.isclose(along + length, line.length) else 0.0


def endKindOnUncutGround(sceneObject, caveDefinition, floor, direction, width, height, end):
  shown, _ = bridgeMeshAccess.readVertexArrays(sceneObject)
  triangles, _ = uncutGround(sceneObject)
  surface = bridgeCaves.caveSurface(shown, triangles, caveDefinition)
  kind, _ = bridgeCaves.endKind(caveDefinition, bridgeCaves.sectionShape(caveDefinition), surface, floor, direction, width, height, end)
  return kind


class FaceFrame:
  """A face's plan frame: `along` the line the face stands on (to the right looking into the rock), `depth` into the rock."""

  def __init__(self, definition):
    self.center = numpy.array(definition["center"], dtype=numpy.float64)
    self.inward = -facingDirection(definition["facingDegrees"])
    self.right = numpy.array([self.inward[1], -self.inward[0]])

  def measure(self, points):
    offsets = points[:, :2] - self.center[:2]
    return offsets @ self.right, offsets @ self.inward

  def point(self, along, depth, height):
    return numpy.array([*(self.center[:2] + along * self.right + depth * self.inward), height])


def facadeExtent(definition):
  """Plan corners of everything a facade can move: its apron and face, and the blend around them."""
  frame = FaceFrame(definition)
  half, reach = definition["width"] / 2 + definition["blend"], definition["apron"] + definition["blend"]
  return numpy.array([frame.point(along, depth, 0.0)[:2] for along in (-half, half) for depth in (-reach, definition["blend"])])


def faceRows(ground, edges, along, depth, half):
  """The vertices the face is made of: the ends of the ground's edges crossing its line, those in front of it (or on it) and those
  behind, each row running between the vertices nearest the face's two sides; and which of them those are."""
  front = depth <= lineTolerance
  crossing = edges[front[edges[:, 0]] != front[edges[:, 1]]]
  if not len(crossing):
    return None
  crossing = numpy.where(front[crossing[:, :1]], crossing, crossing[:, ::-1])
  spacing = float(numpy.median(numpy.linalg.norm(ground[crossing[:, 0], :2] - ground[crossing[:, 1], :2], axis=1)))
  rows = []
  for column in (0, 1):
    candidates = numpy.unique(crossing[:, column])
    candidates = candidates[numpy.abs(along[candidates]) <= half + spacing]
    if not len(candidates):
      return None
    # Nearest each side, ties going to the vertex inside it.
    right = candidates[numpy.lexsort((along[candidates], numpy.abs(along[candidates] - half)))[0]]
    left = candidates[numpy.lexsort((-along[candidates], numpy.abs(along[candidates] + half)))[0]]
    members = candidates[(along[candidates] >= along[left]) & (along[candidates] <= along[right])]
    rows.append((members, left, right))
  return rows


def planFacade(sceneObject, definition, ground):
  """A facade's dressing of ground (world positions of the mesh as it stands under it): the world offset of every vertex and what the
  dressing did. The face's foot (F, the floor where it stands) and top (F + height) are each one row of vertices slid along the path's
  direction onto its line, the vertices nearest its sides slid along the line onto them; the ground in front of it within the apron and
  the width is set level at F; the ground in front of its line past the apron eases back to the ground as it was over blend."""
  frame = FaceFrame(definition)
  floor, height, half = frame.center[2], definition["height"], definition["width"] / 2
  along, depth = frame.measure(ground)
  _, edges = uncutGround(sceneObject)
  rows = faceRows(ground, edges, along, depth, half)
  if rows is None:
    raise ValueError(f"No ground of '{sceneObject.name}' crosses the facade's line within its width at {bridgeCaves.roundedPoint(frame.center)}; stand it where the ground runs into the rock")
  (footRow, footLeft, footRight), (topRow, topLeft, topRight) = rows
  low = topRow[ground[topRow, 2] < floor + height - movedTolerance]
  if len(low):
    lowest = low[ground[low, 2].argmin()]
    raise ValueError(
      f"There is nothing to dress at {bridgeCaves.roundedPoint(ground[lowest])}: the ground just behind the facade's line stands"
      f" {ground[lowest, 2]:.1f} there, under its top at {floor + height:.1f}. Move faceAt into the cliff, or lower the height"
    )
  targets = ground.copy()
  for members, left, right, top in ((footRow, footLeft, footRight, floor), (topRow, topLeft, topRight, floor + height)):
    slide = along[members].copy()
    slide[members == left], slide[members == right] = -half, half
    targets[members] = numpy.array([frame.point(position, 0.0, top) for position in slide.tolist()])
  face = numpy.zeros(len(ground), dtype=bool)
  face[footRow] = face[topRow] = True
  inFront = ~face & (depth <= lineTolerance)
  onApron = inFront & (depth >= -definition["apron"] - lineTolerance) & (numpy.abs(along) <= half)
  targets[onApron, 2] = floor
  beyond = numpy.hypot(numpy.maximum(numpy.abs(along) - half, 0.0), numpy.maximum(-definition["apron"] - depth, 0.0))
  easing = inFront & ~onApron & (depth < -lineTolerance) & (beyond < definition["blend"])
  share = beyond[easing] / definition["blend"]
  targets[easing, 2] += (floor - ground[easing, 2]) * (1 - share * share * (3 - 2 * share))
  offsets, liningLeft = bridgeCaveData.guardedOffsets(sceneObject, targets - ground)
  moved = numpy.abs(offsets).max(axis=1) > movedTolerance
  requireClear(sceneObject, definition, ground, moved)
  apronGround = onApron & ~bridgeCaveData.caveMadeVertices(sceneObject)
  apronChange = offsets[apronGround, 2]
  corners = [frame.point(position, 0.0, level) for position, level in ((-half, floor), (half, floor), (half, floor + height), (-half, floor + height))]
  return {
    "offsets": offsets, "standing": numpy.abs(frame.measure(ground + offsets)[1]) <= lineTolerance,
    "report": {
      "corners": [[round(float(value), 3) for value in corner] for corner in corners],
      "frame": {"center": [round(float(value), 3) for value in frame.center], "facingDegrees": definition["facingDegrees"], "width": definition["width"], "height": height},
      "faceVertices": int(face.sum()), "movedVertices": int(moved.sum()),
      "apron": {"vertices": int(apronGround.sum()), "deepestCut": round(-float(apronChange.min(initial=0.0)), 2), "highestFill": round(float(apronChange.max(initial=0.0)), 2)},
    } | bridgeCaveData.liningReport(liningLeft),
  }


def requireClear(sceneObject, definition, ground, moved):
  """Refuse a facade moving the terrain's border or another cave's ground."""
  border = numpy.flatnonzero(moved & bridgeMeshAccess.boundaryVertexMask(sceneObject))
  if len(border):
    raise ValueError(f"The facade's apron or blend reaches the edge of '{sceneObject.name}' near {bridgeCaves.roundedPoint(ground[border[0]])}; shorten them or dress further inside")
  if bridgeCaveData.holdsCaves(sceneObject):
    others = [name for name in bridgeCaveData.CaveVertices(sceneObject).namesOf(moved) if name != definition["cave"]]
    if others:
      raise ValueError(f"The facade's apron or blend reaches the ground of cave(s) {others}; shorten them, or take those caves back first")
