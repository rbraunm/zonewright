"""A cave's runs (bridgeCaves): its main path and each branch as the author gave them, worked out into centerlines in plan with a floor
height at every point (given, set by a chosen grade, taken evenly between given ones, or the ground at an open end), level landings at
the bends the author names; the floor strokes kept with them (a level way, a pad, rough ground) and the relief they give the floor; and
the share of scene light each run takes along its length. Geometry only: nothing here reads or writes a mesh. Runs under Blender's
Python."""
import math

import numpy

import bridgeNoise

mainRun = "main"
levelTolerance = 0.01
strokeKinds = ("level", "pad", "rough")
strokeKeys = {
  "level": ({"kind", "name", "run", "from", "to", "across"}, {"material"}),
  "pad": ({"kind", "name", "run", "from", "to", "across", "edge"}, {"material", "top", "rise"}),
  "rough": ({"kind", "name", "run", "outline", "rise", "edge", "breakup"}, {"material", "bank"}),
}
runKeys = ({"path", "widths", "heights"}, {"grades", "landings", "daylight"})
branchKeys = ({"name", "from", "path", "widths", "heights"}, {"grades", "landings", "daylight", "overlook"})
roughOctaves = 3
roughRoughness = 0.5
# Where a rough stroke's edge wanders is read from its noise this far off (in noise units) from where its lumps are, so the two are
# independent.
wanderOffset = numpy.array([733.1, -419.7, 0.0])
# Two floor breaks (a stroke's edge across the floor) closer than this are one, and one this close to a wall is the wall's corner.
breakTolerance = 1e-3
# A break kept off a wall it would otherwise meet in a narrower row by at least this, so no floor face closes to nothing.
breakClearance = 0.25


def smoothstep(share):
  return share * share * (3 - 2 * share)


def capitalized(text):
  return text[:1].upper() + text[1:]


def roundedPoint(point):
  return [round(float(value), 1) for value in point]


