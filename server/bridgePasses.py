"""Shaping passes: each shaping operation on a mesh can go into a named pass (a Blender shape key over the mesh's base shape) that can
later be turned up or down, muted, removed, or collapsed into the base, so shaping is revised by adjusting passes rather than redone.
A defined pass keeps the definition it is rebuilt from (bridgeGrading), which goes with it. Runs under Blender's Python."""
import json

import numpy

import bridgeMeshAccess

baseKeyName = "base"
# A pass at -1 inverts what it holds, at 2 doubles it.
strengthRange = (-1.0, 2.0)
# Kept on the mesh's shape keys, so collapsing the passes takes the definitions with them.
definitionsProperty = "zonewrightPassDefinitions"


def passList(sceneObject):
  keys = sceneObject.data.shape_keys
  if keys is None:
    return []
  active = sceneObject.active_shape_key
  return [
    {"name": key.name, "strength": round(key.value, 4), "muted": key.mute, "active": key == active}
    for key in keys.key_blocks if key != keys.reference_key
  ]


def requirePass(sceneObject, name):
  keys = sceneObject.data.shape_keys
  key = keys.key_blocks.get(name) if keys is not None else None
  if key is None or key == keys.reference_key:
    raise ValueError(f"'{sceneObject.name}' has no shaping pass '{name}'; its passes: {[entry['name'] for entry in passList(sceneObject)]}")
  return key


def activePass(sceneObject):
  """The pass shaping writes into: the active one, which must show."""
  keys = sceneObject.data.shape_keys
  key = sceneObject.active_shape_key
  if key is None or key == keys.reference_key:
    raise ValueError(f"'{sceneObject.name}' has shaping passes but none is active; add one for this shaping (addShapingPass) or make one active (setShapingPass)")
  if key.mute or key.value == 0:
    raise ValueError(f"Shaping pass '{key.name}' of '{sceneObject.name}' is {'muted' if key.mute else 'at strength 0'}, so shaping it would not show; unmute it or set a strength first")
  if key.name in passDefinitions(sceneObject):
    raise ValueError(f"Shaping pass '{key.name}' of '{sceneObject.name}' is rebuilt whole from its definition, so shaping put into it would be lost; shape in another pass (addShapingPass, or setShapingPass makeActive)")
  return key


def passDefinitions(sceneObject):
  """The definitions of a mesh's defined passes, by pass name."""
  keys = sceneObject.data.shape_keys
  return {} if keys is None or definitionsProperty not in keys else json.loads(keys[definitionsProperty])


def setPassDefinition(sceneObject, name, definition):
  """Keep a pass's definition with it, or with definition None drop it."""
  definitions = passDefinitions(sceneObject)
  if definition is None:
    definitions.pop(name, None)
  else:
    definitions[name] = definition
  sceneObject.data.shape_keys[definitionsProperty] = json.dumps(definitions)


def keyCoordinates(key):
  # Read in the pass's own single precision, which loses nothing and takes under half the time of converting each value.
  coordinates = numpy.empty(len(key.data) * 3, dtype=numpy.float32)
  key.data.foreach_get("co", coordinates)
  return coordinates.reshape(-1, 3).astype(numpy.float64)


def writeIntoActivePass(sceneObject, localPositions):
  """Move the mesh as seen to localPositions by adding the change, divided by the pass's strength, into the active pass."""
  key = activePass(sceneObject)
  with bridgeMeshAccess.shapedMesh(sceneObject) as mesh:
    shown = numpy.empty(len(mesh.vertices) * 3)
    mesh.vertices.foreach_get("co", shown)
  updated = keyCoordinates(key) + (localPositions - shown.reshape(-1, 3)) / key.value
  key.data.foreach_set("co", updated.ravel())
  sceneObject.data.update()


def addShapingPass(objectName, name):
  sceneObject = bridgeMeshAccess.requireMeshObject(objectName)
  if sceneObject.modifiers:
    raise ValueError(f"'{objectName}' has modifiers; passes combine before modifiers, so apply or remove them first")
  if name == baseKeyName:
    raise ValueError(f"'{baseKeyName}' names the shape passes build on; choose another name")
  keys = sceneObject.data.shape_keys
  if keys is not None and keys.key_blocks.get(name) is not None:
    raise ValueError(f"'{objectName}' already has a shaping pass '{name}'")
  if keys is None:
    sceneObject.shape_key_add(name=baseKeyName, from_mix=False)
  key = sceneObject.shape_key_add(name=name, from_mix=False)
  key.relative_key = sceneObject.data.shape_keys.reference_key
  key.slider_min, key.slider_max = strengthRange
  key.value = 1.0
  sceneObject.active_shape_key_index = list(sceneObject.data.shape_keys.key_blocks).index(key)
  return {"object": objectName, "passes": passList(sceneObject)}


def setShapingPass(objectName, name, strength, muted, makeActive):
  sceneObject = bridgeMeshAccess.requireMeshObject(objectName)
  key = requirePass(sceneObject, name)
  if strength is None and muted is None and not makeActive:
    raise ValueError("setShapingPass needs a strength, muted, or makeActive")
  if strength is not None:
    if not strengthRange[0] <= strength <= strengthRange[1]:
      raise ValueError(f"strength must be within {list(strengthRange)}, got {strength}")
    key.value = strength
  if muted is not None:
    key.mute = muted
  if makeActive:
    sceneObject.active_shape_key_index = list(sceneObject.data.shape_keys.key_blocks).index(key)
  sceneObject.data.update()
  return {"object": objectName, "passes": passList(sceneObject)}


def clearPassesKeeping(sceneObject, localPositions):
  sceneObject.shape_key_clear()
  sceneObject.data.vertices.foreach_set("co", localPositions.ravel())
  sceneObject.data.update()


def removeShapingPass(objectName, name):
  sceneObject = bridgeMeshAccess.requireMeshObject(objectName)
  key = requirePass(sceneObject, name)
  if name in passDefinitions(sceneObject):
    setPassDefinition(sceneObject, name, None)
  sceneObject.shape_key_remove(key)
  keys = sceneObject.data.shape_keys
  if len(keys.key_blocks) == 1:
    clearPassesKeeping(sceneObject, keyCoordinates(keys.reference_key))
  return {"object": objectName, "removed": name, "passes": passList(sceneObject)}


def collapseShapingPasses(objectName):
  """Make the mesh as seen its new base and drop the passes, so its vertices can change."""
  sceneObject = bridgeMeshAccess.requireMeshObject(objectName)
  passes = passList(sceneObject)
  if not passes:
    raise ValueError(f"'{objectName}' has no shaping passes")
  with bridgeMeshAccess.shapedMesh(sceneObject) as mesh:
    shown = numpy.empty(len(mesh.vertices) * 3)
    mesh.vertices.foreach_get("co", shown)
  clearPassesKeeping(sceneObject, shown.reshape(-1, 3))
  return {"object": objectName, "collapsed": [entry["name"] for entry in passes]}


commands = {
  "addShapingPass": (addShapingPass, True),
  "setShapingPass": (setShapingPass, True),
  "removeShapingPass": (removeShapingPass, True),
  "collapseShapingPasses": (collapseShapingPasses, True),
}
