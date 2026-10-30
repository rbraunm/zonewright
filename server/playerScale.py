"""The bodies players play, read from the client's data, in three groups (small, medium, large), and each group's measures that geometry
is judged by, each with its source in the group's `sources`: "measured: how, where, when", "unmeasured: why the value", or "derived: from
what". A group's measures are the strictest of its sample, walked in the measurement zone: its tallest and shortest body by avatarHeight
(the height the client's walks follow) and its widest and thinnest by model width; the most headroom and widest passage they needed, the
least step, the gentlest steepest face, and the shallowest start of swimming. Its eye follows its tallest body. Every tool that judges
where a player stands, climbs, swims, or looks takes them from here, per group, and judges a spot by the groups its region (else its
zone) is built for (builtFor): a built-for group that cannot pass is a failure, any other group a finding."""
import math
from dataclasses import dataclass

groupNames = ("small", "medium", "large")
groupRaces = {"small": (8, 11, 12), "medium": (1, 3, 4, 5, 6, 7, 128, 330, 522), "large": (2, 9, 10, 130)}
raceNames = {
  1: "human", 2: "barbarian", 3: "erudite", 4: "wood elf", 5: "high elf", 6: "dark elf", 7: "half elf", 8: "dwarf", 9: "troll", 10: "ogre",
  11: "halfling", 12: "gnome", 128: "iksar", 130: "vah shir", 330: "froglok", 522: "drakkin",
}
genderNames = ("male", "female")

# (race, gender, model code, spawn height, model height, width across, depth front to back, avatarHeight), standing in the client's
# stand pose (P01, or the EQG STND) at frame 0 and drawn at the classic scale, in EQ units.
bodyRows = (
  (1, 0, "HUM", 6, 7.82, 3.61, 2.01, 3.75),
  (1, 1, "HUF", 6, 7.87, 3.02, 1.67, 3.75),
  (2, 0, "BAM", 7, 9.13, 4.28, 2.56, 4.375),
  (2, 1, "BAF", 7, 9.05, 4.34, 2.68, 4.375),
  (3, 0, "ERM", 6, 8.02, 3.61, 2.01, 3.75),
  (3, 1, "ERF", 6, 7.95, 3.01, 1.56, 3.75),
  (4, 0, "ELM", 5, 6.56, 2.54, 1.36, 3.125),
  (4, 1, "ELF", 5, 6.50, 2.17, 1.68, 3.125),
  (5, 0, "HIM", 6, 7.86, 3.04, 1.64, 3.75),
  (5, 1, "HIF", 6, 8.07, 2.60, 2.02, 3.75),
  (6, 0, "DAM", 5, 6.55, 2.54, 1.36, 3.125),
  (6, 1, "DAF", 5, 6.50, 2.17, 1.68, 3.125),
  (7, 0, "HAM", 5.5, 7.20, 2.79, 1.50, 3.4375),
  (7, 1, "HAF", 5.5, 7.14, 2.39, 1.85, 3.4375),
  (8, 0, "DWM", 4, 5.22, 3.10, 1.74, 2.5),
  (8, 1, "DWF", 4, 5.19, 2.33, 1.54, 2.5),
  (9, 0, "TRM", 8, 10.39, 4.92, 3.73, 5.0),
  (9, 1, "TRF", 8, 10.55, 4.33, 3.34, 5.0),
  (10, 0, "OGM", 9, 11.40, 6.02, 4.63, 5.625),
  (10, 1, "OGF", 9, 11.65, 5.16, 3.36, 5.625),
  (11, 0, "HOM", 3.5, 4.63, 2.21, 1.50, 2.1875),
  (11, 1, "HOF", 3.5, 4.58, 1.90, 2.04, 2.1875),
  (12, 0, "GNM", 3, 3.85, 1.93, 1.37, 1.875),
  (12, 1, "GNF", 3, 3.84, 1.62, 1.16, 1.875),
  (128, 0, "IKM", 6, 7.26, 3.42, 4.89, 3.75),
  (128, 1, "IKF", 6, 6.76, 3.52, 4.98, 3.75),
  (130, 0, "KEM", 7, 8.90, 4.27, 4.89, 4.375),
  (130, 1, "KEF", 7, 8.55, 4.12, 5.10, 4.375),
  (330, 0, "FRM", 5, 5.92, 9.45, 2.81, 3.125),
  (330, 1, "FRF", 5, 5.98, 9.45, 2.70, 3.125),
  (522, 0, "DKM", 6, 7.81, 2.72, 1.81, 4.0625),
  (522, 1, "DKF", 5.7, 7.47, 2.54, 1.58, 3.859375),
)
# No archive the client links at startup holds a froglok's stand (P01, nor a borrowed one), so it stands in its bind pose, arms held out:
# its size is no standing size, and it never limits a group.
bindPoseCodes = frozenset(("FRM", "FRF"))
bodiesSource = (
  "derived: each playable race and gender (eqLooks.playerRaces) from the client's data: the spawn height is the server's"
  " GetRaceGenderDefaultHeight (EQEmu ae4bd2833 common/races.cpp:1489, which zone/mob.cpp:3661 and npc.cpp:164 use; a player's own spawn"
  " goes out with size 0, zone/client.cpp:105, and claudelink's dumps hold height 6 for the walked human male and 5 for a dark elf female);"
  " the model is the one eqgame.exe registers for the race and gender (eqRaces.raceRegistrations), built by eqModels from the client's"
  " global archives in its stand pose at frame 0, and drawn at the scale eqRaces.spawnScale gives that height outside a NewEngineZone zone"
  " (EQEmu sends NewEngineZone false for every zone): its height and width are the posed model's extent up and side to side (arms, hair,"
  " and tails included), avatarHeight its origin over the ground (moddat.ini's ROffset times that scale); tests/testPlayerBodies.py reads"
  " them again from the client"
)

