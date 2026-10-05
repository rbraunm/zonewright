"""Housing as a zone designs it: first the zone's decision (whether it has housing, how central it is, what it is for, and how its plots
are priced), then plots placed one at a time. A plot is a guide on the ground (its outline and a mark at its entrance) with the
client's own border model at its center, as players will see it; plots are moved, turned, resized, graded, assessed, and priced from
where they lie and what they offer, and the zone's housing file is written from them. Runs under Blender's Python."""
import contextlib
import json
import math
import statistics

import bpy
import mathutils
import numpy

import bridgeCaveData
import bridgeGrading
import bridgeMeshAccess
import bridgeModels
import bridgeObjects
import bridgePasses
import bridgeShaping
import bridgeWater
import eqAxes
import playerScale

housingProperty = "zonewrightHousing"
plotProperty = "zonewrightPlot"
# The ground a plot is graded on is held by reference, so renaming the ground keeps the grading, and deleting it leaves the reference
# empty.
gradingProperty = "zonewrightPlotGrading"
housingCollectionName = "housing"
housingRoles = ("none", "incidental", "featured", "primary")
# Where the zone's plots live in Peridot's housing: in the public zone itself (its X3, world plots) or in instanced neighborhoods made
# from the zone as a template (its X2, custom neighborhood maps).
housingPlacements = ("world", "neighborhood")
plotKinds = ("player", "guild")
# Plot sizes [across, along] in Peridot's Sunrise Hills content, from Live's neighborhood map and border stones: the median player plot,
# and the guild plot. A plot's entrance is on one of its across sides; along runs from it to the back.
stockSizes = {"player": (169.1, 170.1), "guild": (351.2, 699.8)}
borderModels = {"player": "OBP_LOTSQUARE", "guild": "OBP_GUILDSQUARE"}
borderArchive = "stonesquare.eqg"
# The open type of a plot border door (EQSWITCH_REALESTATE_PLOT).
borderOpenType = 160
# Pricing at Live's plot limits: the middle tier's 105 items and 5 pets at 84pp, and per item and per pet so that Live's other tiers
# come out at its prices (90 items and 4 pets 42pp, 120 and 6 126pp); a guild plot 210 items and 12 pets at 21pp; upkeep a tenth of the
# price a day. Features scale the price.
defaultPricing = {
  "basePlatinum": {"player": 84, "guild": 21},
  "defaultItems": {"player": 105, "guild": 210},
  "defaultPets": {"player": 5, "guild": 12},
  "platinumPerItem": 2.1,
  "platinumPerPet": 10.5,
  "featureMultipliers": {"prominent": 1.5, "secluded": 1.5, "view": 1.25, "waterfront": 1.25, "sheltered": 1.25, "remote": 0.75, "swamp": 0.5},
  "upkeepShare": 0.1,
}
guideColors = {"player": (0.1, 0.85, 1.0), "guild": (1.0, 0.3, 0.9)}
guideBand = 1.5
guideLift = 0.4
# The entrance mark is a chevron with its tip on the middle of the entrance side, pointing out, kept within 9 units of that side: there
# it lies below the view of someone standing on the entrance looking in (eye 5.5 up, pitched 6 down), and seen from the street it
# stands on the plot's edge.
entranceSpan = 12.0
entranceDepth = 6.0
# assessPlot counts rock or roof this far over a plot's center as cover.
overheadReach = 1000.0
footprintStep = 8.0
gradeBatterReach = 600.0
gradeMinimumReach = 20.0
# A pass holds single-precision coordinates, so what it holds reads back within this of what was written.
heldTolerance = 1e-3
keptGradePrefix = "kept grade "
# Assessment: how far around a plot it looks, and what counts as a drop, a wall, water at its edge, seclusion, prominence. A view is
# judged by looking out from the plot, never measured.
sideProbes = (10.0, 30.0, 60.0)
entranceReach = 10.0
# Grading names the other plots whose ground it changed within this far of their edges, as far as assessPlot looks past each side.
touchedReach = sideProbes[-1]
sideRise = 15.0
waterfrontDistance = 40.0
surroundingRadii = (200.0, 400.0, 600.0)
enclosureReach = 300.0
enclosureDirections = 32
houseHeight = 20.0
secludedNeighbourDistance = 300.0
secludedRouteDistance = 150.0
prominenceRange = 1000.0
prominenceStep = 50.0
prominentShare = 0.3
prominentSamples = 10


# The zone's decision

def readHousing():
  stored = bpy.context.scene.get(housingProperty)
  return json.loads(stored) if stored is not None else None


def requireHousing():
  housing = readHousing()
  if housing is None:
    raise ValueError("The zone has made no housing decision; setZoneHousing says whether it has housing, how central it is, and how its plots are priced")
  if housing["role"] == "none":
    raise ValueError("The zone's housing role is none; it has no plots")
  return housing


def mergedPricing(current, changes):
  unknown = sorted(set(changes) - set(defaultPricing))
  if unknown:
    raise ValueError(f"Unknown pricing keys {unknown}; pricing takes {list(defaultPricing)}")
  pricing = json.loads(json.dumps(current))
  for key, value in changes.items():
    if isinstance(defaultPricing[key], dict):
      if not isinstance(value, dict):
        raise ValueError(f"pricing {key} is an object, got {value!r}")
      pricing[key] = pricing[key] | value
    else:
      pricing[key] = value
  for kindKey in ("basePlatinum", "defaultItems", "defaultPets"):
    if set(pricing[kindKey]) != set(plotKinds) or any(value < 0 for value in pricing[kindKey].values()):
      raise ValueError(f"pricing {kindKey} gives each of {list(plotKinds)} a value of at least 0, got {pricing[kindKey]!r}")
  if any(value <= 0 for value in pricing["featureMultipliers"].values()):
    raise ValueError(f"Feature multipliers must be positive, got {pricing['featureMultipliers']!r}")
  if not 0 <= pricing["upkeepShare"] <= 1 or pricing["platinumPerItem"] < 0 or pricing["platinumPerPet"] < 0:
    raise ValueError("upkeepShare is a fraction from 0 to 1, and platinumPerItem and platinumPerPet are at least 0")
  return pricing


def setZoneHousing(role, intent, placement, plotBudget, pricing, routes):
  current = readHousing() or {"role": None, "intent": None, "placement": None, "plotBudget": None, "pricing": defaultPricing, "routes": []}
  if role is not None:
    if role not in housingRoles:
      raise ValueError(f"role is one of {list(housingRoles)}, got '{role}'")
    if role == "none" and plotObjects():
      raise ValueError(f"The zone has plots {sorted(plot.name for plot in plotObjects())}; remove them before its housing role is none")
    current["role"] = role
  if intent is not None:
    if not intent.strip():
      raise ValueError("intent says what housing is for here: an empty intent says nothing")
    current["intent"] = intent.strip()
  if placement is not None:
    if placement not in housingPlacements:
      raise ValueError(f"placement is one of {list(housingPlacements)}, got '{placement}'")
    current["placement"] = placement
  if plotBudget is not None:
    if set(plotBudget) - set(plotKinds) or any(not isinstance(count, int) or count < 0 for count in plotBudget.values()):
      raise ValueError(f"plotBudget gives counts of {list(plotKinds)}, got {plotBudget!r}")
    current["plotBudget"] = {kind: plotBudget.get(kind, 0) for kind in plotKinds}
  if pricing is not None:
    current["pricing"] = mergedPricing(current["pricing"], pricing)
  if routes is not None:
    for route in routes:
      if len(route) < 2 or any(len(point) != 2 for point in route):
        raise ValueError(f"A route is at least two [x, y] points, got {route!r}")
    current["routes"] = routes
  missing = [key for key in ("role", "intent") if current[key] is None]
  if current["role"] not in (None, "none"):
    missing += [key for key in ("placement", "plotBudget") if current[key] is None]
  if missing:
    raise ValueError(f"A housing decision needs {missing}")
  bpy.context.scene[housingProperty] = json.dumps(current)
  return describeHousing()