class CaveLine:
  """A run's floor centerline in plan: its path's straight runs joined by an arc at each bend, a station at each path point (an arc's
  middle at a bend), widths and heights eased between stations. Once its floors are set (setFloors), the floor grades evenly by plan
  length between knots: each point's station, or for a landing the two ends of its arc, so the floor turns level round it."""

  def __init__(self, plan, widths, heights, landings=(), owner="the cave"):
    plan = numpy.asarray(plan, dtype=numpy.float64)[:, :2]
    self.owner = owner
    label = capitalized(owner) + "'s path"
    self.widths = numpy.array(widths, dtype=numpy.float64)
    self.heights = numpy.array(heights, dtype=numpy.float64)
    runs = numpy.diff(plan, axis=0)
    lengths = numpy.linalg.norm(runs, axis=1)
    for index in numpy.flatnonzero(lengths < 1e-6):
      raise ValueError(f"Points {index} and {index + 1} of {owner}'s path stand at one place in plan")
    directions = runs / lengths[:, None]
    self.lines, self.arcs, self.turns = [], [], {}
    stations, along, start = [0.0], 0.0, plan[0]
    for index in range(1, len(plan) - 1):
      incoming, outgoing = directions[index - 1], directions[index]
      cross = float(incoming[0] * outgoing[1] - incoming[1] * outgoing[0])
      turn = math.atan2(abs(cross), float(incoming @ outgoing))
      if turn > math.pi - 1e-6:
        raise ValueError(f"{label} turns straight back on itself at point {index}")
      if turn < 1e-9:
        along = self.addLine(start, plan[index], along)
        stations.append(along)
        start = plan[index]
        continue
      room = min(lengths[index - 1], lengths[index]) / 2
      radius = min(self.widths[index], room / math.tan(turn / 2))
      if radius <= self.widths[index] / 2:
        raise ValueError(
          f"{label} bends at point {index} tighter than half its width there: it turns {math.degrees(turn):.1f} degrees with"
          f" {room:.1f} of the path on each side to turn in, so its floor's middle would turn on a radius of {radius:.1f}, no more than"
          f" half its width ({self.widths[index] / 2:.1f}), and its inner wall would fold. Move the points around the bend apart or turn less sharply"
        )
      tangent = radius * math.tan(turn / 2)
      turnStart = plan[index] - incoming * tangent
      along = self.addLine(start, turnStart, along)
      sign = 1.0 if cross >= 0 else -1.0
      center = turnStart + numpy.array([-incoming[1], incoming[0]]) * sign * radius
      self.arcs.append((center, radius, math.atan2(turnStart[1] - center[1], turnStart[0] - center[0]), sign * turn, along))
      self.turns[index] = (along, along + radius * turn)
      stations.append(along + radius * turn / 2)
      along += radius * turn
      start = plan[index] + outgoing * tangent
    self.length = self.addLine(start, plan[-1], along)
    stations.append(self.length)
    self.stations = numpy.array(stations)
    self.landings = sorted(int(index) for index in landings)
    for index in self.landings:
      if index <= 0 or index >= len(plan) - 1:
        raise ValueError(f"{capitalized(owner)}'s landing at point {index} is at an end; a landing turns level round a bend, at a point between the ends")
      if index not in self.turns:
        raise ValueError(f"{label} does not bend at point {index}, so there is nothing to turn level on there: a landing is named at a bend")
    self.floors = None

  def addLine(self, start, end, along):
    length = float(numpy.linalg.norm(end - start))
    if length > 1e-9:
      self.lines.append((start, end, along))
    return along + length

  def entry(self, index):
    """Where the floor reaches a point's height along the line: its station, or the start of its landing's arc."""
    return self.turns[index][0] if index in self.landings else float(self.stations[index])

  def exit(self, index):
    return self.turns[index][1] if index in self.landings else float(self.stations[index])

  def segmentRun(self, index):
    """The plan length the floor climbs over from point index to the next: from the first's exit to the second's entry."""
    return self.entry(index + 1) - self.exit(index)

  def setFloors(self, floors):
    self.floors = numpy.array(floors, dtype=numpy.float64)
    alongs, heights = [], []
    for index, floor in enumerate(self.floors):
      for along in dict.fromkeys((self.entry(index), self.exit(index))):
        alongs.append(along)
        heights.append(floor)
    self.knotAlongs, self.knotFloors = numpy.array(alongs), numpy.array(heights)
    return self

  def requireGrades(self, maximumDegrees):
    for index in range(len(self.floors) - 1):
      run, rise = self.segmentRun(index), float(self.floors[index + 1] - self.floors[index])
      degrees = math.degrees(math.atan2(abs(rise), run))
      if degrees > maximumDegrees + 1e-9:
        landed = [point for point in (index, index + 1) if point in self.landings]
        raise ValueError(
          f"{capitalized(self.owner)}'s floor {'rises' if rise > 0 else 'falls'} {abs(rise):.1f} from point {index} to point {index + 1} over a run of"
          f" {run:.1f}" + (f" (the straight beside the landing{'s' if len(landed) > 1 else ''} at point {' and '.join(map(str, landed))})" if landed else "")
          + f", {degrees:.1f} degrees, steeper than {maximumDegrees:g}; at {maximumDegrees:g} degrees it needs a run of"
          f" {abs(rise) / math.tan(math.radians(maximumDegrees)):.1f}"
        )

  def floorAt(self, alongs):
    return numpy.interp(alongs, self.knotAlongs, self.knotFloors)

  def at(self, alongs):
    """Floor points, plan directions, widths, and heights at distances along the line."""
    alongs = numpy.clip(numpy.asarray(alongs, dtype=numpy.float64), 0.0, self.length)
    points, directions = numpy.zeros((len(alongs), 2)), numpy.zeros((len(alongs), 2))
    for start, end, lineAlong in self.lines:
      length = float(numpy.linalg.norm(end - start))
      inside = (alongs >= lineAlong - 1e-9) & (alongs <= lineAlong + length + 1e-9)
      points[inside] = start + numpy.clip((alongs[inside] - lineAlong) / length, 0, 1)[:, None] * (end - start)
      directions[inside] = (end - start) / length
    for center, radius, startAngle, sweep, arcAlong in self.arcs:
      inside = (alongs >= arcAlong - 1e-9) & (alongs <= arcAlong + radius * abs(sweep) + 1e-9)
      angles = startAngle + numpy.clip((alongs[inside] - arcAlong) / radius, 0, abs(sweep)) * math.copysign(1.0, sweep)
      points[inside] = center + radius * numpy.column_stack([numpy.cos(angles), numpy.sin(angles)])
      directions[inside] = math.copysign(1.0, sweep) * numpy.column_stack([-numpy.sin(angles), numpy.cos(angles)])
    floors = self.floorAt(alongs) if self.floors is not None else numpy.zeros(len(alongs))
    return numpy.column_stack([points, floors]), directions, self.eased(self.widths, alongs), self.eased(self.heights, alongs)

  def eased(self, values, alongs):
    """Values given per point, eased (smoothstep) between stations."""
    values = numpy.asarray(values, dtype=numpy.float64)
    alongs = numpy.clip(numpy.asarray(alongs, dtype=numpy.float64), 0.0, self.length)
    span = numpy.clip(numpy.searchsorted(self.stations, alongs, side="right") - 1, 0, len(self.stations) - 2)
    share = (alongs - self.stations[span]) / (self.stations[span + 1] - self.stations[span])
    return values[span] + smoothstep(share) * (values[span + 1] - values[span])

  def samples(self, spacing):
    """Distances along the line every `spacing` or less, every station and arc end among them."""
    marks = [self.stations] + [numpy.array(span) for span in self.turns.values()]
    return numpy.unique(numpy.concatenate([numpy.linspace(0.0, self.length, max(2, math.ceil(self.length / spacing) + 1))] + marks))

  def nearestAlong(self, point):
    """The distance along the line of its centerline point nearest a plan point, and how far off it the point lies."""
    alongs = self.samples(1.0)
    floors, _, _, _ = self.at(alongs)
    distances = numpy.linalg.norm(floors[:, :2] - numpy.asarray(point, dtype=numpy.float64)[:2], axis=1)
    nearest = int(distances.argmin())
    return float(alongs[nearest]), float(distances[nearest])

  def levelStretches(self):
    """Each run of the path whose floor stays level and whose width stays the same (a flare between two widths its own run): where it
    starts and ends, its length, floor height, and narrowest width."""
    runs = []
    for index in range(len(self.floors) - 1):
      if abs(self.floors[index + 1] - self.floors[index]) > levelTolerance:
        continue
      constant = self.widths[index + 1] == self.widths[index]
      previous = runs[-1] if runs else None
      if previous is not None and previous[1] == index and previous[2] and constant and self.widths[index] == self.widths[previous[0]]:
        previous[1] = index + 1
      else:
        runs.append([index, index + 1, constant])
    stretches = []
    for first, last, _ in runs:
      ends, _, _, _ = self.at(numpy.array([self.stations[first], self.stations[last]]))
      stretches.append({
        "points": [first, last], "start": roundedPoint(ends[0, :2]), "end": roundedPoint(ends[1, :2]),
        "length": round(float(self.stations[last] - self.stations[first]), 1), "floor": round(float(self.floors[first]), 2),
        "width": round(float(self.widths[first:last + 1].min()), 1),
      })
    return stretches

  def grading(self):
    """Each segment's grade in degrees and run, each landing's height and the arc it turns level on, and each station's distance along."""
    segments = []
    for index in range(len(self.floors) - 1):
      run = self.segmentRun(index)
      segments.append({
        "from": index, "to": index + 1, "gradeDegrees": round(math.degrees(math.atan2(float(self.floors[index + 1] - self.floors[index]), run)), 2),
        "run": round(run, 1),
      })
    landings = [{"point": index, "floor": round(float(self.floors[index]), 2), "arc": [round(self.turns[index][0], 1), round(self.turns[index][1], 1)]} for index in self.landings]
    return {
      "floors": [round(float(value), 2) for value in self.floors], "stations": [round(float(value), 1) for value in self.stations],
      "length": round(self.length, 1), "segments": segments, "landings": landings,
    }


