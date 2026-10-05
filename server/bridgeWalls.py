"""The wall lay: kit sections end to end along the artist's path, each leg filled exactly with the fewest sections, longest first; sheared
to the ground's fall as the client's sloped sections are (verticals vertical, sections of one rise sharing one model) or stepped level at
each joint; posts at joints or turns. Registered with bridgeStructures. Runs under Blender's Python."""
import json
import math

import bpy
import numpy

import bridgeExport
import bridgeKitData
import bridgeKitGeometry
import bridgeStructureData
import bridgeStructures
from bridgeStructures import requireKeys, requireNonNegative, requirePositive, roundVector, size
from playerScale import stepHeight

wallKeys = ("kitPath", "path", "frontSide", "sections", "follow", "shearStep", "sink", "maximumBurial", "posts", "variants", "collection")
follows = ("shear", "step")
postPlaces = ("joints", "turns")
fillTolerance = 0.01
turnDegrees = 0.5
sampleSpacing = 1.0
loweringRounds = 8
# Ground heights read back from single-precision vertices differ by about a millionth; a wall line lower than this is not lowered.
heightTolerance = 1e-4
# Sections sheared steeper than this would run their courses up a slope no client wall shows; a wall that steep is stepped.
steepestShearDegrees = 30.0
viewOut = 8.0
# An elevation stands this far in front of a face, takes in a leg from this share of its length out (or this share of the wall's height),
# eye at a player's height, aimed this share of the way up the wall from its foot, so the feet stay in frame.
viewBack = 2.0
viewEyeHeight = 6.0
elevationLengthShare = 0.75
elevationHeightShare = 1.3
elevationAimShare = 0.4


