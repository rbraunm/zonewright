"""Defined structures: one artist action laying kit pieces between anchors, along points, or along a path the artist gave, kept with its
definition and laid again from it. The ground a lay stands on (StructureGround), every lookup it made (probes, replayed to tell when the
ground moved), the kit fingerprints it was laid from, the whole-or-nothing lay, staleness, walk lines, and the views each kind is judged
from; the commands that build, edit, take back, and list structures. Kinds register their lays here (bridgeSpans, bridgeWalls). Runs
under Blender's Python."""
import math
import numbers
import os

import bpy
import mathutils
import numpy

import bridgeBoundaries
import bridgeExport
import bridgeKitData
import bridgeKitGeometry
import bridgeMeshAccess
import bridgeObjects
import bridgeReview
import bridgeReviewGuides
import bridgeStructureData
import bridgeViews
from playerScale import playerHeight, stepHeight

structuresCollectionName = "structures"
layingSuffix = "Laying"
# Two lookups of one probe agree when this close; a probe that moves further makes its structure stale.
probeTolerance = 0.01
# Posts, legs, and anchors find the ground within this far below them.
groundReach = 300.0
# Decks are probed for clearance this often along their edges and centerline, and walked in samples this far apart.
clearanceSpacing = 4.0
walkSpacing = 4.0
# A walk line runs on this far past each end that stands on footing.
walkExtension = 5.0
overheadLift = 10.0
castNudge = bridgeMeshAccess.castNudge
up = mathutils.Vector((0.0, 0.0, 1.0))
down = mathutils.Vector((0.0, 0.0, -1.0))
sideNames = ("both", "left", "right")
# Each kind: the build tool that makes it, its definition's keys, its lay, its walk line (or None), and its views.
kinds = {}


def placeCollections():
  """Where a structure's collection may stand: structures, or terrain for a span exported as ground."""
  return (structuresCollectionName, bridgeExport.terrainCollectionName)


class Kind:
  def __init__(self, tool, keys, lay, walkLine, views, indexKeys=()):
    self.tool, self.keys, self.lay, self.walkLine, self.views, self.indexKeys = tool, keys, lay, walkLine, views, indexKeys


def registerKind(kind, tool, keys, lay, walkLine, views, indexKeys=()):
  kinds[kind] = Kind(tool, keys, lay, walkLine, views, indexKeys)


def isNumber(value):
  return isinstance(value, numbers.Real) and not isinstance(value, bool) and math.isfinite(value)


def requirePoint(label, point, sizes=(3,)):
  if not isinstance(point, (list, tuple)) or len(point) not in sizes or not all(isNumber(value) for value in point):
    shapes = " or ".join("[" + ", ".join("xyz"[:size]) + "]" for size in sizes)
    raise ValueError(f"{label} is {shapes}, got {point!r}")
  return [float(value) for value in point]


def requirePositive(label, value):
  if not isNumber(value) or value <= 0:
    raise ValueError(f"{label} must be a positive number, got {value!r}")
  return float(value)


def requireNonNegative(label, value):
  if not isNumber(value) or value < 0:
    raise ValueError(f"{label} must be 0 or more, got {value!r}")
  return float(value)


def requireKeys(label, given, required, optional=()):
  if not isinstance(given, dict) or not set(required) <= set(given) or not set(given) <= set(required) | set(optional):
    shown = ", ".join(list(required) + [f"{key} (optional)" for key in optional])
    raise ValueError(f"{label} is {{{shown}}}, got {given!r}")
  return given


def requireSides(label, sides):
  if sides not in sideNames:
    raise ValueError(f"{label} is one of {list(sideNames)} (of travel), got {sides!r}")
  return {"both": (1, -1), "left": (1,), "right": (-1,)}[sides]


def roundVector(vector, digits=3):
  return [bridgeKitData.plain(round(float(component), digits)) for component in vector]


def floated(value, indexKeys, key=None):
  """A definition's numbers as floats (indices kept whole), as JSON keeps them."""
  if key in indexKeys:
    return value
  if isinstance(value, dict):
    return {name: floated(item, indexKeys, name) for name, item in value.items()}
  if isinstance(value, list):
    return [floated(item, indexKeys) for item in value]
  if isinstance(value, numbers.Real) and not isinstance(value, bool):
    return float(value)
  return value


def keptKitPath(kitPath):
  return None if kitPath is None else bridgeReviewGuides.keptPath(kitPath)


def absoluteKitPath(kept):
  return None if kept is None else os.path.normpath(bpy.path.abspath(kept))


