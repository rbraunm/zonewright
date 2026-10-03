"""Housing as a zone designs it: first the zone's decision (whether it has housing, how central it is, what it is for, and how its plots
are priced), then plots placed one at a time. A plot is a guide on the ground (its outline and an arrow out of its entrance) with the
client's own border model at its center, as players will see it; plots are moved, turned, resized, graded, assessed, and priced from
where they lie and what they offer, and the zone's housing file is written from them. Runs under Blender's Python."""
import json
import math
import statistics

import bpy
import mathutils
import numpy

import bridgeMeshAccess
import bridgeModels
import bridgeObjects
import bridgePasses
import bridgeShaping
import bridgeWater

housingProperty = "zonewrightHousing"
plotProperty = "zonewrightPlot"
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
eqHeadingUnits = 512
# Pricing as Live's Sunrise Hills priced its plots: 84pp for the middle tier's 50 items at Peridot's launch capacities (40, 50, 60;
# 42pp and 126pp for the others, 4.2pp an item), a guild plot 21pp; upkeep a tenth of the price a day. Features scale the price.
defaultPricing = {
  "basePlatinum": {"player": 84, "guild": 21},
  "defaultItems": {"player": 50, "guild": 210},
  "defaultPets": {"player": 6, "guild": 12},
  "platinumPerItem": 4.2,
  "platinumPerPet": 7.0,
  "featureMultipliers": {"prominent": 1.5, "secluded": 1.5, "view": 1.25, "waterfront": 1.25, "sheltered": 1.25, "remote": 0.75, "swamp": 0.5},
  "upkeepShare": 0.1,
}
guideColors = {"player": (0.1, 0.85, 1.0), "guild": (1.0, 0.3, 0.9)}
guideBand = 1.5
guideLift = 0.4
arrowWidth = 16.0
arrowLength = 16.0
# Ground under a plot is looked for from this far above the plot's height: an arch or overhang higher up is not its ground.
groundCastHeight = 50.0
footprintStep = 8.0
gradeBatterReach = 600.0
# Assessment: how far around a plot it looks, and what counts as a drop, a wall, water at its edge, a view, seclusion, prominence.
sideProbes = (10.0, 30.0, 60.0)
sideRise = 15.0
waterfrontDistance = 40.0
surroundingRadii = (200.0, 400.0, 600.0)
viewRise = 60.0
enclosureReach = 300.0
enclosureDirections = 32
eyeHeight = 6.0
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


def footprint(plot, margin=0.0):
  """A plot's corners in plan, counterclockwise from its front right."""
  spec = readPlot(plot)
  across, along = spec["size"]
  front = frontDirection(facingOf(plot))
  right = numpy.array([front[1], -front[0]])
  center = numpy.array(plot.matrix_world.translation[:2])
  halfAcross, halfAlong = across / 2 + margin, along / 2 + margin
  return numpy.array([center + right * a * halfAcross + front * b * halfAlong for a, b in ((1, 1), (-1, 1), (-1, -1), (1, -1))])


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
  """The plot's outline as a band just inside its edges, and an arrow out of the middle of its entrance side (local +Y)."""
  across, along = size
  halfAcross, halfAlong = across / 2, along / 2
  outer = [(halfAcross, halfAlong), (-halfAcross, halfAlong), (-halfAcross, -halfAlong), (halfAcross, -halfAlong)]
  inner = [(x - math.copysign(guideBand, x), y - math.copysign(guideBand, y)) for x, y in outer]
  vertices = [(x, y, guideLift) for x, y in outer + inner]
  faces = [(index, (index + 1) % 4, 4 + (index + 1) % 4, 4 + index) for index in range(4)]
  base = len(vertices)
  vertices += [(-arrowWidth / 2, halfAlong - arrowLength / 2, guideLift), (arrowWidth / 2, halfAlong - arrowLength / 2, guideLift), (0, halfAlong + arrowLength / 2, guideLift)]
  faces.append((base, base + 1, base + 2))
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


def groundHeight(ground, x, y, aboveZ):
  return ground.heightBelow(x, y, aboveZ)


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
    "border": None if border is None else {"size": round(border.scale[0] * 100)},
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
    height = groundHeight(bridgeWater.Ground(), center[0], center[1], bridgeMeshAccess.sceneTopHeight() + 1)
    if height is None:
      raise ValueError(f"No ground under {center} to set the plot on")
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