def describeHousing():
  housing = readHousing()
  if housing is None:
    return {"housing": None}
  plots = [plotRecord(plot, housing) for plot in plotObjects()] if housing["role"] != "none" else []
  counts = {kind: sum(record["kind"] == kind for record in plots) for kind in plotKinds}
  overlaps = []
  objects = plotObjects()
  for index, first in enumerate(objects):
    for second in objects[index + 1:]:
      depth = overlapDepth(footprint(first), footprint(second))
      if depth > 0.01:
        overlaps.append({"plots": [first.name, second.name], "depth": round(depth, 2)})
  return {"housing": housing, "plotCounts": counts, "plots": plots, "overlaps": overlaps}


# Plots

def plotObjects():
  return sorted((sceneObject for sceneObject in bpy.context.scene.objects if plotProperty in sceneObject), key=lambda plot: plot.name)


def requirePlot(address):
  plot = bpy.context.scene.objects.get(address)
  if plot is None or plotProperty not in plot:
    raise ValueError(f"No plot '{address}'; plots: {[plot.name for plot in plotObjects()]}")
  bpy.context.view_layer.update()
  return plot


def readPlot(plot):
  return json.loads(plot[plotProperty])


def facingOf(plot):
  """The direction the plot's entrance faces, as a heading (0 = +Y, clockwise, degrees)."""
  return (-math.degrees(plot.matrix_world.to_euler("XYZ").z)) % 360


def frontDirection(facingDegrees):
  heading = math.radians(facingDegrees)
  return numpy.array([math.sin(heading), math.cos(heading)])


def footprintOf(center, facingDegrees, size, margin=0.0):
  """A footprint's corners in plan, counterclockwise from its front right."""
  across, along = size
  front = frontDirection(facingDegrees)
  right = numpy.array([front[1], -front[0]])
  middle = numpy.array(center[:2], dtype=numpy.float64)
  halfAcross, halfAlong = across / 2 + margin, along / 2 + margin
  return numpy.array([middle + right * a * halfAcross + front * b * halfAlong for a, b in ((1, 1), (-1, 1), (-1, -1), (1, -1))])


def footprint(plot, margin=0.0):
  return footprintOf(plot.matrix_world.translation[:2], facingOf(plot), readPlot(plot)["size"], margin)


def overlapDepth(first, second):
  """How deep two convex footprints overlap, by separating axes: 0 or less when apart."""
  depth = math.inf
  for corners in (first, second):
    for index in range(len(corners)):
      edge = corners[(index + 1) % len(corners)] - corners[index]
      axis = numpy.array([-edge[1], edge[0]]) / numpy.linalg.norm(edge)
      a, b = first @ axis, second @ axis
      depth = min(depth, min(a.max(), b.max()) - max(a.min(), b.min()))
  return depth


def guideMaterial(kind):
  name = f"zonewrightPlotGuide{kind.capitalize()}"
  material = bpy.data.materials.get(name)
  if material is None:
    material = bpy.data.materials.new(name)
    material.use_nodes = True
    nodes = material.node_tree.nodes
    for unused in [node for node in nodes if node.type == "BSDF_PRINCIPLED"]:
      nodes.remove(unused)
    emission = nodes.new("ShaderNodeEmission")
    emission.inputs["Color"].default_value = (*guideColors[kind], 1.0)
    output = next(node for node in nodes if node.type == "OUTPUT_MATERIAL")
    material.node_tree.links.new(emission.outputs["Emission"], output.inputs["Surface"])
  return material


def guideMesh(name, kind, size):
  """The plot's outline as a band just inside its edges, and a chevron pointing out of the middle of its entrance side (local +Y)."""
  across, along = size
  halfAcross, halfAlong = across / 2, along / 2
  outer = [(halfAcross, halfAlong), (-halfAcross, halfAlong), (-halfAcross, -halfAlong), (halfAcross, -halfAlong)]
  inner = [(x - math.copysign(guideBand, x), y - math.copysign(guideBand, y)) for x, y in outer]
  vertices = [(x, y, guideLift) for x, y in outer + inner]
  faces = [(index, (index + 1) % 4, 4 + (index + 1) % 4, 4 + index) for index in range(4)]
  tip = halfAlong
  armBack = tip - entranceDepth
  thickness = guideBand * math.hypot(entranceSpan / 2, entranceDepth) / (entranceSpan / 2)
  base = len(vertices)
  vertices += [(x, y, guideLift) for x, y in (
    (0, tip), (0, tip - thickness), (entranceSpan / 2, armBack), (entranceSpan / 2, armBack - thickness),
    (-entranceSpan / 2, armBack), (-entranceSpan / 2, armBack - thickness),
  )]
  faces += [(base + 2, base, base + 1, base + 3), (base + 1, base, base + 4, base + 5)]
  mesh = bpy.data.meshes.new(name)
  mesh.from_pydata(vertices, [], faces)
  mesh.validate()
  mesh.materials.append(guideMaterial(kind))
  return mesh


def borderOf(plot):
  return next((child for child in plot.children if bridgeMeshAccess.plotBorderProperty in child), None)


def setBorder(plot, kind, size, borderFolder):
  """Put the client's border model for the plot's kind at its center, turned so its open side is the plot's entrance and scaled as
  a door's size scales it (whole percents) to fit the plot."""
  old = borderOf(plot)
  if old is not None:
    oldMesh = old.data
    bpy.data.objects.remove(old)
    if oldMesh.users == 0:
      bpy.data.meshes.remove(oldMesh)
  stockAcross, stockAlong = stockSizes[kind]
  scale = round(min(size[0] / stockAcross, size[1] / stockAlong) * 100) / 100
  border = bridgeModels.modelObject(borderFolder, f"{plot.name} border", scale, (0, 0, 0), -90)
  corners = numpy.array([list(vertex.co) for vertex in border.data.vertices])
  middle = (corners.min(0) + corners.max(0)) / 2 * scale
  # Turned a quarter clockwise, the model's x runs along the plot toward its back and its y across it.
  border.location = (-middle[1], middle[0], 0.0)
  border[bridgeMeshAccess.plotBorderProperty] = plot.name
  border.parent = plot
  for collection in plot.users_collection:
    collection.objects.link(border)
  stockBorder = (corners.max(0) - corners.min(0))[:2]
  return {"model": borderModels[kind], "size": round(scale * 100), "borderAcross": round(float(stockBorder[1] * scale), 1), "borderAlong": round(float(stockBorder[0] * scale), 1)}