def shownDefinition(definition):
  """A definition as a build tool takes it again: its kit path absolute."""
  return definition | {"kitPath": absoluteKitPath(definition["kitPath"])}


class Kit:
  """The pieces a lay takes from one kit (None: the open file's own), each read once and fingerprinted."""

  def __init__(self, kitPath):
    self.kitPath = kitPath
    self.pieces, self.used = {}, {}

  def piece(self, name, allowed, role):
    if not isinstance(name, str):
      raise ValueError(f"{role} names a piece of the kit, got {name!r}")
    if name not in self.pieces:
      collection = bridgeKitData.requirePiece(self.kitPath, name)
      self.pieces[name] = bridgeKitGeometry.pieceData(collection) | {"collection": collection}
      self.used[name] = bridgeKitData.fingerprint(collection)
    data = self.pieces[name]
    if data["record"]["kind"] not in allowed:
      raise ValueError(f"{role} '{name}' is a {data['record']['kind']} piece; {role} takes a {' or '.join(allowed)} piece")
    return data


def size(data, axis):
  return float(data["high"][axis] - data["low"][axis])


class StructureGround:
  """What players collide with but the boundaries, built once; each lay or check casts against it leaving out a structure's own parts
  and every structure laid after it."""

  def __init__(self):
    self.members = bridgeBoundaries.collisionSurfaces(boundaries=False).members

  def without(self, skipped):
    return bridgeMeshAccess.PlayerSurfaces(trees=[(name, matrix, tree) for name, matrix, _, tree in self.members if name not in skipped])


def runProbe(surfaces, row):
  """A probe row [kind, x, y, z, dx, dy, dz, reach, found] looked up again: what it finds now and the object it finds it on."""
  kind, origin, direction, reach = int(row[0]), mathutils.Vector(row[1:4]), mathutils.Vector(row[4:7]), float(row[7])
  if kind == bridgeStructureData.probeKinds["footing"]:
    footing = surfaces.footingOn(origin + up * (stepHeight + castNudge), reach + stepHeight + castNudge)
    return (math.nan, None) if footing is None else (footing.point.z, footing.objectName)
  if kind == bridgeStructureData.probeKinds["below"]:
    above = surfaces.castOn(origin, up, bridgeMeshAccess.waterReach)
    if above is not None and above[1].z > 0:
      return above[0].z, above[2]
    footing = surfaces.footingOn(origin, reach)
    return (math.nan, None) if footing is None else (footing.point.z, footing.objectName)
  if kind == bridgeStructureData.probeKinds["beside"]:
    hit = surfaces.castOn(origin, direction, reach)
    return (math.nan, None) if hit is None else ((hit[0] - origin).length, hit[2])
  if bridgeMeshAccess.rockOverGround(surfaces.castWithNormal, origin.x, origin.y, origin.z) is not None:
    return math.nan, None
  hit = surfaces.castOn(origin, down, bridgeMeshAccess.waterReach)
  return (math.nan, None) if hit is None else (hit[0].z, hit[2])


class GroundLookups:
  """A lay's lookups against its ground, each recorded as a probe with what it found, and the objects found."""

  def __init__(self, surfaces):
    self.surfaces = surfaces
    self.rows, self.standsOn = [], set()

  def look(self, kind, origin, direction, reach):
    row = [float(bridgeStructureData.probeKinds[kind]), *map(float, origin), *map(float, direction), float(reach), math.nan]
    found, owner = runProbe(self.surfaces, row)
    row[8] = found
    self.rows.append(row)
    if owner is not None:
      self.standsOn.add(owner)
    return None if math.isnan(found) else found

  def footing(self, point, reach=stepHeight):
    """The footing within reach under a point, found from a step over it, or None."""
    return self.look("footing", point, (0.0, 0.0, -1.0), reach)

  def below(self, point, reach=groundReach):
    """The ground at a point: the top of the ground it lies in, or the first footing below it within reach; None for neither."""
    return self.look("below", point, (0.0, 0.0, -1.0), reach)

  def beside(self, point, direction, reach):
    """How far a level direction from a point meets something, or None within reach."""
    return self.look("beside", point, direction, reach)

  def overhead(self, x, y):
    """The highest ground at [x, y], looked for from over the whole scene; refused where rock lies over ground there."""
    top = bridgeMeshAccess.sceneTopHeight() + overheadLift
    levels = bridgeMeshAccess.rockOverGround(self.surfaces.castWithNormal, x, y, top)
    if levels is not None:
      raise ValueError(f"At [{x:g}, {y:g}] {bridgeMeshAccess.describeRockOverGround([x, y], [round(level, 1) for level in levels])}, so which ground is a choice: give z")
    return self.look("overhead", (x, y, top), (0.0, 0.0, -1.0), bridgeMeshAccess.waterReach)