def editPlot(address, newAddress, kind, center, facingDegrees, size, height, items, pets, tags, pricePlatinum, borderFolder):
  housing = requireHousing()
  plot = requirePlot(address)
  spec = readPlot(plot)
  changes = [center, facingDegrees, size, height, items, pets, tags, pricePlatinum, kind, newAddress]
  if all(change is None for change in changes):
    raise ValueError("editPlot needs something to change")
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
  if items is not None:
    spec["items"] = items
  if pets is not None:
    spec["pets"] = pets
  requireAllowances(spec)
  if tags is not None:
    spec["tags"] = requireTags(tags, housing)
  if pricePlatinum is not None:
    spec["priceOverride"] = None if pricePlatinum == 0 else requirePrice(pricePlatinum)
  if newAddress is not None and newAddress != plot.name:
    if not newAddress.strip():
      raise ValueError("A plot needs its address")
    bridgeObjects.requireNewName(newAddress)
    plot.name = newAddress
    plot.data.name = newAddress
  if center is not None:
    if len(center) != 2:
      raise ValueError(f"center is [x, y], got {center!r}")
    plot.location.x, plot.location.y = center
    if height is None:
      found = groundHeight(bridgeWater.Ground(), center[0], center[1], bridgeMeshAccess.sceneTopHeight() + 1)
      if found is None:
        raise ValueError(f"No ground under {center} to set the plot on")
      plot.location.z = found
  if height is not None:
    plot.location.z = height
  if facingDegrees is not None:
    plot.rotation_euler = (0, 0, -math.radians(facingDegrees))
  plot[plotProperty] = json.dumps(spec)
  border = None
  if rebuild:
    if borderFolder is None:
      raise ValueError("A plot whose kind or size changes needs its border model folder")
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
  return plotRecord(plot, housing) | ({"border": border} if border else {}) | {"overlaps": overlapsOf(plot)}


def removePlot(address, terrainObject):
  plot = requirePlot(address)
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
  grading = None
  if terrainObject is not None:
    grading = bridgePasses.removeShapingPass(terrainObject, gradePassName(address))
  return {"removed": removed, "grading": grading}


def gradePassName(address):
  return f"grade {address}"


def gradePlot(address, objectName, margin, batterDegrees):
  """Level the ground under a plot and a margin around it to the plot's height, in its own shaping pass, cutting into ground above it
  and filling ground below it, each meeting the slope around it at `batterDegrees`, as a builder's cut and fill slopes do. Grading again
  replaces what the pass held."""
  plot = requirePlot(address)
  if margin < 0 or not 5 <= batterDegrees <= 85:
    raise ValueError(f"margin must be at least 0 and batterDegrees from 5 to 85, got {margin} and {batterDegrees}")
  sceneObject = bridgeMeshAccess.requireMeshObject(objectName)
  passName = gradePassName(address)
  keys = sceneObject.data.shape_keys
  existing = keys.key_blocks.get(passName) if keys is not None else None
  previous = sceneObject.active_shape_key.name if keys is not None and sceneObject.active_shape_key != keys.reference_key else None
  positions, _ = bridgeMeshAccess.readVertexArrays(sceneObject)
  if existing is not None and not existing.mute:
    held = (bridgePasses.keyCoordinates(existing) - bridgePasses.keyCoordinates(keys.reference_key)) * existing.value
    positions = positions - held @ bridgeMeshAccess.matrixArray(sceneObject.matrix_world)[:3, :3].T
  outline = footprint(plot, margin)
  base = float(plot.matrix_world.translation.z)
  distance, _ = bridgeShaping.signedDistanceToOutline(positions, outline)
  slope = math.tan(math.radians(batterDegrees))
  reachOut = 20.0
  for _ in range(4):
    near = distance >= -reachOut
    if not near.any():
      raise ValueError(f"No vertex of '{objectName}' lies under or near plot '{address}'")
    reachOut = max(20.0, float(numpy.abs(positions[near, 2] - base).max()) / slope + 2 * bridgeShaping.medianEdgeLength(sceneObject, positions, near))
  if reachOut > gradeBatterReach:
    raise ValueError(f"Plot '{address}' stands {reachOut * slope:.0f} units off the ground around it; its slopes would reach {reachOut:.0f} units out. Move it, change its height, or steepen batterDegrees")
  if existing is not None:
    existing.data.foreach_set("co", bridgePasses.keyCoordinates(keys.reference_key).ravel())
    existing.mute, existing.value = False, 1.0
    sceneObject.active_shape_key_index = list(keys.key_blocks).index(existing)
    sceneObject.data.update()
  else:
    bridgePasses.addShapingPass(objectName, passName)
  inside = float(distance.max()) + 1
  carved = bridgeShaping.sculptOutline(objectName, "carve", outline.tolist(), base, [[-reachOut, reachOut * slope], [0, 0], [inside, 0]], 1.0, False)
  filled = bridgeShaping.sculptOutline(objectName, "fill", outline.tolist(), base, [[-reachOut, -reachOut * slope], [0, 0], [inside, 0]], 1.0, False)
  if previous is not None and previous != passName:
    sceneObject.active_shape_key_index = list(sceneObject.data.shape_keys.key_blocks).index(sceneObject.data.shape_keys.key_blocks[previous])
  after, _ = bridgeMeshAccess.readVertexArrays(sceneObject)
  moved = after[:, 2] - positions[:, 2]
  return {
    "plot": address, "pass": passName, "height": round(base, 2), "slopesReach": round(reachOut, 1),
    "deepestCut": round(float(-moved.min()), 2), "highestFill": round(float(moved.max()), 2),
    "movedVertices": int((numpy.abs(moved) > 1e-6).sum()), "foldedFaces": carved["foldedFaces"] + filled["foldedFaces"],
  }