def isNumber(value):
  return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def requireNumber(value, what):
  if not isNumber(value):
    raise ValueError(f"{what} is a number, got {value!r}")
  return float(value)


def requireNumbers(values, count, what, low=None, high=None):
  if not isinstance(values, list) or len(values) != count or not all(isNumber(value) for value in values):
    raise ValueError(f"{what} are {count} numbers, got {values!r}")
  if low is not None and min(values) < low or high is not None and max(values) > high:
    raise ValueError(f"{what} lie from {low:g} to {high:g}, got {values!r}")
  return [float(value) for value in values]


def runDefinition(run, label, isBranch):
  """A run as a definition keeps it: points [x, y] or [x, y, z], a positive width and height per point, an optional grade per segment
  (signed degrees, or null), landing point indices, an optional daylight share per point, and for a branch whether it is an overlook."""
  required, optional = branchKeys if isBranch else runKeys
  path = run["path"]
  if not isinstance(path, list) or len(path) < 2 or any(not isinstance(point, list) or len(point) not in (2, 3) or not all(isNumber(value) for value in point) for point in path):
    raise ValueError(f"{label}'s path is at least two points, each [x, y] or [x, y, z] in numbers (a floor height, or none to take it from the grades or the even grade between given ones), got {path!r}")
  count = len(path)
  widths, heights = run["widths"], run["heights"]
  if not isinstance(widths, list) or not isinstance(heights, list) or len(widths) != count or len(heights) != count or not all(isNumber(value) and value > 0 for value in widths + heights):
    raise ValueError(f"{label}'s widths and heights are one positive value per path point ({count}), got {widths!r} and {heights!r}")
  grades = run.get("grades")
  if grades is not None:
    if not isinstance(grades, list) or len(grades) != count - 1 or any(grade is not None and not isNumber(grade) for grade in grades):
      raise ValueError(f"{label}'s grades are one per segment ({count - 1}), each signed degrees or null where the points' heights decide, got {grades!r}")
    grades = [None if grade is None else float(grade) for grade in grades]
  landings = run.get("landings") or []
  if not isinstance(landings, list) or any(not isinstance(index, int) or isinstance(index, bool) for index in landings) or len(set(landings)) != len(landings):
    raise ValueError(f"{label}'s landings are path point indices, each once, got {landings!r}")
  daylight = run.get("daylight")
  if daylight is not None:
    daylight = requireNumbers(daylight, count, f"{label}'s daylight shares (the share of scene light its lining takes at each point)", 0.0, 1.0)
  definition = {
    "path": [[float(value) for value in point] for point in path], "widths": [float(value) for value in widths], "heights": [float(value) for value in heights],
    "grades": grades, "landings": sorted(landings), "daylight": daylight,
  }
  if isBranch:
    overlook = run.get("overlook", False)
    if not isinstance(overlook, bool):
      raise ValueError(f"{label}'s overlook is true or false, got {overlook!r}")
    definition = {"name": run["name"], "from": run["from"]} | definition | {"overlook": overlook}
  return definition


