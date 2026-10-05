"""The lays of spans, each baked into one mesh as the client's whole-span bridges are: a bridge between two anchors (its deck straight,
sagging, or arched; planks across or a floor swept along it; stringers, posts and anchors, rails or ropes, trestle bents), a flight
between a foot and a head (equal risers, treads fitted to one step's run), and a walkway along points (level landings, graded deck
legs, listed stair legs laid by the flight code, posts or brackets into the rock). Registered with bridgeStructures. Runs under
Blender's Python."""
import math

import mathutils
import numpy

import bridgeKitGeometry
import bridgeStructures
from bridgeStructures import isNumber, requireKeys, requireNonNegative, requirePoint, requirePositive, requireSides, roundVector, size
from playerScale import eyeHeight, stepHeight, walkableNormalZ

bridgeKeys = ("kitPath", "start", "end", "width", "deck", "profile", "posts", "rails", "stringers", "bents", "sink", "maximumDeckDegrees", "collection")
stairsKeys = ("kitPath", "bottom", "top", "width", "tread", "riser", "stringers", "posts", "rails", "sink", "collection")
walkwayKeys = (
  "kitPath", "points", "width", "deck", "treads", "stairLegs", "riser", "posts", "brackets", "rails", "stringers", "sink", "maximumGradeDegrees",
  "collection",
)
steepestFlightDegrees = 45.0
maximumRiser = stepHeight
maximumTurnDegrees = 150.0
# Ground this far over a deck's underside meets it; a flight's end this far under what it stands on is buried in it.
meetingTolerance = 1e-3
buriedTolerance = 0.05
# A flight's or walkway's underside may rest in the ground it starts from this far in from its ends, in plan; past that it clears the
# ground.
endRest = 2.0
# Arc lengths along a bridge's deck are measured over this many pieces, and its swept parts and walk follow it every this far in plan.
profileSamples = 1024
followSpacing = 2.0
viewBack = 12.0
headBack = 3.0
up = numpy.array([0.0, 0.0, 1.0])


def plan(vector):
  return numpy.array([vector[0], vector[1], 0.0])


def unit(vector):
  return vector / numpy.linalg.norm(vector)


def leftOf(direction):
  return numpy.array([-direction[1], direction[0], 0.0])


def shifted(geometry, offset):
  return geometry | {"positions": geometry["positions"] + numpy.asarray(offset, dtype=numpy.float64)}


def stretchedPlaced(data, scales, origin, xAxis, yAxis):
  return bridgeKitGeometry.placed(bridgeKitGeometry.stretched(data, scales), bridgeKitGeometry.frameMatrix(origin, xAxis, yAxis))


def requireRepeatable(label, given, sides=True):
  """posts {piece, spacing, ...}: its spacing positive."""
  requirePositive(f"{label} spacing", given["spacing"])
  if sides:
    return requireSides(f"{label} sides", given.get("sides", "both"))
  return None


def railsSpec(laying, rails, posts):
  if rails is None:
    return None
  requireKeys("rails", rails, ("piece", "height"), ("sides",))
  if posts is None:
    raise ValueError("Rails run from post to post: give posts (rails without posts are refused)")
  data = laying.kit.piece(rails["piece"], ("rail", "ropeRail"), "rails")
  return {"data": data, "height": requirePositive("rails height", rails["height"]), "sides": requireSides("rails sides", rails.get("sides", "both")), "piece": rails["piece"]}


def railOut(rail, postDepth):
  """How far out from the deck's edge a rail runs: a bar's inner face on the edge, as the posts' are, so nothing lies between the deck
  and it; a rope through the posts."""
  return size(rail["data"], 1) / 2 if rail["data"]["record"]["kind"] == "rail" else postDepth / 2


def cutSquare(geometry, planes):
  """Cut off what lies in front of vertical planes at a span's open ends, so its end stands square there (a deck's sloped end face would
  be a steep sliver to step onto); the cut is closed in the material and mapping of the piece's own end."""
  inWorld = geometry | {"gradients": bridgeKitGeometry.faceGradients(geometry)}
  return bridgeKitGeometry.bisected(inWorld, planes, clearOuter=True)


def postBar(bake, data, center, direction, bottom, top, label):
  height = top - bottom
  if height <= 0:
    raise ValueError(f"{label} at {roundVector(center)} would run from {bottom:.2f} up to {top:.2f}: no length to stand")
  bake.add(stretchedPlaced(data, (1.0, 1.0, height / size(data, 2)), (center[0], center[1], bottom), direction, leftOf(direction)))
  return height


def railBars(bake, rail, tops):
  """Rail bars between consecutive points, their tops on them, or a rope swept along them, its top on them; returns the rail's length."""
  data, length = rail["data"], 0.0
  if data["record"]["kind"] == "ropeRail":
    cardHeight = size(data, 2)
    line = numpy.array(tops)
    bake.add(bridgeKitGeometry.swept(shifted(data, (0.0, 0.0, -cardHeight)), line, plumb=True))
    return float(numpy.linalg.norm(numpy.diff(line, axis=0), axis=1).sum())
  for first, second in zip(tops[:-1], tops[1:]):
    first, second = numpy.asarray(first), numpy.asarray(second)
    xAxis = unit(second - first)
    yAxis = leftOf(unit(plan(xAxis)))
    zAxis = numpy.cross(xAxis, yAxis)
    span = float(numpy.linalg.norm(second - first))
    origin = (first + second) / 2 - zAxis * size(data, 2)
    bake.add(stretchedPlaced(data, (span / size(data, 0), 1.0, 1.0), origin, xAxis, yAxis))
    length += span
  return length


def refuseUnderside(what, depth, where, along):
  raise ValueError(f"{what}'s underside meets the ground {depth:.2f} deep at {roundVector(where, 1)} ({along:.1f} along): it would run through the hill; raise or move its ends, or grade the ground (gradeRoute)")


class Profile:
  """A bridge's deck along its centerline: straight in plan between its ends, its height the chord's less a sag (or plus an arch) as a
  parabola, level across."""

  def __init__(self, start, end, bulge):
    self.start, self.end, self.bulge = start, end, bulge
    self.planVector = plan(end - start)
    self.span = float(numpy.linalg.norm(self.planVector))
    self.direction = self.planVector / self.span
    self.left = leftOf(self.direction)
    self.rise = float(end[2] - start[2])
    samples = numpy.linspace(0.0, 1.0, profileSamples + 1)
    points = numpy.array([self.pointAt(t) for t in samples])
    self.samples = samples
    self.arcs = numpy.concatenate([[0.0], numpy.cumsum(numpy.linalg.norm(numpy.diff(points, axis=0), axis=1))])
    self.length = float(self.arcs[-1])
    self.lowest = points[int(points[:, 2].argmin())]

  def heightAt(self, t):
    return float(self.start[2] + self.rise * t + 4 * self.bulge * t * (1 - t))

  def slopeAt(self, t):
    return (self.rise + 4 * self.bulge * (1 - 2 * t)) / self.span

  def pointAt(self, t):
    return numpy.array([self.start[0] + self.planVector[0] * t, self.start[1] + self.planVector[1] * t, self.heightAt(t)])

  def tangentAt(self, t):
    return unit(self.direction + up * self.slopeAt(t))

  def tAtArc(self, arc):
    return float(numpy.interp(arc, self.arcs, self.samples))

  def line(self):
    count = max(2, math.ceil(self.span / followSpacing))
    return numpy.array([self.pointAt(index / count) for index in range(count + 1)])


