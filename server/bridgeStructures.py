"""Structures laid from their definitions, as one artist action on one thing the artist defined: its parts made whole or not at all
from the definition, its kit, and the ground; every lookup of the ground kept as a probe row, so a structure is stale when its ground
or its kit changed since, and lays again from its definition. Kinds register their definition, lay, and views here at import. Runs
under Blender's Python."""
import math
import os
import typing

import bpy
import mathutils
import numpy

import bridgeBoundaries
import bridgeExport
import bridgeKitData
import bridgeMeshAccess
import bridgeObjects
import bridgeReviewGuides
import bridgeStructureData
import playerScale

# The kit pieces and prefabs a structure was laid from, held by reference so a linked one stays in the file while the structure does.
sourcesProperty = "zonewrightStructureSources"
probeTolerance = 0.01
# Lookups from above the scene start this far over its top; footing is looked for this far below a step over a point, as walkRoute does.
overheadLift = 10.0
footingReach = 60.0
reservedCollections = (bridgeExport.terrainCollectionName,)
up = bridgeMeshAccess.up
down = bridgeMeshAccess.down
castNudge = bridgeMeshAccess.castNudge
kinds = {}


class Kind(typing.NamedTuple):
  """A structure kind: define(arguments) checks a build tool's arguments and gives the definition kept; lay(laying) makes the parts and
  gives the build result; views(structure) gives renderView views derived from it."""
  definitionKeys: tuple
  define: typing.Callable
  lay: typing.Callable
  views: typing.Callable


def registerKind(kind, definitionKeys, define, lay, views):
  kinds[kind] = Kind(tuple(definitionKeys), define, lay, views)


class StructureGround:
  """What a structure stands on, as players collide with it (no water, cutout or passable faces, guides, regions, spawns, doors, or
  boundaries), leaving out the objects named in skipping (the structure's own parts and every structure laid after it); each lookup is
  recorded as a probe row [kind, origin, direction, reach, found]."""

  def __init__(self, trees, skipping):
    kept = [tree for tree in trees if tree[0] not in skipping]
    self.surfaces = bridgeMeshAccess.PlayerSurfaces(trees=kept) if kept else None
    self.top = None
    self.rows, self.standsOn = [], set()

  def record(self, kind, origin, direction, reach, found, owner):
    row = [float(bridgeStructureData.probeKinds.index(kind))] + [float(value) for value in origin] + [float(value) for value in direction]
    self.rows.append(row + [float(reach), numpy.nan if found is None else float(found)])
    if owner is not None:
      self.standsOn.add(owner)
    return found

  def lookDown(self, origin, reach):
    footing = None if self.surfaces is None else self.surfaces.footingOn(mathutils.Vector(origin), reach)
    return (None, None) if footing is None else (footing.point.z, footing.objectName)

  def lookAlong(self, origin, direction, reach):
    hit = None if self.surfaces is None else self.surfaces.castOn(mathutils.Vector(origin), mathutils.Vector(direction), reach)
    return (None, None) if hit is None else ((hit[0] - mathutils.Vector(origin)).length, hit[2])

  def lookFromAbove(self, origin, reach):
    hit = None if self.surfaces is None else self.surfaces.castOn(mathutils.Vector(origin), down, reach)
    return (None, None) if hit is None else (hit[0].z, hit[2])

  def lookAtLevel(self, x, y, level):
    """The ground for a player at a level, as plots find it (PlayerSurfaces.groundAtLevel): where a step over the level lies inside a
    solid, the top of it; otherwise the footing under that point."""
    if self.surfaces is None:
      return None, None
    origin = mathutils.Vector((x, y, level + playerScale.stepHeight))
    above = self.surfaces.castOn(origin, up, bridgeMeshAccess.waterReach)
    if above is not None and above[1].z > 0 and self.surfaces.enclosedAround(origin):
      return above[0].z, above[2]
    return self.lookDown(origin, bridgeMeshAccess.waterReach)

  def footing(self, point):
    """The footing under a point, looked for from a step over it (walkRoute's footingAt); its height or None."""
    origin = mathutils.Vector(point) + up * (playerScale.stepHeight + castNudge)
    reach = footingReach + playerScale.stepHeight + castNudge
    return self.record("footing", origin, down, reach, *self.lookDown(origin, reach))

  def below(self, point, reach):
    """The first up-facing surface below a point within reach; its height or None."""
    return self.record("below", point, down, reach, *self.lookDown(point, reach))

  def beside(self, point, direction, reach):
    """How far a level direction from a point meets a face, within reach; None for none."""
    return self.record("beside", point, direction, reach, *self.lookAlong(point, direction, reach))

  def overhead(self, x, y):
    """Looking down at [x, y] from above the scene: the height of what is met first (None for nothing), and where rock lies over
    ground there, the heights of its top, its underside, and that ground (bridgeMeshAccess.rockOverGround)."""
    if self.top is None:
      self.top = bridgeMeshAccess.sceneTopHeight() + overheadLift
    origin = (x, y, self.top)
    found = self.record("overhead", origin, down, bridgeMeshAccess.waterReach, *self.lookFromAbove(origin, bridgeMeshAccess.waterReach))
    levels = None if self.surfaces is None else bridgeMeshAccess.rockOverGround(self.surfaces.castWithNormal, x, y, self.top)
    return found, levels

  def level(self, x, y, level):
    """The ground at [x, y] for a floor at level (lookAtLevel); its height or None."""
    return self.record("level", (x, y, level), (0.0, 0.0, 0.0), bridgeMeshAccess.waterReach, *self.lookAtLevel(x, y, level))

  def replay(self, rows):
    """What each recorded lookup finds now, and the objects met."""
    found, owners = numpy.full(len(rows), numpy.nan), set()
    for index, row in enumerate(rows):
      kind = bridgeStructureData.probeKinds[int(row[0])]
      origin, direction, reach = row[1:4], row[4:7], row[7]
      if kind in ("footing", "below"):
        value, owner = self.lookDown(origin, reach)
      elif kind == "beside":
        value, owner = self.lookAlong(origin, direction, reach)
      elif kind == "overhead":
        value, owner = self.lookFromAbove(origin, reach)
      else:
        value, owner = self.lookAtLevel(*origin)
      if value is not None:
        found[index] = value
      if owner is not None:
        owners.add(owner)
    return found, owners


