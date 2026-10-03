"""Blocking out, organizing, and inspecting objects. Runs under Blender's Python."""
import json
import math

import bmesh
import bpy
import mathutils
import numpy

import bridgeExport
import bridgeMeshAccess
import bridgePasses

roundShapes = ("cylinder", "cone", "sphere")
eulerModes = ("XYZ", "XZY", "YXZ", "YZX", "ZXY", "ZYX")
measureCastDistance = 100000.0
primitiveKinds = ("plane", "grid", "cube") + roundShapes
splineSamplesPerSegment = 64
sectionSamples = 720


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


def catmullRom(points):
  """Dense samples of a centripetal Catmull-Rom curve through the points, with each sample's fractional index along them."""
  controls = numpy.vstack([2 * points[0] - points[1], points, 2 * points[-1] - points[-2]])
  samples, indices = [], []
  for segment in range(len(points) - 1):
    p0, p1, p2, p3 = controls[segment:segment + 4]
    t1 = numpy.linalg.norm(p1 - p0) ** 0.5
    t2 = t1 + numpy.linalg.norm(p2 - p1) ** 0.5
    t3 = t2 + numpy.linalg.norm(p3 - p2) ** 0.5
    steps = numpy.arange(splineSamplesPerSegment) / splineSamplesPerSegment
    t = (t1 + (t2 - t1) * steps)[:, None]
    a1 = ((t1 - t) * p0 + t * p1) / t1
    a2 = ((t2 - t) * p1 + (t - t1) * p2) / (t2 - t1)
    a3 = ((t3 - t) * p2 + (t - t2) * p3) / (t3 - t2)
    b1 = ((t2 - t) * a1 + t * a2) / t2
    b2 = ((t3 - t) * a2 + (t - t1) * a3) / (t3 - t1)
    samples.append(((t2 - t) * b1 + (t - t1) * b2) / (t2 - t1))
    indices.append(segment + steps)
  return numpy.vstack(samples + [points[-1:]]), numpy.concatenate(indices + [[len(points) - 1]])


def monotoneCurve(values, fractionalIndices):
  """Values given at each control point, between them a smooth curve that never overshoots them (cubic Hermite with Fritsch-Butland
  slopes), so a thickness that narrows to a span and widens again never dips below its narrowest."""
  values = numpy.asarray(values, dtype=numpy.float64)
  slopes = numpy.diff(values)
  tangents = numpy.empty(len(values))
  tangents[0], tangents[-1] = slopes[0], slopes[-1]
  left, right = slopes[:-1], slopes[1:]
  sameSign = left * right > 0
  tangents[1:-1] = 0.0
  tangents[1:-1][sameSign] = 2 * left[sameSign] * right[sameSign] / (left[sameSign] + right[sameSign])
  segments = numpy.minimum(numpy.floor(fractionalIndices).astype(numpy.int64), len(values) - 2)
  s = fractionalIndices - segments
  return ((2 * s ** 3 - 3 * s ** 2 + 1) * values[segments] + (s ** 3 - 2 * s ** 2 + s) * tangents[segments]
          + (-2 * s ** 3 + 3 * s ** 2) * values[segments + 1] + (s ** 3 - s ** 2) * tangents[segments + 1])


def superellipseOutline(squareness):
  """A unit superellipse from its top, clockwise seen along the path, densely sampled: lateral and vertical, each -1 to 1."""
  angles = numpy.pi / 2 - numpy.linspace(0, 2 * numpy.pi, sectionSamples, endpoint=False)
  cosines, sines = numpy.cos(angles), numpy.sin(angles)
  return numpy.column_stack([numpy.sign(cosines) * numpy.abs(cosines) ** (2 / squareness), numpy.sign(sines) * numpy.abs(sines) ** (2 / squareness)])


def evenlyAround(outline, count):
  """count points evenly spaced along a closed outline, the first at its first point."""
  closed = numpy.vstack([outline, outline[:1]])
  lengths = numpy.concatenate([[0.0], numpy.cumsum(numpy.linalg.norm(numpy.diff(closed, axis=0), axis=1))])
  targets = numpy.linspace(0, lengths[-1], count, endpoint=False)
  return numpy.column_stack([numpy.interp(targets, lengths, closed[:, axis]) for axis in range(2)])