def profileBulge(profile):
  if profile is None:
    return 0.0, None
  if not isinstance(profile, dict) or len(profile) != 1 or next(iter(profile)) not in ("sag", "arch"):
    raise ValueError(f"profile is {{\"sag\": d}} (hanging d under the chord at mid-span), {{\"arch\": r}} (rising r over it), or null (straight), got {profile!r}")
  key, value = next(iter(profile.items()))
  amount = requirePositive(f"profile {key}", value)
  return (-amount if key == "sag" else amount), key


def requireEnd(laying, label, point):
  found = laying.lookups.footing(point)
  if found is None:
    raise ValueError(f"The {label} {roundVector(point, 2)} has no footing within {bridgeStructures.supportTolerance:g} below it; set it on the ground or a deck within that (gradeRoute the abutment)")
  hit = laying.lookups.surfaces.castWithNormal(mathutils.Vector(point) + mathutils.Vector((0.0, 0.0, bridgeStructures.castNudge)), mathutils.Vector((0.0, 0.0, 1.0)), bridgeStructures.playerHeight)
  if hit is not None:
    raise ValueError(f"The {label} {roundVector(point, 2)} has rock over it {hit[0].z - point[2]:.2f} up, within a player's height ({bridgeStructures.playerHeight:g})")
  return float(point[2] - found)