# Judging a plot

def localGrid(across, along, step):
  xs = numpy.linspace(-across / 2, across / 2, max(2, math.ceil(across / step) + 1))
  ys = numpy.linspace(-along / 2, along / 2, max(2, math.ceil(along / step) + 1))
  return numpy.stack(numpy.meshgrid(xs, ys), -1).reshape(-1, 2)


def toWorld(plot, local):
  front = frontDirection(facingOf(plot))
  right = numpy.array([front[1], -front[0]])
  return numpy.array(plot.matrix_world.translation[:2]) + local[:, :1] * right + local[:, 1:] * front


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
  castFrom = center[2] + groundCastHeight
  samples = toWorld(plot, localGrid(across, along, footprintStep))
  heights = numpy.array([numpy.nan if (height := ground.heightBelow(x, y, castFrom)) is None else height for x, y in samples])
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
        height = ground.heightBelow(x, y, center[2] + 200)
        relative.append(numpy.nan if height is None else height - center[2])
    relative = numpy.array(relative)
    finite = relative[~numpy.isnan(relative)]
    sides[side] = {
      "lowest": None if not len(finite) else round(float(finite.min()), 1), "highest": None if not len(finite) else round(float(finite.max()), 1),
      "drop": bool(len(finite) and finite.min() < -sideRise), "wall": bool(len(finite) and finite.max() > sideRise),
      "offGround": int(numpy.isnan(relative).sum()),
    }
  entrance = center[:2] + front * (along / 2 + 10)
  entranceGround = ground.heightBelow(*entrance, center[2] + 200)
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
  eye = mathutils.Vector((center[0], center[1], center[2] + eyeHeight))
  blocked = 0
  for angle in numpy.linspace(0, 2 * math.pi, enclosureDirections, endpoint=False):
    for elevation in (0.0, math.radians(15)):
      direction = mathutils.Vector((math.sin(angle) * math.cos(elevation), math.cos(angle) * math.cos(elevation), math.sin(elevation)))
      blocked += ground.tree.ray_cast(eye, direction, enclosureReach)[0] is not None
  enclosure = round(blocked / (2 * enclosureDirections), 2)
  overhead = ground.tree.ray_cast(mathutils.Vector((center[0], center[1], center[2] + 10)), mathutils.Vector((0, 0, 1)), 1000)[0] is not None
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
          viewer = mathutils.Vector((x, y, height + eyeHeight))
          toTarget = target - viewer
          hit = ground.tree.ray_cast(viewer, toTarget.normalized(), toTarget.length)
          seen += hit[0] is None or (hit[0] - target).length < 10
    prominence = {"seenFrom": seen, "routeSamplesInRange": inRange, "share": round(seen / inRange, 2) if inRange else None}
  suggested = []
  if water is not None and water <= waterfrontDistance:
    suggested.append({"feature": "waterfront", "because": f"water {water} units from its edge"})
  if standsAbove is not None and standsAbove >= viewRise and enclosure < 0.5:
    suggested.append({"feature": "view", "because": f"stands {standsAbove} above its surroundings, open on {round((1 - enclosure) * 100)}% of sides"})
  if (nearestPlot is None or nearestPlot >= secludedNeighbourDistance) and (routeDistance is None or routeDistance >= secludedRouteDistance) and enclosure >= 0.5:
    suggested.append({"feature": "secluded", "because": f"nearest plot {None if nearestPlot is None else round(nearestPlot)} units off, enclosed on {round(enclosure * 100)}% of sides"})
  if prominence is not None and prominence["routeSamplesInRange"] >= prominentSamples and prominence["share"] >= prominentShare:
    suggested.append({"feature": "prominent", "because": f"seen from {prominence['seenFrom']} of {prominence['routeSamplesInRange']} route points within {prominenceRange:.0f}"})
  if overhead:
    suggested.append({"feature": "sheltered", "because": "rock or roof over its center"})
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
  from the path on the chosen side (or both), facing the street, numbered in order along it (left before right at each station). Plots
  that would overlap another, or find no ground, are skipped and listed. Adjust each one afterwards."""
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
  segments = numpy.diff(pathArray, axis=0)
  lengths = numpy.linalg.norm(segments, axis=1)
  arc = numpy.concatenate([[0.0], numpy.cumsum(lengths)])
  stations = numpy.arange(across / 2, arc[-1] - across / 2 + 1e-9, across + gap)
  placed, skipped = [], []
  number = firstNumber
  for station in stations:
    segment = min(int(numpy.searchsorted(arc, station, side="right")) - 1, len(segments) - 1)
    point = pathArray[segment] + segments[segment] * (station - arc[segment]) / lengths[segment]
    tangent = segments[segment] / lengths[segment]
    leftward = numpy.array([-tangent[1], tangent[0]])
    sides = {"left": [leftward], "right": [-leftward], "both": [leftward, -leftward]}[side]
    for offset in sides:
      address = f"{number} {street}"
      number += 1
      center = point + offset * (setback + along / 2)
      facing = math.degrees(math.atan2(-offset[0], -offset[1])) % 360
      try:
        result = placePlot(address, kind, center.tolist(), facing, [across, along], None, items, pets, tags, None, borderFolder, collection)
      except ValueError as error:
        skipped.append({"address": address, "center": [round(float(value), 1) for value in center], "reason": str(error)})
        continue
      if result["overlaps"]:
        removePlot(address, None)
        skipped.append({"address": address, "center": [round(float(value), 1) for value in center], "reason": f"overlaps {[entry['plot'] for entry in result['overlaps']]}"})
        continue
      placed.append({key: result[key] for key in ("address", "center", "facingDegrees", "pricePlatinum")})
  return {"street": street, "placed": placed, "skipped": skipped, "housing": housing["role"]}


# Export

def serverOrder(point):
  """Blender (zone file) x, y, z as the server's x, y, z: the server swaps the first two."""
  return [round(float(point[1]), 3), round(float(point[0]), 3), round(float(point[2]), 3)]


def eqHeading(counterclockwiseDegrees):
  return round((counterclockwiseDegrees * eqHeadingUnits / 360) % eqHeadingUnits, 3) % eqHeadingUnits


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
    heading = eqHeading(math.degrees(border.matrix_world.to_euler("XYZ").z))
    doors.append({
      "door": doorNumber, "name": borderModels[spec["kind"]], "position": serverOrder(border.matrix_world.translation), "heading": heading,
      "openType": borderOpenType, "size": round(border.matrix_world.to_scale()[0] * 100),
    })
    records.append({
      "kind": "plot" if spec["kind"] == "player" else "guild plot", "address": plot.name, "door": doorNumber,
      "center": serverOrder(plot.matrix_world.translation), "heading": heading, "sizeAcross": spec["size"][0], "sizeAlong": spec["size"][1],
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
