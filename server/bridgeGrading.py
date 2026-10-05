"""Defined passes: shaping passes rebuilt whole from a definition kept with them. A graded route (gradeRoute) keeps its definition with
its pass, "route <name>", and a facade dressed at a cave's mouth (dressFacade, bridgeFacades) with its pass, "facade <cave> <end>"; the
plots graded on a mesh keep theirs on the plots and are graded together (bridgeHousing), as one feature placed at the first of their
passes. Each is graded on the ground under it as it stands without its own pass and without any defined pass made after it, so where two
meet the later one wins: grading one again replays every later one whose ground that changed, and regradeTerrain replays them all in the
order they were made, then cuts again every cave whose ground moved. Runs under Blender's Python."""
import math

import bpy
import mathutils
import mathutils.bvhtree
import numpy

import bridgeAuthoring
import bridgeCaveData
import bridgeCaves
import bridgeExport
import bridgeFacades
import bridgeHousing
import bridgeMeshAccess
import bridgePasses
import bridgeReview
import bridgeShaping
import bridgeStructures
import bridgeSurfacing

routePassPrefix = "route "
routeKind = "route"
# Batters are looked for this far out at least and refused past the farthest, as gradePlot's are.
minimumReach = 20.0
maximumReach = 600.0
# A turn sharper than this is a hairpin with a flat landing. A bend turns on an arc the route's width in radius, so that across it the
# bench is level from edge to edge instead of stepping where the two stretches' cross-sections meet; a hairpin turns on one half the
# width, so its landing spans no more of a hillside than it must, and the landing takes in the ground within half the width of that
# arc's center, so its inner corner is a curve the grid can carry rather than a point.
landingTurnDegrees = 90.0
# A ledge's bench may stand this far over the ground under it, a step up from it.
ledgeStandOff = 2.0
# Two parts of a route clash when their benches differ in height by more than the cut batter spans between them, beyond this.
clashTolerance = 0.01
# A vertex snapped onto its bench's edge lies on it to within this.
edgeTolerance = 1e-4
# Faces left turned over on or beside the bench are worked out at most this many times.
benchRepairs = 8
# Rock over a route is looked for from this far over its graded bench, clear of the faces the bench's own vertices share.
rockClearance = 0.5
arcSampleDegrees = 10.0
walkSampleSpacing = 4.0
clashRowsPerChunk = 512
up = mathutils.Vector((0.0, 0.0, 1.0))
down = mathutils.Vector((0.0, 0.0, -1.0))


# Routes