# The RoF2 walk of 2026-10-04: one human male, a medium body; it sets only the design eye now.
rof2Walk = "the RoF2 client on 2026-10-04, a human male of size 6 (avatarHeight 3.75) walking straight at each site without jumping"
walkedAvatarHeight = 3.75
designEyeHeight = 5.5
swimEyeAboveSurfaceValue = 1.0

# The measurement zone's walks (design #10): claudelink's walkMeasurementLanes, each body walked by its permanent character Measure<code>
# of its real race and gender (claudeperidot ledger 20261012-233442 and its predecessors; a #race illusion leaves the client's
# avatarHeight at the human's), walking straight without jumping, with MacroQuest's frame limiter suspended (about 190 to 230 fps). Per
# model code: the highest riser climbed and the lowest refused (risers 4.50 to 5.30 by 0.05), the steepest ramp climbed and the least
# steep refused (70 to 89 degrees by 1, walked 2026-10-12 after the ramps past 80 were added), the deepest water over the pool's bed walked and the depth floating began (the bed sloping 0
# to 10 at 0.1 per unit), the lowest ceiling passed and the one that stopped it (14 down to 3 by 0.25), the narrowest opening passed and
# the one that stopped it (10 down to 0.5 by 0.25), and the avatarHeight the client held. None: the course holds no value on that side
# (every riser climbed, or none). At the background frame rate (about 30 to 80 fps) the same bodies, walked the
# morning of 2026-10-12, stopped on ramps from 74 to 79 degrees and needed 0.25 to 1.0 more headroom (the gnome 0.25 less).
measurementWalk = (
  "claudelink's walkMeasurementLanes in arttest on claudeperidot (the spare RoF2 client) at a high frame rate (MacroQuest's frame"
  " limiter suspended, about 190 to 230 fps), 2026-10-12"
)
walkedBodyRows = (
  ("DWM", None, 4.5, 86, 87, 2.81, 2.82, 5.5, 5.25, 2.5, 2.25, 2.5),
  ("GNF", None, 4.5, 86, 87, 2.17, 2.18, 4.5, 4.25, 2.0, 1.75, 1.875),
  ("DKM", 5.3, None, 86, 87, 4.37, 4.38, 7.75, 7.5, 4.0, 3.75, 4.0625),
  ("ELF", 4.95, 5.0, 85, 86, 3.45, 3.45, 6.25, 6.0, 3.0, 2.75, 3.125),
  ("HUM", 5.3, None, 85, 86, 4.06, 4.07, 7.25, 7.0, 3.75, 3.5, 3.75),
  ("OGM", 5.3, None, 85, 86, 5.81, 5.81, 10.5, 10.25, 5.25, 5.0, 5.5),
  ("KEF", 5.3, None, 85, 86, 4.68, 4.69, 8.25, 8.0, 4.25, 4.0, 4.375),
)
# Each group's sample by role. Bodies often tie in the bodies table (the dwarves' avatarHeight, the ogres', the human and erudite
# males' width); the owner chose the one walked. bodyGroup checks each holds its role's extreme.
sampleRoles = {
  "small": {"tallest": "DWM", "shortest": "GNF", "widest": "DWM", "thinnest": "GNF"},
  "medium": {"tallest": "DKM", "shortest": "ELF", "widest": "HUM", "thinnest": "ELF"},
  "large": {"tallest": "OGM", "shortest": "KEF", "widest": "OGM", "thinnest": "KEF"},
}
# The body whose step the risers bracket. Where the course does not bracket a body's step, the step is taken to follow avatarHeight by
# this body's ratio (1.584 times avatarHeight); every bracket and bound the walks give agrees with it.
stepRatioBody = "ELF"