def resolveFloors(line, path, grades, startHeight, endHeight):
  """Each point's floor height: given, set by the graded segment into it from its start's height, or between two known heights at the
  even grade by the plan length the floor climbs (landing arcs left out); an end with none takes startHeight() or endHeight(). Refuses
  a point whose height is set two ways, and a graded segment whose start has no height."""
  count = len(path)
  given = [point[2] if len(point) == 3 else None for point in path]
  grades = grades or [None] * (count - 1)
  floors = list(given)
  if floors[0] is None:
    floors[0] = float(startHeight())
  if floors[-1] is None and grades[-1] is None:
    floors[-1] = float(endHeight())
  anchor, pending = 0, []
  for index in range(count - 1):
    if grades[index] is not None:
      if floors[index] is None:
        raise ValueError(
          f"{capitalized(line.owner)}'s segment {index} is graded {grades[index]:g} degrees from point {index}, which has no height yet: give point {index} a height"
          " or grade the segment into it"
        )
      if given[index + 1] is not None:
        raise ValueError(
          f"{capitalized(line.owner)}'s point {index + 1} has its height set two ways: given as {given[index + 1]:g}, and by segment {index}'s grade of"
          f" {grades[index]:g} degrees; leave out the point's height or the segment's grade"
        )
      floors[index + 1] = floors[index] + math.tan(math.radians(grades[index])) * line.segmentRun(index)
    if floors[index + 1] is None:
      pending.append(index + 1)
      continue
    if pending:
      climbs = numpy.cumsum([line.segmentRun(point) for point in range(anchor, index + 1)])
      for point in pending:
        floors[point] = floors[anchor] + (floors[index + 1] - floors[anchor]) * climbs[point - anchor - 1] / climbs[-1]
    anchor, pending = index + 1, []
  return floors


def runOwner(name):
  """How messages name a run: the cave's main path, or a branch by name."""
  return "the cave" if name == mainRun else f"branch '{name}'"


def runLine(run, name):
  """A run's line, its geometry only: its floors are set once worked out (resolveFloors)."""
  return CaveLine([point[:2] for point in run["path"]], run["widths"], run["heights"], run["landings"], runOwner(name))


# Floor strokes

def requireName(value, what):
  if not isinstance(value, str) or not value.strip():
    raise ValueError(f"{what} needs a name, got {value!r}")
  return value