class Centerline:
  """A route's centerline in plan: straight stretches joined by an arc at each bend, a station at each of its points (an arc's middle
  at a bend), widths changing evenly between the stations, flat landings at sharp turns, and heights graded evenly by plan length,
  landings left out, between the stations whose heights are fixed."""

  def __init__(self, name, points, widths, landingLength):
    self.name = name
    plan = numpy.array([point[:2] for point in points], dtype=numpy.float64)
    self.widths = numpy.array(widths, dtype=numpy.float64)
    runs = numpy.diff(plan, axis=0)
    lengths = numpy.linalg.norm(runs, axis=1)
    for index in numpy.flatnonzero(lengths < 1e-6):
      raise ValueError(f"Points {index} and {index + 1} of route '{name}' stand at one place in plan")
    directions = runs / lengths[:, None]
    count = len(plan)
    self.turns = numpy.zeros(count)
    radii, tangents, signs = numpy.zeros(count), numpy.zeros(count), numpy.zeros(count)
    for index in range(1, count - 1):
      incoming, outgoing = directions[index - 1], directions[index]
      cross = incoming[0] * outgoing[1] - incoming[1] * outgoing[0]
      turn = math.atan2(abs(cross), float(incoming @ outgoing))
      if turn > math.pi - 1e-6:
        raise ValueError(f"Route '{name}' turns straight back on itself at point {index}; turn a hairpin on two points, or on one with the legs leaving it apart")
      self.turns[index], signs[index] = turn, 1.0 if cross >= 0 else -1.0
      radii[index] = self.widths[index] / (2 if self.isLanding(index) else 1)
      tangents[index] = radii[index] * math.tan(turn / 2)
    for index in range(count - 1):
      if tangents[index] + tangents[index + 1] > lengths[index] + 1e-9:
        raise ValueError(
          f"Route '{name}': the bends at points {index} and {index + 1} take {tangents[index]:.1f} and {tangents[index + 1]:.1f} of the"
          f" {lengths[index]:.1f} between them; a bend turns on an arc the route's width in radius, a turn over {landingTurnDegrees:g} degrees on"
          " one half its width. Move the points apart or turn less sharply"
        )
    self.lines, self.arcs, self.turnCenters, bends = [], [], [], []
    stations, along, start = [0.0], 0.0, plan[0]
    arcLengths = numpy.zeros(count)
    for index in range(1, count - 1):
      turnStart = plan[index] - directions[index - 1] * tangents[index]
      along = self.addLine(start, turnStart, along)
      if self.turns[index] == 0:
        stations.append(along)
        start = turnStart
        continue
      left = numpy.array([-directions[index - 1][1], directions[index - 1][0]])
      center = turnStart + left * signs[index] * radii[index]
      arcLengths[index] = radii[index] * self.turns[index]
      self.arcs.append((center, radii[index], math.atan2(turnStart[1] - center[1], turnStart[0] - center[0]), signs[index] * self.turns[index], along))
      if self.isLanding(index):
        self.turnCenters.append((center, along + arcLengths[index] / 2))
      else:
        bends.append((along, along + arcLengths[index], (radii[index] - self.widths[index] / 2) / radii[index]))
      stations.append(along + arcLengths[index] / 2)
      along += arcLengths[index]
      start = plan[index] + directions[index] * tangents[index]
    self.length = self.addLine(start, plan[-1], along)
    stations.append(self.length)
    self.stations = numpy.array(stations)
    self.landings = []
    for index in range(1, count - 1):
      if not self.isLanding(index):
        continue
      length = max(self.widths[index] if landingLength is None else landingLength, arcLengths[index])
      low, high = self.stations[index] - length / 2, self.stations[index] + length / 2
      if low < self.stations[index - 1] - 1e-9 or high > self.stations[index + 1] + 1e-9:
        raise ValueError(f"The landing at point {index} of route '{name}' ({length:.1f} long) reaches past the next point along; shorten landingLength or move the points apart")
      if self.landings and low < self.landings[-1]["high"] - 1e-9:
        raise ValueError(f"The landings at points {self.landings[-1]['point']} and {index} of route '{name}' overlap; shorten landingLength or move the points apart")
      self.landings.append({"point": index, "low": low, "high": high})
    self.rateBreaks = numpy.unique([0.0, self.length] + [value for landing in self.landings for value in (landing["low"], landing["high"])] + [value for start, end, _ in bends for value in (start, end)])
    middles = (self.rateBreaks[:-1] + self.rateBreaks[1:]) / 2
    self.rates = numpy.ones(len(middles))
    for start, end, share in bends:
      self.rates[(middles > start) & (middles < end)] = share
    for landing in self.landings:
      self.rates[(middles > landing["low"]) & (middles < landing["high"])] = 0.0
    self.gradedAtBreaks = numpy.concatenate([[0.0], numpy.cumsum(self.rates * numpy.diff(self.rateBreaks))])

  def isLanding(self, index):
    return self.turns[index] > math.radians(landingTurnDegrees)

  def addLine(self, start, end, along):
    length = float(numpy.linalg.norm(end - start))
    if length > 1e-9:
      self.lines.append((start, end, along))
    return along + length

  def benchPieces(self, beyond):
    """The piece of the route each point belongs to (beyond: its distance past each piece's bench edge, as project orders the pieces):
    the stretch or arc whose bench it lies on, else a hairpin's inner corner it lies in, else the one whose bench it lies nearest."""
    best = beyond.argmin(axis=1)
    if not self.turnCenters:
      return best
    stretches = beyond[:, :-len(self.turnCenters)]
    return numpy.where(stretches.min(axis=1) <= edgeTolerance, stretches.argmin(axis=1), best)

  def gradedLength(self, along):
    """Length along the route as its grade runs: along the centerline, but none over a landing, and round a bend along the bench's inner
    edge, so that no part of the bench there is steeper than the grade."""
    span = numpy.clip(numpy.searchsorted(self.rateBreaks, along, side="right") - 1, 0, len(self.rates) - 1)
    return self.gradedAtBreaks[span] + self.rates[span] * (along - self.rateBreaks[span])

  def grade(self, fixed, maximumGradeDegrees):
    """Grade evenly between the points whose heights are fixed (fixed: a height or None per point, the ends given)."""
    indices = [index for index, height in enumerate(fixed) if height is not None]
    self.fixedStations = self.stations[indices]
    self.fixedHeights = numpy.array([fixed[index] for index in indices])
    self.fixedGraded = self.gradedLength(self.fixedStations)
    self.grades = []
    for (first, second), run, rise in zip(zip(indices[:-1], indices[1:]), numpy.diff(self.fixedGraded), numpy.diff(self.fixedHeights)):
      degrees = math.degrees(math.atan2(abs(rise), run)) if run > 1e-9 else (0.0 if abs(rise) < 1e-9 else 90.0)
      if degrees > maximumGradeDegrees + 1e-9:
        needed = abs(rise) / math.tan(math.radians(maximumGradeDegrees))
        planLength = self.stations[second] - self.stations[first]
        beside = f" (its plan length {planLength:.1f}, landings level and bends graded along their inner edges)" if planLength - run > 1e-9 else ""
        raise ValueError(
          f"Route '{self.name}' {'rises' if rise > 0 else 'falls'} {abs(rise):.1f} from point {first} to point {second} over a run of {run:.1f}{beside},"
          f" {degrees:.1f} degrees, steeper than {maximumGradeDegrees:g}; at {maximumGradeDegrees:g} degrees it needs a run of {needed:.1f}."
          " Lengthen the route there or give it a switchback's points (it never adds one)"
        )
      self.grades.append(rise / run if run > 1e-9 else 0.0)
    self.grades = numpy.array(self.grades)

  def heightAt(self, along):
    span = numpy.clip(numpy.searchsorted(self.fixedStations, along, side="right") - 1, 0, len(self.grades) - 1)
    return self.fixedHeights[span] + self.grades[span] * (self.gradedLength(along) - self.fixedGraded[span])

  def gradeAt(self, along):
    return self.grades[numpy.clip(numpy.searchsorted(self.fixedStations, along, side="right") - 1, 0, len(self.grades) - 1)]

  def widthAt(self, along):
    return numpy.interp(along, self.stations, self.widths)

  def project(self, points):
    """Each point's distance in plan from every stretch and arc, how far along the route the nearest spot on each lies, and that spot."""
    pieces = []
    for start, end, along in self.lines:
      run = end - start
      share = ((points - start) @ run) / (run @ run)
      nearest = start + numpy.clip(share, 0, 1)[:, None] * run
      # Past the route's ends the bench runs on at its end's grade, so its round end is one plane with the stretch it ends.
      share = numpy.clip(share, -numpy.inf if along == 0 else 0, numpy.inf if math.isclose(along + math.sqrt(run @ run), self.length) else 1)
      pieces.append((nearest, along + share * math.sqrt(run @ run)))
    for center, radius, startAngle, sweep, along in self.arcs:
      offset = points - center
      angle = numpy.arctan2(offset[:, 1], offset[:, 0])
      turned = numpy.mod((angle - startAngle) * math.copysign(1.0, sweep), 2 * math.pi)
      ends = [center + radius * numpy.array([math.cos(endAngle), math.sin(endAngle)]) for endAngle in (startAngle, startAngle + sweep)]
      nearerStart = numpy.linalg.norm(points - ends[0], axis=1) <= numpy.linalg.norm(points - ends[1], axis=1)
      onArc = turned <= abs(sweep)
      nearest = numpy.where(onArc[:, None], center + radius * numpy.stack([numpy.cos(angle), numpy.sin(angle)], axis=1), numpy.where(nearerStart[:, None], ends[0], ends[1]))
      pieces.append((nearest, along + radius * numpy.where(onArc, turned, numpy.where(nearerStart, 0.0, abs(sweep)))))
    for center, along in self.turnCenters:
      pieces.append((numpy.broadcast_to(center, points.shape), numpy.full(len(points), along)))
    nearest = numpy.stack([piece[0] for piece in pieces], axis=1)
    return numpy.linalg.norm(points[:, None] - nearest, axis=2), numpy.stack([piece[1] for piece in pieces], axis=1), nearest

  def pointAt(self, along):
    """Plan positions of spots along the route."""
    along = numpy.clip(along, 0.0, self.length)
    points = numpy.zeros((len(along), 2))
    for start, end, lineAlong in self.lines:
      length = float(numpy.linalg.norm(end - start))
      inside = (along >= lineAlong - 1e-6) & (along <= lineAlong + length + 1e-6)
      points[inside] = start + numpy.clip((along[inside] - lineAlong) / length, 0, 1)[:, None] * (end - start)
    for center, radius, startAngle, sweep, arcAlong in self.arcs:
      inside = (along >= arcAlong - 1e-6) & (along <= arcAlong + radius * abs(sweep) + 1e-6)
      angle = startAngle + numpy.clip((along[inside] - arcAlong) / radius, 0, abs(sweep)) * math.copysign(1.0, sweep)
      points[inside] = center + radius * numpy.stack([numpy.cos(angle), numpy.sin(angle)], axis=1)
    return points

  def samples(self):
    """Spots along the route at every stretch's ends and every few degrees round each arc, as [[x, y, z], ...] at the graded heights."""
    alongs = [0.0, self.length] + [along for _, _, along in self.lines] + [along + float(numpy.linalg.norm(end - start)) for start, end, along in self.lines]
    for _, radius, _, sweep, along in self.arcs:
      steps = max(1, math.ceil(math.degrees(abs(sweep)) / arcSampleDegrees))
      alongs += [along + radius * abs(sweep) * step / steps for step in range(steps + 1)]
    alongs = numpy.unique(alongs)
    alongs = alongs[numpy.concatenate([[True], numpy.diff(alongs) > 1e-6])]
    return numpy.column_stack([self.pointAt(alongs), self.heightAt(alongs)])