@contextlib.contextmanager
def ungradedGround():
  """The ground players stand on with every plot's grading taken out: what plots are seated on, whatever has been graded around them."""
  muted = []
  for plot in plotObjects():
    grading = gradingOf(plot)
    key = None if grading is None else gradeKey(grading["ground"], plot.name)
    if key is not None and not key.mute:
      key.mute = True
      muted.append(key)
  try:
    yield bridgeWater.Ground()
  finally:
    for key in muted:
      key.mute = False


def seatGround(centers):
  """The ungraded ground's height under each center (None where there is none), and where rock lies over ground there, its levels
  (bridgeMeshAccess.rockOverGround)."""
  with ungradedGround() as ground:
    top = bridgeMeshAccess.sceneTopHeight() + 1
    return [(ground.heightBelow(x, y, top), bridgeMeshAccess.rockOverGround(ground.castWithNormal, x, y, top)) for x, y in centers]


def seatHeight(center):
  """The ungraded ground's height at a plot's center; refused where there is none, or where rock lies over ground and either could be meant."""
  height, levels = seatGround([center])[0]
  if height is None:
    raise ValueError(f"No ground under {center} to set the plot on")
  if levels is not None:
    raise ValueError(f"A plot's ground is a choice here: {bridgeMeshAccess.describeRockOverGround(center, levels)}; give the plot its height, the ground under the rock or its top")
  return height


def requireSize(kind, size):
  if size is None:
    return list(stockSizes[kind])
  if len(size) != 2 or min(size) <= 0:
    raise ValueError(f"size is [across, along] in units, both positive, got {size!r}")
  return [float(value) for value in size]


def requireTags(tags, housing):
  known = housing["pricing"]["featureMultipliers"]
  unknown = sorted(set(tags) - set(known))
  if unknown:
    raise ValueError(f"Features {unknown} are not in the zone's pricing; its features: {sorted(known)} (setZoneHousing pricing.featureMultipliers adds more)")
  return sorted(set(tags))


def plotPrice(spec, housing):
  """The plot's price from the zone's pricing: its kind's base scaled by its area against a stock plot, plus or less its item and pet
  allowances against the kind's defaults, times each feature's multiplier; or its override."""
  pricing = housing["pricing"]
  kind = spec["kind"]
  stockArea = stockSizes[kind][0] * stockSizes[kind][1]
  area = spec["size"][0] * spec["size"][1]
  steps = [{"step": f"{kind} base", "platinum": pricing["basePlatinum"][kind]}]
  price = pricing["basePlatinum"][kind] * area / stockArea
  if abs(area - stockArea) > 1e-6:
    steps.append({"step": f"area {area / stockArea:.2f} of a stock plot", "platinum": round(price, 2)})
  itemChange = (spec["items"] - pricing["defaultItems"][kind]) * pricing["platinumPerItem"]
  petChange = (spec["pets"] - pricing["defaultPets"][kind]) * pricing["platinumPerPet"]
  if itemChange:
    price += itemChange
    steps.append({"step": f"{spec['items']} items against {pricing['defaultItems'][kind]}", "platinum": round(price, 2)})
  if petChange:
    price += petChange
    steps.append({"step": f"{spec['pets']} pets against {pricing['defaultPets'][kind]}", "platinum": round(price, 2)})
  for tag in spec["tags"]:
    price *= pricing["featureMultipliers"][tag]
    steps.append({"step": f"{tag} x{pricing['featureMultipliers'][tag]:g}", "platinum": round(price, 2)})
  derived = max(1, round(price))
  final = spec["priceOverride"] if spec["priceOverride"] is not None else derived
  return {"pricePlatinum": final, "derivedPlatinum": derived, "overridden": spec["priceOverride"] is not None, "steps": steps,
    "upkeepPlatinumPerDay": round(final * pricing["upkeepShare"], 2)}


def plotRecord(plot, housing):
  spec = readPlot(plot)
  border = borderOf(plot)
  return {
    "address": plot.name, "kind": spec["kind"], "center": bridgeObjects.roundVector(plot.matrix_world.translation, 2),
    "facingDegrees": round(facingOf(plot), 2), "size": spec["size"], "items": spec["items"], "pets": spec["pets"], "tags": spec["tags"],
    "border": None if border is None else {"size": round(border.scale[0] * 100)}, "grading": gradingRecord(plot),
  } | plotPrice(spec, housing)


def placePlot(address, kind, center, facingDegrees, size, height, items, pets, tags, pricePlatinum, borderFolder, collection):
  housing = requireHousing()
  if not address.strip():
    raise ValueError("A plot needs its address, such as 101 Canyon Way")
  bridgeObjects.requireNewName(address)
  if kind not in plotKinds:
    raise ValueError(f"kind is one of {list(plotKinds)}, got '{kind}'")
  if len(center) != 2:
    raise ValueError(f"center is [x, y], got {center!r}")
  size = requireSize(kind, size)
  pricing = housing["pricing"]
  spec = {
    "kind": kind, "size": size, "items": pricing["defaultItems"][kind] if items is None else items,
    "pets": pricing["defaultPets"][kind] if pets is None else pets, "tags": requireTags(tags or [], housing),
    "priceOverride": requirePrice(pricePlatinum),
  }
  requireAllowances(spec)
  if height is None:
    height = seatHeight(center)
  plot = bpy.data.objects.new(address, guideMesh(address, kind, size))
  plot[plotProperty] = json.dumps(spec)
  plot[bridgeMeshAccess.guideProperty] = True
  plot.location = (center[0], center[1], height)
  plot.rotation_euler = (0, 0, -math.radians(facingDegrees))
  bridgeObjects.targetCollection(collection or housingCollectionName).objects.link(plot)
  border = setBorder(plot, kind, size, borderFolder)
  bpy.context.view_layer.update()
  return plotRecord(plot, housing) | {"border": border, "overlaps": overlapsOf(plot)}


def requirePrice(pricePlatinum):
  if pricePlatinum is None:
    return None
  if not isinstance(pricePlatinum, int) or pricePlatinum < 1:
    raise ValueError(f"A price is a whole number of platinum, at least 1, got {pricePlatinum!r}")
  return pricePlatinum


def requireAllowances(spec):
  if not isinstance(spec["items"], int) or not isinstance(spec["pets"], int) or spec["items"] < 1 or spec["pets"] < 0:
    raise ValueError(f"items is a whole number of at least 1 and pets at least 0, got {spec['items']!r} and {spec['pets']!r}")


def overlapsOf(plot):
  mine = footprint(plot)
  return [{"plot": other.name, "depth": round(depth, 2)} for other in plotObjects() if other != plot for depth in [overlapDepth(mine, footprint(other))] if depth > 0.01]