def requirePosition(value, what):
  """A place along a run: a distance along it, or {"point": index} for a path point's station."""
  if isinstance(value, dict) and set(value) == {"point"} and isinstance(value["point"], int) and not isinstance(value["point"], bool):
    return {"point": value["point"]}
  if isinstance(value, (int, float)) and not isinstance(value, bool):
    return float(value)
  raise ValueError(f"{what} is a distance along the run, or {{\"point\": index}} for a path point, got {value!r}")


def strokeDefinitions(floor, runNames, edgeLength):
  """Floor strokes as a definition keeps them, refusing any that is not well formed; each names a run of the cave."""
  if floor is None:
    return []
  if not isinstance(floor, list):
    raise ValueError(f"floor is a list of strokes, each {{kind: level, pad, or rough, name, run, ...}}, got {floor!r}")
  strokes, names = [], set()
  for index, stroke in enumerate(floor):
    if not isinstance(stroke, dict) or stroke.get("kind") not in strokeKinds:
      raise ValueError(f"Floor stroke {index} is {{kind: level, pad, or rough, ...}}, got {stroke!r}")
    kind = stroke["kind"]
    required, optional = strokeKeys[kind]
    missing, unknown = sorted(required - set(stroke)), sorted(set(stroke) - required - optional)
    if missing or unknown:
      raise ValueError(f"Floor stroke {index} ({kind}) takes {sorted(required)} and optionally {sorted(optional)}; missing {missing}, unknown {unknown}")
    name = requireName(stroke["name"], f"Floor stroke {index}")
    if name in names:
      raise ValueError(f"Two floor strokes are named '{name}'; each stroke's name is its own")
    names.add(name)
    if stroke["run"] not in runNames:
      raise ValueError(f"Floor stroke '{name}' is on run {stroke['run']!r}; the cave's runs are {runNames}")
    what = f"Floor stroke '{name}'"
    defined = {"kind": kind, "name": name, "run": stroke["run"]}
    material = stroke.get("material")
    if material is not None and not isinstance(material, str):
      raise ValueError(f"{what}'s material is a material's name, got {material!r}")
    defined["material"] = material
    if kind in ("level", "pad"):
      across = stroke["across"]
      if not isinstance(across, list) or len(across) != 2 or not all(isNumber(value) for value in across) or across[0] >= across[1]:
        raise ValueError(f"{what}'s across is [left, right], units from the run's middle (negative to the left looking along it), left under right, got {across!r}")
      defined |= {"from": requirePosition(stroke["from"], f"{what}'s from"), "to": requirePosition(stroke["to"], f"{what}'s to"), "across": [float(value) for value in across]}
    if kind == "pad":
      top, rise = stroke.get("top"), stroke.get("rise")
      if (top is None) == (rise is None):
        raise ValueError(f"{what} is set at top (a height) or rise (over the run's floor at its middle), one of them (the other null), got top {top!r} and rise {rise!r}")
      edge = requireNumber(stroke["edge"], f"{what}'s edge")
      if not edge >= 0.5:
        raise ValueError(f"{what}'s edge is how far its side runs out to the floor around it, at least 0.5 (a riser; more is a slope), got {stroke['edge']!r}")
      defined |= {"top": None if top is None else requireNumber(top, f"{what}'s top"), "rise": None if rise is None else requireNumber(rise, f"{what}'s rise"), "edge": edge}
    if kind == "rough":
      outline = stroke["outline"]
      if not isinstance(outline, list) or len(outline) < 3 or any(not isinstance(point, list) or len(point) != 2 or not all(isNumber(value) for value in point) for point in outline):
        raise ValueError(f"{what}'s outline is at least three [x, y] points in plan, got {outline!r}")
      breakup = stroke["breakup"]
      if (
        not isinstance(breakup, dict) or set(breakup) != {"featureSize", "amplitude", "seed"} or not isNumber(breakup["featureSize"]) or not isNumber(breakup["amplitude"])
        or breakup["featureSize"] <= 0 or breakup["amplitude"] <= 0 or not isinstance(breakup["seed"], int) or isinstance(breakup["seed"], bool)
      ):
        raise ValueError(f"{what}'s breakup is {{featureSize, amplitude, seed}} with a positive featureSize and amplitude and a whole seed, got {breakup!r}")
      rise, edge = requireNumber(stroke["rise"], f"{what}'s rise"), requireNumber(stroke["edge"], f"{what}'s edge")
      if not rise > 0 or not edge > 0:
        raise ValueError(f"{what}'s rise (how high its rubble stands over the floor) and edge (how wide the band is where it eases to the floor) are positive, got {stroke['rise']!r} and {stroke['edge']!r}")
      bank = stroke.get("bank")
      if bank is not None and not (isNumber(bank) and bank > 0):
        raise ValueError(f"{what}'s bank is how high its rubble heaps up a wall it meets (talus), a positive height, or none, got {bank!r}")
      if breakup["featureSize"] < 2 * edgeLength - 1e-9:
        raise ValueError(
          f"{what}'s featureSize {breakup['featureSize']:g} is under twice the cave's edgeLength ({edgeLength:g}), too fine for its floor to hold:"
          f" cut the cave at an edgeLength of {breakup['featureSize'] / 2:g} or less, or give a featureSize of {2 * edgeLength:g} or more"
        )
      defined |= {
        "outline": [[float(value) for value in point] for point in outline], "rise": rise, "edge": edge, "bank": None if bank is None else float(bank),
        "breakup": {"featureSize": float(breakup["featureSize"]), "amplitude": float(breakup["amplitude"]), "seed": breakup["seed"]},
      }
    strokes.append(defined)
  return strokes