def routeDefinition(points, width, widths, maximumGradeDegrees, cutBatterDegrees, fillBatterDegrees, landingLength):
  if len(points) < 2 or any(len(point) not in (2, 3) for point in points):
    raise ValueError(f"A route's points are at least two [x, y] or [x, y, z], got {points!r}")
  if (width is None) == (widths is None):
    raise ValueError("Give either width (the whole route) or widths (one per point)")
  widths = [width] * len(points) if widths is None else widths
  if len(widths) != len(points) or min(widths) <= 0:
    raise ValueError(f"widths are one positive width per point ({len(points)}), got {widths!r}")
  if not 0 < maximumGradeDegrees < 60:
    raise ValueError(f"maximumGradeDegrees is above 0 and under 60 (steeper is not walked), got {maximumGradeDegrees}")
  if not 5 <= cutBatterDegrees <= 85 or (fillBatterDegrees is not None and not 5 <= fillBatterDegrees <= 85):
    raise ValueError(f"cutBatterDegrees and fillBatterDegrees are from 5 to 85 (fillBatterDegrees null for a ledge), got {cutBatterDegrees} and {fillBatterDegrees}")
  if landingLength is not None and landingLength <= 0:
    raise ValueError(f"landingLength is positive, got {landingLength}")
  return {
    "kind": routeKind, "points": [[float(value) for value in point] for point in points], "widths": [float(value) for value in widths],
    "maximumGradeDegrees": float(maximumGradeDegrees), "cutBatterDegrees": float(cutBatterDegrees),
    "fillBatterDegrees": None if fillBatterDegrees is None else float(fillBatterDegrees), "landingLength": None if landingLength is None else float(landingLength),
  }


def surfaceHeightAt(surface, point, top):
  location, _, _, _ = surface.ray_cast(mathutils.Vector((point[0], point[1], top)), down)
  return None if location is None else location.z


def otherRock(sceneObject):
  """What else in the terrain collection could stand over a route on sceneObject: rock masses, arches."""
  collection = bpy.data.collections.get(bridgeExport.terrainCollectionName)
  others = [] if collection is None else [member for member in collection.all_objects if member != sceneObject and bridgeMeshAccess.isPlayerSolid(member) and member.type == "MESH"]
  trees = bridgeMeshAccess.solidTrees(others)
  return bridgeMeshAccess.PlayerSurfaces(trees=trees) if trees else None


def planRoute(sceneObject, name, definition, ground, faces, edgeLength):
  """A route's grading on ground (world positions of the mesh as it stands under the route): the world offset of every vertex and what
  the grading did."""
  centerline = Centerline(name, definition["points"], definition["widths"], definition["landingLength"])
  surface = mathutils.bvhtree.BVHTree.FromPolygons(ground.tolist(), faces)
  top = float(ground[:, 2].max()) + 1
  fixed = []
  for index, point in enumerate(definition["points"]):
    if len(point) == 3:
      fixed.append(point[2])
    elif index in (0, len(definition["points"]) - 1):
      height = surfaceHeightAt(surface, point, top)
      if height is None:
        raise ValueError(f"No ground under the end of route '{name}' at {point}")
      fixed.append(height)
    else:
      fixed.append(None)
  centerline.grade(fixed, definition["maximumGradeDegrees"])
  tanCut = math.tan(math.radians(definition["cutBatterDegrees"]))
  tanFill = None if definition["fillBatterDegrees"] is None else math.tan(math.radians(definition["fillBatterDegrees"]))
  plan = ground[:, :2]
  pathPoints = numpy.array([point[:2] for point in definition["points"]])
  low, high = pathPoints.min(axis=0) - centerline.widths.max() / 2, pathPoints.max(axis=0) + centerline.widths.max() / 2
  reach = minimumReach
  while True:
    rows = numpy.flatnonzero(((plan >= low - reach) & (plan <= high + reach)).all(axis=1))
    distances, alongs, nearest = centerline.project(plan[rows])
    beyond = distances - centerline.widthAt(alongs) / 2
    picked = numpy.arange(len(rows)), centerline.benchPieces(beyond)
    within = beyond[picked] <= reach
    if not within.any():
      raise ValueError(f"No vertex of '{sceneObject.name}' lies under or near route '{name}'; grade it on the ground it runs over")
    aboveBench = ground[rows, 2] - centerline.heightAt(alongs[picked])
    needed = numpy.where(aboveBench > 0, aboveBench / tanCut, 0.0 if tanFill is None else -aboveBench / tanFill)
    neededReach = max(minimumReach, float(needed[within].max()) + 2 * edgeLength)
    if neededReach > maximumReach:
      worst = numpy.flatnonzero(within)[needed[within].argmax()]
      raise ValueError(
        f"Route '{name}' runs {abs(aboveBench[worst]):.0f} units off the ground around it near {[round(float(value), 1) for value in plan[rows[worst]]]};"
        f" its batters would reach {neededReach:.0f} units out (at most {maximumReach:g}). Change its heights there, move it, or steepen its batters"
      )
    if neededReach <= reach:
      break
    reach = neededReach + edgeLength
  count = len(ground)
  affected = numpy.zeros(count, dtype=bool)
  affected[rows] = True
  lateral, edges, outward = numpy.zeros(count), numpy.zeros((count, 1)), numpy.zeros((count, 2))
  lateral[rows] = distances[picked]
  edges[rows, 0] = centerline.widthAt(alongs[picked]) / 2
  away = distances[picked] > 1e-9
  outward[rows[away]] = (plan[rows[away]] - nearest[picked][away]) / distances[picked][away, None]
  snapped, _ = bridgeShaping.snapOntoBreaks(sceneObject, ground, affected, lateral, edges, outward, bridgeMeshAccess.boundaryVertexMask(sceneObject) | bridgeCaveData.fixedInPlan(sceneObject))
  positions = ground.copy()
  positions[rows, :2] = snapped[rows, :2]
  loopTotals, loopVertices = bridgeMeshAccess.faceLoops(sceneObject)
  faceOfLoop = numpy.repeat(numpy.arange(len(loopTotals)), loopTotals)

  def corners(faceMask):
    mask = numpy.zeros(count, dtype=bool)
    mask[loopVertices[faceMask[faceOfLoop]]] = True
    return mask

  turnedBefore = bridgeShaping.overturnedFaces(sceneObject, ground, affected)
  widened = numpy.zeros(count, dtype=bool)
  for _ in range(benchRepairs + 1):
    graded, onBench, benchAlongs = gradedHeights(centerline, positions[rows, :2], ground[rows, 2], reach, tanCut, tanFill, widened[rows])
    result = positions.copy()
    result[rows, 2] = graded
    # A vertex slid onto an edge but left at its height changes nothing where it stood, so it stays there.
    slid = numpy.abs(result[:, :2] - ground[:, :2]).max(axis=1) > 0
    unshaped = slid & (numpy.abs(result[:, 2] - ground[:, 2]) <= 1e-9)
    result[unshaped] = ground[unshaped]
    slid &= ~unshaped
    bench = numpy.zeros(count, dtype=bool)
    bench[rows[onBench]] = True
    # A vertex slid past a neighbour turns a face over: it goes back where it stood, at the bench's height there, so the bench widens
    # a little rather than a face from beyond its edge reaching under the path. A wrinkle of the ground the bench levels (an overhang
    # roughened into a slope) was turned over already: its corners on the bench move toward their neighbours. Each is graded where it
    # then stands.
    turned = bridgeShaping.overturnedFaces(sceneObject, result, affected)
    returning, relaxing = corners(turned & ~turnedBefore) & slid, corners(turned & turnedBefore) & bench
    if not returning.any() and not relaxing.any():
      break
    positions[returning, :2] = ground[returning, :2]
    widened |= returning
    positions[relaxing, :2] = bridgeShaping.vertexNeighbourAverages(sceneObject, result)[relaxing, :2]
  offsets, left = bridgeCaveData.guardedOffsets(sceneObject, result - ground)
  result = ground + offsets
  # A cave's lining under the bench in plan is no part of it: it holds still inside the rock.
  benchRows = rows[onBench & ~bridgeCaveData.liningVertices(sceneObject)[rows]]
  requireNoClash(name, result[benchRows], tanCut, definition["cutBatterDegrees"])
  if tanFill is None:
    requireFooting(name, centerline, result[benchRows, 2] - ground[benchRows, 2], benchAlongs, edgeLength)
  requireOpenSky(name, sceneObject, result, faces, result[benchRows])
  change = result[:, 2] - ground[:, 2]
  return {
    "offsets": result - ground,
    "report": {
      "planLength": round(centerline.length, 1), "segments": segmentReport(centerline), "landings": landingReport(centerline),
      "deepestCut": round(-float(change.min(initial=0.0)), 2), "highestFill": round(float(change.max(initial=0.0)), 2),
      "batterReach": round(reach, 1), "movedVertices": int((numpy.abs(result - ground).max(axis=1) > 1e-9).sum()),
      "centerline": [[round(float(value), 2) for value in sample] for sample in centerline.samples()],
    } | bridgeCaveData.liningReport(left),
  }