def groundChange(rows, found):
  """How the ground moved under recorded probes: how many moved, the largest change and where, and those found before and not now or
  the other way."""
  recorded = rows[:, 8]
  lost, gained = numpy.isnan(found) & ~numpy.isnan(recorded), ~numpy.isnan(found) & numpy.isnan(recorded)
  both = ~numpy.isnan(found) & ~numpy.isnan(recorded)
  changes = numpy.where(both, numpy.abs(found - recorded), 0.0)
  moved = (changes > probeTolerance) | lost | gained
  largest = int(numpy.argmax(changes)) if len(changes) else None
  return {
    "moved": int(moved.sum()), "probes": len(rows),
    "largestChange": round(float(changes[largest]), 3) if largest is not None and changes[largest] > probeTolerance else 0.0,
    "at": bridgeKitData.roundVector([rows[largest, 1], rows[largest, 2], recorded[largest]], 2) if largest is not None and changes[largest] > probeTolerance else None,
    "lost": int(lost.sum()), "found": int(gained.sum()),
  }


def laterParts(order):
  return {part.name for collection in bridgeStructureData.structureCollections() if bridgeStructureData.readStructure(collection)["order"] > order for part in collection.objects}


def keptKitPath(kitPath):
  return None if kitPath is None else bridgeReviewGuides.keptPath(kitPath)


def absoluteKitPath(kept):
  return None if kept is None else os.path.normpath(bpy.path.abspath(kept))


def requireNewStructureName(name):
  if not isinstance(name, str) or not name:
    raise ValueError(f"name is the structure's name, got {name!r}")
  if bridgeStructureData.structureNamed(name) is not None:
    raise ValueError(f"'{name}' is already a structure: editStructure changes it")
  for kind, found in (("a collection", bpy.data.collections.get(name)), ("an object", bpy.data.objects.get(name))):
    if found is not None:
      raise ValueError(f"'{name}' is already the name of {kind} in this file")


def requireStructureCollection(collection):
  if collection in reservedCollections:
    raise ValueError(f"A structure of this kind is not ground; lay it in another collection than '{collection}' (by default 'structures')")
  if not isinstance(collection, str) or not collection:
    raise ValueError(f"collection names the collection the structure goes in, got {collection!r}")