def positionAlong(line, value, what):
  if isinstance(value, dict):
    index = value["point"]
    if not 0 <= index < len(line.stations):
      raise ValueError(f"{what} names point {index}; the run has points 0 to {len(line.stations) - 1}")
    return float(line.stations[index])
  return value


class FloorRelief:
  """The floor strokes of one run, placed on its line: where they need rows (along) and floor points (across), and the relief they give
  the floor: level holds it at the run's floor and wins over everything; a pad sets it level at its top, its sides running out over its
  edge to what stands round it, and wins over rough ground; rough ground rises in lumps of the author's noise about its outline, its edge
  broken by the same noise, heaped up a wall it meets where the author gives it a bank."""

  def __init__(self, strokes, line):
    self.line = line
    self.level, self.pads, self.rough = [], [], []
    for stroke in strokes:
      what = f"Floor stroke '{stroke['name']}'"
      if stroke["kind"] == "rough":
        self.rough.append(stroke | {"outlineArray": numpy.array(stroke["outline"])})
        continue
      start, end = positionAlong(line, stroke["from"], f"{what}'s from"), positionAlong(line, stroke["to"], f"{what}'s to")
      if start >= end - 1e-9:
        raise ValueError(f"{what} runs from {start:.1f} to {end:.1f} along its run; from is under to")
      reach = stroke["edge"] if stroke["kind"] == "pad" else 0.0
      if start - reach < -1e-9 or end + reach > line.length + 1e-9:
        raise ValueError(
          f"{what} reaches from {start - reach:.1f} to {end + reach:.1f} along its run, outside the run's floor, which runs from 0 to"
          f" {line.length:.1f}; keep it within the run"
        )
      placed = stroke | {"start": start, "end": end}
      self.requireOnFloor(placed, reach)
      if stroke["kind"] == "pad":
        middleFloor = float(line.floorAt(numpy.array([(start + end) / 2]))[0])
        placed["topHeight"] = stroke["top"] if stroke["top"] is not None else middleFloor + stroke["rise"]
        self.pads.append(placed)
      else:
        self.level.append(placed)

  def requireOnFloor(self, stroke, reach):
    alongs = numpy.unique(numpy.concatenate([numpy.linspace(stroke["start"], stroke["end"], 64), [stroke["start"], stroke["end"]]]))
    halves = self.line.eased(self.line.widths, alongs) / 2
    for side, offset in (("left", -stroke["across"][0]), ("right", stroke["across"][1])):
      over = offset - halves
      if over.max() > breakTolerance:
        worst = int(over.argmax())
        raise ValueError(
          f"Floor stroke '{stroke['name']}' reaches {over[worst]:.1f} past its run's {side} wall at {alongs[worst]:.1f} along it, where the floor"
          f" is {2 * halves[worst]:.1f} wide; keep its across within the floor"
        )

  def stations(self):
    """Distances along the run where a row must stand: each level stroke's and pad's ends, and a pad's sides' feet."""
    marks = [stroke[key] for stroke in self.level for key in ("start", "end")]
    marks += [value for pad in self.pads for value in (pad["start"] - pad["edge"], pad["start"], pad["end"], pad["end"] + pad["edge"])]
    return numpy.array(sorted(value for value in marks if -1e-9 <= value <= self.line.length + 1e-9))

  def breaks(self):
    """Offsets across the floor where a floor point must stand in every row: each level stroke's and pad's sides, and a pad's sides'
    feet; with the width (narrowest in the stroke's reach) each was measured against."""
    found = []
    for stroke, reach in [(stroke, 0.0) for stroke in self.level] + [(pad, pad["edge"]) for pad in self.pads]:
      alongs = numpy.linspace(stroke["start"] - reach, stroke["end"] + reach, 32)
      narrowest = float(self.line.eased(self.line.widths, numpy.clip(alongs, 0.0, self.line.length)).min())
      left, right = stroke["across"]
      for offset in (left - reach, left, right, right + reach) if reach else (left, right):
        if abs(offset) < narrowest / 2 - breakTolerance:
          found.append((offset, narrowest))
    found.sort()
    kept = []
    for offset, width in found:
      if kept and offset - kept[-1][0] <= breakTolerance:
        continue
      kept.append((offset, width))
    return kept

  def relief(self, alongs, offsets, plan, widths):
    """The floor's relief over the run's floor at floor points: alongs (rows), offsets (rows x points, across), plan (rows x points x 2)
    and widths (rows). Rough ground stands in its own ground (roughWeights), lumps of its noise (lumpHeights) easing to nothing at a
    level stroke's side over its edge; a pad blends from its top at its sides to what stands round it at its feet; a level stroke holds
    the floor."""
    rows, count = offsets.shape
    alongGrid = numpy.repeat(alongs[:, None], count, axis=1)
    flatPlan = plan.reshape(-1, 2)
    relief = numpy.zeros((rows, count))
    wallDistance = numpy.maximum(widths[:, None] / 2 - numpy.abs(offsets), 0.0)
    for stroke in self.rough:
      weights = self.roughWeights(stroke, flatPlan).reshape(rows, count)
      lumps = lumpHeights(stroke, flatPlan, wallDistance.ravel()).reshape(rows, count)
      for level in self.level:
        lumps *= smoothstep(numpy.clip(strokeDistances(alongGrid, offsets, level) / stroke["edge"], 0.0, 1.0))
      relief = numpy.maximum(relief, weights * lumps)
    floors = self.line.floorAt(alongs)[:, None]
    padWeight = numpy.zeros((rows, count))
    for pad in self.pads:
      weight = numpy.minimum(sideWeights(alongGrid, pad["start"], pad["end"], pad["edge"]), sideWeights(offsets, pad["across"][0], pad["across"][1], pad["edge"]))
      over = (weight > padWeight) & (weight > 0)
      relief = numpy.where(over, weight * (pad["topHeight"] - floors) + (1 - weight) * relief, relief)
      padWeight = numpy.maximum(padWeight, weight)
    level = numpy.zeros((rows, count), dtype=bool)
    for stroke in self.level:
      level |= insideStroke(alongGrid, offsets, stroke)
    return numpy.where(level, 0.0, relief)

  def requireRoughOnFloor(self, points):
    """Refuse a rough stroke none of whose ground reaches the run's floor points (plan, flat)."""
    for stroke in self.rough:
      if not self.roughWeights(stroke, points).any():
        raise ValueError(
          f"Floor stroke '{stroke['name']}' (rough) lies wholly off its run '{stroke['run']}': no floor of the run lies within its outline or"
          f" {stroke['edge']:g} of it. Draw its outline over the run's floor, or name the run it lies on"
        )

  def strokesAt(self, alongs, offsets, points):
    """The names of the rubble and pads each floor point (flat arrays) stands in or on the side of."""
    found = [[] for _ in range(len(alongs))]
    for stroke in self.rough:
      for index in numpy.flatnonzero(self.roughWeights(stroke, points) > 0):
        found[index].append(stroke["name"])
    for pad in self.pads:
      weight = numpy.minimum(sideWeights(alongs, pad["start"], pad["end"], pad["edge"]), sideWeights(offsets, pad["across"][0], pad["across"][1], pad["edge"]))
      for index in numpy.flatnonzero(weight > 0):
        found[index].append(pad["name"])
    return found

  def roughWeights(self, stroke, points):
    """How far each plan point stands in a rough stroke's ground, 0 to 1: full inside its outline, easing to nothing past it at its foot,
    which wanders by the stroke's noise from a quarter of `edge` to `edge` out, so the rubble's foot runs as broken ground rather than
    along the outline's straight sides."""
    breakup = stroke["breakup"]
    inside = pointsInPolygon(points, stroke["outlineArray"])
    samples = bridgeNoise.noiseSamplePoints(numpy.column_stack([points, numpy.zeros(len(points))]), breakup["featureSize"], breakup["seed"]) + wanderOffset
    foot = stroke["edge"] * (0.625 + 0.375 * numpy.tanh(bridgeNoise.fractalNoise(samples, roughOctaves, roughRoughness)))
    return numpy.where(inside, 1.0, smoothstep(numpy.clip(1 - polygonDistances(points, stroke["outlineArray"]) / foot, 0.0, 1.0)))

  def materials(self, alongs, offsets, points):
    """The stroke material each floor point (alongs, offsets, plan points; flat) stands under, or None for the floor's own: the level
    stroke's it stands in, else the pad top's, else the rough ground's it stands at least half in (the most, of two)."""
    found = numpy.full(len(alongs), None, dtype=object)
    strongest = numpy.full(len(alongs), 0.5)
    for stroke in self.rough:
      if stroke["material"] is None:
        continue
      weights = self.roughWeights(stroke, points)
      wins = weights >= strongest
      found[wins], strongest[wins] = stroke["material"], weights[wins]
    for stroke in self.pads + self.level:
      found[insideStroke(alongs, offsets, stroke)] = stroke["material"]
    return found