def gradedHeights(centerline, points, natural, reach, tanCut, tanFill, widened):
  """Heights of ground points graded to a route: on its bench (and at the points widened onto it) the bench's height; beyond it, the
  ground kept between the highest fill and the lowest cut any stretch's batters allow within reach, the cut winning where they
  disagree, and between two legs no higher than one straight bank from one's edge to the other's. Also which points are on the bench,
  and how far along the route each of those lies."""
  distances, alongs, nearest = centerline.project(points)
  widths = centerline.widthAt(alongs)
  beyond = distances - widths / 2
  picked = numpy.arange(len(points)), centerline.benchPieces(beyond)
  onBench = (beyond[picked] <= edgeTolerance) | widened
  pieceBeyond = numpy.maximum(beyond, 0.0)
  pieceHeights = centerline.heightAt(alongs)
  reaching = pieceBeyond <= reach
  highs = numpy.where(reaching, pieceHeights + pieceBeyond * tanCut, numpy.inf).min(axis=1)
  highs = numpy.minimum(highs, bankBetweenLegs(centerline, points, nearest, alongs, widths, pieceBeyond, pieceHeights, reaching, tanCut, tanCut if tanFill is None else tanFill))
  lows = numpy.full(len(points), -numpy.inf) if tanFill is None else numpy.where(reaching, pieceHeights - pieceBeyond * tanFill, -numpy.inf).max(axis=1)
  graded = numpy.where(onBench, pieceHeights[picked], numpy.minimum(numpy.maximum(natural, lows), highs))
  return graded, onBench, alongs[picked][onBench]


def bankBetweenLegs(centerline, points, nearest, alongs, widths, pieceBeyond, pieceHeights, reaching, tanCut, tanBank):
  """The highest each point may stand where it lies between two legs of the route. Two stretches are legs to each other at a point
  when they lie on either side of it and the route between their spots runs further than the width beyond their distance apart (a
  hairpin's legs, not a bend's sides). Where the ground between them is no wider than the route plus what their difference in height
  needs at the cut batter, it is one bank, straight from one leg's bench edge to the other's; where it is no wider than that at the
  bank's own batter (the fill's, or the cut's for a ledge), nothing in it stands above the higher leg, so no berm or fin is left
  between them. A hill a route goes round, its parts at one height, is left alone."""
  stretches = pieceBeyond.shape[1] - len(centerline.turnCenters)
  toNearest = nearest[:, :stretches] - points[:, None]
  first, second = numpy.triu_indices(stretches, 1)
  opposite = (toNearest[:, first] * toNearest[:, second]).sum(axis=2) < 0
  apart = numpy.linalg.norm(nearest[:, first] - nearest[:, second], axis=2)
  width = (widths[:, first] + widths[:, second]) / 2
  legs = numpy.abs(alongs[:, first] - alongs[:, second]) > apart + width
  gap = pieceBeyond[:, first] + pieceBeyond[:, second]
  rise = pieceHeights[:, second] - pieceHeights[:, first]
  between = opposite & legs & (gap > 0) & reaching[:, first] & reaching[:, second]
  oneBank = between & (gap <= width + numpy.abs(rise) / tanCut)
  belowHigher = between & (gap <= width + numpy.abs(rise) / tanBank)
  chord = pieceHeights[:, first] + rise * numpy.divide(pieceBeyond[:, first], gap, out=numpy.zeros_like(gap), where=gap > 0)
  higher = numpy.maximum(pieceHeights[:, first], pieceHeights[:, second])
  return numpy.where(oneBank, chord, numpy.where(belowHigher, higher, numpy.inf)).min(axis=1, initial=numpy.inf)


def segmentReport(centerline):
  rises = numpy.diff(centerline.heightAt(centerline.stations))
  middles = (centerline.stations[:-1] + centerline.stations[1:]) / 2
  return [
    {"points": [index, index + 1], "planLength": round(float(length), 1), "rise": round(float(rise), 2), "gradeDegrees": round(math.degrees(math.atan(float(grade))), 2)}
    for index, (length, rise, grade) in enumerate(zip(numpy.diff(centerline.stations), rises, centerline.gradeAt(middles)))
  ]


def landingReport(centerline):
  report = []
  for landing in centerline.landings:
    middle = numpy.array([(landing["low"] + landing["high"]) / 2])
    report.append({
      "point": landing["point"], "center": [round(float(value), 2) for value in centerline.pointAt(middle)[0]], "height": round(float(centerline.heightAt(middle)[0]), 2),
      "length": round(landing["high"] - landing["low"], 1), "turnDegrees": round(math.degrees(centerline.turns[landing["point"]]), 1),
    })
  return report