class Laying:
  """A lay in progress: its definition, its ground, and the parts it makes, kept in a temporary collection until every part is made and
  every check passed."""

  def __init__(self, name, definition, ground, collection, ownParts):
    self.name, self.definition, self.ground, self.collection, self.ownParts = name, definition, ground, collection, ownParts
    self.parts, self.sources = [], {}

  def addPart(self, sceneObject, finalName):
    taken = bpy.data.objects.get(finalName)
    if taken is not None and taken != sceneObject and taken.name not in self.ownParts:
      raise ValueError(f"'{finalName}' is already the name of an object, so structure '{self.name}' cannot name a part so")
    if sceneObject.type == "MESH":
      takenMesh = bpy.data.meshes.get(finalName)
      ownMeshes = {bpy.data.objects[part].data for part in self.ownParts if bpy.data.objects[part].type == "MESH"}
      if takenMesh is not None and takenMesh != sceneObject.data and takenMesh not in ownMeshes:
        raise ValueError(f"'{finalName}' is already the name of a mesh, so structure '{self.name}' cannot name a part's mesh so")
    self.collection.objects.link(sceneObject)
    sceneObject[bridgeStructureData.partProperty] = self.name
    self.parts.append((sceneObject, finalName))
    return sceneObject

  def useSource(self, collection):
    self.sources[collection.name] = collection
    return collection


def parentsOf(collection):
  return [parent for parent in [bpy.context.scene.collection] + list(bpy.data.collections) if collection.name in parent.children]


def commit(laying, existing, kind, order):
  parentName = laying.definition["collection"]
  if existing is None:
    structure = bpy.data.collections.new(laying.name)
    bridgeObjects.targetCollection(parentName).children.link(structure)
  else:
    structure = existing
    parent = bridgeObjects.targetCollection(parentName)
    if parent not in parentsOf(structure):
      for old in parentsOf(structure):
        old.children.unlink(structure)
      parent.children.link(structure)
    for part in list(structure.objects):
      data = part.data
      bpy.data.objects.remove(part)
      if isinstance(data, bpy.types.Mesh) and data.users == 0:
        bpy.data.meshes.remove(data)
  for part, finalName in laying.parts:
    laying.collection.objects.unlink(part)
    structure.objects.link(part)
    part.name = finalName
    if part.type == "MESH" and part.data.users == 1:
      part.data.name = finalName
  bpy.data.collections.remove(laying.collection)
  bridgeStructureData.writeStructure(structure, {
    "kind": kind, "order": order, "definition": laying.definition,
    "kit": {name: bridgeKitData.sourceFingerprint(source) for name, source in sorted(laying.sources.items())},
  })
  bridgeStructureData.writeProbes(structure, laying.ground.rows)
  structure[sourcesProperty] = dict(laying.sources)
  bpy.context.view_layer.update()
  return structure


def layStructure(name, kind, definition):
  """Lay a structure from its definition, whole or not at all: on any refusal its new parts and anything linked for them go, and an
  existing structure stays exactly as it was."""
  existing = bridgeStructureData.structureNamed(name)
  order = bridgeStructureData.readStructure(existing)["order"] if existing is not None else bridgeStructureData.nextOrder()
  ownParts = {part.name for part in existing.objects} if existing is not None else set()
  trees = bridgeBoundaries.collisionTrees(boundaries=False)
  ground = StructureGround(trees, ownParts | laterParts(order))
  moved = None
  if existing is not None:
    rows = bridgeStructureData.readProbes(existing)
    moved = groundChange(rows, ground.replay(rows)[0])
  with bridgeKitData.linkingUndone():
    layingCollection = bpy.data.collections.new(f"{name}Laying")
    bpy.context.scene.collection.children.link(layingCollection)
    laying = Laying(name, definition, ground, layingCollection, ownParts)
    result = kinds[kind].lay(laying)
    structure = commit(laying, existing, kind, order)
  return describeStructure(structure) | result | {"standsOn": sorted(ground.standsOn), "views": kinds[kind].views(structure)} | (
    {"groundMoved": moved} if moved is not None else {}
  )


def partRole(structureName, part):
  role = part.name[len(structureName):] if part.name.startswith(structureName) else part.name
  return role[:1].lower() + role[1:] if role else "whole"


def describeParts(structure):
  parts = []
  for part in bridgeStructureData.partsOf(structure):
    role = "mesh" if part.type == "MESH" else "instance"
    parts.append({
      "name": part.name, "role": partRole(structure.name, part), "triangles": sum(bridgeMeshAccess.triangleCount(mesh) for mesh, _ in bridgeMeshAccess.objectParts(part)),
      "model": f"obj_{bridgeExport.modelStem(bridgeExport.modelKey(part, role))}.mod",
    })
  return parts


def describeStructure(structure):
  record = bridgeStructureData.readStructure(structure)
  parts = describeParts(structure)
  return {"structure": {"name": structure.name, "kind": record["kind"], "order": record["order"]}, "parts": parts, "triangles": sum(part["triangles"] for part in parts)}


def argumentsOf(definition):
  """A definition as its build tool's arguments, its kit path absolute again."""
  return definition | ({"kitPath": absoluteKitPath(definition["kitPath"])} if "kitPath" in definition else {})


