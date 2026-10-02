"""Blocking out, organizing, and inspecting objects. Runs under Blender's Python."""
import math

import bmesh
import bpy
import mathutils
import numpy

import bridgeMeshAccess
import bridgePasses

roundShapes = ("cylinder", "cone", "sphere")
eulerModes = ("XYZ", "XZY", "YXZ", "YZX", "ZXY", "ZYX")
measureCastDistance = 100000.0
primitiveKinds = ("plane", "grid", "cube") + roundShapes


def roundVector(vector, digits=3):
  return [round(float(component), digits) for component in vector]


def targetCollection(collectionName):
  if collectionName is None:
    return bpy.context.scene.collection
  collection = bpy.data.collections.get(collectionName)
  if collection is None:
    collection = bpy.data.collections.new(collectionName)
    bpy.context.scene.collection.children.link(collection)
  return collection


def requireNewName(name):
  if bpy.data.objects.get(name) is not None:
    raise ValueError(f"An object named '{name}' already exists")


def describeTransform(sceneObject):
  return {
    "name": sceneObject.name,
    "location": roundVector(sceneObject.location),
    "rotationDegrees": roundVector([math.degrees(angle) for angle in sceneObject.rotation_euler], 2),
    "scale": roundVector(sceneObject.scale),
    "dimensions": roundVector(sceneObject.dimensions),
  }


def fitToSize(meshEditor, size, flat):
  """Scale the new geometry to exactly the requested bounding size, with the origin at its base center (center for flat shapes)."""
  positions = numpy.array([vertex.co for vertex in meshEditor.verts])
  minimum, maximum = positions.min(0), positions.max(0)
  extent = maximum - minimum
  scale = numpy.array([size[0] / extent[0], size[1] / extent[1], 1.0 if flat else size[2] / extent[2]])
  base = numpy.array([(minimum[0] + maximum[0]) / 2, (minimum[1] + maximum[1]) / 2, (minimum[2] + maximum[2]) / 2 if flat else minimum[2]])
  for vertex in meshEditor.verts:
    vertex.co = mathutils.Vector((numpy.array(vertex.co) - base) * scale)


def linkNewMesh(name, meshEditor, location, rotationDegrees, collectionName):
  mesh = bpy.data.meshes.new(name)
  meshEditor.to_mesh(mesh)
  meshEditor.free()
  newObject = bpy.data.objects.new(name, mesh)
  newObject.location = location
  newObject.rotation_euler = [math.radians(angle) for angle in rotationDegrees]
  targetCollection(collectionName).objects.link(newObject)
  return newObject