@dataclass(frozen=True)
class WalkedBody:
  code: str
  stepClimbed: float
  stepRefused: float
  slopeClimbed: float
  slopeRefused: float
  swimWalkedTo: float
  swimFloatedFrom: float
  ceilingPassed: float
  ceilingStopped: float
  openingPassed: float
  openingStopped: float
  avatarHeight: float

  def __post_init__(self):
    for name in ("slopeClimbed", "swimWalkedTo", "swimFloatedFrom", "ceilingPassed", "ceilingStopped", "openingPassed", "openingStopped", "avatarHeight"):
      if getattr(self, name) is None:
        raise ValueError(f"Walked body {self.code} has no {name}")
    if self.stepClimbed is None and self.stepRefused is None:
      raise ValueError(f"Walked body {self.code} has neither a riser climbed nor one refused")
    if self.stepClimbed is not None and self.stepRefused is not None and self.stepClimbed >= self.stepRefused:
      raise ValueError(f"Walked body {self.code} climbed {self.stepClimbed}, not under the {self.stepRefused} it was refused")


walkedBodies = {row[0]: WalkedBody(*row) for row in walkedBodyRows}


@dataclass(frozen=True)
class Body:
  race: int
  gender: int
  code: str
  spawnHeight: float
  modelHeight: float
  width: float
  depth: float
  avatarHeight: float

  @property
  def name(self):
    return f"{raceNames[self.race]} {genderNames[self.gender]}"

  def described(self):
    return {
      "race": self.race, "gender": self.gender, "name": self.name, "model": self.code, "spawnHeight": self.spawnHeight, "modelHeight": self.modelHeight,
      "width": self.width, "depth": self.depth, "avatarHeight": self.avatarHeight, "standing": self.code not in bindPoseCodes,
    }


bodies = tuple(Body(*row) for row in bodyRows)


@dataclass(frozen=True)
class BodyGroup:
  name: str
  members: tuple
  tallest: Body
  shortest: Body
  widest: Body
  thinnest: Body
  playerHeight: float
  playerWidth: float
  stepHeight: float
  walkableNormalZ: float
  eyeHeight: float
  swimEyeAboveSurface: float
  minimumSwimDepth: float
  sources: dict

  @property
  def steepestWalkableDegrees(self):
    return math.degrees(math.acos(self.walkableNormalZ))

  def measure(self, measureName):
    return {"value": getattr(self, measureName), "source": self.sources[measureName]}

  def described(self):
    return {
      "group": self.name, "races": [raceNames[race] for race in groupRaces[self.name]],
      "limitingBodies": {role: getattr(self, role).name for role in sampleRoleNames},
    } | {name: self.measure(name) for name in measureNames}