class Laying:
  """What one lay makes, kept apart until every part is made and every check passed: parts under temporary names in a collection of
  their own outside the scene, and shared meshes to make or replace."""

  def __init__(self, name, definition, kit, lookups):
    self.name, self.definition, self.kit, self.lookups = name, definition, kit, lookups
    self.collection = bpy.data.collections.new(name + layingSuffix)
    self.parts, self.sharedMeshes = [], []

  def link(self, sceneObject, finalName):
    self.collection.objects.link(sceneObject)
    self.parts.append((sceneObject, finalName))
    return sceneObject

  def addMesh(self, finalName, bake, matrix):
    """One mesh of baked pieces, its origin and turn the given matrix's."""
    mesh = bake.mesh(finalName + layingSuffix, numpy.linalg.inv(matrix))
    meshObject = bpy.data.objects.new(finalName + layingSuffix, mesh)
    meshObject.matrix_world = mathutils.Matrix(matrix.tolist())
    return self.link(meshObject, finalName)

  def addInstance(self, finalName, collection, location, facingDegrees):
    instance = bpy.data.objects.new(finalName + layingSuffix, None)
    instance.instance_type = "COLLECTION"
    instance.instance_collection = collection
    instance.location = location
    instance.rotation_euler = (0.0, 0.0, math.radians(-facingDegrees))
    return self.link(instance, finalName)

  def addSharedMeshObject(self, finalName, mesh, location, facingDegrees):
    meshObject = bpy.data.objects.new(finalName + layingSuffix, mesh)
    meshObject.location = location
    meshObject.rotation_euler = (0.0, 0.0, math.radians(-facingDegrees))
    return self.link(meshObject, finalName)

  def discard(self):
    bpy.data.collections.remove(self.collection)


def requireNewStructureName(name):
  if not isinstance(name, str) or not name:
    raise ValueError(f"name names the structure, got {name!r}")
  if bridgeStructureData.findStructure(name) is not None:
    raise ValueError(f"'{name}' is already a structure (editStructure changes it, removeStructure takes it back)")
  if bpy.data.collections.get(name) is not None:
    raise ValueError(f"'{name}' is already the name of a collection in this file")
  if bpy.data.objects.get(name) is not None:
    raise ValueError(f"'{name}' is already the name of an object in this file")


def skippedNames(order):
  """The parts of every structure laid at or after an order: a structure's ground leaves out its own and later ones."""
  return {part.name for collection in bridgeStructureData.structureCollections() if bridgeStructureData.readStructure(collection)["order"] >= order for part in bridgeStructureData.partsOf(collection)}


def partRole(structureName, part):
  if part.name == structureName:
    return "span"
  if part.name.startswith(structureName + "Section"):
    return "section"
  if part.name.startswith(structureName + "Post"):
    return "post"
  return "part"


def partTriangles(part):
  if part.type == "MESH":
    return bridgeMeshAccess.triangleCount(part)
  return sum(bridgeMeshAccess.triangleCount(member) for member in bridgeKitData.pieceMembers(part.instance_collection))


def isGround(collection):
  terrain = bpy.data.collections.get(bridgeExport.terrainCollectionName)
  return terrain is not None and collection.name in terrain.children


def exportModel(part, collection):
  """The model file an export writes a part into, or None for a span laid as ground (its triangles go into the terrain)."""
  if isGround(collection):
    return None
  role = "mesh" if part.type == "MESH" else "instance"
  return f"obj_{bridgeExport.modelStem(bridgeExport.modelKey(part, role))}.mod"


def describeParts(collection):
  return [{"name": part.name, "role": partRole(collection.name, part), "triangles": partTriangles(part), "model": exportModel(part, collection)} for part in bridgeStructureData.partsOf(collection)]


def placeCollection(collection, collectionName):
  """The structure's collection under structures (or terrain), and under nothing else."""
  parent = bridgeObjects.targetCollection(collectionName)
  for other in bpy.data.collections:
    if other is not parent and collection.name in other.children:
      other.children.unlink(collection)
  if collection.name in bpy.context.scene.collection.children:
    bpy.context.scene.collection.children.unlink(collection)
  if collection.name not in parent.children:
    parent.children.link(collection)