def lumpHeights(stroke, points, wallDistances):
  """A rough stroke's height at plan points over its ground: ridged by its noise, crests standing at `rise` where the noise crosses its
  middle and falling to `rise` less twice `amplitude` away from them (never under the floor), so the rubble breaks into sharp crests
  rather than standing flat; and with a bank, heaped up a wall it meets the same way up to `bank`, reaching as far out from the wall as
  it stands high there."""
  breakup = stroke["breakup"]
  samples = bridgeNoise.noiseSamplePoints(numpy.column_stack([points, numpy.zeros(len(points))]), breakup["featureSize"], breakup["seed"])
  shape = 1 - 2 * numpy.abs(numpy.tanh(bridgeNoise.fractalNoise(samples, roughOctaves, roughRoughness)))
  lumps = numpy.maximum(stroke["rise"] - breakup["amplitude"] + breakup["amplitude"] * shape, 0.0)
  if stroke["bank"] is None:
    return lumps
  talus = numpy.maximum(stroke["bank"] - breakup["amplitude"] + breakup["amplitude"] * shape, 0.0)
  banked = talus * smoothstep(numpy.clip(1 - wallDistances / numpy.maximum(talus, 1e-9), 0.0, 1.0))
  return numpy.maximum(lumps, banked)


def strokeDistances(alongs, offsets, stroke):
  """How far floor points stand from a level stroke or pad top, along and across the run: 0 inside it."""
  alongGap = numpy.maximum(numpy.maximum(stroke["start"] - alongs, alongs - stroke["end"]), 0.0)
  acrossGap = numpy.maximum(numpy.maximum(stroke["across"][0] - offsets, offsets - stroke["across"][1]), 0.0)
  return numpy.hypot(alongGap, acrossGap)