def editPlot(address, newAddress, kind, center, facingDegrees, size, height, items, pets, tags, pricePlatinum, objectName, borderFolder):
  housing = requireHousing()
  plot = requirePlot(address)
  spec = readPlot(plot)
  changes = [center, facingDegrees, size, height, items, pets, tags, pricePlatinum, kind, newAddress, objectName]
  if all(change is None for change in changes):
    raise ValueError("editPlot needs something to change")
  grading = gradingOf(plot)
  if objectName is not None and grading is None:
    raise ValueError(f"Plot '{address}' is not graded; objectName names the ground a graded plot is graded on (gradePlot grades it)")
  ground = None if grading is None else grading["ground"] if objectName is None else bridgeMeshAccess.requireEditableMesh(objectName, "editPlot")
  rebuild = False
  if kind is not None and kind != spec["kind"]:
    if kind not in plotKinds:
      raise ValueError(f"kind is one of {list(plotKinds)}, got '{kind}'")
    spec["kind"] = kind
    spec["size"] = list(stockSizes[kind]) if size is None else spec["size"]
    rebuild = True
  if size is not None:
    spec["size"] = requireSize(spec["kind"], size)
    rebuild = True
  if rebuild and borderFolder is None:
    raise ValueError("A plot whose kind or size changes needs its border model folder")
  if items is not None:
    spec["items"] = items
  if pets is not None:
    spec["pets"] = pets
  requireAllowances(spec)
  if tags is not None:
    spec["tags"] = requireTags(tags, housing)
  if pricePlatinum is not None:
    spec["priceOverride"] = None if pricePlatinum == 0 else requirePrice(pricePlatinum)
  name = plot.name
  if newAddress is not None and newAddress != plot.name:
    if not newAddress.strip():
      raise ValueError("A plot needs its address")
    bridgeObjects.requireNewName(newAddress)
    name = newAddress
  location = numpy.array(plot.matrix_world.translation)
  if center is not None:
    if len(center) != 2:
      raise ValueError(f"center is [x, y], got {center!r}")
    location[:2] = center
    if height is None:
      location[2] = seatHeight(center)
  if height is not None:
    location[2] = height
  facing = facingOf(plot) if facingDegrees is None else facingDegrees
  plans = []
  if grading is not None and (name != plot.name or center is not None or height is not None or facingDegrees is not None or rebuild or ground != grading["ground"]):
    pad = padOf(name, location, facing, spec["size"], location[2], grading)
    if ground == grading["ground"]:
      plans.append(bridgeGrading.planPlots(ground, {plot.name: None, name: pad}, {plot.name, name}))
    else:
      plans += [bridgeGrading.planPlots(grading["ground"], {plot.name: None}, {plot.name}), bridgeGrading.planPlots(ground, {name: pad}, {name})]
  plot.name = name
  plot.data.name = name
  plot.location = location.tolist()
  if facingDegrees is not None:
    plot.rotation_euler = (0, 0, -math.radians(facingDegrees))
  plot[plotProperty] = json.dumps(spec)
  if grading is not None:
    setGrading(plot, ground, grading["margin"], grading["batterDegrees"])
  border = None
  if rebuild:
    oldMesh = plot.data
    plot.data = guideMesh(plot.name, spec["kind"], spec["size"])
    bpy.data.meshes.remove(oldMesh)
    border = setBorder(plot, spec["kind"], spec["size"], borderFolder)
  else:
    existing = borderOf(plot)
    if existing is not None:
      existing.name = f"{plot.name} border"
      existing[bridgeMeshAccess.plotBorderProperty] = plot.name
  bpy.context.view_layer.update()
  graded = {}
  if plans:
    applied = [bridgeGrading.applyPlots(plan) for plan in plans]
    graded = {"grading": applied[-1] | padSummary(bridgeGrading.plotPlanOf(plans[-1]), name) | ({"formerGround": applied[0]} if len(plans) > 1 else {})}
  return plotRecord(plot, housing) | ({"border": border} if border else {}) | {"overlaps": overlapsOf(plot)} | graded


def removePlot(address, keepGrading):
  """Remove a plot and its border, taking its grading back unless keepGrading keeps it as ordinary shaping."""
  plot = requirePlot(address)
  stored = plot.get(gradingProperty)
  plan = None
  if stored is not None and stored["ground"] is None and not keepGrading:
    raise ValueError(f"Plot '{address}' was graded on ground that no longer exists, so there is no grading to take back; removePlot with keepGrading true removes the plot alone")
  if stored is not None and stored["ground"] is not None:
    sceneObject = stored["ground"]
    key = gradeKey(sceneObject, address)
    if key is None and not keepGrading:
      raise ValueError(f"'{sceneObject.name}' has no shaping pass '{gradePassName(address)}' to take back (were its passes collapsed?); removePlot with keepGrading true keeps the ground as it is")
    keys = sceneObject.data.shape_keys
    if keepGrading and key is not None and keys.key_blocks.get(keptGradePrefix + address) is not None:
      raise ValueError(f"'{sceneObject.name}' already has a shaping pass '{keptGradePrefix}{address}'; rename or remove it first")
    plan = bridgeGrading.planPlots(sceneObject, {address: None}, {address}, kept=address if keepGrading else None)
  removed = [plot.name]
  border = borderOf(plot)
  if border is not None:
    removed.append(border.name)
    borderMesh = border.data
    bpy.data.objects.remove(border)
    if borderMesh.users == 0:
      bpy.data.meshes.remove(borderMesh)
  guide = plot.data
  bpy.data.objects.remove(plot)
  bpy.data.meshes.remove(guide)
  return {"removed": removed, "grading": None if plan is None else bridgeGrading.applyPlots(plan)}


def gradePassName(address):
  return f"grade {address}"


def gradeKey(sceneObject, address):
  keys = sceneObject.data.shape_keys
  return None if keys is None else keys.key_blocks.get(gradePassName(address))


def setGrading(plot, ground, margin, batterDegrees):
  plot[gradingProperty] = {"ground": ground, "margin": float(margin), "batterDegrees": float(batterDegrees)}


def gradingOf(plot):
  """A plot's grading (the ground it is graded on, its margin and batter), or None when it is not graded."""
  stored = plot.get(gradingProperty)
  if stored is None:
    return None
  if stored["ground"] is None:
    raise ValueError(f"Plot '{plot.name}' was graded on ground that no longer exists; grade it on the ground it stands on (gradePlot), or remove it (removePlot with keepGrading true)")
  return {"ground": stored["ground"], "margin": stored["margin"], "batterDegrees": stored["batterDegrees"]}


def gradingRecord(plot):
  stored = plot.get(gradingProperty)
  if stored is None:
    return None
  return {"object": None if stored["ground"] is None else stored["ground"].name, "margin": stored["margin"], "batterDegrees": stored["batterDegrees"]}


def gradedOn(sceneObject):
  """The plots graded on an object, by address."""
  return {plot.name: plot for plot in plotObjects() if (stored := plot.get(gradingProperty)) is not None and stored["ground"] == sceneObject}


def padOf(address, center, facingDegrees, size, height, grading):
  """A graded plot's pad: its footprint, that grown by its margin (all level at its height), and the slope of its batters."""
  return {
    "plot": address, "footprint": footprintOf(center, facingDegrees, size), "outline": footprintOf(center, facingDegrees, size, grading["margin"]),
    "height": float(height), "slope": math.tan(math.radians(grading["batterDegrees"])),
  }


def plotPad(plot):
  return padOf(plot.name, plot.matrix_world.translation, facingOf(plot), readPlot(plot)["size"], plot.matrix_world.translation.z, gradingOf(plot))