def requireNoClash(name, bench, tanCut, cutBatterDegrees):
  """Refuse where two parts of the route's bench differ in height by more than its cut batter spans between them: one's batter would
  cut into or bury the other."""
  worst = None
  for start in range(0, len(bench), clashRowsPerChunk):
    chunk = bench[start:start + clashRowsPerChunk]
    apart = numpy.linalg.norm(chunk[:, None, :2] - bench[None, :, :2], axis=2)
    excess = numpy.abs(chunk[:, None, 2] - bench[None, :, 2]) - apart * tanCut
    first, second = numpy.unravel_index(excess.argmax(), excess.shape)
    if worst is None or excess[first, second] > worst[0]:
      worst = (float(excess[first, second]), chunk[first], bench[second], float(apart[first, second]))
  if worst is not None and worst[0] > clashTolerance:
    _, first, second, apart = worst
    rise = abs(float(first[2] - second[2]))
    raise ValueError(
      f"Route '{name}' clashes with itself near {[round(float(value), 1) for value in (first[:2] + second[:2]) / 2]}: its bench at"
      f" {[round(float(value), 1) for value in first[:2]]} stands {first[2]:.1f} high and at {[round(float(value), 1) for value in second[:2]]},"
      f" {apart:.1f} away in plan, {second[2]:.1f}; one's batter would reach the other's bench. At {cutBatterDegrees:g} degrees they need"
      f" {rise / tanCut:.1f} between them. Move those parts apart, change their heights, or steepen cutBatterDegrees"
    )


def requireFooting(name, centerline, standing, alongs, edgeLength):
  """Refuse a ledge (no fill batter) where its bench would stand more than a step over the ground, naming each span where it would."""
  over = standing > ledgeStandOff + 1e-6
  if not over.any():
    return
  order = numpy.argsort(alongs[over])
  overAlongs, overStanding = alongs[over][order], standing[over][order]
  breaks = numpy.flatnonzero(numpy.diff(overAlongs) > 2 * edgeLength) + 1
  spans = []
  for alongRun, standingRun in zip(numpy.split(overAlongs, breaks), numpy.split(overStanding, breaks)):
    ends = centerline.pointAt(numpy.array([alongRun[0], alongRun[-1]]))
    spans.append(
      f"from {[round(float(value), 1) for value in ends[0]]} to {[round(float(value), 1) for value in ends[1]]} ({alongRun[0]:.1f} to"
      f" {alongRun[-1]:.1f} along it) up to {float(standingRun.max()):.1f}"
    )
  raise ValueError(
    f"Route '{name}' has no fill batter, so it is a ledge, but its bench would stand more than {ledgeStandOff:g} over the ground"
    f" {'; '.join(spans)}. Move it into the slope there, change its heights, or give it a fillBatterDegrees"
  )


def requireOpenSky(name, sceneObject, graded, faces, bench):
  """Refuse a route any of whose bench lies under rock once it is graded: a covered way is a cave."""
  surface = mathutils.bvhtree.BVHTree.FromPolygons(graded.tolist(), faces)
  others = otherRock(sceneObject)
  for point in bench:
    origin = mathutils.Vector((point[0], point[1], point[2] + rockClearance))
    location, _, _, _ = surface.ray_cast(origin, up)
    if location is None and others is not None:
      location = others.cast(origin, up, bridgeMeshAccess.waterReach)
    if location is not None:
      raise ValueError(f"Route '{name}' runs under rock at {[round(float(value), 1) for value in point[:2]]} (rock {location.z - point[2]:.1f} over its bench there): a covered way is a cave, not a graded route")


# Defined features and their replay

def worldOffsets(sceneObject, key, reference):
  """What a pass adds to the mesh as seen, in world units: nothing while it is muted."""
  if key.mute:
    return numpy.zeros_like(reference)
  return (bridgePasses.keyCoordinates(key) - reference) * key.value @ bridgeMeshAccess.matrixArray(sceneObject.matrix_world)[:3, :3].T


def plotsFeature(sceneObject, overrides, subjects, kept):
  plots = sorted(set(bridgeHousing.gradedOn(sceneObject)) | set(overrides))
  return {"kind": "plots", "passes": [bridgeHousing.gradePassName(address) for address in plots if address != kept], "overrides": overrides, "subjects": subjects, "kept": kept}


def definedFeatures(sceneObject):
  """The defined features on a mesh in the order they were made: each route and facade, and the plots graded on it as one, at its first
  plot's pass (at the end when none has a pass yet)."""
  keys = sceneObject.data.shape_keys
  definitions = bridgePasses.passDefinitions(sceneObject)
  plotPasses = {bridgeHousing.gradePassName(address) for address in bridgeHousing.gradedOn(sceneObject)}
  features, plotsPlaced = [], False
  for key in [] if keys is None else keys.key_blocks:
    definition = definitions.get(key.name)
    if definition is not None and definition["kind"] == routeKind:
      features.append({"kind": routeKind, "name": key.name[len(routePassPrefix):], "passes": [key.name], "definition": definition})
    elif definition is not None and definition["kind"] == bridgeFacades.facadeKind:
      features.append({"kind": bridgeFacades.facadeKind, "name": bridgeFacades.featureName(definition), "passes": [key.name], "definition": definition})
    elif key.name in plotPasses and not plotsPlaced:
      features.append(plotsFeature(sceneObject, {}, set(), None))
      plotsPlaced = True
  if plotPasses and not plotsPlaced:
    features.append(plotsFeature(sceneObject, {}, set(), None))
  return features


def featureExtent(sceneObject, feature):
  """Plan corners a feature's own grading starts from: its route's points and widths, or its plots' outlines."""
  if feature["kind"] == routeKind:
    points = numpy.array([point[:2] for point in feature["definition"]["points"]])
    half = max(feature["definition"]["widths"]) / 2
    return numpy.vstack([points - half, points + half])
  if feature["kind"] == bridgeFacades.facadeKind:
    return bridgeFacades.facadeExtent(feature["definition"])
  pads = [bridgeHousing.plotPad(plot) for plot in bridgeHousing.gradedOn(sceneObject).values()] + [pad for pad in feature["overrides"].values() if pad is not None]
  return numpy.vstack([pad["outline"] for pad in pads]) if pads else numpy.zeros((0, 2))


def overlapsChange(sceneObject, feature, held, state, changed, margin):
  """Whether a feature's grading (what its passes hold, or where its definition lies) reaches anywhere the ground under it changed."""
  if not changed.any():
    return False
  corners = numpy.vstack([state[numpy.abs(held).max(axis=1) > 0, :2], featureExtent(sceneObject, feature)])
  if not len(corners):
    return False
  low, high = corners.min(axis=0) - margin, corners.max(axis=0) + margin
  return bool(((state[changed, :2] >= low) & (state[changed, :2] <= high)).all(axis=1).any())


def gradeFeature(sceneObject, feature, ground, faces, edgeLength):
  if feature["kind"] == routeKind:
    return planRoute(sceneObject, feature["name"], feature["definition"], ground, faces, edgeLength)
  if feature["kind"] == bridgeFacades.facadeKind:
    return bridgeFacades.planFacade(sceneObject, feature["definition"], ground)
  plotPlan = bridgeHousing.planGrading(sceneObject, feature["overrides"], feature["subjects"], feature["kept"], ground)
  offsets = numpy.zeros_like(ground)
  offsets[:, 2] = plotPlan["graded"] - ground[:, 2]
  return {"offsets": bridgeCaveData.guardedOffsets(sceneObject, offsets)[0], "plotPlan": plotPlan}