measureNames = ("playerHeight", "playerWidth", "stepHeight", "walkableNormalZ", "steepestWalkableDegrees", "eyeHeight", "swimEyeAboveSurface", "minimumSwimDepth")


def walkedName(code):
  return next(body.name for body in bodies if body.code == code)


sampleRoleNames = ("tallest", "shortest", "widest", "thinnest")
sampleExtremes = {"tallest": (max, "avatarHeight"), "shortest": (min, "avatarHeight"), "widest": (max, "width"), "thinnest": (min, "width")}


def sampleOf(name, roles, standing):
  """The group's sample bodies by role, each checked to hold its role's extreme among the group's standing bodies."""
  if set(roles) != set(sampleRoleNames):
    raise ValueError(f"The {name} group's sample names {sorted(roles)}, not {list(sampleRoleNames)}")
  sample = {}
  for role in sampleRoleNames:
    found = [body for body in standing if body.code == roles[role]]
    if not found:
      raise ValueError(f"The {name} group's {role} sample body {roles[role]} is no standing body of the group")
    pick, field = sampleExtremes[role]
    extreme = pick(getattr(body, field) for body in standing)
    if getattr(found[0], field) != extreme:
      raise ValueError(f"The {name} group's {role} sample body {roles[role]} has {field} {getattr(found[0], field)}, not the group's {extreme}")
    sample[role] = found[0]
  return sample


def walkedOf(name):
  """The group's walked bodies: its sample's, every one walked, in walkedBodyRows' order."""
  codes = set(sampleRoles[name].values())
  unwalked = sorted(codes - set(walkedBodies))
  if unwalked:
    raise ValueError(f"The {name} group's sample bodies {unwalked} were not walked")
  return [walkedBodies[row[0]] for row in walkedBodyRows if row[0] in codes]


def stepOf(walked):
  """The step a walked body climbs: the highest riser it climbed, or, where it climbed none, its avatarHeight times stepRatioBody's
  ratio, which must lie under the lowest riser it was refused."""
  if walked.stepClimbed is not None:
    return walked.stepClimbed
  ratioBody = walkedBodies[stepRatioBody]
  if ratioBody.stepClimbed is None or ratioBody.stepRefused is None:
    raise ValueError(f"{stepRatioBody}'s step is not bracketed by the risers, so it gives no ratio")
  value = walked.avatarHeight * ratioBody.stepClimbed / ratioBody.avatarHeight
  if value >= walked.stepRefused:
    raise ValueError(f"{walked.code}'s step from {stepRatioBody}'s ratio, {value:.2f}, is not under the {walked.stepRefused} it was refused")
  return value


def bracketText(climbed, refused, unit=""):
  low = "none" if climbed is None else f"{climbed:g}{unit}"
  high = "none" if refused is None else f"{refused:g}{unit}"
  return f"climbed up to {low}, stopped from {high}"