def removeUnusedShearMeshes():
  unused = [mesh for mesh in bpy.data.meshes if bridgeStructureData.shearProperty in mesh and mesh.users == 0]
  names = sorted(mesh.name for mesh in unused)
  if unused:
    bpy.data.batch_remove(unused)
  return names


def removeParts(parts):
  meshes = []
  for part in parts:
    data = part.data
    bpy.data.objects.remove(part)
    if isinstance(data, bpy.types.Mesh) and data.users == 0 and bridgeStructureData.shearProperty not in data:
      meshes.append(data)
  if meshes:
    bpy.data.batch_remove(meshes)


def commit(laying, kind, order, existing):
  """Swap the old parts for the new, name them, and keep the record: only once the whole lay has been made."""
  name = laying.name
  oldParts = bridgeStructureData.partsOf(existing) if existing is not None else []
  oldNames = {part.name for part in oldParts}
  for _, finalName in laying.parts:
    taken = bpy.data.objects.get(finalName)
    if taken is not None and taken.name not in oldNames:
      raise ValueError(f"Part name '{finalName}' is taken by an object outside structure '{name}'; rename it (organize) or name the structure otherwise")
  removeParts(oldParts)
  collection = existing if existing is not None else bpy.data.collections.new(name)
  placeCollection(collection, laying.definition["collection"])
  for mesh, replaced in laying.sharedMeshes:
    if replaced is not None:
      finalName = replaced.name
      replaced.user_remap(mesh)
      bpy.data.meshes.remove(replaced)
      mesh.name = finalName
  for sceneObject, finalName in laying.parts:
    laying.collection.objects.unlink(sceneObject)
    collection.objects.link(sceneObject)
    sceneObject.name = finalName
    if sceneObject.type == "MESH" and bridgeStructureData.shearProperty not in sceneObject.data:
      sceneObject.data.name = finalName
    sceneObject[bridgeStructureData.partProperty] = name
  laying.discard()
  removeUnusedShearMeshes()
  bridgeStructureData.writeStructure(collection, {"kind": kind, "order": order, "definition": laying.definition, "kit": laying.kit.used})
  bridgeStructureData.writeProbes(collection, laying.lookups.rows)
  bpy.context.view_layer.update()
  return collection


def layStructure(name, kind, definition, existing):
  """Lay a structure from its definition against the ground and kit as they now are, whole or not at all."""
  spec = kinds[kind]
  if definition["collection"] not in placeCollections():
    raise ValueError(f"collection is one of {list(placeCollections())} (terrain for a span exported as ground), got {definition['collection']!r}")
  order = bridgeStructureData.readStructure(existing)["order"] if existing is not None else bridgeStructureData.nextOrder()
  with bridgeKitData.linkingUndone():
    ground = StructureGround()
    laying = Laying(name, definition, Kit(absoluteKitPath(definition["kitPath"])), GroundLookups(ground.without(skippedNames(order))))
    report = spec.lay(laying)
    collection = commit(laying, kind, order, existing)
  result = {"structure": {"name": name, "kind": kind, "order": order}} | report | {
    "parts": describeParts(collection), "standsOn": sorted(laying.lookups.standsOn), "views": spec.views(definition, groundHeight),
  }
  if spec.walkLine is not None:
    result["walk"] = bridgeReview.walkRoute(walkLine(collection), walkSpacing)
  return result


def requireCollectionArgument(collection, kind):
  if collection not in placeCollections():
    raise ValueError(f"collection is one of {list(placeCollections())}, got {collection!r}")
  if kind == "wall" and collection == bridgeExport.terrainCollectionName:
    raise ValueError("A wall is placed sections, not ground; lay it in 'structures'")


def buildStructure(kind, name, arguments):
  requireNewStructureName(name)
  spec = kinds[kind]
  requireCollectionArgument(arguments["collection"], kind)
  definition = floated({key: arguments[key] for key in spec.keys}, spec.indexKeys) | {"kitPath": keptKitPath(arguments["kitPath"])}
  return layStructure(name, kind, definition, None)