def planReplay(sceneObject, features, first, everything):
  """Grade features[first] and replay what follows it on the ground the order they were made gives each: every later feature whose
  ground that changed, or with everything, all of them. Nothing is written."""
  if sceneObject.modifiers:
    raise ValueError(f"'{sceneObject.name}' has modifiers; grading passes combine before modifiers, so apply or remove them first")
  shown, _ = bridgeMeshAccess.readVertexArrays(sceneObject)
  keys = sceneObject.data.shape_keys
  reference = None if keys is None else bridgePasses.keyCoordinates(keys.reference_key)
  held = []
  for feature in features[first:]:
    total = numpy.zeros_like(shown)
    for name in feature["passes"]:
      key = None if keys is None else keys.key_blocks.get(name)
      if key is not None:
        total += worldOffsets(sceneObject, key, reference)
    held.append(total)
  state = shown - sum(held)
  faces = [indices.tolist() for indices in bridgeMeshAccess.faceVertexIndices(sceneObject)]
  edgeLength = bridgeShaping.medianEdgeLength(sceneObject, shown, numpy.ones(len(shown), dtype=bool))
  changed = numpy.zeros(len(shown), dtype=bool)
  entries = []
  for offset, (feature, old) in enumerate(zip(features[first:], held)):
    graded = None
    if offset == 0 or everything or overlapsChange(sceneObject, feature, old, state, changed, edgeLength):
      graded = gradeFeature(sceneObject, feature, state, faces, edgeLength)
    new = old if graded is None else graded["offsets"]
    changed |= numpy.abs(new - old).max(axis=1) > bridgeHousing.heldTolerance
    state = state + new
    entries.append({"feature": feature, "held": old, "graded": graded})
  return {"object": sceneObject, "shown": shown, "entries": entries}


def passState(sceneObject, names):
  keys = sceneObject.data.shape_keys
  found = [keys.key_blocks.get(name) for name in names] if keys is not None else []
  return [{"pass": key.name, "muted": key.mute, "strength": round(key.value, 4)} for key in found if key is not None and (key.mute or key.value != 1.0)]


def writeDefinedPass(sceneObject, feature, offsets):
  passName = feature["passes"][0]
  keys = sceneObject.data.shape_keys
  if keys is None or keys.key_blocks.get(passName) is None:
    bridgePasses.addShapingPass(sceneObject.name, passName)
    keys = sceneObject.data.shape_keys
  bridgePasses.setPassDefinition(sceneObject, passName, feature["definition"])
  key = keys.key_blocks[passName]
  local = offsets @ bridgeMeshAccess.matrixArray(sceneObject.matrix_world.inverted())[:3, :3].T
  key.data.foreach_set("co", (bridgePasses.keyCoordinates(keys.reference_key) + local).astype(numpy.float32).ravel())
  key.mute, key.value = False, 1.0


def applyReplay(plan):
  """Write a planned replay into its passes; the pass active before stays active unless it is a defined pass, and then none is."""
  sceneObject = plan["object"]
  keys = sceneObject.data.shape_keys
  active = None if keys is None or sceneObject.active_shape_key in (None, keys.reference_key) else sceneObject.active_shape_key.name
  summaries, plotOutcome = [], None
  dressed = numpy.zeros(len(plan["shown"]), dtype=bool)
  for entry in plan["entries"]:
    graded = entry["graded"]
    if graded is None:
      continue
    feature = entry["feature"]
    restored = passState(sceneObject, feature["passes"])
    moved = numpy.abs(graded["offsets"] - entry["held"]).max(axis=1)
    if feature["kind"] in (routeKind, bridgeFacades.facadeKind):
      writeDefinedPass(sceneObject, feature, graded["offsets"])
      name = {feature["kind"]: feature["name"]}
      if feature["kind"] == bridgeFacades.facadeKind:
        dressed |= (moved > bridgeHousing.heldTolerance) | (numpy.abs(graded["offsets"]).max(axis=1) > bridgeHousing.heldTolerance)
    else:
      plotOutcome = bridgeHousing.applyGrading(graded["plotPlan"])
      name = {"plots": plotOutcome["gradedPlots"]}
    summary = name | {"movedVertices": int((moved > bridgeHousing.heldTolerance).sum()), "largestChange": round(float(moved.max(initial=0.0)), 2)}
    summaries.append(summary | ({"restoredFrom": restored} if restored else {}) | overriddenWork(sceneObject, feature, graded["offsets"], moved))
  keys = sceneObject.data.shape_keys
  if keys is not None:
    defined = set(bridgePasses.passDefinitions(sceneObject)) | {bridgeHousing.gradePassName(address) for address in bridgeHousing.gradedOn(sceneObject)}
    keep = active is not None and active not in defined and keys.key_blocks.get(active) is not None
    sceneObject.active_shape_key_index = list(keys.key_blocks).index(keys.key_blocks[active]) if keep else 0
  sceneObject.data.update()
  after, _ = bridgeMeshAccess.readVertexArrays(sceneObject)
  shaped = numpy.abs(after - plan["shown"]).max(axis=1) > bridgeHousing.heldTolerance
  # A facade's face stands vertical by design, so its cells, with no area in plan, are no fold.
  standing = numpy.zeros(len(after), dtype=bool)
  for entry in plan["entries"]:
    if entry["graded"] is not None and "standing" in entry["graded"]:
      standing |= entry["graded"]["standing"]
  folded = 0
  if shaped.any():
    bridgeShaping.triangulateAlongContours(sceneObject, after, shaped)
    loopTotals, loopVertices = bridgeMeshAccess.faceLoops(sceneObject)
    starts = numpy.cumsum(loopTotals) - loopTotals
    onFace = numpy.logical_and.reduceat(standing[loopVertices], starts)
    # Its returns, from the face's edge on the line back to the ground beside it, stand vertical as the face does: no fold either.
    edgeLength = bridgeShaping.medianEdgeLength(sceneObject, after, shaped)
    vertical = numpy.abs(bridgeMeshAccess.faceNormals(sceneObject, after)[:, 2]) <= bridgeShaping.degenerateAreaShare * edgeLength * edgeLength
    returns = numpy.logical_or.reduceat(standing[loopVertices], starts) & vertical
    folded = int((bridgeShaping.overturnedFaces(sceneObject, after, shaped) & ~onFace & ~returns).sum())
  remapDressed(sceneObject, dressed)
  return {"summaries": summaries, "plots": plotOutcome, "foldedFaces": folded, "dressed": dressed} | staleCaveReport(sceneObject)


def overriddenWork(sceneObject, feature, offsets, moved):
  """The hand passes a replayed feature overrode: where it moved the ground, each other pass that is not a defined one still holds an
  offset there, which the feature's new grading, made on the ground as all of them leave it, now takes back."""
  keys = sceneObject.data.shape_keys
  changed = moved > bridgeHousing.heldTolerance
  if keys is None or not changed.any():
    return {}
  defined = set(bridgePasses.passDefinitions(sceneObject)) | {bridgeHousing.gradePassName(address) for address in bridgeHousing.gradedOn(sceneObject)}
  reference = bridgePasses.keyCoordinates(keys.reference_key)
  overrode = []
  for key in list(keys.key_blocks)[1:]:
    if key.name in defined or key.name in feature["passes"]:
      continue
    held = numpy.abs(worldOffsets(sceneObject, key, reference)[changed]).max(axis=1)
    if held.max(initial=0.0) > bridgeHousing.heldTolerance:
      overrode.append({"pass": key.name, "vertices": int((held > bridgeHousing.heldTolerance).sum()), "largestOffset": round(float(held.max()), 2)})
  return {"overrodeHandWork": overrode} if overrode else {}