def groupSources(walked):
  ratioBody = walkedBodies[stepRatioBody]
  step = min(walked, key=stepOf)
  slope = min(walked, key=lambda body: body.slopeClimbed)
  swim = min(walked, key=lambda body: body.swimWalkedTo)
  ceiling = max(walked, key=lambda body: body.ceilingPassed)
  opening = max(walked, key=lambda body: body.openingPassed)
  walkedList = ", ".join(walkedName(body.code) for body in walked)
  stepWalk = (
    f"the least step of the group's walked bodies ({walkedList}), the {walkedName(step.code)}'s, {measurementWalk}: on risers 4.50 to"
    f" 5.30 by 0.05 it {bracketText(step.stepClimbed, step.stepRefused)}"
  )
  if step.stepClimbed is None:
    stepText = (
      f"derived: the {walkedName(step.code)}'s avatarHeight {step.avatarHeight:g} times the {walkedName(stepRatioBody)}'s step over its"
      f" avatarHeight ({ratioBody.stepClimbed:g} / {ratioBody.avatarHeight:g}), under the {step.stepRefused:g} riser it was refused; {stepWalk}"
    )
  elif step.stepRefused is None:
    stepText = f"measured: {stepWalk}, the course's highest riser, so the limit lies above it"
  else:
    stepText = f"measured: {stepWalk}"
  if slope.slopeRefused is None:
    slopeText = (
      f"measured: every one of the group's walked bodies ({walkedList}), {measurementWalk}: on ramps of 70 to 89 degrees by 1 each"
      f" climbed up to {slope.slopeClimbed:g} degrees, the course's steepest, so the value is a lower bound and the limit lies steeper"
    )
  else:
    slopeText = (
      f"measured: the least steep limit of the group's walked bodies ({walkedList}), the {walkedName(slope.code)}'s, {measurementWalk}: on"
      f" ramps of 70 to 89 degrees by 1 it {bracketText(slope.slopeClimbed, slope.slopeRefused, ' degrees')}"
    )
  return {
    "stepHeight": stepText,
    "walkableNormalZ": slopeText,
    "minimumSwimDepth": (
      f"measured: the shallowest start of swimming among the group's walked bodies ({walkedList}), the {walkedName(swim.code)}'s,"
      f" {measurementWalk}: down a pool bed sloping 0 to 10 deep it walked the bed to {swim.swimWalkedTo:g} deep and floated from"
      f" {swim.swimFloatedFrom:g}; the value is the deepest walked, so the lane errs toward swimming"
    ),
    "playerHeight": (
      f"measured: as clearance, the most headroom among the group's walked bodies ({walkedList}), the {walkedName(ceiling.code)}'s,"
      f" {measurementWalk}: along ceilings stepping down from 14 to 3 by 0.25 it passed {ceiling.ceilingPassed:g} and was stopped by"
      f" {ceiling.ceilingStopped:g}"
    ),
    "playerWidth": (
      f"measured: as clearance, the widest passage among the group's walked bodies ({walkedList}), the {walkedName(opening.code)}'s,"
      f" {measurementWalk}: through openings narrowing from 10 to 0.5 by 0.25 it passed {opening.openingPassed:g} and was stopped by"
      f" {opening.openingStopped:g}"
    ),
  }


def bodyGroup(name):
  members = tuple(body for body in bodies if body.race in groupRaces[name])
  standing = [body for body in members if body.code not in bindPoseCodes]
  sample = sampleOf(name, sampleRoles[name], standing)
  tallest = sample["tallest"]
  walked = walkedOf(name)
  eyeHeight = designEyeHeight * tallest.avatarHeight / walkedAvatarHeight
  sources = groupSources(walked) | {
    "steepestWalkableDegrees": "derived: walkableNormalZ as the angle from level of the steepest face the group walks",
    "eyeHeight": (
      f"unmeasured: the design eye of eye-level views, {designEyeHeight:g} for a body of avatarHeight {walkedAvatarHeight:g} scaled by"
      f" the {tallest.name}'s avatarHeight ({tallest.avatarHeight:g}); RoF2's first-person eye sits 3.69 over a human male's origin"
      " (docs/clientRendering.md, Camera), which these views do not follow: a review camera's client pose (getReviewCameras) flies the"
      " character so its first-person eye sits at the design eye"
    ),
    "swimEyeAboveSurface": (
      f"unmeasured: a unit over the water keeps a swimming view's eye clear of the surface; in {rof2Walk}, a swimming player's origin"
      " floated 0.10 under the swim box's top, which does not give a view's eye"
    ),
  }
  return BodyGroup(
    name=name, members=members, tallest=tallest, shortest=sample["shortest"], widest=sample["widest"], thinnest=sample["thinnest"],
    playerHeight=max(body.ceilingPassed for body in walked), playerWidth=max(body.openingPassed for body in walked),
    stepHeight=min(stepOf(body) for body in walked), walkableNormalZ=math.cos(math.radians(min(body.slopeClimbed for body in walked))),
    eyeHeight=eyeHeight, swimEyeAboveSurface=swimEyeAboveSurfaceValue, minimumSwimDepth=min(body.swimWalkedTo for body in walked),
    sources=sources,
  )


groups = {name: bodyGroup(name) for name in groupNames}
unsampled = sorted(set(walkedBodies) - {code for roles in sampleRoles.values() for code in roles.values()})
if unsampled:
  raise ValueError(f"Walked bodies {unsampled} are in no group's sample")