def insideStroke(alongs, offsets, stroke):
  return (alongs >= stroke["start"] - 1e-6) & (alongs <= stroke["end"] + 1e-6) & (offsets >= stroke["across"][0] - 1e-6) & (offsets <= stroke["across"][1] + 1e-6)


def sideWeights(values, low, high, edge):
  """1 between low and high, falling evenly to 0 over edge beyond each."""
  return numpy.clip(1 - numpy.maximum(low - values, values - high) / edge, 0.0, 1.0)


def pointsInPolygon(points, polygon):
  inside = numpy.zeros(len(points), dtype=bool)
  for index in range(len(polygon)):
    a, b = polygon[index], polygon[(index + 1) % len(polygon)]
    crosses = (a[1] > points[:, 1]) != (b[1] > points[:, 1])
    with numpy.errstate(divide="ignore", invalid="ignore"):
      x = a[0] + (points[:, 1] - a[1]) * (b[0] - a[0]) / (b[1] - a[1])
    inside ^= crosses & (points[:, 0] < x)
  return inside


def polygonDistances(points, polygon):
  distances = numpy.full(len(points), numpy.inf)
  for index in range(len(polygon)):
    a, b = polygon[index], polygon[(index + 1) % len(polygon)]
    segment = b - a
    share = numpy.clip(((points - a) @ segment) / max(float(segment @ segment), 1e-12), 0.0, 1.0)
    distances = numpy.minimum(distances, numpy.linalg.norm(points - (a + share[:, None] * segment), axis=1))
  return distances