def sourceState(structure):
  """The kit pieces and prefabs a structure was laid from that are missing (their kit file, library, or collection), and those whose
  fingerprint changed since."""
  record = bridgeStructureData.readStructure(structure)
  sources = structure.get(sourcesProperty) or {}
  missing, changed = [], []
  for name, recorded in record["kit"].items():
    source = sources.get(name)
    library = None if source is None else source.library
    if source is None or source.is_missing or (library is not None and (library.is_missing or not os.path.isfile(bpy.path.abspath(library.filepath)))):
      missing.append(name)
    elif bridgeKitData.sourceFingerprint(source) != recorded:
      changed.append(name)
  return missing, changed


def staleness(structure, trees):
  """Why a structure is stale (ground, kit, missing), each with its detail, and the objects its probes stand on now."""
  record = bridgeStructureData.readStructure(structure)
  ground = StructureGround(trees, {part.name for part in structure.objects} | laterParts(record["order"]))
  rows = bridgeStructureData.readProbes(structure)
  found, owners = ground.replay(rows)
  why = {}
  change = groundChange(rows, found)
  if change["moved"]:
    why["ground"] = change
  missing, changed = sourceState(structure)
  if changed:
    why["kit"] = {"changed": changed}
  if missing:
    why["missing"] = {"sources": missing, "kit": record["definition"].get("kitPath")}
  return why, sorted(owners)


def editStructure(name, changes):
  structure = bridgeStructureData.requireStructure(name)
  record = bridgeStructureData.readStructure(structure)
  kind = kinds[record["kind"]]
  changes = changes or {}
  if not isinstance(changes, dict):
    raise ValueError(f"changes is an object of definition keys, got {changes!r}")
  unknown = sorted(set(changes) - set(kind.definitionKeys))
  if unknown:
    raise ValueError(f"A {record['kind']} has no {unknown} to change; its definition's keys: {list(kind.definitionKeys)}")
  definition = kind.define(argumentsOf(record["definition"]) | changes)
  return layStructure(name, record["kind"], definition) | {"changes": changes}


def removeStructure(name):
  structure = bridgeStructureData.requireStructure(name)
  record = bridgeStructureData.readStructure(structure)
  for part in list(structure.objects):
    data = part.data
    bpy.data.objects.remove(part)
    if isinstance(data, bpy.types.Mesh) and data.users == 0:
      bpy.data.meshes.remove(data)
  bpy.data.collections.remove(structure)
  bpy.context.view_layer.update()
  return {"name": name, "kind": record["kind"], "definition": argumentsOf(record["definition"])}


def looseKitPieces():
  counts = {}
  for placement in bridgeKitData.placedPieces():
    if bridgeStructureData.structureOf(placement) is not None:
      continue
    collection = placement.instance_collection
    key = (bridgeKitData.kitPathOf(collection), collection.name)
    counts[key] = counts.get(key, 0) + 1
  return [{"kit": kit, "piece": piece, "placements": count} for (kit, piece), count in sorted(counts.items(), key=lambda item: (item[0][0] or "", item[0][1]))]


def getStructures(names):
  every = bridgeStructureData.structureCollections()
  if names is not None:
    unknown = sorted(set(names) - {structure.name for structure in every})
    if unknown:
      raise ValueError(f"No structures named {unknown}; the structures: {[structure.name for structure in every]}")
  trees = bridgeBoundaries.collisionTrees(boundaries=False)
  described = []
  for structure in every:
    if names is not None and structure.name not in names:
      continue
    record = bridgeStructureData.readStructure(structure)
    why, standsOn = staleness(structure, trees)
    described.append(describeStructure(structure) | {
      "definition": argumentsOf(record["definition"]), "standsOn": standsOn, "stale": bool(why), "why": why,
      "views": {} if "missing" in why else kinds[record["kind"]].views(structure),
    })
  return {"structures": described, "looseKitPieces": looseKitPieces()}


def describePart(sceneObject):
  """A structure part's structure, kind, and role, or None."""
  owner = bridgeStructureData.structureOf(sceneObject)
  if owner is None:
    return None
  structure = bridgeStructureData.structureNamed(owner)
  return {"structure": owner, "kind": None if structure is None else bridgeStructureData.readStructure(structure)["kind"], "role": partRole(owner, sceneObject)}


def turnAbout(facingDegrees):
  return mathutils.Matrix.Rotation(math.radians(-facingDegrees), 3, "Z")


commands = {
  "editStructure": (editStructure, True),
  "removeStructure": (removeStructure, True),
  "getStructures": (getStructures, False),
}