def layBridge(laying):
  definition = laying.definition
  start = numpy.array(requirePoint("start", definition["start"]))
  end = numpy.array(requirePoint("end", definition["end"]))
  width = requirePositive("width", definition["width"])
  sink = requireNonNegative("sink", definition["sink"])
  maximum = definition["maximumDeckDegrees"]
  steepestWalkable = math.degrees(math.acos(walkableNormalZ))
  if not isNumber(maximum) or not 0 < maximum <= steepestWalkable:
    raise ValueError(f"maximumDeckDegrees runs over 0 up to {steepestWalkable:.1f} (the steepest face players walk, playerScale), got {maximum!r}")
  deck = laying.kit.piece(definition["deck"], ("plank", "floor"), "deck")
  isPlank = deck["record"]["kind"] == "plank"
  endZone = size(deck, 1) if isPlank else size(deck, 2)
  span = float(numpy.linalg.norm(plan(end - start)))
  if span < 2 * endZone:
    raise ValueError(f"The ends are {span:.2f} apart in plan; a bridge spans at least two {'plank depths' if isPlank else 'deck thicknesses'} ({2 * endZone:g})")
  chord = float(numpy.linalg.norm(end - start))
  if width > chord:
    raise ValueError(f"width {width:g} is more than the chord between the ends ({chord:.2f})")
  bulge, profileKind = profileBulge(definition["profile"])
  profile = Profile(start, end, bulge)
  limit = math.tan(math.radians(maximum))
  slopes = {"start": profile.slopeAt(0.0), "end": profile.slopeAt(1.0)}
  steepestEnd = max(slopes, key=lambda key: abs(slopes[key]))
  steepest = math.degrees(math.atan(abs(slopes[steepestEnd])))
  if steepest > maximum + 1e-9:
    message = f"The deck is {steepest:.2f} degrees steep at its {steepestEnd}, over maximumDeckDegrees {maximum:g}"
    if profileKind is not None:
      fits = (span * limit - abs(profile.rise)) / 4
      message += f"; the largest {profileKind} that fits is {fits:.2f}" if fits > 0 else f"; the ends' heights alone ({abs(profile.rise):.2f} over {span:.2f}) exceed it"
    raise ValueError(message)
  gaps = {"start": requireEnd(laying, "start", start), "end": requireEnd(laying, "end", end)}
  stringers = None if definition["stringers"] is None else laying.kit.piece(definition["stringers"], ("beam",), "stringers")
  posts = definition["posts"]
  if posts is not None:
    requireKeys("posts", posts, ("piece", "spacing"), ("above", "sides"))
    requireRepeatable("posts", posts, sides=False)
    postData = laying.kit.piece(posts["piece"], ("post",), "posts")
    above = requireNonNegative("posts above", posts.get("above", 0.0))
  rail = railsSpec(laying, definition["rails"], posts)
  postSides = ()
  if posts is not None:
    # An open side's posts would stand as stubs at the deck's edge, so posts follow the rails unless given their own sides.
    postSides = requireSides("posts sides", posts["sides"]) if "sides" in posts else rail["sides"] if rail is not None else (1, -1)
    if rail is not None and not set(rail["sides"]) <= set(postSides):
      raise ValueError("Rails run from post to post: give posts on every side the rails run (posts sides)")
  bents = definition["bents"]
  if bents is not None:
    requireKeys("bents", bents, ("stations", "post"), ("beam",))
    if not isinstance(bents["stations"], list) or not bents["stations"] or not all(isNumber(station) for station in bents["stations"]):
      raise ValueError(f"bents stations is a list of plan distances from the start, got {bents['stations']!r}")
    for station in bents["stations"]:
      if not width < station < span - width:
        raise ValueError(f"Bent station {station:g} lies within a width ({width:g}) of an end or past it; bents stand between {width:g} and {span - width:.2f} along")
    legData = laying.kit.piece(bents["post"], ("post",), "bents post")
    beamData = None if bents.get("beam") is None else laying.kit.piece(bents["beam"], ("beam",), "bents beam")
  thickness = size(deck, 2)
  stringerHeight = 0.0 if stringers is None else size(stringers, 2)
  bake = bridgeKitGeometry.Bake()
  right = -profile.left
  line = profile.line()
  ends = [(start, -profile.direction), (end, profile.direction)]
  if isPlank:
    count = max(1, round(profile.length / size(deck, 1)))
    run = profile.length / count
    for index in range(count):
      t = profile.tAtArc((index + 0.5) * run)
      along = profile.tangentAt(t)
      origin = profile.pointAt(t) - numpy.cross(right, along) * thickness
      plank = stretchedPlaced(deck, (width / size(deck, 0), run / size(deck, 1), 1.0), origin, right, along)
      bake.add(cutSquare(plank, ends) if index in (0, count - 1) else plank)
    laidDeck = {"planks": count, "plankRun": round(run, 4), "covered": round(count * run, 4)}
  else:
    bake.add(cutSquare(bridgeKitGeometry.swept(shifted(deck, (0.0, 0.0, -thickness)), line, width / size(deck, 1)), ends))
    laidDeck = {"floor": definition["deck"]}
  if stringers is not None:
    for side in (1, -1):
      bake.add(cutSquare(bridgeKitGeometry.swept(shifted(stringers, (0.0, side * (width - size(stringers, 1)) / 2, -thickness - stringerHeight)), line), ends))
  least = None
  for station in list(numpy.arange(0.0, span, bridgeStructures.clearanceSpacing)) + [span]:
    t = station / span
    underside = profile.heightAt(t) - thickness
    for offset in (0.0, width / 2, -width / 2):
      point = profile.pointAt(t) + profile.left * offset
      point[2] = underside
      ground = laying.lookups.below(point)
      if ground is None:
        continue
      if min(station, span - station) > endZone:
        if ground > underside + meetingTolerance:
          refuseUnderside("The deck", ground - underside, point, station)
        if least is None or underside - ground < least["clearance"]:
          least = {"clearance": round(underside - ground, 3), "at": roundVector(point, 2), "along": round(float(station), 2)}
  postReport, railTops = [], {1: [], -1: []}
  if posts is not None:
    postDepth = size(postData, 1)
    intervals = max(1, math.ceil(span / posts["spacing"] - 1e-9))
    for index in range(intervals + 1):
      t = index / intervals
      deckTop = profile.heightAt(t)
      for side in postSides:
        center = profile.pointAt(t) + profile.left * side * (width + postDepth) / 2
        railed = rail is not None and side in rail["sides"]
        top = deckTop + (rail["height"] if railed else 0.0) + above
        anchor = index in (0, intervals)
        if anchor:
          ground = laying.lookups.below((center[0], center[1], deckTop + bridgeStructures.groundProbeLift))
          if ground is None:
            raise ValueError(f"The anchor post at {roundVector(center[:2], 2)} has no ground within {bridgeStructures.groundReach:g} below it")
          bottom = ground - sink
        else:
          bottom = deckTop - thickness - stringerHeight
        length = postBar(bake, postData, center, profile.direction, bottom, top, "The anchor post" if anchor else "A post")
        postReport.append({"at": roundVector(center[:2], 2), "side": "left" if side == 1 else "right", "anchor": anchor, "bottom": round(bottom, 3), "top": round(top, 3), "length": round(length, 3)})
        if railed:
          railAt = profile.pointAt(t) + profile.left * side * (width / 2 + railOut(rail, postDepth))
          railTops[side].append([railAt[0], railAt[1], deckTop + rail["height"]])
  railReport = None
  if rail is not None:
    length = 0.0
    for side in rail["sides"]:
      if rail["data"]["record"]["kind"] == "ropeRail":
        length += railBars(bake, rail, line + profile.left * side * (width + size(postData, 1)) / 2 + up * rail["height"])
      else:
        length += railBars(bake, rail, railTops[side])
    railReport = {"piece": rail["piece"], "sides": ["left" if side == 1 else "right" for side in rail["sides"]], "height": rail["height"], "length": round(length, 3)}
  bentReport = []
  if bents is not None:
    for station in bents["stations"]:
      t = station / span
      underside = profile.heightAt(t) - thickness - stringerHeight
      legTop = underside
      if beamData is not None:
        legTop = underside - size(beamData, 2)
        origin = profile.pointAt(t)
        origin[2] = legTop
        bake.add(stretchedPlaced(beamData, (width / size(beamData, 0), 1.0, 1.0), origin, profile.left, -profile.direction))
      legs = []
      for side in (1, -1):
        center = profile.pointAt(t) + profile.left * side * (width - size(legData, 1)) / 2
        ground = laying.lookups.below((center[0], center[1], legTop))
        if ground is None:
          raise ValueError(f"The bent leg at station {station:g} ({roundVector(center[:2], 2)}) has no ground within {bridgeStructures.groundReach:g} below it")
        legs.append(round(postBar(bake, legData, center, profile.direction, ground - sink, legTop, f"The bent leg at station {station:g}"), 3))
      bentReport.append({"station": float(station), "legs": legs, "beam": beamData is not None})
  matrix = bridgeKitGeometry.frameMatrix(start, profile.direction, profile.left)
  laying.addMesh(laying.name, bake, matrix)
  return {
    "span": round(span, 3), "chord": round(chord, 3), "deckLength": round(profile.length, 3),
    "endSlopes": {key: round(math.degrees(math.atan(value)), 3) for key, value in slopes.items()},
    "steepest": {"degrees": round(steepest, 3), "at": steepestEnd}, "lowestDeck": roundVector(profile.lowest, 3), "deck": laidDeck,
    "endFootingGaps": {key: round(value, 3) for key, value in gaps.items()}, "posts": postReport, "rails": railReport, "bents": bentReport,
    "leastClearance": least, "triangles": bake.triangles(),
  }


def bridgeWalkLine(definition):
  bulge, _ = profileBulge(definition["profile"])
  return Profile(numpy.array(definition["start"]), numpy.array(definition["end"]), bulge).line().tolist()


def bridgeViewSet(definition, groundHeight):
  bulge, _ = profileBulge(definition["profile"])
  start, end = numpy.array(definition["start"]), numpy.array(definition["end"])
  profile = Profile(start, end, bulge)
  middle = profile.pointAt(0.5)
  views = {
    "fromStart": bridgeStructures.standView(start - profile.direction * viewBack, start, profile.direction, groundHeight),
    "fromEnd": bridgeStructures.standView(end + profile.direction * viewBack, end, -profile.direction, groundHeight),
    "across": bridgeStructures.lookView(middle + profile.left * 0.9 * profile.span + up * 0.25 * profile.span, middle),
  }
  below = middle + profile.left * 0.4 * profile.span
  ground = groundHeight(below[0], below[1])
  if ground is not None:
    views["below"] = bridgeStructures.lookView((below[0], below[1], ground + eyeHeight), middle)
  return views