def heldGrading(sceneObject, addresses):
  """The mesh as seen, and the rows each of the named plots' passes moves, and how far."""
  shown, _ = bridgeMeshAccess.readVertexArrays(sceneObject)
  keys = sceneObject.data.shape_keys
  held = {}
  if keys is not None:
    reference = bridgePasses.keyCoordinates(keys.reference_key)
    for address in addresses:
      key = gradeKey(sceneObject, address)
      if key is not None and not key.mute:
        offsets = (bridgePasses.keyCoordinates(key) - reference) * key.value
        rows = numpy.flatnonzero(numpy.abs(offsets).max(axis=1) > 0)
        held[address] = (rows, offsets[rows])
  return shown, held


class GroundLookup:
  """A mesh's vertices sorted along x and its edges by vertex, so a pad looks only at the ground near it."""

  def __init__(self, sceneObject, ground):
    self.sceneObject, self.ground = sceneObject, ground
    self.order = numpy.argsort(ground[:, 0], kind="stable")
    self.xs = ground[self.order, 0]
    self.edges = numpy.stack(bridgeShaping.meshEdgeEnds(sceneObject), axis=1)
    ends = numpy.concatenate([self.edges[:, 0], self.edges[:, 1]])
    self.byVertex = numpy.argsort(ends, kind="stable")
    self.firsts = numpy.searchsorted(ends[self.byVertex], numpy.arange(len(ground) + 1))

  def inBox(self, low, high):
    rows = self.order[numpy.searchsorted(self.xs, low[0], "left"):numpy.searchsorted(self.xs, high[0], "right")]
    y = self.ground[rows, 1]
    return numpy.sort(rows[(y >= low[1]) & (y <= high[1])])

  def medianEdgeLength(self, rows):
    """The median length of the edges touching the vertices at rows."""
    counts = self.firsts[rows + 1] - self.firsts[rows]
    slots = numpy.repeat(self.firsts[rows] - numpy.cumsum(counts) + counts, counts) + numpy.arange(counts.sum())
    touching = self.edges[numpy.unique(self.byVertex[slots] % len(self.edges))]
    return float(numpy.median(numpy.linalg.norm(self.ground[touching[:, 0]] - self.ground[touching[:, 1]], axis=1)))


def padReach(lookup, pad):
  """The vertices a pad's batters reach, how far each lies beyond its level ground, and how far they reach."""
  ground = lookup.ground
  reach, edgeLength = gradeMinimumReach, None
  while True:
    candidates = lookup.inBox(pad["outline"].min(axis=0) - reach, pad["outline"].max(axis=0) + reach)
    outside = -bridgeShaping.signedDistanceToOutline(ground[candidates], pad["outline"])[0]
    within = outside <= reach
    if edgeLength is None:
      if not within.any():
        raise ValueError(f"No vertex of '{lookup.sceneObject.name}' lies under or near plot '{pad['plot']}'; grade it on the ground it stands on (objectName)")
      edgeLength = lookup.medianEdgeLength(candidates[within])
    needed = max(gradeMinimumReach, float(numpy.abs(ground[candidates[within], 2] - pad["height"]).max()) / pad["slope"] + 2 * edgeLength)
    if needed > gradeBatterReach:
      raise ValueError(f"Plot '{pad['plot']}' stands {needed * pad['slope']:.0f} units off the ground around it; its slopes would reach {needed:.0f} units out. Move it, change its height, or steepen batterDegrees")
    if needed <= reach:
      return candidates[within], numpy.maximum(outside[within], 0.0), reach
    reach = needed + edgeLength


def gradedHeights(ground, pads, reached):
  """The ground graded to every pad at once, the pad holding each vertex's change (-1 for none), and banks steeper than the batters."""
  # Each vertex is kept between the lowest and highest the batters reaching it allow, so the result does not depend on the order plots
  # were graded in. Where two pads stand too close for their difference in height, every batter between them steepens alike, just
  # enough that both edges are met; where two pads' level ground overlaps, a vertex takes the height of the footprint it lies deeper in.
  heights = ground[:, 2]
  count = len(heights)
  steepening = numpy.ones(count)
  banks = {}
  tied = numpy.zeros(count, dtype=bool)
  tiedPads = set()
  boxes = [(ground[rows, :2].min(axis=0), ground[rows, :2].max(axis=0)) for rows, _, _ in reached]
  for first in range(len(pads)):
    for second in range(first + 1, len(pads)):
      rise = abs(pads[first]["height"] - pads[second]["height"])
      if rise == 0 or (boxes[first][0] > boxes[second][1]).any() or (boxes[second][0] > boxes[first][1]).any():
        continue
      rows, inFirst, inSecond = numpy.intersect1d(reached[first][0], reached[second][0], assume_unique=True, return_indices=True)
      if not len(rows):
        continue
      run = reached[first][1][inFirst] * pads[first]["slope"] + reached[second][1][inSecond] * pads[second]["slope"]
      level = run == 0
      if level.any():
        tied[rows[level]] = True
        tiedPads |= {first, second}
      ratio = rise / numpy.where(level, 1.0, run)
      steepening[rows[~level]] = numpy.maximum(steepening[rows[~level]], ratio[~level])
      worst = math.inf if level.any() else float(ratio.max())
      if worst > 1:
        pair = (pads[first]["plot"], pads[second]["plot"])
        banks[pair] = 90.0 if worst == math.inf else math.degrees(math.atan(worst * max(pads[first]["slope"], pads[second]["slope"])))
  low, high = numpy.full(count, -numpy.inf), numpy.full(count, numpy.inf)
  lowPad, highPad = numpy.full(count, -1), numpy.full(count, -1)
  for index, (pad, (rows, excess, _)) in enumerate(zip(pads, reached)):
    spread = excess * pad["slope"] * steepening[rows]
    padLow, padHigh = pad["height"] - spread, pad["height"] + spread
    raising, lowering = padLow > low[rows], padHigh < high[rows]
    low[rows[raising]], lowPad[rows[raising]] = padLow[raising], index
    high[rows[lowering]], highPad[rows[lowering]] = padHigh[lowering], index
  graded = numpy.minimum(numpy.maximum(heights, low), high)
  owner = numpy.where(graded > heights, lowPad, numpy.where(graded < heights, highPad, -1))
  if tied.any():
    rows = numpy.flatnonzero(tied)
    deepest, winner = numpy.full(len(rows), -numpy.inf), numpy.full(len(rows), -1)
    for index in sorted(tiedPads):
      padRows, excess, _ = reached[index]
      onLevel = numpy.isin(rows, padRows[excess == 0])
      depth = numpy.where(onLevel, bridgeShaping.signedDistanceToOutline(ground[rows], pads[index]["footprint"])[0], -numpy.inf)
      deeper = depth > deepest
      deepest[deeper], winner[deeper] = depth[deeper], index
    graded[rows] = numpy.array([pad["height"] for pad in pads])[winner]
    owner[rows] = winner
  return graded, owner, [{"plots": list(pair), "steepestDegrees": round(degrees, 1)} for pair, degrees in sorted(banks.items())]