def createRockMass(name, path, widths, thicknesses, spacing, squareness, collection):
  requireNewName(name)
  pathArray = bridgeMeshAccess.toArray(path)
  if pathArray.ndim != 2 or pathArray.shape[1] != 3 or len(pathArray) < 2:
    raise ValueError(f"A path is at least two [x, y, z] points, got {path!r}")
  if (numpy.linalg.norm(numpy.diff(pathArray, axis=0), axis=1) < 1e-6).any():
    raise ValueError("The path repeats a point; each point must differ from the one before it")
  for label, values in (("widths", widths), ("thicknesses", thicknesses)):
    if len(values) != len(pathArray) or min(values) <= 0:
      raise ValueError(f"{label} are one positive value per path point ({len(pathArray)}), got {values!r}")
  if spacing <= 0 or squareness < 1:
    raise ValueError(f"spacing must be positive and squareness at least 1, got {spacing} and {squareness}")
  samples, fractionalIndices = catmullRom(pathArray)
  arcLengths = numpy.concatenate([[0.0], numpy.cumsum(numpy.linalg.norm(numpy.diff(samples, axis=0), axis=1))])
  ringCount = max(2, math.ceil(arcLengths[-1] / spacing) + 1)
  ringArcs = numpy.linspace(0, arcLengths[-1], ringCount)
  centers = numpy.column_stack([numpy.interp(ringArcs, arcLengths, samples[:, axis]) for axis in range(3)])
  tangents = numpy.gradient(samples, axis=0)
  ringTangents = numpy.column_stack([numpy.interp(ringArcs, arcLengths, tangents[:, axis]) for axis in range(2)])
  horizontal = numpy.linalg.norm(ringTangents, axis=1)
  if (horizontal < 1e-9).any():
    raise ValueError("The path runs straight up or down somewhere; a rock mass follows a path that travels across")
  laterals = numpy.column_stack([-ringTangents[:, 1], ringTangents[:, 0], numpy.zeros(ringCount)]) / horizontal[:, None]
  ringIndices = numpy.interp(ringArcs, arcLengths, fractionalIndices)
  halfWidths = monotoneCurve(widths, ringIndices) / 2
  halfThicknesses = monotoneCurve(thicknesses, ringIndices) / 2
  outline = superellipseOutline(squareness)
  perimeters = [numpy.linalg.norm(numpy.diff(numpy.vstack([outline, outline[:1]]) * [a, b], axis=0), axis=1).sum() for a, b in zip(halfWidths, halfThicknesses)]
  sectionCount = max(8, 4 * math.ceil(max(perimeters) / spacing / 4))
  meshEditor = bmesh.new()
  rings = []
  for center, lateral, a, b in zip(centers, laterals, halfWidths, halfThicknesses):
    section = evenlyAround(outline * [a, b], sectionCount)
    points = center + section[:, :1] * lateral + (section[:, 1:] - b) * numpy.array([0.0, 0.0, 1.0])
    rings.append([meshEditor.verts.new(point) for point in points])
  for ring, nextRing in zip(rings, rings[1:]):
    for index in range(sectionCount):
      following = (index + 1) % sectionCount
      meshEditor.faces.new((ring[index], ring[following], nextRing[following], nextRing[index]))
  for ring in (rings[0], rings[-1]):
    middle = meshEditor.verts.new(numpy.mean([vertex.co for vertex in ring], axis=0))
    for index in range(sectionCount):
      meshEditor.faces.new((middle, ring[index], ring[(index + 1) % sectionCount]))
  bmesh.ops.recalc_face_normals(meshEditor, faces=list(meshEditor.faces))
  newObject = linkNewMesh(name, meshEditor, [0, 0, 0], [0, 0, 0], bridgeExport.terrainCollectionName if collection is None else collection)
  return describeTransform(newObject) | bridgeMeshAccess.meshCounts(newObject) | {"length": round(float(arcLengths[-1]), 1), "rings": ringCount, "sectionPoints": sectionCount}


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
    "surfaceLayers": json.loads(sceneObject[bridgeMeshAccess.surfaceLayersProperty]) if bridgeMeshAccess.surfaceLayersProperty in sceneObject else [],
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
  "createRockMass": (createRockMass, True),
  "transformObjects": (transformObjects, True),
  "duplicateObjects": (duplicateObjects, True),
  "joinObjects": (joinObjects, True),
  "deleteObjects": (deleteObjects, True),
  "organize": (organize, True),
  "getObjectDetail": (getObjectDetail, False),
  "measure": (measure, False),
}