def groundVertices(sceneObject):
  return ~bridgeCaveData.caveMadeVertices(sceneObject)


def carried(sceneObject, mask, groundBefore):
  """A vertex mask carried across caves cut again: a cut makes and takes only its own ring and lining vertices, so the ground's own keep
  their order."""
  moved = numpy.zeros(len(sceneObject.data.vertices), dtype=bool)
  moved[numpy.flatnonzero(groundVertices(sceneObject))] = mask[numpy.flatnonzero(groundBefore)]
  return moved


def remapDressed(sceneObject, dressed):
  """Box-map again, from where they show now, the ground faces (not a cave's lining) with a vertex a facade moved, each material at its
  repeat as box projection reads it over the faces left alone: a dressed face and its returns carry their texture unstretched, and
  ground a facade let go of takes back the mapping it had. Returns how many faces it mapped."""
  mesh = sceneObject.data
  if not dressed.any() or not mesh.uv_layers:
    return 0
  loopTotals, loopVertices = bridgeMeshAccess.faceLoops(sceneObject)
  loopFaces = numpy.repeat(numpy.arange(len(loopTotals)), loopTotals)
  touched = numpy.logical_or.reduceat(dressed[loopVertices], numpy.cumsum(loopTotals) - loopTotals)
  if bridgeCaveData.holdsCaves(sceneObject):
    for name in bridgeCaveData.caves(sceneObject):
      touched &= bridgeCaveData.faceTags(sceneObject, name) != bridgeCaveData.liningFaceTag
  if not touched.any():
    return 0
  positions, _ = bridgeMeshAccess.readVertexArrays(sceneObject)
  _, faceNormals, _ = bridgeMeshAccess.readFaceArrays(sceneObject)
  slots = numpy.empty(len(loopTotals), dtype=numpy.int64)
  mesh.polygons.foreach_get("material_index", slots)
  mesh.calc_loop_triangles()
  triangleLoops = numpy.empty(len(mesh.loop_triangles) * 3, dtype=numpy.int64)
  mesh.loop_triangles.foreach_get("loops", triangleLoops)
  triangleLoops = triangleLoops.reshape(-1, 3)
  trianglePolygons = loopFaces[triangleLoops[:, 0]]
  uvs = numpy.empty(len(mesh.loops) * 2)
  mesh.uv_layers.active.data.foreach_get("uv", uvs)
  uvCorners = uvs.reshape(-1, 2)[triangleLoops]
  corners = positions[loopVertices[triangleLoops]]
  boxAreas = numpy.abs(numpy.cross(corners[:, 1] - corners[:, 0], corners[:, 2] - corners[:, 0])[numpy.arange(len(corners)), numpy.abs(faceNormals).argmax(axis=1)[trianglePolygons]]) / 2
  uvEdges = uvCorners[:, 1:] - uvCorners[:, :1]
  uvAreas = numpy.abs(uvEdges[:, 0, 0] * uvEdges[:, 1, 1] - uvEdges[:, 0, 1] * uvEdges[:, 1, 0]) / 2
  left = ~touched[trianglePolygons]
  repeats = {}
  for slot in numpy.unique(slots[touched]).tolist():
    measured = left & (slots[trianglePolygons] == slot)
    repeat = bridgeMeshAccess.worldUnitsPerRepeat(boxAreas[measured].sum(), uvAreas[measured].sum())
    if repeat is None:
      raise ValueError(f"Material slot {slot} of '{sceneObject.name}' has no mapped faces left alone by the facade to read its repeat from; projectUVs the ground first")
    repeats[slot] = repeat
  boxAxes = numpy.array([bridgeSurfacing.planarAxes(numpy.eye(3)[axis]) for axis in range(3)])
  selected = touched[loopFaces]
  projected = numpy.zeros((len(loopFaces), 2))
  loops = numpy.flatnonzero(selected)
  loopAxes = boxAxes[numpy.abs(faceNormals).argmax(axis=1)[loopFaces[loops]]]
  loopRepeats = numpy.array([repeats[int(slot)] for slot in slots[loopFaces[loops]]])
  projected[loops] = numpy.einsum("lj,laj->la", positions[loopVertices[loops]], loopAxes) / loopRepeats[:, None]
  bridgeAuthoring.writeProjection(sceneObject, selected, projected)
  return int(touched.sum())


def staleCaveReport(sceneObject):
  """The caves whose ground a grading moved, to cut again (editCave, or regradeTerrain for all): nothing for a mesh without caves."""
  return {"staleCaves": bridgeCaves.staleCaves(sceneObject)} if bridgeCaveData.holdsCaves(sceneObject) else {}


def planPlots(sceneObject, overrides, subjects, kept=None):
  """Plan grading the plots on a mesh (bridgeHousing.planGrading, overrides applied) on the ground the order defined passes were made
  in gives them, and the replay of every later defined pass whose ground that changes."""
  features = definedFeatures(sceneObject)
  index = next((position for position, feature in enumerate(features) if feature["kind"] == "plots"), len(features))
  features = features[:index] + [plotsFeature(sceneObject, overrides, subjects, kept)] + features[index + 1:]
  return planReplay(sceneObject, features, index, False)


def plotPlanOf(plan):
  return plan["entries"][0]["graded"]["plotPlan"]


def applyPlots(plan):
  """Write planned plot grading and its replay: bridgeHousing.applyGrading's outcome, with the later defined passes replayed."""
  applied = applyReplay(plan)
  return applied["plots"] | {"foldedFaces": applied["foldedFaces"], "replayed": applied["summaries"][1:]} | staleCaveReport(plan["object"])


def gradeRoute(objectName, name, points, width, widths, maximumGradeDegrees, cutBatterDegrees, fillBatterDegrees, landingLength):
  sceneObject = bridgeMeshAccess.requireEditableMesh(objectName, "gradeRoute")
  if not name.strip():
    raise ValueError("A route needs a name")
  definition = routeDefinition(points, width, widths, maximumGradeDegrees, cutBatterDegrees, fillBatterDegrees, landingLength)
  passName = routePassPrefix + name
  route = {"kind": routeKind, "name": name, "passes": [passName], "definition": definition}
  features = definedFeatures(sceneObject)
  index = next((position for position, feature in enumerate(features) if feature["passes"] == [passName] and feature["kind"] == routeKind), None)
  if index is None:
    keys = sceneObject.data.shape_keys
    if keys is not None and keys.key_blocks.get(passName) is not None:
      raise ValueError(f"'{objectName}' already has a shaping pass '{passName}' that is not a graded route; rename or remove it first")
    index = len(features)
    features.append(route)
  else:
    features[index] = route
  plan = planReplay(sceneObject, features, index, False)
  report = plan["entries"][0]["graded"]["report"]
  applied = applyReplay(plan)
  walk = bridgeReview.walkRoute(report["centerline"], walkSampleSpacing)
  return {"object": objectName, "route": name, "pass": passName} | report | {"foldedFaces": applied["foldedFaces"], "replayed": applied["summaries"][1:], "walk": walk} | staleCaveReport(sceneObject)