class Flight:
  """A straight flight from a foot to a head: equal risers, as many as keep each at most `riser`; each tread one step's run."""

  def __init__(self, bottom, top, riser, label):
    self.bottom, self.top = numpy.asarray(bottom, dtype=numpy.float64), numpy.asarray(top, dtype=numpy.float64)
    self.planVector = plan(self.top - self.bottom)
    self.run = float(numpy.linalg.norm(self.planVector))
    self.rise = float(self.top[2] - self.bottom[2])
    if self.run < 1e-6:
      raise ValueError(f"{label} has its foot and head at one place in plan")
    self.direction = self.planVector / self.run
    self.left = leftOf(self.direction)
    if self.rise < riser - 1e-9:
      raise ValueError(f"{label} rises {self.rise:.2f}, less than one riser ({riser:g}); a flight climbs at least one")
    self.count = math.ceil(self.rise / riser - 1e-9)
    self.riserHeight = self.rise / self.count
    self.tread = self.run / self.count
    self.pitch = math.degrees(math.atan2(self.rise, self.run))
    if self.pitch > steepestFlightDegrees + 1e-9:
      raise ValueError(f"{label} is {self.pitch:.2f} degrees steep, over {steepestFlightDegrees:g}: rising {self.rise:.2f} it needs a run of at least {self.rise / math.tan(math.radians(steepestFlightDegrees)):.2f} (it has {self.run:.2f})")

  def planPoint(self, along):
    return self.bottom + self.direction * along

  def lineHeight(self, along):
    return float(self.bottom[2] + self.rise * along / self.run)

  def lay(self, bake, width, treadData, stringerData):
    """The treads, each the plank piece across fitted to the width and one step's run, and stringers under their ends."""
    thickness = size(treadData, 2)
    right = -self.left
    for step in range(1, self.count + 1):
      origin = self.planPoint((step - 0.5) * self.tread)
      origin[2] = self.bottom[2] + step * self.riserHeight - thickness
      bake.add(stretchedPlaced(treadData, (width / size(treadData, 0), self.tread / size(treadData, 1), 1.0), origin, right, self.direction))
    if stringerData is not None:
      line = numpy.array([self.bottom - up * thickness, self.top - up * thickness])
      for side in (1, -1):
        bake.add(bridgeKitGeometry.swept(shifted(stringerData, (0.0, side * (width - size(stringerData, 1)) / 2, -size(stringerData, 2))), line))
    return {"risers": {"count": self.count, "height": round(self.riserHeight, 6)}, "runPerStep": round(self.tread, 6), "pitchDegrees": round(self.pitch, 3), "planLength": round(self.run, 3)}


def requireRiser(riser):
  if not isNumber(riser) or not 0 < riser <= maximumRiser:
    raise ValueError(f"riser must be above 0 and at most a step ({maximumRiser:g}), got {riser!r}")
  return float(riser)


class Station:
  """Where a post or bracket stands along an edge: its point on the edge, the walking surface's height there, the underside's, the
  way along, and the side's outward direction."""

  def __init__(self, point, height, underside, direction, outward, stretch, label, bracketed=False):
    self.point, self.height, self.underside, self.direction, self.outward, self.stretch, self.label = point, height, underside, direction, outward, stretch, label
    self.bracketed = bracketed
    self.offsetOut = outward


def layGroundedPosts(laying, bake, postData, stations, rail, side, sink, onlyRaised):
  """Posts at stations on one side, from sink under the ground up to the rail's top (or the underside without a rail); returns each
  post's report and the rail tops of the posts laid, in order, None where a station has no post."""
  reports, tops = [], []
  postDepth = size(postData, 1)
  for station in stations:
    center = station.point + station.offsetOut * postDepth / 2
    ground = laying.lookups.below((center[0], center[1], station.underside))
    if ground is None:
      raise ValueError(f"The post at {roundVector(center[:2], 2)} ({station.label}) has no ground within {bridgeStructures.groundReach:g} below it")
    railed = rail is not None and side in rail["sides"]
    top = station.height + rail["height"] if railed else station.underside
    bottom = ground - sink
    railAt = station.point + station.offsetOut * (railOut(rail, postDepth) if railed else 0.0)
    railTop = [railAt[0], railAt[1], station.height + rail["height"]] if railed else None
    # A rail runs the whole stretch, so a railed station keeps its post however low the walk runs there; where what it stands on
    # already reaches the rail (the post of a walkway the flight lands on), the rail meets that.
    if railed and ground >= top - size(rail["data"], 2):
      tops.append(railTop)
      continue
    if (onlyRaised and not railed and station.underside - ground <= bridgeStructures.supportTolerance) or top - bottom <= 0:
      tops.append(None)
      continue
    length = postBar(bake, postData, center, station.direction, bottom, top, "A post")
    reports.append({"at": roundVector(center[:2], 2), "side": "left" if side == 1 else "right", "bottom": round(bottom, 3), "top": round(top, 3), "length": round(length, 3)})
    tops.append(railTop)
  return reports, tops


def railRuns(tops, breaks=()):
  """Runs of consecutive railed posts, broken where a station has none and after the stations listed in breaks."""
  runs, current = [], []
  for index, top in enumerate(tops):
    if top is None:
      if len(current) > 1:
        runs.append(current)
      current = []
      continue
    current.append(top)
    if index in breaks:
      if len(current) > 1:
        runs.append(current)
      current = [top]
  if len(current) > 1:
    runs.append(current)
  return runs


def mitered(first, second):
  """The plan offset that stands a unit out from two edges at once, their outward directions first and second, at the corner they meet."""
  return (first + second) / (1.0 + float(first @ second))


def evenStations(length, spacing):
  intervals = max(1, math.ceil(length / spacing - 1e-9))
  return [length * index / intervals for index in range(intervals + 1)]


def requireStairsPosts(posts):
  requireKeys("posts", posts, ("piece", "spacing"), ("sides",))
  return requireRepeatable("posts", posts)


def flightClearance(laying, flight, width, underside, label):
  """The flight's underside probed every clearanceSpacing along its edges and middle, refused where it meets the ground past endRest from
  its ends; returns the least clearance there."""
  least = None
  for along in list(numpy.arange(0.0, flight.run, bridgeStructures.clearanceSpacing)) + [flight.run]:
    for offset in (0.0, width / 2, -width / 2):
      point = flight.planPoint(along) + flight.left * offset
      point[2] = flight.lineHeight(along) - underside
      ground = laying.lookups.below(point)
      if ground is None or min(along, flight.run - along) <= endRest:
        continue
      if ground > point[2] + meetingTolerance:
        refuseUnderside(label, ground - point[2], point, along)
      if least is None or point[2] - ground < least["clearance"]:
        least = {"clearance": round(point[2] - ground, 3), "at": roundVector(point, 2)}
  return least