def planGrading(sceneObject, overrides, subjects, kept, ground):
  """Work out the grading of every plot on an object, overrides {address: pad or None} applied, on ground (world positions of the mesh
  without the plots' passes, as bridgeGrading.planPlots gives it), before anything changes."""
  current = gradedOn(sceneObject)
  pads = {address: plotPad(plot) for address, plot in current.items()} | overrides
  pads = [pad for _, pad in sorted(pads.items()) if pad is not None]
  owned = (set(current) | set(overrides)) - {kept}
  shown, held = heldGrading(sceneObject, sorted(owned))
  lookup = GroundLookup(sceneObject, ground)
  reached = [padReach(lookup, pad) for pad in pads]
  graded, owner, banks = gradedHeights(ground, pads, reached)
  return {
    "object": sceneObject, "shown": shown, "ground": ground, "graded": graded, "owner": owner, "pads": pads, "reaches": [reach for _, _, reach in reached],
    "held": held, "banks": banks, "stale": sorted(owned - {pad["plot"] for pad in pads}), "kept": kept, "subjects": subjects,
  }


def applyGrading(plan):
  """Write a worked-out grading into the plots' passes, each holding the part of the change its own pad makes."""
  sceneObject = plan["object"]
  keys = sceneObject.data.shape_keys
  active = None if keys is None or sceneObject.active_shape_key in (None, keys.reference_key) else sceneObject.active_shape_key.name
  if plan["kept"] is not None and (key := gradeKey(sceneObject, plan["kept"])) is not None:
    key.name = keptGradePrefix + plan["kept"]
  for address in plan["stale"]:
    if gradeKey(sceneObject, address) is not None:
      bridgePasses.removeShapingPass(sceneObject.name, gradePassName(address))
  for pad in plan["pads"]:
    if gradeKey(sceneObject, pad["plot"]) is None:
      bridgePasses.addShapingPass(sceneObject.name, gradePassName(pad["plot"]))
  change = plan["graded"] - plan["ground"][:, 2]
  if plan["pads"]:
    local = change[:, None] * bridgeMeshAccess.matrixArray(sceneObject.matrix_world.inverted())[:3, 2][None, :]
    reference = bridgePasses.keyCoordinates(sceneObject.data.shape_keys.reference_key)
    for index, pad in enumerate(plan["pads"]):
      key = gradeKey(sceneObject, pad["plot"])
      rows = numpy.flatnonzero(plan["owner"] == index)
      padLocal = numpy.zeros_like(local)
      padLocal[rows] = local[rows]
      padLocal, _ = bridgeCaveData.guardedOffsets(sceneObject, padLocal)
      rows = numpy.union1d(rows, numpy.flatnonzero(numpy.abs(padLocal).max(axis=1) > 0))
      previous = plan["held"].get(pad["plot"])
      if previous is not None and key.value == 1.0 and numpy.array_equal(previous[0], rows) and numpy.allclose(previous[1], padLocal[rows], rtol=0, atol=heldTolerance):
        continue
      key.data.foreach_set("co", (reference + padLocal).astype(numpy.float32).ravel())
      key.mute, key.value = False, 1.0
  keys = sceneObject.data.shape_keys
  if keys is not None:
    # Grading rewrites the plots' passes whole, so shaping must never go into one: the pass active before stays active, or none is.
    gradeNames = {gradePassName(address) for address in plan["stale"]} | {gradePassName(pad["plot"]) for pad in plan["pads"]}
    keep = active is not None and active not in gradeNames and keys.key_blocks.get(active) is not None
    sceneObject.active_shape_key_index = list(keys.key_blocks).index(keys.key_blocks[active]) if keep else 0
  sceneObject.data.update()
  after, _ = bridgeMeshAccess.readVertexArrays(sceneObject)
  difference = numpy.abs(after[:, 2] - plan["shown"][:, 2])
  shaped = difference > heldTolerance
  folded = 0
  if shaped.any():
    bridgeShaping.triangulateAlongContours(sceneObject, after, shaped)
    folded = int(bridgeShaping.overturnedFaces(sceneObject, after, shaped).sum())
  changed = numpy.flatnonzero(difference > 0.01)
  touched = []
  for plot in plotObjects():
    if plot.name in plan["subjects"]:
      continue
    near = changed[bridgeMeshAccess.insidePolygon(after[changed, :2], footprint(plot, touchedReach))]
    if len(near):
      touched.append({"plot": plot.name, "largestChange": round(float(difference[near].max()), 2)})
  return {"object": sceneObject.name, "gradedPlots": [pad["plot"] for pad in plan["pads"]], "foldedFaces": folded, "touched": touched, "steepBanks": plan["banks"]}


def padSummary(plan, address):
  """What a plot's own pass holds: its pad's height, how far its batters reach, and its deepest cut and highest fill."""
  index = [pad["plot"] for pad in plan["pads"]].index(address)
  change = (plan["graded"] - plan["ground"][:, 2])[plan["owner"] == index]
  return {
    "pass": gradePassName(address), "height": round(plan["pads"][index]["height"], 2), "slopesReach": round(plan["reaches"][index], 1),
    "deepestCut": round(0.0 - float(change.min(initial=0.0)), 2), "highestFill": round(float(change.max(initial=0.0)), 2),
    "movedVertices": int((numpy.abs(change) > 1e-6).sum()),
  }


def gradePlot(address, objectName, margin, batterDegrees):
  """Grade a plot on a mesh, with every plot graded on it, taking its grading back from the ground it was graded on before."""
  plot = requirePlot(address)
  if margin < 0 or not 5 <= batterDegrees <= 85:
    raise ValueError(f"margin must be at least 0 and batterDegrees from 5 to 85, got {margin} and {batterDegrees}")
  sceneObject = bridgeMeshAccess.requireEditableMesh(objectName, "gradePlot")
  stored = plot.get(gradingProperty)
  # Grading again where the ground it was graded on was deleted is how such a plot is put right: there is nothing there to take back.
  former = None if stored is None or stored["ground"] in (None, sceneObject) else bridgeGrading.planPlots(stored["ground"], {address: None}, {address})
  pad = padOf(address, plot.matrix_world.translation, facingOf(plot), readPlot(plot)["size"], plot.matrix_world.translation.z, {"margin": margin, "batterDegrees": batterDegrees})
  plan = bridgeGrading.planPlots(sceneObject, {address: pad}, {address})
  setGrading(plot, sceneObject, margin, batterDegrees)
  formerGround = {} if former is None else {"formerGround": bridgeGrading.applyPlots(former)}
  return {"plot": address} | padSummary(bridgeGrading.plotPlanOf(plan), address) | bridgeGrading.applyPlots(plan) | formerGround


# Judging a plot

def localGrid(across, along, step):
  xs = numpy.linspace(-across / 2, across / 2, max(2, math.ceil(across / step) + 1))
  ys = numpy.linspace(-along / 2, along / 2, max(2, math.ceil(along / step) + 1))
  return numpy.stack(numpy.meshgrid(xs, ys), -1).reshape(-1, 2)


def toWorld(plot, local):
  front = frontDirection(facingOf(plot))
  right = numpy.array([front[1], -front[0]])
  return numpy.array(plot.matrix_world.translation[:2]) + local[:, :1] * right + local[:, 1:] * front


def entrancePoint(plot):
  """Where players come to a plot: entranceReach out from the middle of its entrance side, at the plot's height."""
  center = numpy.array(plot.matrix_world.translation)
  return numpy.append(center[:2] + frontDirection(facingOf(plot)) * (readPlot(plot)["size"][1] / 2 + entranceReach), center[2])


