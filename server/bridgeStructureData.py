"""Structures as records on collections: each structure's record (kind, order, definition, the kit fingerprints it was laid from), its
probes (every ground lookup its lay made), the tag on each part, and the guard that keeps hand edits off parts. Light on imports, so
every tool module can guard with it. Runs under Blender's Python."""
import json

import bpy
import numpy

structureProperty = "zonewrightStructure"
probesProperty = "zonewrightStructureProbes"
partProperty = "zonewrightStructurePart"
shearProperty = "zonewrightShearOf"
probeKinds = {"footing": 0, "below": 1, "beside": 2, "overhead": 3}
probeKindNames = {code: name for name, code in probeKinds.items()}
probeWidth = 9


def isStructure(collection):
  return collection.library is None and structureProperty in collection


def readStructure(collection):
  return json.loads(collection[structureProperty])


def writeStructure(collection, record):
  collection[structureProperty] = json.dumps(record)


def structureCollections():
  """Every structure of the open file, in the order they were first laid."""
  return sorted((collection for collection in bpy.data.collections if isStructure(collection)), key=lambda collection: readStructure(collection)["order"])


def findStructure(name):
  collection = bpy.data.collections.get(name)
  return collection if collection is not None and isStructure(collection) else None


def requireStructure(name):
  collection = findStructure(name)
  if collection is None:
    raise ValueError(f"No structure named '{name}'; structures: {[found.name for found in structureCollections()]}")
  return collection


def nextOrder():
  return 1 + max((readStructure(collection)["order"] for collection in structureCollections()), default=0)


def readProbes(collection):
  if probesProperty not in collection:
    return numpy.zeros((0, probeWidth))
  return numpy.array(list(collection[probesProperty]), dtype=numpy.float64).reshape(-1, probeWidth)


def writeProbes(collection, rows):
  collection[probesProperty] = numpy.asarray(rows, dtype=numpy.float64).reshape(-1).tolist()


def partsOf(collection):
  return sorted((member for member in collection.objects if member.get(partProperty) == collection.name), key=lambda member: member.name)


def namedObjects(name):
  """The objects a name stands for in a view: a structure's parts, or the object of that name."""
  collection = findStructure(name)
  return [part.name for part in partsOf(collection)] if collection is not None else [name]


def structureOf(sceneObject):
  """The name of the structure an object is a part of, or None."""
  return sceneObject.get(partProperty)


def requireNotStructurePart(sceneObject, action):
  """Refuse a hand edit (the tool named by action) of a structure's part, which the next lay would silently undo."""
  owner = structureOf(sceneObject)
  if owner is not None:
    raise ValueError(
      f"'{sceneObject.name}' is part of structure '{owner}', laid from its definition, so {action} cannot change it: editStructure changes it,"
      " removeStructure takes it back"
    )