def layStairs(laying):
  definition = laying.definition
  bottom = numpy.array(requirePoint("bottom", definition["bottom"]))
  top = numpy.array(requirePoint("top", definition["top"]))
  width = requirePositive("width", definition["width"])
  sink = requireNonNegative("sink", definition["sink"])
  riser = requireRiser(definition["riser"])
  flight = Flight(bottom, top, riser, "The flight")
  treadData = laying.kit.piece(definition["tread"], ("plank",), "tread")
  stringerData = None if definition["stringers"] is None else laying.kit.piece(definition["stringers"], ("beam",), "stringers")
  posts = definition["posts"]
  sides = () if posts is None else requireStairsPosts(posts)
  postData = None if posts is None else laying.kit.piece(posts["piece"], ("post",), "posts")
  rail = railsSpec(laying, definition["rails"], posts)
  gaps = {}
  for label, point in (("foot", bottom), ("head", top)):
    found = laying.lookups.footing(point)
    if found is None:
      raise ValueError(f"The flight's {label} {roundVector(point, 2)} has no footing within {bridgeStructures.supportTolerance:g} below it")
    if found - point[2] > buriedTolerance:
      found = round(found, 2) + 0.0
      raise ValueError(
        f"The flight's {label} {roundVector(point, 2)} lies {found - point[2]:.2f} under what it stands on there (the top of '{laying.lookups.lastOwner}' at"
        f" {found:.2f}), so its {'top tread would run into its side' if label == 'head' else 'first treads would lie in it'}; set the {label} on that top (z {found:.2f})"
      )
    gaps[label] = round(float(point[2] - found), 3)
  thickness = size(treadData, 2)
  lastMiddle = flight.planPoint(flight.run - flight.tread / 2)
  underLast = laying.lookups.below((lastMiddle[0], lastMiddle[1], top[2] - thickness / 2))
  if underLast is not None and underLast > top[2] - thickness + meetingTolerance:
    raise ValueError(
      f"The flight's top tread would lie inside what its head stands on ('{laying.lookups.lastOwner}', its top at {round(underLast, 2) + 0.0:.2f}), the two tops one surface:"
      f" the head runs onto it; set the head at its edge, where the flight meets it"
    )
  bake = bridgeKitGeometry.Bake()
  report = flight.lay(bake, width, treadData, stringerData)
  least = flightClearance(laying, flight, width, size(treadData, 2), "The flight")
  underside = size(treadData, 2) + (0.0 if stringerData is None else size(stringerData, 2))
  legReport, railLength = [], 0.0
  for side in sides:
    stations = [
      Station(flight.planPoint(along) + flight.left * side * width / 2, flight.lineHeight(along), flight.lineHeight(along) - underside, flight.direction, flight.left * side, "flight", f"{along:.1f} along")
      for along in evenStations(flight.run, posts["spacing"])
    ]
    reports, tops = layGroundedPosts(laying, bake, postData, stations, rail, side, sink, True)
    legReport += reports
    if rail is not None and side in rail["sides"]:
      for run in railRuns(tops):
        railLength += railBars(bake, rail, run)
  matrix = bridgeKitGeometry.frameMatrix(bottom, flight.direction, flight.left)
  laying.addMesh(laying.name, bake, matrix)
  return report | {
    "footingGaps": gaps, "legs": legReport, "rails": None if rail is None else {"piece": rail["piece"], "length": round(railLength, 3)},
    "leastClearance": least, "triangles": bake.triangles(),
  }


def stairsWalkLine(definition):
  return [definition["bottom"], definition["top"]]


def stairsViewSet(definition, groundHeight):
  """From the foot looking up it, from just behind the head looking down it (from further back a steep flight hides behind its top
  tread), and from the side its eye stands in the open on, not inside the hill the flight climbs beside."""
  bottom, top = numpy.array(definition["bottom"]), numpy.array(definition["top"])
  direction = unit(plan(top - bottom))
  run = float(numpy.linalg.norm(plan(top - bottom)))
  rise = float(top[2] - bottom[2])
  middle = (bottom + top) / 2
  reach = max(run, rise) * 1.2 + 10
  eyes = [middle + leftOf(direction) * side * reach for side in (1, -1)]
  grounds = [groundHeight(eye[0], eye[1]) for eye in eyes]
  buried = [-math.inf if ground is None else ground - eye[2] for ground, eye in zip(grounds, eyes)]
  headPitch = -math.degrees(math.atan2(eyeHeight + rise / 2, headBack + run / 2))
  return {
    "fromFoot": bridgeStructures.standView(bottom - direction * viewBack, bottom, direction, groundHeight, round(math.degrees(math.atan2(rise, run + viewBack)), 2)),
    "fromHead": bridgeStructures.standView(top + direction * headBack, top, -direction, groundHeight, round(headPitch, 2)),
    "side": bridgeStructures.lookView(eyes[int(numpy.argmin(buried))], middle),
  }


def cross2(first, second):
  return float(first[0] * second[1] - first[1] * second[0])


def convexHull(points):
  """The convex hull of plan points, counterclockwise."""
  unique = []
  for point in sorted((float(point[0]), float(point[1])) for point in points):
    if not unique or math.hypot(point[0] - unique[-1][0], point[1] - unique[-1][1]) > 1e-6:
      unique.append(point)
  if len(unique) < 3:
    return [numpy.array(point) for point in unique]

  def half(ordered):
    chain = []
    for point in ordered:
      while len(chain) >= 2 and cross2(numpy.subtract(chain[-1], chain[-2]), numpy.subtract(point, chain[-2])) <= 1e-12:
        chain.pop()
      chain.append(point)
    return chain

  lower, upper = half(unique), half(list(reversed(unique)))
  return [numpy.array(point) for point in lower[:-1] + upper[:-1]]


def lineMeeting(point, direction, otherPoint, otherDirection):
  """Where two plan lines meet."""
  denominator = cross2(direction, otherDirection)
  along = cross2(numpy.subtract(otherPoint, point), otherDirection) / denominator
  return numpy.asarray(point) + numpy.asarray(direction) * along