def plotEntrances():
  """Each plot's entrance (entrancePoint), facing into the plot: [{plot, at, headingDegrees}]."""
  return [
    {"plot": plot.name, "at": [round(float(value), 3) + 0.0 for value in entrancePoint(plot)], "headingDegrees": round((facingOf(plot) + 180) % 360, 3)}
    for plot in plotObjects()
  ]


def assessPlot(address):
  """Measure a plot where it lies: the ground under it, what rises and falls around each side, water beside it, how high it stands
  over its surroundings, how enclosed it is, its neighbours, and how much of the zone's main routes see it; and the features those
  suggest, for pricing. The measures check what a picture shows; look at the plot too."""
  housing = requireHousing()
  plot = requirePlot(address)
  spec = readPlot(plot)
  across, along = spec["size"]
  center = numpy.array(plot.matrix_world.translation)
  ground = bridgeWater.Ground()
  # The ground under, beside, and in front of the plot is looked up from its own height, so a plot in a cave measures the cave, not the
  # hill over it.
  surfaces = bridgeMeshAccess.PlayerSurfaces()
  samples = toWorld(plot, localGrid(across, along, footprintStep))
  heights = numpy.array([numpy.nan if (height := surfaces.groundAtLevel(x, y, center[2])) is None else height for x, y in samples])
  onGround = ~numpy.isnan(heights)
  under = {"samples": len(samples), "offGround": int((~onGround).sum())}
  if onGround.any():
    known = heights[onGround]
    fit = numpy.linalg.lstsq(numpy.column_stack([samples[onGround], numpy.ones(int(onGround.sum()))]), known, rcond=None)[0]
    under |= {
      "lowest": round(float(known.min()), 2), "highest": round(float(known.max()), 2), "unevenness": round(float(known.max() - known.min()), 2),
      "tiltDegrees": round(math.degrees(math.atan(math.hypot(fit[0], fit[1]))), 2),
      "cutToLevel": round(float(max(0.0, known.max() - center[2])), 2), "fillToLevel": round(float(max(0.0, center[2] - known.min())), 2),
    }
  front = frontDirection(facingOf(plot))
  right = numpy.array([front[1], -front[0]])
  sides = {}
  for side, outward, halfDepth, halfWidth, sideways in (("front", front, along / 2, across / 2, right), ("back", -front, along / 2, across / 2, right),
                                                        ("right", right, across / 2, along / 2, front), ("left", -right, across / 2, along / 2, front)):
    relative = []
    for distance in sideProbes:
      for offset in (-0.6, 0.0, 0.6):
        x, y = center[:2] + outward * (halfDepth + distance) + sideways * offset * halfWidth
        height = surfaces.groundAtLevel(x, y, center[2])
        relative.append(numpy.nan if height is None else height - center[2])
    relative = numpy.array(relative)
    finite = relative[~numpy.isnan(relative)]
    sides[side] = {
      "lowest": None if not len(finite) else round(float(finite.min()), 1), "highest": None if not len(finite) else round(float(finite.max()), 1),
      "drop": bool(len(finite) and finite.min() < -sideRise), "wall": bool(len(finite) and finite.max() > sideRise),
      "offGround": int(numpy.isnan(relative).sum()),
    }
  entrance = entrancePoint(plot)[:2]
  entranceGround = surfaces.groundAtLevel(*entrance, center[2])
  water = None
  bodies = [body for body in bpy.context.scene.objects if bridgeMeshAccess.waterProperty in body and not body.hide_render]
  if bodies:
    surface = bridgeMeshAccess.worldTree(bodies)
    edges = toWorld(plot, numpy.array([[x, y] for x in numpy.linspace(-across / 2, across / 2, 9) for y in (-along / 2, along / 2)] + [[x, y] for y in numpy.linspace(-along / 2, along / 2, 9) for x in (-across / 2, across / 2)]))
    water = round(min(surface.find_nearest(mathutils.Vector((x, y, center[2])))[3] for x, y in edges), 1)
  ring = [ground.heightBelow(*(center[:2] + radius * numpy.array([math.sin(angle), math.cos(angle)])), center[2] + 2000)
          for radius in surroundingRadii for angle in numpy.linspace(0, 2 * math.pi, 24, endpoint=False)]
  ring = [height for height in ring if height is not None]
  standsAbove = round(float(center[2] - statistics.median(ring)), 1) if ring else None
  eye = mathutils.Vector((center[0], center[1], center[2] + playerScale.eyeHeight))
  blocked = 0
  for angle in numpy.linspace(0, 2 * math.pi, enclosureDirections, endpoint=False):
    for elevation in (0.0, math.radians(15)):
      direction = mathutils.Vector((math.sin(angle) * math.cos(elevation), math.cos(angle) * math.cos(elevation), math.sin(elevation)))
      blocked += ground.tree.ray_cast(eye, direction, enclosureReach)[0] is not None
  enclosure = round(blocked / (2 * enclosureDirections), 2)
  cover = surfaces.cast(mathutils.Vector((center[0], center[1], center[2] + bridgeMeshAccess.levelProbeLift)), bridgeMeshAccess.up, overheadReach)
  overhead = None if cover is None else round(cover.z, 2)
  others = [other for other in plotObjects() if other != plot]
  nearestPlot = min((float(numpy.linalg.norm(numpy.array(other.matrix_world.translation[:2]) - center[:2])) for other in others), default=None)
  routes = housing["routes"]
  routeDistance, prominence = None, None
  if routes:
    routeDistance = round(float(min(bridgeMeshAccess.distancesToPolyline(center[None, :], [[x, y, 0.0] for x, y in route], horizontal=True)[0][0] for route in routes)), 1)
    seen = inRange = 0
    target = mathutils.Vector((center[0], center[1], center[2] + houseHeight))
    for route in routes:
      routeArray = numpy.asarray(route, dtype=numpy.float64)
      for start, end in zip(routeArray[:-1], routeArray[1:]):
        steps = max(1, math.ceil(numpy.linalg.norm(end - start) / prominenceStep))
        for step in range(steps):
          x, y = start + (end - start) * step / steps
          if math.hypot(x - center[0], y - center[1]) > prominenceRange:
            continue
          height = ground.heightBelow(x, y, bridgeMeshAccess.sceneTopHeight() + 1)
          if height is None:
            continue
          inRange += 1
          viewer = mathutils.Vector((x, y, height + playerScale.eyeHeight))
          toTarget = target - viewer
          hit = ground.tree.ray_cast(viewer, toTarget.normalized(), toTarget.length)
          seen += hit[0] is None or (hit[0] - target).length < 10
    prominence = {"seenFrom": seen, "routeSamplesInRange": inRange, "share": round(seen / inRange, 2) if inRange else None}
  suggested = []
  if water is not None and water <= waterfrontDistance:
    suggested.append({"feature": "waterfront", "because": f"water {water} units from its edge"})
  if (nearestPlot is None or nearestPlot >= secludedNeighbourDistance) and (routeDistance is None or routeDistance >= secludedRouteDistance) and enclosure >= 0.5:
    suggested.append({"feature": "secluded", "because": f"nearest plot {None if nearestPlot is None else round(nearestPlot)} units off, enclosed on {round(enclosure * 100)}% of sides"})
  if prominence is not None and prominence["routeSamplesInRange"] >= prominentSamples and prominence["share"] >= prominentShare:
    suggested.append({"feature": "prominent", "because": f"seen from {prominence['seenFrom']} of {prominence['routeSamplesInRange']} route points within {prominenceRange:.0f}"})
  if overhead is not None:
    suggested.append({"feature": "sheltered", "because": f"rock or roof over its center at {overhead}"})
  return {
    "address": address, "under": under, "sides": sides,
    "entrance": {"point": [round(float(value), 1) for value in entrance], "ground": None if entranceGround is None else round(entranceGround, 2)},
    "waterDistance": water, "standsAboveSurroundings": standsAbove, "enclosure": enclosure, "overhead": overhead,
    "nearestPlot": None if nearestPlot is None else round(nearestPlot, 1), "routeDistance": routeDistance, "prominence": prominence,
    "overlaps": overlapsOf(plot), "suggestedFeatures": suggested, "features": spec["tags"],
    "unpricedSuggestions": sorted({entry["feature"] for entry in suggested} - set(housing["pricing"]["featureMultipliers"])),
  }