def replayProbes(collection, ground):
  """The structure's probes looked up again on the ground as it now is: how many moved, the largest change and where, and what they
  find now."""
  record = bridgeStructureData.readStructure(collection)
  surfaces = ground.without(skippedNames(record["order"]))
  moved, largest, standsOn = 0, None, set()
  for row in bridgeStructureData.readProbes(collection):
    found, owner = runProbe(surfaces, row)
    if owner is not None:
      standsOn.add(owner)
    before = row[8]
    if math.isnan(found) and math.isnan(before):
      continue
    change = math.inf if math.isnan(found) != math.isnan(before) else abs(found - before)
    if change > probeTolerance:
      moved += 1
      if largest is None or change > largest["change"]:
        largest = {"change": change, "at": roundVector(row[1:4]), "probe": bridgeStructureData.probeKindNames[int(row[0])], "before": None if math.isnan(before) else round(float(before), 3), "now": None if math.isnan(found) else round(float(found), 3)}
  if largest is not None:
    largest["change"] = None if math.isinf(largest["change"]) else round(largest["change"], 3)
  return {"moved": moved, "largest": largest}, sorted(standsOn)


def kitState(record):
  """The pieces whose fingerprints differ from the lay's, and what cannot be found."""
  changed, missing = [], []
  kitPath = absoluteKitPath(record["definition"]["kitPath"])
  for piece, fingerprint in sorted(record["kit"].items()):
    try:
      collection = bridgeKitData.requirePiece(kitPath, piece)
      if collection.library is not None and collection.library.is_missing:
        raise FileNotFoundError(f"Kit '{kitPath}' is missing")
      if bridgeKitData.fingerprint(collection) != fingerprint:
        changed.append(piece)
    except (FileNotFoundError, ValueError) as error:
      missing.append({"piece": piece, "why": str(error)})
  return changed, missing


def describeStaleness(collection, ground):
  record = bridgeStructureData.readStructure(collection)
  groundState, standsOn = replayProbes(collection, ground)
  changed, missing = kitState(record)
  why = []
  if groundState["moved"]:
    why.append("ground")
  if changed:
    why.append("kit")
  if missing:
    why.append("missing")
  return {"stale": bool(why), "why": why, "ground": groundState, "kitChanged": changed, "missing": missing}, standsOn


def staleStructures():
  """Every structure stale now, with why."""
  stale = []
  with bridgeKitData.linkingUndone(always=True):
    ground = StructureGround()
    for collection in bridgeStructureData.structureCollections():
      state, _ = describeStaleness(collection, ground)
      if state["stale"]:
        stale.append({"structure": collection.name, "why": state["why"]})
  return stale


def walkLine(collection):
  """A span's walk line: its centerline at deck height, run on walkExtension past each end that stands within a step of footing."""
  record = bridgeStructureData.readStructure(collection)
  spec = kinds[record["kind"]]
  if spec.walkLine is None:
    raise ValueError(f"'{collection.name}' is a {record['kind']}; only bridges, flights, and walkways have a walk line")
  points = [mathutils.Vector(point) for point in spec.walkLine(record["definition"])]
  surfaces = StructureGround().without(skippedNames(record["order"]))
  extensions = []
  for end, inner in ((points[0], points[1]), (points[-1], points[-2])):
    footing = surfaces.footingOn(end + up * (stepHeight + castNudge), 2 * stepHeight + castNudge)
    outward = mathutils.Vector((end.x - inner.x, end.y - inner.y, 0.0)).normalized()
    extensions.append([] if footing is None or abs(end.z - footing.point.z) > stepHeight else [list(end + outward * walkExtension)])
  return extensions[0] + [list(point) for point in points] + extensions[1]


def structureWalkLine(name):
  collection = bridgeStructureData.findStructure(name)
  return None if collection is None else walkLine(collection)


def groundHeight(x, y):
  """The highest ground players stand on at [x, y], for placing a view's eye (a view is not a probe), or None."""
  top = bridgeMeshAccess.sceneTopHeight() + overheadLift
  footing = bridgeMeshAccess.PlayerSurfaces().footingBelow(mathutils.Vector((x, y, top)), top + bridgeMeshAccess.waterReach)
  return None if footing is None else footing.z


def headingOf(direction):
  return round(math.degrees(math.atan2(direction[0], direction[1])) % 360.0, 3)


def standView(at, direction, pitch=-5.0):
  return {"standAt": roundVector(at), "headingDegrees": headingOf(direction), "pitchDegrees": pitch}


def lookView(eye, target):
  return {"eye": roundVector(eye), "target": roundVector(target)}