class WalkwayPlan:
  """A walkway's legs and landings in plan: a level landing at each inner point spanning both legs' end edges, the legs stopping half a
  width short of it."""

  def __init__(self, points, width):
    self.points = [numpy.array(point, dtype=numpy.float64) for point in points]
    self.width = width
    half = width / 2
    count = len(self.points)
    self.legs = []
    for index in range(count - 1):
      first, second = self.points[index], self.points[index + 1]
      length = float(numpy.linalg.norm(plan(second - first)))
      if length < 1e-6:
        raise ValueError(f"Points {index} and {index + 1} stand at one place in plan")
      direction = plan(second - first) / length
      startTrim = half if index > 0 else 0.0
      endTrim = half if index + 1 < count - 1 else 0.0
      laid = length - startTrim - endTrim
      if laid <= 1e-6:
        raise ValueError(f"Leg {index} is {length:.2f} long in plan, no longer than the {startTrim + endTrim:g} its landings take (half the width at each turn)")
      start, end = first + direction * startTrim, second - direction * endTrim
      start[2], end[2] = first[2], second[2]
      self.legs.append({"index": index, "direction": direction, "left": leftOf(direction), "length": length, "laid": laid, "start": start, "end": end})
    self.landings = []
    for index in range(1, count - 1):
      incoming, outgoing = self.legs[index - 1], self.legs[index]
      turn = math.degrees(math.atan2(cross2(incoming["direction"], outgoing["direction"]), float(incoming["direction"] @ outgoing["direction"])))
      if abs(turn) > maximumTurnDegrees:
        raise ValueError(f"The walkway turns {abs(turn):.1f} degrees at point {index}, over {maximumTurnDegrees:g}")
      point = self.points[index]
      corners = {}
      for side in (1, -1):
        corners[("in", side)] = plan(incoming["end"]) + incoming["left"] * side * half
        corners[("out", side)] = plan(outgoing["start"]) + outgoing["left"] * side * half
      outer = None
      if abs(turn) > 1e-6:
        outer = -1 if turn > 0 else 1
        corners["miter"] = numpy.append(lineMeeting(corners[("in", outer)][:2], incoming["direction"][:2], corners[("out", outer)][:2], outgoing["direction"][:2]), 0.0)
      self.landings.append({
        "index": index, "point": point, "height": float(point[2]), "turn": turn, "outer": outer, "corners": corners,
        "outline": convexHull([corner[:2] for corner in corners.values()]),
      })

  def landingAt(self, pointIndex):
    return self.landings[pointIndex - 1]

  def edge(self, side):
    """The walkway's edge on one side as stretches along travel, legs and the landings between, each a list of segments (start, its
    height, end, its height, the legs whose edge it carries on: a landing's outer corner turns from the incoming leg's to the
    outgoing leg's)."""
    stretches = []
    half = self.width / 2
    for leg in self.legs:
      if leg["index"] > 0:
        landing = self.landingAt(leg["index"])
        corners, height = landing["corners"], landing["height"]
        if landing["outer"] == side:
          segments = [
            (corners[("in", side)], height, corners["miter"], height, {leg["index"] - 1}),
            (corners["miter"], height, corners[("out", side)], height, {leg["index"]}),
          ]
        else:
          segments = [(corners[("in", side)], height, corners[("out", side)], height, {leg["index"] - 1, leg["index"]})]
        stretches.append({"kind": "landing", "index": landing["index"], "segments": segments})
      offset = leg["left"] * side * half
      stretches.append({"kind": "leg", "index": leg["index"], "segments": [(plan(leg["start"]) + offset, float(leg["start"][2]), plan(leg["end"]) + offset, float(leg["end"][2]), {leg["index"]})]})
    return stretches


def requireWalkwayPoints(points):
  if not isinstance(points, list) or len(points) < 2:
    raise ValueError(f"points is at least two [x, y, z] points, got {points!r}")
  return [requirePoint(f"point {index}", point) for index, point in enumerate(points)]


def landingDeck(bake, deck, landing, incoming, thickness):
  """A landing's deck laid across the incoming leg's direction, cut to the landing's outline."""
  direction, right = incoming["direction"], -incoming["left"]
  outline = landing["outline"]
  center = plan(landing["point"])
  along = [float((numpy.append(corner, 0.0) - center) @ direction) for corner in outline]
  across = [float((numpy.append(corner, 0.0) - center) @ right) for corner in outline]
  low, high = min(along), max(along)
  acrossLow, acrossHigh = min(across), max(across)
  isPlank = deck["record"]["kind"] == "plank"
  count = max(1, round((high - low) / size(deck, 1))) if isPlank else 1
  run = (high - low) / count
  for index in range(count):
    middle = low + (index + 0.5) * run
    origin = center + direction * middle + right * (acrossLow + acrossHigh) / 2
    origin[2] = landing["height"] - thickness
    if isPlank:
      scales, xAxis, yAxis = ((acrossHigh - acrossLow) / size(deck, 0), run / size(deck, 1), 1.0), right, direction
    else:
      scales, xAxis, yAxis = (run / size(deck, 0), (acrossHigh - acrossLow) / size(deck, 1), 1.0), direction, incoming["left"]
    matrix = bridgeKitGeometry.frameMatrix(origin, xAxis, yAxis)
    inverse = numpy.linalg.inv(matrix)
    planes = []
    for corner, following in zip(outline, outline[1:] + outline[:1]):
      edge = numpy.append(following - corner, 0.0)
      outward = unit(numpy.array([edge[1], -edge[0], 0.0]))
      planes.append((inverse[:3, :3] @ numpy.append(corner, origin[2]) + inverse[:3, 3], inverse[:3, :3] @ outward))
    piece = bridgeKitGeometry.stretched(deck, scales)
    clipping = [plane for plane in planes if ((piece["positions"] - plane[0]) @ plane[1] > 1e-6).any()]
    if clipping:
      piece = bridgeKitGeometry.bisected(piece, clipping, clearOuter=True)
    if len(piece["loopTotals"]):
      bake.add(bridgeKitGeometry.placed(piece, matrix))
  return count


def legDeck(bake, deck, leg, width, thickness, ends):
  """A deck leg: planks across, or a floor swept along it; cut square at the walkway's open ends among them."""
  start, end = leg["start"], leg["end"]
  if deck["record"]["kind"] == "plank":
    length = float(numpy.linalg.norm(end - start))
    count = max(1, round(length / size(deck, 1)))
    along = unit(end - start)
    right = -leg["left"]
    for index in range(count):
      center = start + (end - start) * (index + 0.5) / count
      plank = stretchedPlaced(deck, (width / size(deck, 0), length / count / size(deck, 1), 1.0), center - numpy.cross(right, along) * thickness, right, along)
      bake.add(cutSquare(plank, ends) if ends and index in (0, count - 1) else plank)
    return count
  floor = bridgeKitGeometry.swept(shifted(deck, (0.0, 0.0, -thickness)), numpy.array([start, end]), width / size(deck, 1))
  bake.add(cutSquare(floor, ends) if ends else floor)
  return 1