def layOutPlots(street, path, side, kind, size, firstNumber, gap, setback, items, pets, tags, borderFolder, collection):
  """A row of plots along a street as a starting point: stations along the path a plot's width plus `gap` apart, each plot `setback`
  from the path on the chosen side (or both), facing the street. Each station's place on each side takes the next address number in
  order along the street (left before right), whether or not a plot fits there, so an address says where along the street a plot
  stands and, on both sides, each side keeps its own odd or even numbers. Places where a plot would overlap another or find no ground
  are skipped and listed with the address they keep free. Adjust each plot afterwards."""
  housing = requireHousing()
  if kind not in plotKinds:
    raise ValueError(f"kind is one of {list(plotKinds)}, got '{kind}'")
  if side not in ("left", "right", "both"):
    raise ValueError(f"side is left, right, or both, got '{side}'")
  if gap < 0 or setback < 0:
    raise ValueError(f"gap and setback must be at least 0, got {gap} and {setback}")
  pathArray = numpy.asarray(path, dtype=numpy.float64)
  if pathArray.ndim != 2 or pathArray.shape[1] != 2 or len(pathArray) < 2:
    raise ValueError(f"path is at least two [x, y] points, got {path!r}")
  across, along = requireSize(kind, size)
  requireTags(tags or [], housing)
  requireAllowances({"items": housing["pricing"]["defaultItems"][kind] if items is None else items, "pets": housing["pricing"]["defaultPets"][kind] if pets is None else pets})
  segments = numpy.diff(pathArray, axis=0)
  lengths = numpy.linalg.norm(segments, axis=1)
  arc = numpy.concatenate([[0.0], numpy.cumsum(lengths)])
  places = []
  for station in numpy.arange(across / 2, arc[-1] - across / 2 + 1e-9, across + gap):
    segment = min(int(numpy.searchsorted(arc, station, side="right")) - 1, len(segments) - 1)
    point = pathArray[segment] + segments[segment] * (station - arc[segment]) / lengths[segment]
    tangent = segments[segment] / lengths[segment]
    leftward = numpy.array([-tangent[1], tangent[0]])
    for offset in {"left": [leftward], "right": [-leftward], "both": [leftward, -leftward]}[side]:
      center = point + offset * (setback + along / 2)
      places.append((f"{firstNumber + len(places)} {street}", center, math.degrees(math.atan2(-offset[0], -offset[1])) % 360))
  taken = [address for address, _, _ in places if bpy.data.objects.get(address) is not None]
  if taken:
    raise ValueError(f"Addresses {taken} are taken; remove those plots or start from another firstNumber")
  placed, skipped = [], []
  for (address, center, facing), (height, levels) in zip(places, seatGround([center for _, center, _ in places])):
    entry = {"address": address, "center": [round(float(value), 1) for value in center]}
    overlapping = [plot.name for plot in plotObjects() if overlapDepth(footprintOf(center, facing, (across, along)), footprint(plot)) > 0.01]
    if height is None:
      skipped.append(entry | {"reason": "no ground under it"})
      continue
    if levels is not None:
      skipped.append(entry | {"reason": bridgeMeshAccess.describeRockOverGround(entry["center"], levels) + "; place it with placePlot and its height"})
      continue
    if overlapping:
      skipped.append(entry | {"reason": f"overlaps {overlapping}"})
      continue
    result = placePlot(address, kind, center.tolist(), facing, [across, along], height, items, pets, tags, None, borderFolder, collection)
    placed.append({key: result[key] for key in ("address", "center", "facingDegrees", "pricePlatinum")})
  return {"street": street, "placed": placed, "skipped": skipped, "housing": housing["role"]}


# Export

def serverPoint(point):
  return [round(float(value), 3) for value in eqAxes.serverFromZone(point)]


def collectHousing():
  """The zone's housing for its housing file: its decision, each plot as Peridot's plot content gives one (address, border door,
  center in the server's axes, EQ heading, size across and along, price, item capacity, pets), and the border doors. None when the
  zone has neither a decision nor plots."""
  housing = readHousing()
  plots = plotObjects()
  if housing is None:
    if plots:
      raise ValueError(f"The zone has plots {[plot.name for plot in plots]} but no housing decision; setZoneHousing")
    return None
  if housing["role"] == "none":
    return {"housing": housing, "plots": [], "doors": [], "assets": []}
  if not plots:
    raise ValueError(f"The zone's housing is {housing['role']} but it has no plots")
  for index, first in enumerate(plots):
    for second in plots[index + 1:]:
      if overlapDepth(footprint(first), footprint(second)) > 0.01:
        raise ValueError(f"Plots '{first.name}' and '{second.name}' overlap; the server refuses overlapping plots")
  records, doors = [], []
  for doorNumber, plot in enumerate(plots, 1):
    spec = readPlot(plot)
    border = borderOf(plot)
    if border is None:
      raise ValueError(f"Plot '{plot.name}' has no border")
    price = plotPrice(spec, housing)
    heading = round(eqAxes.eqHeadingFromTurn(math.degrees(border.matrix_world.to_euler("XYZ").z)), 3) % eqAxes.eqHeadingUnits
    doors.append({
      "door": doorNumber, "name": borderModels[spec["kind"]], "position": serverPoint(border.matrix_world.translation), "heading": heading,
      "openType": borderOpenType, "size": round(border.matrix_world.to_scale()[0] * 100),
    })
    records.append({
      "kind": "plot" if spec["kind"] == "player" else "guild plot", "address": plot.name, "door": doorNumber,
      "center": serverPoint(plot.matrix_world.translation), "heading": heading, "sizeAcross": spec["size"][0], "sizeAlong": spec["size"][1],
      "pricePlatinum": price["pricePlatinum"], "upkeepPlatinumPerDay": price["upkeepPlatinumPerDay"], "capacity": spec["items"], "pets": spec["pets"],
      "features": spec["tags"],
    })
  return {"housing": housing, "plots": records, "doors": doors, "assets": [borderArchive]}


commands = {
  "setZoneHousing": (setZoneHousing, True),
  "getHousing": (describeHousing, False),
  "placePlot": (placePlot, True),
  "editPlot": (editPlot, True),
  "removePlot": (removePlot, True),
  "gradePlot": (gradePlot, True),
  "assessPlot": (assessPlot, False),
  "layOutPlots": (layOutPlots, True),
}