def regradeTerrain(objectName):
  sceneObject = bridgeMeshAccess.requireEditableMesh(objectName, "regradeTerrain")
  features = definedFeatures(sceneObject)
  if not features and not bridgeCaveData.holdsCaves(sceneObject):
    raise ValueError(f"'{objectName}' has no defined passes and no caves: no route graded on it (gradeRoute), no plot (gradePlot), no facade (dressFacade), and no cave (cutCave)")
  with bridgeCaves.restoredOnFailure(sceneObject):
    applied = applyReplay(planReplay(sceneObject, features, 0, True)) if features else {"summaries": [], "foldedFaces": 0, "dressed": numpy.zeros(len(sceneObject.data.vertices), dtype=bool)}
    groundBefore = groundVertices(sceneObject)
    refitted = bridgeCaves.refitStaleCaves(sceneObject)
    remapDressed(sceneObject, carried(sceneObject, applied["dressed"], groundBefore))
  return {"object": objectName, "replayed": applied["summaries"], "foldedFaces": applied["foldedFaces"], "refittedCaves": refitted, "staleStructures": bridgeStructures.staleStructures()}


def describeDefinedPasses(sceneObject):
  """Each defined feature on a mesh in the order it was made, with whether grading it again would move it (its ground moved since)."""
  features = definedFeatures(sceneObject)
  if not features:
    return []
  try:
    plan = planReplay(sceneObject, features, 0, True)
  except ValueError as error:
    return [{"passes": feature["passes"], "stale": True, "regradeRefused": str(error)} for feature in features]
  described = []
  for entry in plan["entries"]:
    feature = entry["feature"]
    moved = numpy.abs(entry["graded"]["offsets"] - entry["held"]).max(axis=1)
    named = {feature["kind"]: feature["name"]} if feature["kind"] in (routeKind, bridgeFacades.facadeKind) else {"plots": sorted(bridgeHousing.gradedOn(sceneObject))}
    described.append(named | {"passes": feature["passes"], "stale": bool((moved > bridgeHousing.heldTolerance).any()), "largestChange": round(float(moved.max(initial=0.0)), 3)})
  return described


def dressFacade(objectName, cave, end, faceAt, width, height, apron, blend, turnDegrees):
  sceneObject = bridgeCaves.requireTerrain(objectName)
  bridgeCaves.requireIntact(sceneObject)
  definition = bridgeFacades.facadeDefinition(sceneObject, cave, end, faceAt, width, height, apron, blend, turnDegrees)
  return {"object": objectName, "cave": cave, "end": end} | dress(sceneObject, definition)


def dress(sceneObject, definition):
  """Grade a facade's pass from its definition and replay what follows it, cut its cave again on the dressed ground, and map the faces
  it moved again, whole or not at all."""
  cave = definition["cave"]
  passName = bridgeFacades.passName(cave, definition["end"])
  facade = {"kind": bridgeFacades.facadeKind, "name": bridgeFacades.featureName(definition), "passes": [passName], "definition": definition}
  features = definedFeatures(sceneObject)
  index = next((position for position, feature in enumerate(features) if feature["passes"] == [passName]), None)
  if index is None:
    keys = sceneObject.data.shape_keys
    if keys is not None and keys.key_blocks.get(passName) is not None:
      raise ValueError(f"'{sceneObject.name}' already has a shaping pass '{passName}' that is not a facade; rename or remove it first")
    index = len(features)
    features.append(facade)
  else:
    features[index] = facade
  plan = planReplay(sceneObject, features, index, False)
  report = plan["entries"][0]["graded"]["report"]
  with bridgeCaves.restoredOnFailure(sceneObject):
    applied = applyReplay(plan)
    groundBefore = groundVertices(sceneObject)
    refit = bridgeCaves.recut(sceneObject, cave, {}) if cave in bridgeCaves.staleCaves(sceneObject) else None
    remapped = remapDressed(sceneObject, carried(sceneObject, applied["dressed"], groundBefore))
  return {"pass": passName} | report | {
    "foldedFaces": applied["foldedFaces"], "remappedFaces": remapped, "replayed": applied["summaries"][1:], "refit": refit, "staleCaves": bridgeCaves.staleCaves(sceneObject),
  }


def editCave(objectName, name, changes):
  """Cut a cave again with changes (bridgeCaves.recut), and dress its facades again where the cave they frame moved under them, refusing
  a change a facade would no longer frame."""
  sceneObject = bridgeCaves.requireTerrain(objectName)
  bridgeCaves.requireIntact(sceneObject)
  facades = [feature["definition"] for feature in definedFeatures(sceneObject) if feature["kind"] == bridgeFacades.facadeKind and feature["definition"]["cave"] == name]
  with bridgeCaves.restoredOnFailure(sceneObject):
    result = {"object": objectName, "cave": name, "changes": changes or {}} | bridgeCaves.recut(sceneObject, name, changes or {})
    refitted = []
    for definition in facades:
      try:
        current = bridgeFacades.redefined(sceneObject, definition)
      except ValueError as refusal:
        passName = bridgeFacades.passName(name, definition["end"])
        raise ValueError(
          f"The facade at the cave's {definition['end']} would no longer frame it: {refusal}. Dress it again to fit first (dressFacade), or take it back"
          f" (removeShapingPass '{passName}')"
        ) from refusal
      if current != definition:
        refitted.append({"end": definition["end"]} | dress(sceneObject, current))
  return result | {"refitFacades": refitted}


def removeShapingPass(objectName, name):
  """Remove a shaping pass (bridgePasses); a facade's pass takes its dressing back, cuts its cave again to fit the ground as it was, and
  maps the faces it had moved again."""
  sceneObject = bridgeMeshAccess.requireMeshObject(objectName)
  definition = bridgePasses.passDefinitions(sceneObject).get(name)
  if definition is None or definition["kind"] != bridgeFacades.facadeKind:
    return bridgePasses.removeShapingPass(objectName, name)
  bridgeCaves.requireIntact(sceneObject)
  keys = sceneObject.data.shape_keys
  undressed = numpy.abs(worldOffsets(sceneObject, keys.key_blocks[name], bridgePasses.keyCoordinates(keys.reference_key))).max(axis=1) > bridgeHousing.heldTolerance
  with bridgeCaves.restoredOnFailure(sceneObject):
    removed = bridgePasses.removeShapingPass(objectName, name)
    cave = definition["cave"]
    groundBefore = groundVertices(sceneObject)
    refit = bridgeCaves.recut(sceneObject, cave, {}) if cave in bridgeCaves.staleCaves(sceneObject) else None
    remapped = remapDressed(sceneObject, carried(sceneObject, undressed, groundBefore))
  return removed | {"refit": refit, "remappedFaces": remapped, "staleCaves": bridgeCaves.staleCaves(sceneObject)}


commands = {
  "gradeRoute": (gradeRoute, True),
  "regradeTerrain": (regradeTerrain, True),
  "dressFacade": (dressFacade, True),
  "editCave": (editCave, True),
  "removeShapingPass": (removeShapingPass, True),
}