def layWalkway(laying):
  definition = laying.definition
  points = requireWalkwayPoints(definition["points"])
  width = requirePositive("width", definition["width"])
  sink = requireNonNegative("sink", definition["sink"])
  riser = requireRiser(definition["riser"])
  maximum = definition["maximumGradeDegrees"]
  if not isNumber(maximum) or not 0 < maximum < 90:
    raise ValueError(f"maximumGradeDegrees runs over 0 up to 90, got {maximum!r}")
  stairLegs = definition["stairLegs"] or []
  if not isinstance(stairLegs, list) or not all(isinstance(index, int) and not isinstance(index, bool) and 0 <= index < len(points) - 1 for index in stairLegs):
    raise ValueError(f"stairLegs lists leg indices 0 to {len(points) - 2} (leg i runs from point i to point i + 1), got {stairLegs!r}")
  walkway = WalkwayPlan(points, width)
  deck = None if definition["deck"] is None else laying.kit.piece(definition["deck"], ("plank", "floor"), "deck")
  treadData = None if definition["treads"] is None else laying.kit.piece(definition["treads"], ("plank",), "treads")
  stringerData = None if definition["stringers"] is None else laying.kit.piece(definition["stringers"], ("beam",), "stringers")
  limit = math.tan(math.radians(maximum))
  flights, legReport = {}, []
  for leg in walkway.legs:
    rise = float(leg["end"][2] - leg["start"][2])
    grade = math.degrees(math.atan2(abs(rise), leg["laid"]))
    if leg["index"] in stairLegs:
      if treadData is None:
        raise ValueError(f"Leg {leg['index']} is a stair leg: give treads (a plank piece)")
      foot, head = (leg["start"], leg["end"]) if rise >= 0 else (leg["end"], leg["start"])
      flights[leg["index"]] = Flight(foot, head, riser, f"Stair leg {leg['index']}")
      continue
    if grade > maximum + 1e-9:
      needed = abs(rise) / limit + (leg["length"] - leg["laid"])
      raise ValueError(f"Leg {leg['index']} grades {grade:.2f} degrees, over maximumGradeDegrees {maximum:g}: rising {abs(rise):.2f} it needs a run of {needed:.2f} between its points (it has {leg['length']:.2f}), or list it in stairLegs")
    if deck is None:
      raise ValueError(f"Leg {leg['index']} is a deck leg: give deck (a plank or floor piece)")
  if walkway.landings and deck is None:
    raise ValueError("Landings are decked: give deck (a plank or floor piece)")
  posts = definition["posts"]
  postSides = ()
  if posts is not None:
    postSides = requireStairsPosts(posts)
    postData = laying.kit.piece(posts["piece"], ("post",), "posts")
  brackets = definition["brackets"]
  bracketLegs, bracketSide = set(), None
  if brackets is not None:
    requireKeys("brackets", brackets, ("piece", "side", "reach", "legs"))
    if brackets["side"] not in ("left", "right"):
      raise ValueError(f"brackets side is \"left\" or \"right\" of travel, got {brackets['side']!r}")
    bracketSide = 1 if brackets["side"] == "left" else -1
    requirePositive("brackets reach", brackets["reach"])
    if not isinstance(brackets["legs"], list) or not all(isinstance(index, int) and not isinstance(index, bool) and 0 <= index < len(walkway.legs) for index in brackets["legs"]):
      raise ValueError(f"brackets legs lists leg indices 0 to {len(walkway.legs) - 1}, got {brackets['legs']!r}")
    if posts is None:
      raise ValueError("Brackets stand at the post stations: give posts (its spacing; its sides may leave the bracketed side out)")
    bracketLegs = set(brackets["legs"])
    bracketData = laying.kit.piece(brackets["piece"], ("beam",), "brackets")
  rail = railsSpec(laying, definition["rails"], posts)
  deckThickness = 0.0 if deck is None else size(deck, 2)
  treadThickness = 0.0 if treadData is None else size(treadData, 2)
  stringerHeight = 0.0 if stringerData is None else size(stringerData, 2)

  def undersideBelow(stretch, stringersToo=True):
    """How far under the walking surface a stretch's underside lies: its deck's or treads' (clearance), with the stringers under it
    (what posts and brackets hold up)."""
    if stretch["kind"] == "leg" and stretch["index"] in flights:
      return treadThickness + (stringerHeight if stringersToo else 0.0)
    return deckThickness + (stringerHeight if stretch["kind"] == "leg" and stringersToo else 0.0)

  gaps = {}
  for label, point in (("start", points[0]), ("end", points[-1])):
    found = laying.lookups.footing(point)
    gaps[label] = None if found is None else round(float(point[2] - found), 3)
  bake = bridgeKitGeometry.Bake()
  for leg in walkway.legs:
    if leg["index"] in flights:
      flight = flights[leg["index"]]
      legReport.append({"leg": leg["index"], "laid": "flight", "gradeDegrees": round(flight.pitch, 4)} | flight.lay(bake, width, treadData, stringerData))
      continue
    ends = [(leg["start"], -leg["direction"])] if leg["index"] == 0 else []
    ends += [(leg["end"], leg["direction"])] if leg["index"] == len(walkway.legs) - 1 else []
    planks = legDeck(bake, deck, leg, width, deckThickness, ends)
    if stringerData is not None:
      line = numpy.array([leg["start"], leg["end"]])
      for side in (1, -1):
        bake.add(bridgeKitGeometry.swept(shifted(stringerData, (0.0, side * (width - size(stringerData, 1)) / 2, -deckThickness - stringerHeight)), line))
    rise = float(leg["end"][2] - leg["start"][2])
    legReport.append({"leg": leg["index"], "laid": "deck", "planks": planks, "gradeDegrees": round(math.degrees(math.atan2(rise, leg["laid"])), 4)})
  for leg, entry in zip(walkway.legs, legReport):
    entry |= {"from": roundVector(points[leg["index"]]), "to": roundVector(points[leg["index"] + 1]), "planLength": round(leg["length"], 3), "laidLength": round(leg["laid"], 3)}
  for landing in walkway.landings:
    landingDeck(bake, deck, landing, walkway.legs[landing["index"] - 1], deckThickness)
  endLines = [(plan(walkway.points[0]), walkway.legs[0]["left"]), (plan(walkway.points[-1]), walkway.legs[-1]["left"])]

  def nearEnd(point):
    """Within endRest of either end's edge across the deck, where a deck may rest in the ground it starts from."""
    for center, left in endLines:
      offset = plan(point) - center
      across = numpy.clip(offset @ left, -width / 2, width / 2)
      if numpy.linalg.norm(offset - left * across) <= endRest:
        return True
    return False

  for leg in walkway.legs:
    below = undersideBelow({"kind": "leg", "index": leg["index"]}, False)
    if leg["index"] in flights:
      flightClearance(laying, flights[leg["index"]], width, below, f"Stair leg {leg['index']}")
      continue
    for along in list(numpy.arange(0.0, leg["laid"], bridgeStructures.clearanceSpacing)) + [leg["laid"]]:
      for offset in (0.0, width / 2, -width / 2):
        point = leg["start"] + (leg["end"] - leg["start"]) * along / leg["laid"] + leg["left"] * offset
        point[2] -= below
        ground = laying.lookups.below(point)
        if ground is not None and not nearEnd(point) and ground > point[2] + meetingTolerance:
          refuseUnderside(f"Leg {leg['index']}", ground - point[2], point, along)
  for landing in walkway.landings:
    for corner in [numpy.append(corner, 0.0) for corner in landing["outline"]] + [plan(landing["point"])]:
      point = corner.copy()
      point[2] = landing["height"] - deckThickness
      ground = laying.lookups.below(point)
      if ground is not None and ground > point[2] + meetingTolerance:
        refuseUnderside(f"The landing at point {landing['index']}", ground - point[2], point, 0.0)
  postReport, bracketReport, railLength = [], [], 0.0
  spacing = None if posts is None else posts["spacing"]
  for side in (1, -1):
    stations, breaks = [], set()
    for stretch in walkway.edge(side):
      isFlight = stretch["kind"] == "leg" and stretch["index"] in flights
      below = undersideBelow(stretch)
      for first, firstHeight, second, secondHeight, carried in stretch["segments"]:
        bracketed = side == bracketSide and bool(carried & bracketLegs)
        length = float(numpy.linalg.norm(second - first))
        if length < 1e-6:
          continue
        direction = (second - first) / length
        outward = leftOf(direction) * side
        for along in evenStations(length, spacing if spacing is not None else bridgeStructures.clearanceSpacing):
          point = first + direction * along
          height = firstHeight + (secondHeight - firstHeight) * along / length
          label = f"{stretch['kind']} {stretch['index']}"
          if stations and numpy.linalg.norm(stations[-1].point - point) < 1e-6:
            if isFlight or (stations[-1].stretch == "flight") != isFlight:
              breaks.add(len(stations) - 1)
            # Where the edge turns, a post and its rail stand out along the miter, on both edges' offset lines at once.
            stations[-1].offsetOut = mitered(stations[-1].outward, outward)
            continue
          stations.append(Station(point, height, height - below, direction, outward, "flight" if isFlight else stretch["kind"], label, bracketed))
      if isFlight and stations:
        breaks.add(len(stations) - 1)
    heldByPosts = posts is not None and side in postSides
    for station in stations:
      if station.bracketed or heldByPosts:
        continue
      ground = laying.lookups.below((station.point[0], station.point[1], station.underside))
      if ground is not None and station.underside - ground > bridgeStructures.supportTolerance and not nearEnd(station.point):
        raise ValueError(f"The {station.label} stands {station.underside - ground:.2f} over the ground at {roundVector(station.point[:2], 2)} with its {'left' if side == 1 else 'right'} edge held by neither posts nor brackets")
    if posts is None:
      continue
    tops = []
    for index, station in enumerate(stations):
      if station.bracketed:
        tops.append(None)
        edgeGround = laying.lookups.below((station.point[0], station.point[1], station.underside))
        if edgeGround is not None and station.underside - edgeGround <= size(bracketData, 2):
          continue
        origin = (station.point[0], station.point[1], station.underside - size(bracketData, 2) / 2)
        reach = laying.lookups.beside(origin, station.outward, brackets["reach"])
        if reach is None:
          raise ValueError(f"No rock within reach {brackets['reach']:g} beside the bracket station at {roundVector(station.point[:2], 2)} ({station.label}, {'left' if side == 1 else 'right'})")
        length = width + reach + sink
        center = station.point - station.outward * width + station.outward * length / 2
        center[2] = station.underside - size(bracketData, 2)
        bake.add(stretchedPlaced(bracketData, (length / size(bracketData, 0), 1.0, 1.0), center, station.outward, numpy.cross(up, station.outward)))
        bracketReport.append({"at": roundVector(station.point[:2], 2), "station": station.label, "reach": round(reach, 3), "length": round(length, 3)})
        continue
      if side not in postSides:
        tops.append(None)
        continue
      reports, stationTops = layGroundedPosts(laying, bake, postData, [station], rail, side, sink, False)
      postReport += reports
      tops += stationTops
    if rail is not None and side in rail["sides"]:
      for run in railRuns(tops, breaks):
        railLength += railBars(bake, rail, run)
  start = numpy.array(points[0])
  first = walkway.legs[0]
  laying.addMesh(laying.name, bake, bridgeKitGeometry.frameMatrix(start, first["direction"], first["left"]))
  lengths = [post["length"] for post in postReport]
  return {
    "legs": legReport,
    "landings": [{"point": landing["index"], "height": round(landing["height"], 3), "turnDegrees": round(landing["turn"], 3), "outline": [roundVector(corner, 3) for corner in landing["outline"]]} for landing in walkway.landings],
    "posts": {"count": len(postReport), "longest": max(lengths, default=None), "shortest": min(lengths, default=None), "each": postReport},
    "brackets": bracketReport, "rails": None if rail is None else {"piece": rail["piece"], "length": round(railLength, 3)},
    "endFootingGaps": gaps, "triangles": bake.triangles(),
  }


