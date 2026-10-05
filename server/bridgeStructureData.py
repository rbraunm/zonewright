"""Structures as records on collections: a structure's collection holds its parts and its record (kind, order, definition, the kit
fingerprints its lay used), and every lookup of the ground its lay made as probe rows. Light on imports (bpy, json, numpy only), so
every tool module can refuse to change a structure's parts by hand. Runs under Blender's Python."""
import json

import bpy
import numpy

structureProperty = "zonewrightStructure"
probesProperty = "zonewrightStructureProbes"
partProperty = "zonewrightStructurePart"
# A probe row: kind code, the cast's origin, its direction, its reach, and what it found (a height or a distance, NaN for none).
probeWidth = 9
probeKinds = ("footing", "below", "beside", "overhead", "level")


def readStructure(collection):
  return json.loads(collection[structureProperty]) if structureProperty in collection else None


def writeStructure(collection, record):
  collection[structureProperty] = json.dumps(record)


def structureCollections():
  """Every structure of the open file, in the order they were first laid."""
  found = [collection for collection in bpy.data.collections if collection.library is None and structureProperty in collection]
  return sorted(found, key=lambda collection: readStructure(collection)["order"])


def structureNamed(name):
  collection = next((found for found in bpy.data.collections if found.name == name and found.library is None), None)
  return collection if collection is not None and structureProperty in collection else None


def requireStructure(name):
  collection = structureNamed(name)
  if collection is None:
    raise ValueError(f"No structure named '{name}'; the structures: {[found.name for found in structureCollections()]}")
  return collection


def nextOrder():
  return 1 + max((readStructure(collection)["order"] for collection in structureCollections()), default=0)


def structureOf(sceneObject):
  """The name of the structure an object is a part of, or None."""
  return sceneObject.get(partProperty)


def requireNotStructurePart(sceneObject, action):
  owner = structureOf(sceneObject)
  if owner is not None:
    raise ValueError(
      f"Cannot {action} '{sceneObject.name}': it is part of structure '{owner}', laid from its definition: editStructure changes it,"
      f" removeStructure takes it back"
    )


def partsOf(collection):
  return sorted(collection.objects, key=lambda part: part.name)


def readProbes(collection):
  if probesProperty not in collection:
    return numpy.zeros((0, probeWidth))
  return numpy.array(collection[probesProperty], dtype=numpy.float64).reshape(-1, probeWidth)


def writeProbes(collection, rows):
  collection[probesProperty] = numpy.asarray(rows, dtype=numpy.float64).reshape(-1).tolist()