def createPrimitive(kind, name, size, location, rotationDegrees, collection, segments, divisions):
  if kind not in primitiveKinds:
    raise ValueError(f"kind must be one of {list(primitiveKinds)}, got '{kind}'")
  requireNewName(name)
  flat = kind in ("plane", "grid")
  if len(size) != 3 or any(dimension <= 0 for dimension in size[:2]) or (not flat and size[2] <= 0):
    raise ValueError(f"size must be [x, y, z] with positive dimensions (z ignored for flat shapes), got {size}")
  if kind in roundShapes and (segments is None or segments < 3):
    raise ValueError(f"A {kind} needs segments of at least 3")
  if kind == "grid" and (divisions is None or len(divisions) != 2 or min(divisions) < 1):
    raise ValueError("A grid needs divisions [x, y] of at least 1 each")
  meshEditor = bmesh.new()
  if kind == "plane":
    bmesh.ops.create_grid(meshEditor, x_segments=1, y_segments=1, size=1)
  elif kind == "grid":
    bmesh.ops.create_grid(meshEditor, x_segments=divisions[0], y_segments=divisions[1], size=1)
  elif kind == "cube":
    bmesh.ops.create_cube(meshEditor, size=1)
  elif kind == "cylinder":
    bmesh.ops.create_cone(meshEditor, cap_ends=True, segments=segments, radius1=1, radius2=1, depth=1)
  elif kind == "cone":
    bmesh.ops.create_cone(meshEditor, cap_ends=True, segments=segments, radius1=1, radius2=0, depth=1)
  else:
    bmesh.ops.create_uvsphere(meshEditor, u_segments=segments, v_segments=max(3, segments // 2), radius=1)
  fitToSize(meshEditor, size, flat)
  newObject = linkNewMesh(name, meshEditor, location, rotationDegrees, collection)
  return describeTransform(newObject) | bridgeMeshAccess.meshCounts(newObject)


def createTerrainGrid(name, size, spacing, location, collection):
  requireNewName(name)
  if spacing <= 0:
    raise ValueError(f"spacing must be positive, got {spacing}")
  divisions = [size[0] / spacing, size[1] / spacing]
  if any(abs(count - round(count)) > 1e-6 or round(count) < 1 for count in divisions):
    raise ValueError(f"size {size[0]:g} x {size[1]:g} is not a whole number of {spacing:g}-unit cells")
  meshEditor = bmesh.new()
  bmesh.ops.create_grid(meshEditor, x_segments=round(divisions[0]), y_segments=round(divisions[1]), size=1)
  fitToSize(meshEditor, [size[0], size[1], 0], flat=True)
  newObject = linkNewMesh(name, meshEditor, location, [0, 0, 0], collection)
  return describeTransform(newObject) | bridgeMeshAccess.meshCounts(newObject) | {"spacing": spacing}


def transformObjects(names, translate, rotateDegrees, scale, location, rotationDegrees):
  if translate is not None and location is not None:
    raise ValueError("Pass translate or location, not both")
  if rotateDegrees is not None and rotationDegrees is not None:
    raise ValueError("Pass rotateDegrees or rotationDegrees, not both")
  if all(value is None for value in (translate, rotateDegrees, scale, location, rotationDegrees)):
    raise ValueError("Nothing to change: pass at least one of translate, rotateDegrees, scale, location, rotationDegrees")
  sceneObjects = [bridgeMeshAccess.requireObject(name) for name in names]
  for sceneObject in sceneObjects:
    if location is not None:
      sceneObject.location = location
    if translate is not None:
      sceneObject.location = sceneObject.location + mathutils.Vector(translate)
    if rotationDegrees is not None:
      sceneObject.rotation_euler = [math.radians(angle) for angle in rotationDegrees]
    if rotateDegrees is not None:
      if sceneObject.rotation_mode not in eulerModes:
        raise ValueError(f"'{sceneObject.name}' rotates by {sceneObject.rotation_mode}; rotateDegrees needs an Euler rotation mode")
      worldRotation = mathutils.Euler([math.radians(angle) for angle in rotateDegrees]).to_matrix()
      sceneObject.rotation_euler = (worldRotation @ sceneObject.rotation_euler.to_matrix()).to_euler(sceneObject.rotation_mode)
    if scale is not None:
      sceneObject.scale = [current * factor for current, factor in zip(sceneObject.scale, scale)]
  bpy.context.view_layer.update()
  return {"objects": [describeTransform(sceneObject) for sceneObject in sceneObjects]}


def duplicateObjects(names, offset, linkData):
  duplicates = {}
  for name in names:
    source = bridgeMeshAccess.requireObject(name)
    duplicate = source.copy()
    if source.data is not None and not linkData:
      duplicate.data = source.data.copy()
    duplicate.location = source.location + mathutils.Vector(offset)
    for collection in source.users_collection:
      collection.objects.link(duplicate)
    duplicates[name] = duplicate.name
  bpy.context.view_layer.update()
  return duplicates


def joinObjects(names, into):
  """Merge meshes into one object; the `into` object keeps its name, origin, and transform, and the others are removed."""
  if into not in names or len(set(names)) < 2:
    raise ValueError(f"joinObjects needs at least two distinct meshes including '{into}', got {names}")
  sceneObjects = [bridgeMeshAccess.requireMeshObject(name) for name in names]
  target = bridgeMeshAccess.requireMeshObject(into)
  if any(len(sceneObject.modifiers) for sceneObject in sceneObjects):
    raise ValueError("Apply or remove modifiers before joining; join merges the base meshes")
  for sceneObject in sceneObjects:
    bridgeMeshAccess.requireNoShapingPasses(sceneObject, "join it")
  for sceneObject in sceneObjects:
    if sceneObject.data.users > 1:
      sceneObject.data = sceneObject.data.copy()
  with bpy.context.temp_override(active_object=target, object=target, selected_objects=sceneObjects, selected_editable_objects=sceneObjects):
    result = bpy.ops.object.join()
  if result != {"FINISHED"}:
    raise RuntimeError(f"join returned {result}")
  bpy.context.view_layer.update()
  return describeTransform(target) | bridgeMeshAccess.meshCounts(target) | {"materials": [slot.material.name if slot.material else None for slot in target.material_slots]}


def deleteObjects(names):
  sceneObjects = [bridgeMeshAccess.requireObject(name) for name in names]
  removedData = []
  for sceneObject in sceneObjects:
    data = sceneObject.data
    bpy.data.objects.remove(sceneObject)
    if isinstance(data, bpy.types.Mesh) and data.users == 0:
      removedData.append(data.name)
      bpy.data.meshes.remove(data)
  return {"deleted": names, "removedMeshes": removedData}


def organize(renames, parents, collections):
  if renames is None and parents is None and collections is None:
    raise ValueError("Nothing to organize: pass renames, parents, or collections")
  for oldName, newName in (renames or {}).items():
    sceneObject = bridgeMeshAccess.requireObject(oldName)
    requireNewName(newName)
    sceneObject.name = newName
  bpy.context.view_layer.update()
  for childName, parentName in (parents or {}).items():
    child = bridgeMeshAccess.requireObject(childName)
    worldMatrix = child.matrix_world.copy()
    child.parent = bridgeMeshAccess.requireObject(parentName) if parentName is not None else None
    child.matrix_world = worldMatrix
  for objectName, collectionName in (collections or {}).items():
    sceneObject = bridgeMeshAccess.requireObject(objectName)
    destination = targetCollection(collectionName)
    for collection in list(sceneObject.users_collection):
      collection.objects.unlink(sceneObject)
    destination.objects.link(sceneObject)
  bpy.context.view_layer.update()
  affected = set((renames or {}).values()) | set(parents or {}) | set(collections or {})
  return {"objects": [describeTransform(bridgeMeshAccess.requireObject(name)) | {
    "parent": bridgeMeshAccess.requireObject(name).parent.name if bridgeMeshAccess.requireObject(name).parent else None,
    "collections": [collection.name for collection in bridgeMeshAccess.requireObject(name).users_collection],
  } for name in sorted(affected)]}


def uvDensity(sceneObject):
  """World units per texture repeat: the square root of the mesh's world area over its UV area."""
  mesh = sceneObject.data
  if not mesh.uv_layers:
    return None
  mesh.calc_loop_triangles()
  triangleLoops = numpy.empty(len(mesh.loop_triangles) * 3, dtype=numpy.int64)
  mesh.loop_triangles.foreach_get("loops", triangleLoops)
  triangleLoops = triangleLoops.reshape(-1, 3)
  loopVertices = numpy.empty(len(mesh.loops), dtype=numpy.int64)
  mesh.loops.foreach_get("vertex_index", loopVertices)
  worldPositions, _ = bridgeMeshAccess.readVertexArrays(sceneObject)
  uvs = numpy.empty(len(mesh.loops) * 2)
  mesh.uv_layers.active.data.foreach_get("uv", uvs)
  uvs = uvs.reshape(-1, 2)
  corners = worldPositions[loopVertices[triangleLoops]]
  worldArea = numpy.linalg.norm(numpy.cross(corners[:, 1] - corners[:, 0], corners[:, 2] - corners[:, 0]), axis=1).sum() / 2
  uvCorners = uvs[triangleLoops]
  uvEdgeA, uvEdgeB = uvCorners[:, 1] - uvCorners[:, 0], uvCorners[:, 2] - uvCorners[:, 0]
  uvArea = numpy.abs(uvEdgeA[:, 0] * uvEdgeB[:, 1] - uvEdgeA[:, 1] * uvEdgeB[:, 0]).sum() / 2
  return round(math.sqrt(worldArea / uvArea), 3) if uvArea > 0 else None


def getObjectDetail(name):
  sceneObject = bridgeMeshAccess.requireObject(name)
  corners = [sceneObject.matrix_world @ mathutils.Vector(corner) for corner in sceneObject.bound_box]
  detail = describeTransform(sceneObject) | {
    "type": sceneObject.type,
    "parent": sceneObject.parent.name if sceneObject.parent else None,
    "collections": [collection.name for collection in sceneObject.users_collection],
    "worldMinimum": roundVector([min(corner[axis] for corner in corners) for axis in range(3)]),
    "worldMaximum": roundVector([max(corner[axis] for corner in corners) for axis in range(3)]),
    "modifiers": [{"name": modifier.name, "type": modifier.type} for modifier in sceneObject.modifiers],
  }
  if sceneObject.type == "MESH":
    mesh = sceneObject.data
    faceMaterials = numpy.empty(len(mesh.polygons), dtype=numpy.int32)
    mesh.polygons.foreach_get("material_index", faceMaterials)
    detail |= bridgeMeshAccess.meshCounts(sceneObject) | {
      "edges": len(mesh.edges),
      "materials": [{"material": slot.material.name if slot.material else None, "faces": int((faceMaterials == index).sum())} for index, slot in enumerate(sceneObject.material_slots)],
      "uvLayers": [layer.name for layer in mesh.uv_layers],
      "worldUnitsPerTextureRepeat": uvDensity(sceneObject),
      "vertexGroups": [group.name for group in sceneObject.vertex_groups],
      "sharedMeshUsers": mesh.users,
      "shapingPasses": bridgePasses.passList(sceneObject),
    }
  if sceneObject.instance_type == "COLLECTION" and sceneObject.instance_collection is not None:
    detail["instanceCollection"] = sceneObject.instance_collection.name
  return detail


def measure(points, snapToSurface):
  if not points:
    raise ValueError("measure needs at least one point")
  measured = []
  for point in points:
    if snapToSurface:
      hit = bridgeMeshAccess.rayCast(point, (0, 0, -1), measureCastDistance)
      if hit is None:
        raise ValueError(f"No surface below {point}")
      measured.append(hit[0])
    else:
      measured.append(mathutils.Vector(point))
  segments = []
  for start, end in zip(measured, measured[1:]):
    horizontal = math.hypot(end.x - start.x, end.y - start.y)
    rise = end.z - start.z
    segments.append({
      "distance": round((end - start).length, 3),
      "horizontalDistance": round(horizontal, 3),
      "heightChange": round(rise, 3),
      "slopeDegrees": round(math.degrees(math.atan2(abs(rise), horizontal)), 2),
    })
  return {"points": [roundVector(point) for point in measured], "segments": segments, "totalDistance": round(sum(segment["distance"] for segment in segments), 3)}


commands = {
  "createPrimitive": (createPrimitive, True),
  "createTerrainGrid": (createTerrainGrid, True),
  "transformObjects": (transformObjects, True),
  "duplicateObjects": (duplicateObjects, True),
  "joinObjects": (joinObjects, True),
  "deleteObjects": (deleteObjects, True),
  "organize": (organize, True),
  "getObjectDetail": (getObjectDetail, False),
  "measure": (measure, False),
}