def fillLeg(length, modules):
  """The fewest sections (longest first) whose modules add up to the length within fillTolerance, or None."""
  best = None

  def search(index, remaining, chosen):
    nonlocal best
    if abs(remaining) <= fillTolerance:
      if best is None or len(chosen) < len(best):
        best = list(chosen)
      return
    if index == len(modules) or remaining < -fillTolerance:
      return
    module = modules[index]
    for count in range(int((remaining + fillTolerance) // module), -1, -1):
      search(index + 1, remaining - count * module, chosen + [module] * count)

  search(0, length, [])
  return best


def nearestFills(length, modules):
  """The nearest lengths below and above that the modules fill."""
  limit = length + max(modules)
  sums = {0.0}
  for module in modules:
    sums = {round(total + count * module, 6) for total in sums for count in range(int((limit - total) // module) + 1)}
  below = max((total for total in sums if total < length - fillTolerance), default=None)
  above = min((total for total in sums if total > length + fillTolerance), default=None)
  return below, above


def partNumber(index, count):
  return f"{index + 1:02d}" if count <= 99 else f"{index + 1:03d}"


def requireWallPath(path):
  if not isinstance(path, list) or len(path) < 2:
    raise ValueError(f"path is at least two [x, y] or [x, y, z] points, got {path!r}")
  points = [bridgeStructures.requirePoint(f"path point {index}", point, (2, 3)) for index, point in enumerate(path)]
  given = {len(point) for point in points}
  if len(given) > 1:
    raise ValueError("path points are all [x, y] (the ground found from above) or all [x, y, z] (the ground found from a step above each z)")
  return points, given == {3}


def bodyDepth(data):
  """A wall piece's depth between its faces, without the frames standing proud of them."""
  return size(data, 1) - 2 * bridgeKitData.frameProud(data["record"])


def facingOf(direction):
  return math.degrees(math.atan2(direction[0], direction[1])) % 360.0


def turnAt(incoming, outgoing):
  return abs(math.degrees(math.atan2(incoming[0] * outgoing[1] - incoming[1] * outgoing[0], incoming @ outgoing)))


def shearMeshName(piece, rise):
  return f"{piece}{'Up' if rise > 0 else 'Down'}{round(abs(rise) * 100)}"


def shearMesh(laying, data, rise, made):
  """The shared mesh of a piece sheared by a rise: reused when one of this kit, piece, rise, and shape exists; made otherwise (replacing
  one of an older shape of the piece once the lay commits)."""
  name = shearMeshName(data["piece"], rise)
  if name in made:
    return made[name]
  identity = {"kitPath": laying.definition["kitPath"], "piece": data["piece"], "rise": rise, "fingerprint": laying.kit.used[data["piece"]]}
  existing = bpy.data.meshes.get(name)
  if existing is not None and bridgeStructureData.shearProperty in existing:
    kept = json.loads(existing[bridgeStructureData.shearProperty])
    if (kept["kitPath"], kept["piece"]) != (identity["kitPath"], identity["piece"]):
      raise ValueError(f"Mesh '{name}' is the shear of '{kept['piece']}' from {kept['kitPath']}, not of this kit's piece; rename one of the kits' pieces")
    if kept == identity:
      made[name] = (existing, "reused")
      return made[name]
  elif existing is not None:
    raise ValueError(f"Mesh name '{name}' is taken by a mesh that is not a wall shear; rename it")
  bake = bridgeKitGeometry.Bake()
  bake.add(bridgeKitGeometry.sheared(data, rise, size(data, 0)))
  mesh = bake.mesh(name if existing is None else name + bridgeStructures.layingSuffix, numpy.identity(4))
  mesh[bridgeStructureData.shearProperty] = json.dumps(identity)
  if existing is not None:
    laying.sharedMeshes.append((mesh, existing))
  made[name] = (mesh, "made" if existing is None else "replaced")
  return made[name]


def layWall(laying):
  definition = laying.definition
  points, withHeights = requireWallPath(definition["path"])
  if definition["frontSide"] not in ("left", "right"):
    raise ValueError(f"frontSide is \"left\" or \"right\" of travel, got {definition['frontSide']!r}")
  if definition["follow"] not in follows:
    raise ValueError(f"follow is one of {list(follows)}, got {definition['follow']!r}")
  shearStep = requirePositive("shearStep", definition["shearStep"])
  sink = requireNonNegative("sink", definition["sink"])
  maximumBurial = requireNonNegative("maximumBurial", definition["maximumBurial"])
  frontSign = 1 if definition["frontSide"] == "left" else -1
  if not isinstance(definition["sections"], list) or not definition["sections"]:
    raise ValueError(f"sections names straight wall pieces, got {definition['sections']!r}")
  pieces = [laying.kit.piece(name, ("wall",), "sections") for name in definition["sections"]]
  depth, height = bodyDepth(pieces[0]), size(pieces[0], 2)
  for data in pieces:
    if abs(bodyDepth(data) - depth) > 1e-6 or abs(size(data, 2) - height) > 1e-6:
      raise ValueError(f"Sections are of one depth and height: '{data['piece']}' is {bodyDepth(data):g} deep and {size(data, 2):g} tall, '{pieces[0]['piece']}' {depth:g} and {height:g}")
  byModule = {}
  for data in pieces:
    module = bridgeKitData.moduleOf(data["record"])
    if module in byModule:
      raise ValueError(f"'{data['piece']}' and '{byModule[module]['piece']}' are both {module:g} long; each section piece its own module (variants put others in place)")
    byModule[module] = data
  modules = sorted(byModule, reverse=True)
  closed = len(points) > 2 and numpy.allclose(points[0], points[-1])
  legs, joints = [], []
  for index, (first, second) in enumerate(zip(points[:-1], points[1:])):
    first, second = numpy.array(first), numpy.array(second)
    length = float(numpy.linalg.norm(second[:2] - first[:2]))
    if length < min(modules) - fillTolerance:
      raise ValueError(f"Leg {index} is {length:.2f} long, shorter than the shortest section ({min(modules):g})")
    fill = fillLeg(length, modules)
    if fill is None:
      below, above = nearestFills(length, modules)
      fits = " or ".join(f"{value:g} (move its end {value - length:+.2f})" for value in (below, above) if value is not None)
      raise ValueError(f"Leg {index} is {length:.2f} long, which no set of the modules {modules} fills; the nearest that fill: {fits}")
    direction = numpy.append((second[:2] - first[:2]) / length, 0.0)
    legs.append({"index": index, "first": first, "second": second, "length": length, "direction": direction, "fill": fill})
  for leg, following in zip(legs, legs[1:] + (legs[:1] if closed else [])):
    leg["turnAfter"] = turnAt(leg["direction"], following["direction"])
  posts = definition["posts"]
  postData = None
  if posts is not None:
    requireKeys("posts", posts, ("piece", "at"))
    if posts["at"] not in postPlaces:
      raise ValueError(f"posts at is one of {list(postPlaces)}, got {posts['at']!r}")
    postData = laying.kit.piece(posts["piece"], ("post",), "posts")
    if size(postData, 0) <= depth or size(postData, 1) <= depth:
      raise ValueError(f"Post '{posts['piece']}' is {size(postData, 0):g} x {size(postData, 1):g}, no wider or deeper than the wall ({depth:g}); a post must stand proud of the sections' faces, else they flicker through it")
  for leg in legs:
    if "turnAfter" in leg and leg["turnAfter"] > turnDegrees and posts is None:
      at = (leg["index"] + 1) % len(points) if not closed else (leg["index"] + 1) % (len(points) - 1)
      raise ValueError(f"The path turns {leg['turnAfter']:.2f} degrees at point {at}; a turn needs a post (posts)")
  sections = []
  for leg in legs:
    along = 0.0
    for module in leg["fill"]:
      sections.append({"leg": leg["index"], "from": along, "to": along + module, "module": module, "data": byModule[module]})
      along += module
  variants = definition["variants"] or {}
  if not isinstance(variants, dict):
    raise ValueError(f"variants is {{\"<section index>\": piece}}, got {variants!r}")
  for key, piece in variants.items():
    index = int(key) if str(key).lstrip("-").isdigit() else None
    if index is None or not 0 <= index < len(sections):
      raise ValueError(f"Variant index {key!r} is not a section of this wall: its sections are 0 to {len(sections) - 1}")
    data = laying.kit.piece(piece, ("wall",), "variant")
    module = bridgeKitData.moduleOf(data["record"])
    if abs(module - sections[index]["module"]) > fillTolerance or abs(bodyDepth(data) - depth) > 1e-6 or abs(size(data, 2) - height) > 1e-6:
      raise ValueError(f"Variant '{piece}' at section {index} is {module:g} long, {bodyDepth(data):g} deep, {size(data, 2):g} tall; section {index} is a {sections[index]['module']:g} module {depth:g} deep and {height:g} tall")
    sections[index]["data"] = data
  jointPoints, jointGround = [], []
  lookups = laying.lookups

  def legPoint(leg, along):
    point = leg["first"][:2] + leg["direction"][:2] * along
    if withHeights:
      height = leg["first"][2] + (leg["second"][2] - leg["first"][2]) * along / leg["length"]
      return numpy.array([point[0], point[1], height])
    return numpy.array([point[0], point[1]])

  def groundAt(point, label):
    if withHeights:
      found = lookups.below((point[0], point[1], point[2] + stepHeight))
    else:
      found = lookups.overhead(point[0], point[1])
    if found is None:
      raise ValueError(f"{label} {roundVector(point, 2)} has no ground under it")
    return found

  for section in sections:
    leg = legs[section["leg"]]
    section["start"], section["end"] = legPoint(leg, section["from"]), legPoint(leg, section["to"])
  jointList = [sections[0]["start"]] + [section["end"] for section in sections]
  if closed:
    jointList = jointList[:-1]
  for index, point in enumerate(jointList):
    jointPoints.append(point)
    jointGround.append(groundAt(point, f"Joint {index}"))
  jointCount = len(jointPoints)

  def jointOf(sectionIndex, end):
    return (sectionIndex + end) % jointCount if closed else sectionIndex + end

  for index, section in enumerate(sections):
    leg = legs[section["leg"]]
    first, second = jointOf(index, 0), jointOf(index, 1)
    count = max(1, math.ceil(section["module"] / sampleSpacing))
    left = numpy.array([-leg["direction"][1], leg["direction"][0]])
    samples = []
    for step in range(count + 1):
      share = step / count
      center = section["start"][:2] + (section["end"][:2] - section["start"][:2]) * share
      if withHeights:
        top = section["start"][2] + (section["end"][2] - section["start"][2]) * share + stepHeight
      else:
        top = max(jointGround[first], jointGround[second]) + stepHeight
      for face in (1, -1):
        point = center + left * face * depth / 2
        found = lookups.below((point[0], point[1], top))
        if found is None:
          raise ValueError(f"Section {index} finds no ground under {roundVector(point, 2)}")
        samples.append((share, found))
    section["samples"] = samples
    section["joints"] = (first, second)
  if definition["follow"] == "shear":
    margin = sink + shearStep / 2
    heights = [ground - margin for ground in jointGround]
    for _ in range(loweringRounds):
      # A joint two sections share is lowered once, by the more either needs, so a run on level ground stays level.
      lowering = [0.0] * jointCount
      for section in sections:
        first, second = section["joints"]
        excess = max(heights[first] + (heights[second] - heights[first]) * share - (ground - margin) for share, ground in section["samples"])
        if excess > heightTolerance:
          lowering[first], lowering[second] = max(lowering[first], excess), max(lowering[second], excess)
      if not any(lowering):
        break
      heights = [height - lower for height, lower in zip(heights, lowering)]
    steps = [math.floor((height - heights[0] + heightTolerance) / shearStep) for height in heights]
    jointHeights = [heights[0] + count * shearStep for count in steps]
    for section in sections:
      first, second = section["joints"]
      section["base"] = (jointHeights[first], jointHeights[second])
      section["riseSteps"] = steps[second] - steps[first]
  else:
    for section in sections:
      base = min(ground for _, ground in section["samples"]) - sink
      section["base"] = (base, base)
      section["riseSteps"] = 0
  shearMade, sectionReport, placements = {}, [], {}
  for index, section in enumerate(sections):
    burials = [ground - (section["base"][0] + (section["base"][1] - section["base"][0]) * share) for share, ground in section["samples"]]
    if max(burials) > maximumBurial + 1e-9:
      sheared = definition["follow"] == "shear"
      remedy = 'or step it (follow "step")' if sheared else "or allow a deeper burial (maximumBurial)"
      raise ValueError(
        f"Section {index} would be buried {max(burials):.2f} deep (the ground {'bulges over its line' if sheared else 'rises along it'}), over maximumBurial"
        f" {maximumBurial:g}; move or grade the wall's line, {remedy}"
      )
    shearDegrees = math.degrees(math.atan2(abs(section["base"][1] - section["base"][0]), section["module"]))
    if shearDegrees > steepestShearDegrees + 1e-9:
      raise ValueError(
        f"Section {index} would be sheared {shearDegrees:.1f} degrees (its base rising {section['base'][1] - section['base'][0]:+.2f} over its {section['module']:g}),"
        f" steeper than {steepestShearDegrees:g}: its courses would run up the slope; step the wall (follow \"step\"), or run its line across the slope"
      )
    leg = legs[section["leg"]]
    front = numpy.array([-leg["direction"][1], leg["direction"][0], 0.0]) * frontSign
    facing = facingOf(front)
    middle = (section["start"][:2] + section["end"][:2]) / 2
    location = (float(middle[0]), float(middle[1]), (section["base"][0] + section["base"][1]) / 2)
    name = f"{laying.name}Section{partNumber(index, len(sections))}"
    rise = section["riseSteps"] * shearStep * frontSign
    data = section["data"]
    if section["riseSteps"] == 0:
      laying.addInstance(name, data["collection"], location, facing)
      model, shear = f"obj_{bridgeExport.fileStem(data['piece'])}.mod", None
    else:
      mesh, state = shearMesh(laying, data, rise, shearMade)
      laying.addMeshObject(name, mesh, location, facing)
      model, shear = f"obj_{bridgeExport.fileStem(shearMeshName(data['piece'], rise))}.mod", state
    entry = placements.setdefault(model, {"placements": 0, "triangles": sum(face - 2 for face in data["loopTotals"].tolist())})
    entry["placements"] += 1
    sectionReport.append({
      "index": index, "name": name, "piece": data["piece"], "base": [round(value, 6) for value in section["base"]], "rise": round(rise, 6), "model": model,
      "shear": shear, "deepestBurial": round(max(burials), 3), "shallowestBurial": round(min(burials), 3),
    })
  def jointBases(index):
    """The bases of the sections meeting at a joint where they meet it: the one ending there, the one starting there (None past an end)."""
    ending = sections[index - 1] if index > 0 or closed else None
    starting = sections[index] if index < len(sections) else None
    return [None if ending is None else ending["base"][1], None if starting is None else starting["base"][0]]

  postReport = []
  if posts is not None:
    wanted = []
    for index in range(jointCount):
      atEnd = not closed and index in (0, jointCount - 1)
      incoming = sections[index - 1] if (index > 0 or closed) else None
      outgoing = sections[index] if index < len(sections) else None
      turning = incoming is not None and outgoing is not None and turnAt(legs[incoming["leg"]]["direction"], legs[outgoing["leg"]]["direction"]) > turnDegrees
      if posts["at"] == "joints" or atEnd or turning:
        wanted.append((index, (outgoing or incoming)))
    for number, (index, section) in enumerate(wanted):
      leg = legs[section["leg"]]
      center = jointPoints[index][:2]
      direction = leg["direction"][:2]
      left = numpy.array([-direction[1], direction[0]])
      top = jointGround[index] + stepHeight if not withHeights else jointPoints[index][2] + stepHeight
      lowest = None
      for corner in ((0, 0), (1, 1), (1, -1), (-1, 1), (-1, -1)):
        point = center + direction * corner[0] * size(postData, 0) / 2 + left * corner[1] * size(postData, 1) / 2
        found = lookups.below((point[0], point[1], top))
        if found is None:
          raise ValueError(f"The post at joint {index} finds no ground under {roundVector(point, 2)}")
        lowest = found if lowest is None else min(lowest, found)
      base = lowest - sink
      wallTop = max(value for value in jointBases(index) if value is not None) + height
      if base + size(postData, 2) < wallTop - heightTolerance:
        raise ValueError(
          f"The post at joint {index} would stand {wallTop - base - size(postData, 2):.2f} under the top of the wall beside it ({wallTop:.2f}): its base, {sink:g}"
          f" under the lowest ground at its foot, is at {base:.2f} and '{posts['piece']}' is {size(postData, 2):g} tall; give a taller post, or run the line where"
          " the ground at the joint is flatter"
        )
      front = numpy.array([-leg["direction"][1], leg["direction"][0], 0.0]) * frontSign
      name = f"{laying.name}Post{partNumber(number, len(wanted))}"
      laying.addInstance(name, postData["collection"], (float(center[0]), float(center[1]), base), facingOf(front))
      model = f"obj_{bridgeExport.fileStem(postData['piece'])}.mod"
      entry = placements.setdefault(model, {"placements": 0, "triangles": sum(face - 2 for face in postData["loopTotals"].tolist())})
      entry["placements"] += 1
      postReport.append({"name": name, "joint": index, "at": roundVector(center, 3), "base": round(base, 3)})
  return {
    "legs": [{"index": leg["index"], "length": round(leg["length"], 3), "sections": [byModule[module]["piece"] for module in leg["fill"]]} for leg in legs],
    "joints": [
      {"at": roundVector(point[:2], 3), "ground": round(ground, 3)}
      | ({"height": round(jointHeights[index], 6)} if definition["follow"] == "shear" else {"bases": [None if base is None else round(base, 6) for base in jointBases(index)]})
      for index, (point, ground) in enumerate(zip(jointPoints, jointGround))
    ],
    "sections": sectionReport, "shearModels": [{"mesh": name, "state": state} for name, (_, state) in sorted(shearMade.items())],
    "posts": postReport, "models": placements, "closed": bool(closed),
  }


def rayMeetsSegment(origin, direction, start, end):
  """How far along a plan ray from origin it crosses the segment from start to end, or None where it does not."""
  edge = end - start
  denominator = direction[0] * edge[1] - direction[1] * edge[0]
  if abs(denominator) < 1e-12:
    return None
  offset = start - origin
  along = (offset[0] * edge[1] - offset[1] * edge[0]) / denominator
  share = (offset[0] * direction[1] - offset[1] * direction[0]) / denominator
  return float(along) if along > 0 and -1e-9 <= share <= 1 + 1e-9 else None


def wallViewSet(definition, groundHeight):
  """Along it from its start on its front side at eye level, an elevation of each leg from its front, and a plan of its extent."""
  points = [numpy.array(point, dtype=numpy.float64) for point in definition["path"]]
  frontSign = 1 if definition["frontSide"] == "left" else -1
  views = {}
  first = (points[1][:2] - points[0][:2]) / numpy.linalg.norm(points[1][:2] - points[0][:2])
  front = numpy.array([-first[1], first[0]]) * frontSign
  standAt = points[0][:2] + front * viewOut
  views["along"] = {"standAt": roundVector(list(standAt) + ([float(points[0][2])] if len(points[0]) == 3 else [])), "headingDegrees": round(facingOf(first), 3), "pitchDegrees": 2.0}
  low, high = bridgeKitGeometry.pieceBounds(bridgeKitData.requirePiece(bridgeStructures.absoluteKitPath(definition["kitPath"]), definition["sections"][0]))
  depth, height = float(high[1] - low[1]), float(high[2] - low[2])
  legs = [(start[:2], end[:2]) for start, end in zip(points[:-1], points[1:])]
  for index, (start, end) in enumerate(legs):
    direction = (end - start) / numpy.linalg.norm(end - start)
    legFront = numpy.array([-direction[1], direction[0]]) * frontSign
    middle = (start + end) / 2
    length = float(numpy.linalg.norm(end - start))
    face = middle + legFront * (depth / 2 + viewBack)
    foot = groundHeight(face[0], face[1])
    if foot is None:
      raise ValueError(f"No ground stands in front of leg {index} of the wall at {roundVector(face, 2)} to look at it from")
    # Far enough to take in the leg and the wall's height, but short of any other leg of the wall the eye would look through.
    distance = max(length * elevationLengthShare + viewOut, height * elevationHeightShare)
    for other, (otherStart, otherEnd) in enumerate(legs):
      if other != index:
        meeting = rayMeetsSegment(middle, legFront, otherStart, otherEnd)
        if meeting is not None:
          distance = min(distance, meeting - depth - viewBack)
    distance = max(distance, depth / 2 + viewBack)
    eye = middle + legFront * distance
    standing = groundHeight(eye[0], eye[1])
    eyeZ = (foot if standing is None else standing) + viewEyeHeight
    views[f"front{index}"] = bridgeStructures.lookView((eye[0], eye[1], eyeZ), (middle[0], middle[1], foot + height * elevationAimShare))
  plans = numpy.array([point[:2] for point in points])
  low, high = plans.min(0), plans.max(0)
  views["plan"] = {"map": {"center": roundVector((low + high) / 2), "width": round(float(max(high - low)) + 60, 3)}}
  return views


bridgeStructures.registerKind("wall", wallKeys, layWall, None, wallViewSet)