def editStructure(name, changes):
  collection = bridgeStructureData.requireStructure(name)
  record = bridgeStructureData.readStructure(collection)
  spec = kinds[record["kind"]]
  changes = changes or {}
  if not isinstance(changes, dict):
    raise ValueError(f"changes is {{key: value}} of the structure's definition, got {changes!r}")
  unknown = sorted(set(changes) - set(spec.keys))
  if unknown:
    raise ValueError(f"{unknown} are not in a {record['kind']}'s definition; its keys: {list(spec.keys)}")
  if "collection" in changes:
    requireCollectionArgument(changes["collection"], record["kind"])
  definition = record["definition"] | floated(changes, spec.indexKeys)
  if "kitPath" in changes:
    definition["kitPath"] = keptKitPath(changes["kitPath"])
  moved, _ = replayProbes(collection, StructureGround())
  return layStructure(name, record["kind"], definition, collection) | {"changes": changes, "probesMovedSinceLaid": moved}


def removeStructure(name):
  collection = bridgeStructureData.requireStructure(name)
  record = bridgeStructureData.readStructure(collection)
  parts = [part.name for part in bridgeStructureData.partsOf(collection)]
  removeParts(bridgeStructureData.partsOf(collection))
  bpy.data.collections.remove(collection)
  removed = removeUnusedShearMeshes()
  bpy.context.view_layer.update()
  return {"name": name, "kind": record["kind"], "definition": shownDefinition(record["definition"]), "removedParts": parts, "removedShearMeshes": removed}


def loosePieces():
  """Kit pieces placed by hand (not parts of a structure), counted by kit and piece."""
  counts = {}
  for sceneObject in bridgeKitData.placedPieces():
    if bridgeStructureData.structureOf(sceneObject) is None:
      key = (bridgeKitData.kitPathOf(sceneObject.instance_collection), sceneObject.instance_collection.name)
      counts[key] = counts.get(key, 0) + 1
  return [{"kit": kit, "piece": piece, "placements": count} for (kit, piece), count in sorted(counts.items(), key=lambda item: (item[0][0] or "", item[0][1]))]


def getStructures(names):
  collections = bridgeStructureData.structureCollections()
  if names is not None:
    known = {collection.name for collection in collections}
    unknown = [name for name in names if name not in known]
    if unknown:
      raise ValueError(f"No structures named {unknown}; structures: {sorted(known)}")
    collections = [collection for collection in collections if collection.name in names]
  described = []
  with bridgeKitData.linkingUndone(always=True):
    ground = StructureGround()
    for collection in collections:
      record = bridgeStructureData.readStructure(collection)
      spec = kinds[record["kind"]]
      state, standsOn = describeStaleness(collection, ground)
      entry = {
        "name": collection.name, "kind": record["kind"], "order": record["order"], "definition": shownDefinition(record["definition"]),
        "parts": describeParts(collection), "standsOn": [owner for owner in standsOn if owner not in {part.name for part in bridgeStructureData.partsOf(collection)}],
      } | state | {"views": spec.views(record["definition"], groundHeight)}
      if spec.walkLine is not None:
        entry["walkLine"] = [roundVector(point) for point in walkLine(collection)]
      described.append(entry)
  return {"structures": described, "loosePieces": loosePieces()}


def describePart(sceneObject):
  """A part's structure, kind, and role, or None for an object that is not one."""
  owner = bridgeStructureData.structureOf(sceneObject)
  if owner is None:
    return None
  collection = bridgeStructureData.findStructure(owner)
  return {"structure": owner, "kind": None if collection is None else bridgeStructureData.readStructure(collection)["kind"], "role": partRole(owner, sceneObject)}


def buildBridge(name, kitPath, start, end, width, deck, profile, posts, rails, stringers, bents, sink, maximumDeckDegrees, collection):
  return buildStructure("bridge", name, locals())


def buildStairs(name, kitPath, bottom, top, width, tread, riser, stringers, posts, rails, sink, collection):
  return buildStructure("stairs", name, locals())


def buildWalkway(name, kitPath, points, width, deck, treads, stairLegs, riser, posts, brackets, rails, stringers, sink, maximumGradeDegrees, collection):
  return buildStructure("walkway", name, locals())


def buildWall(name, kitPath, path, frontSide, sections, follow, shearStep, sink, maximumBurial, posts, variants, collection):
  return buildStructure("wall", name, locals())


commands = {
  "buildBridge": (buildBridge, True),
  "buildStairs": (buildStairs, True),
  "buildWalkway": (buildWalkway, True),
  "buildWall": (buildWall, True),
  "editStructure": (editStructure, True),
  "removeStructure": (removeStructure, True),
  "getStructures": (getStructures, False),
}