def walkwayWalkLine(definition):
  return definition["points"]


def walkwayViewSet(definition, groundHeight):
  """From each end, and each landing from the side its brackets do not take (else from the outside of its turn)."""
  points = [numpy.array(point) for point in definition["points"]]
  width = definition["width"]
  first, last = unit(plan(points[1] - points[0])), unit(plan(points[-1] - points[-2]))
  views = {
    "fromStart": bridgeStructures.standView(points[0] - first * viewBack, points[0], first, groundHeight),
    "fromEnd": bridgeStructures.standView(points[-1] + last * viewBack, points[-1], -last, groundHeight),
  }
  brackets = definition["brackets"]
  for index in range(1, len(points) - 1):
    incoming, outgoing = unit(plan(points[index] - points[index - 1])), unit(plan(points[index + 1] - points[index]))
    middle = unit(incoming + outgoing)
    side = leftOf(middle) if brackets is None or brackets["side"] == "right" else -leftOf(middle)
    if brackets is None and abs(cross2(incoming, outgoing)) > 1e-6:
      side = leftOf(middle) * (-1 if cross2(incoming, outgoing) > 0 else 1)
    views[f"landing{index}"] = bridgeStructures.lookView(points[index] + side * (3 * width + 20) + up * 4, points[index])
  return views


bridgeStructures.registerKind("bridge", bridgeKeys, layBridge, bridgeWalkLine, bridgeViewSet)
bridgeStructures.registerKind("stairs", stairsKeys, layStairs, stairsWalkLine, stairsViewSet)
bridgeStructures.registerKind("walkway", walkwayKeys, layWalkway, walkwayWalkLine, walkwayViewSet, indexKeys=("stairLegs", "legs"))