def groupOfCode(code):
  """The group of the playable body drawn with a model code (the scale figure's dark elf female is a medium body)."""
  found = [body for body in bodies if body.code == code]
  if not found:
    raise ValueError(f"No playable body is drawn with the model {code}")
  return next(name for name in groupNames if found[0].race in groupRaces[name])


@dataclass(frozen=True)
class Strictest:
  """What every one of some groups passes: the least step, the least steep walkable face, and the most clearance among them."""
  groups: tuple
  stepHeight: float
  walkableNormalZ: float
  playerHeight: float
  playerWidth: float

  @property
  def steepestWalkableDegrees(self):
    return math.degrees(math.acos(self.walkableNormalZ))

  def described(self):
    return {"groups": list(self.groups), "stepHeight": self.stepHeight, "steepestWalkableDegrees": round(self.steepestWalkableDegrees, 1), "playerHeight": self.playerHeight}


def strictest(names):
  chosen = [groups[name] for name in checkedGroupNames(list(names))]
  return Strictest(
    groups=tuple(body.name for body in chosen), stepHeight=min(body.stepHeight for body in chosen),
    walkableNormalZ=max(body.walkableNormalZ for body in chosen), playerHeight=max(body.playerHeight for body in chosen),
    playerWidth=max(body.playerWidth for body in chosen),
  )


def highestOrigin():
  """The highest any playable body's origin stands over its feet (its avatarHeight)."""
  return max(body.avatarHeight for body in bodies)


def shortest():
  """The group with the least clearance, which any space some player fits holds."""
  return min(groups.values(), key=lambda candidate: candidate.playerHeight)


def tallest():
  """The group with the most clearance to judge by, which frames views of what any group meets."""
  return max(groups.values(), key=lambda candidate: candidate.playerHeight)


def planGroup(builtFor):
  """The group a plan of the lane shows when none is named: of the groups the zone is built for, the one judged by the most clearance,
  whose lane marks most of what some group cannot pass."""
  return max(builtFor, key=lambda name: groups[name].playerHeight)


def viewGroup(builtFor):
  """The group whose eye a view stands at when none is named: the middle of the groups the zone is built for (the larger of two)."""
  ordered = [name for name in groupNames if name in builtFor]
  return ordered[len(ordered) // 2]


def group(name):
  if name not in groups:
    raise ValueError(f"No body group '{name}': the groups are {list(groupNames)}")
  return groups[name]


def checkedGroupNames(names):
  """A builtFor list as given: one or more distinct group names, returned in groupNames' order."""
  if not isinstance(names, (list, tuple)) or not names:
    raise ValueError(f"builtFor is a list of one or more of {list(groupNames)}, got {names!r}")
  unknown = [name for name in names if name not in groups]
  if unknown:
    raise ValueError(f"builtFor names no body group {unknown}: the groups are {list(groupNames)}")
  if len(set(names)) != len(names):
    raise ValueError(f"builtFor names a group twice: {list(names)}")
  return tuple(name for name in groupNames if name in names)


def builtForAt(zoneBuiltFor, regionBuiltFor):
  """The groups a spot is built for: its region's setting, else its zone's, else every group."""
  if regionBuiltFor is not None:
    return tuple(regionBuiltFor)
  return tuple(zoneBuiltFor) if zoneBuiltFor is not None else groupNames


def judged(passingGroups, builtFor):
  """A spot's verdict from the groups that pass it: "fails" when a group it is built for does not, "finding" when only others do not,
  "passes" when every group does; with the groups that fail."""
  failing = [name for name in groupNames if name not in passingGroups]
  builtFailing = [name for name in failing if name in builtFor]
  verdict = "fails" if builtFailing else "finding" if failing else "passes"
  return {"verdict": verdict, "passingGroups": [name for name in groupNames if name in passingGroups], "failingGroups": failing, "builtFor": list(builtFor)}


def described():
  return {"bodies": [body.described() for body in bodies], "bodiesSource": bodiesSource, "groups": [groups[name].described() for name in groupNames]}
