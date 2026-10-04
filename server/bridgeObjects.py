"""Blocking out, organizing, and inspecting objects. Runs under Blender's Python."""
import json
import math

import bmesh
import bpy
import mathutils
import mathutils.bvhtree
import mathutils.geometry
import numpy

import bridgeExport
import bridgeMeshAccess
import bridgePasses
import bridgeShaping

roundShapes = ("cylinder", "cone", "sphere")
eulerModes = ("XYZ", "XZY", "YXZ", "YZX", "ZXY", "ZYX")
measureCastDistance = 100000.0
groundTuck = 2.0
minimumRockThickness = 1.0
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


def objectDimensions(sceneObject):
  """The object's scaled size along its own axes: its mesh's, a collection instance's meshes' together, or None when it instances none."""
  if not bridgeMeshAccess.isCollectionInstance(sceneObject):
    return roundVector(sceneObject.dimensions)
  inverse = sceneObject.matrix_world.inverted()
  corners = numpy.array([list(inverse @ corner) for corner in bridgeMeshAccess.worldBoundsCorners(sceneObject, bpy.context.evaluated_depsgraph_get())])
  if len(corners) == 0:
    return None
  return roundVector((corners.max(0) - corners.min(0)) * numpy.abs(numpy.array(sceneObject.scale)))


def rotationDegrees(sceneObject):
  """The object's rotation as XYZ Euler angles in degrees, whatever its rotation mode."""
  rotation = sceneObject.rotation_euler if sceneObject.rotation_mode == "XYZ" else sceneObject.matrix_basis.decompose()[1].to_euler("XYZ")
  return roundVector([math.degrees(angle) for angle in rotation], 2)


def describeTransform(sceneObject):
  return {
    "name": sceneObject.name,
    "location": roundVector(sceneObject.location),
    "rotationDegrees": rotationDegrees(sceneObject),
    "scale": roundVector(sceneObject.scale),
    "dimensions": objectDimensions(sceneObject),
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


def requireProfile(label, profile, meaning):
  profileArray = bridgeMeshAccess.toArray(profile)
  if profileArray.ndim != 2 or profileArray.shape[1] != 2 or len(profileArray) < 2 or (numpy.diff(profileArray[:, 0]) <= 0).any():
    raise ValueError(f"{label} is [[{meaning}], ...]: at least two points with distances rising, got {profile!r}")
  return profileArray


def smoothProfile(distances, profileArray):
  """Heights along a profile at the given distances: a smooth curve through its points that never overshoots them (piecewise cubic
  Hermite with weighted harmonic-mean slopes), holding its end heights beyond its ends."""
  knots, heights = profileArray[:, 0], profileArray[:, 1]
  widths = numpy.diff(knots)
  secants = numpy.diff(heights) / widths
  slopes = numpy.empty(len(knots))
  slopes[0], slopes[-1] = secants[0], secants[-1]
  if len(knots) > 2:
    left, right = secants[:-1], secants[1:]
    leftWeight, rightWeight = 2 * widths[1:] + widths[:-1], widths[1:] + 2 * widths[:-1]
    sameSign = left * right > 0
    slopes[1:-1] = 0.0
    slopes[1:-1][sameSign] = (leftWeight + rightWeight)[sameSign] / (leftWeight[sameSign] / left[sameSign] + rightWeight[sameSign] / right[sameSign])
  clamped = numpy.clip(distances, knots[0], knots[-1])
  segments = numpy.clip(numpy.searchsorted(knots, clamped, side="right") - 1, 0, len(knots) - 2)
  width = widths[segments]
  s = (clamped - knots[segments]) / width
  return ((2 * s ** 3 - 3 * s ** 2 + 1) * heights[segments] + (s ** 3 - 2 * s ** 2 + s) * width * slopes[segments]
          + (-2 * s ** 3 + 3 * s ** 2) * heights[segments + 1] + (s ** 3 - s ** 2) * width * slopes[segments + 1])


def requireSimpleOutline(outline):
  """The outline as an array running counterclockwise, refused when it repeats a point or crosses itself."""
  outlineArray = bridgeMeshAccess.toArray(outline)
  if outlineArray.ndim != 2 or outlineArray.shape[1] != 2 or len(outlineArray) < 3:
    raise ValueError(f"An outline is at least three [x, y] points, got {outline!r}")
  ends = numpy.roll(outlineArray, -1, axis=0)
  if (numpy.linalg.norm(ends - outlineArray, axis=1) < 1e-6).any():
    raise ValueError("The outline repeats a point; each point must differ from the one before it")
  count = len(outlineArray)
  for first in range(count):
    for second in range(first + 2, count - (first == 0)):
      if mathutils.geometry.intersect_line_line_2d(outlineArray[first], ends[first], outlineArray[second], ends[second]) is not None:
        raise ValueError(f"The outline crosses itself: its sides from point {first} and from point {second}")
  area = (outlineArray[:, 0] * ends[:, 1] - ends[:, 0] * outlineArray[:, 1]).sum() / 2
  return outlineArray if area > 0 else outlineArray[::-1].copy()


def resampledOutline(outlineArray, spacing):
  """Points along the outline at most `spacing` apart, its own points among them so its corners stay sharp."""
  points = []
  for start, end in zip(outlineArray, numpy.roll(outlineArray, -1, axis=0)):
    pieces = max(1, math.ceil(numpy.linalg.norm(end - start) / spacing - 1e-9))
    points.extend(start + (end - start) * (numpy.arange(pieces) / pieces)[:, None])
  return numpy.array(points)


def surfaceHeightsBelow(sceneObject, plan):
  """The height of the uppermost surface of an object at each [x, y] point, or -inf where there is none."""
  depsgraph = bpy.context.evaluated_depsgraph_get()
  tree = mathutils.bvhtree.BVHTree.FromObject(sceneObject, depsgraph)
  inverse = sceneObject.matrix_world.inverted()
  down = (inverse.to_3x3() @ mathutils.Vector((0.0, 0.0, -1.0))).normalized()
  castFrom = max((sceneObject.matrix_world @ mathutils.Vector(corner)).z for corner in sceneObject.bound_box) + 1.0
  heights = numpy.full(len(plan), -numpy.inf)
  for index, (x, y) in enumerate(plan):
    location, _, _, _ = tree.ray_cast(inverse @ mathutils.Vector((x, y, castFrom)), down)
    if location is not None:
      heights[index] = (sceneObject.matrix_world @ location).z
  return heights


def stitchColumns(left, right, heights):
  """Triangles joining two vertical columns of vertex indices, each listed top to bottom, taking whichever next vertex stands higher."""
  triangles, first, second = [], 0, 0
  while first < len(left) - 1 or second < len(right) - 1:
    if second == len(right) - 1 or (first < len(left) - 1 and heights[left[first + 1]] >= heights[right[second + 1]]):
      triangles.append((left[first], left[first + 1], right[second]))
      first += 1
    else:
      triangles.append((left[first], right[second + 1], right[second]))
      second += 1
  return triangles


def createRockFromOutline(name, outline, top, underside, flare, spacing, ground, collection):
  requireNewName(name)
  if spacing <= 0:
    raise ValueError(f"spacing must be positive, got {spacing}")
  outlineArray = requireSimpleOutline(outline)
  if not isinstance(underside, dict) or set(underside) != {"axis", "profile"}:
    raise ValueError(f"underside is {{\"axis\": [[x, y], [x, y]], \"profile\": [[distance, height], ...]}}, got {underside!r}")
  axis = bridgeMeshAccess.toArray(underside["axis"])
  if axis.shape != (2, 2) or numpy.linalg.norm(axis[1] - axis[0]) < 1e-6:
    raise ValueError(f"underside axis is two different [x, y] points, got {underside['axis']!r}")
  undersideProfile = requireProfile("underside profile", underside["profile"], "distance along the axis, height")
  flareProfile = None if flare is None else requireProfile("flare", flare, "distance inside a face, rise")
  groundObject = None if ground is None else bridgeMeshAccess.requireMeshObject(ground)

  boundary = resampledOutline(outlineArray, spacing)
  low, high = outlineArray.min(0), outlineArray.max(0)
  xs = numpy.arange(math.floor(low[0] / spacing), math.ceil(high[0] / spacing) + 1) * spacing
  ys = numpy.arange(math.floor(low[1] / spacing), math.ceil(high[1] / spacing) + 1) * spacing
  grid = numpy.stack(numpy.meshgrid(xs, ys), -1).reshape(-1, 2)
  depthInside, _ = bridgeShaping.signedDistanceToOutline(grid, outlineArray)
  interior = grid[depthInside > spacing / 2]
  plan = numpy.vstack([boundary, interior])
  coordinates, _, triangles, originals, _, _ = mathutils.geometry.delaunay_2d_cdt(
    [mathutils.Vector(point) for point in plan], [], [list(range(len(boundary)))], 1, 1e-4, True)
  if any(len(sources) != 1 for sources in originals):
    raise ValueError(f"Points of the outline lie too close together to mesh at spacing {spacing:g}")
  outputIndex = numpy.empty(len(plan), dtype=numpy.int64)
  outputIndex[[sources[0] for sources in originals]] = numpy.arange(len(originals))
  plan = numpy.array([[point.x, point.y] for point in coordinates])
  rim = outputIndex[:len(boundary)]

  axisDirection = (axis[1] - axis[0]) / numpy.linalg.norm(axis[1] - axis[0])
  bottoms = smoothProfile((plan - axis[0]) @ axisDirection, undersideProfile)
  if flareProfile is not None:
    depth, _ = bridgeShaping.signedDistanceToOutline(plan, outlineArray)
    bottoms = bottoms + smoothProfile(numpy.maximum(depth, 0.0), flareProfile)
  tops = numpy.full(len(plan), float(top))
  tucked = numpy.zeros(len(plan), dtype=bool)
  if groundObject is not None:
    groundHeights = surfaceHeightsBelow(groundObject, plan)
    tucked = groundHeights >= top - groundTuck
    tops[tucked] = groundHeights[tucked] - groundTuck
  thickness = tops - bottoms
  thinnest = int(thickness.argmin())
  if thickness[thinnest] < minimumRockThickness:
    raise ValueError(f"The underside comes within {minimumRockThickness:g} of the top at {roundVector(plan[thinnest], 1)}: {thickness[thinnest]:.1f} thick")

  vertices = [(x, y, z) for (x, y), z in zip(plan, tops)] + [(x, y, z) for (x, y), z in zip(plan, bottoms)]
  heights = list(tops) + list(bottoms)
  faces = [tuple(triangle) for triangle in triangles] + [tuple(len(plan) + index for index in reversed(triangle)) for triangle in triangles]
  columns = []
  for index in rim:
    rows = numpy.arange(math.ceil(bottoms[index] / spacing), math.floor(tops[index] / spacing) + 1)[::-1] * spacing
    rows = rows[(rows < tops[index] - spacing / 4) & (rows > bottoms[index] + spacing / 4)]
    column = [int(index)]
    for row in rows:
      column.append(len(vertices))
      vertices.append((plan[index, 0], plan[index, 1], row))
      heights.append(row)
    columns.append(column + [len(plan) + int(index)])
  for column, following in zip(columns, columns[1:] + columns[:1]):
    faces.extend(stitchColumns(following, column, heights))

  meshEditor = bmesh.new()
  meshVertices = [meshEditor.verts.new(vertex) for vertex in vertices]
  for face in faces:
    meshEditor.faces.new([meshVertices[index] for index in face])
  bmesh.ops.recalc_face_normals(meshEditor, faces=list(meshEditor.faces))
  newObject = linkNewMesh(name, meshEditor, [0, 0, 0], [0, 0, 0], bridgeExport.terrainCollectionName if collection is None else collection)
  return describeTransform(newObject) | bridgeMeshAccess.meshCounts(newObject) | {
    "thinnest": {"thickness": round(float(thickness[thinnest]), 2), "at": roundVector(plan[thinnest], 1)},
    "tuckedVertices": int(tucked.sum()),
  }


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
  """Copy objects with everything parented under them, once each: an object named under another named one comes with that one."""
  sources = [bridgeMeshAccess.requireObject(name) for name in names]
  roots = [source for source in sources if not any(ancestor in sources for ancestor in ancestorsOf(source))]
  copies = {}
  for root in roots:
    carried = [root] + list(root.children_recursive)
    for source in carried:
      duplicate = source.copy()
      if source.data is not None and not linkData:
        duplicate.data = source.data.copy()
      for collection in source.users_collection:
        collection.objects.link(duplicate)
      copies[source] = duplicate
    for source in carried[1:]:
      copies[source].parent = copies[source.parent]
    copies[root].matrix_world = mathutils.Matrix.Translation(offset) @ root.matrix_world
  bpy.context.view_layer.update()
  if not linkData:
    for duplicate in copies.values():
      nameOwnMesh(duplicate)
  return {source.name: duplicate.name for source, duplicate in copies.items()}


def nameOwnMesh(sceneObject):
  """Name a mesh only this object uses after the object, as zone export names its model; a mesh linked copies share keeps its own."""
  if isinstance(sceneObject.data, bpy.types.Mesh) and sceneObject.data.users == 1:
    sceneObject.data.name = sceneObject.name


def ancestorsOf(sceneObject):
  ancestors = []
  while sceneObject.parent is not None:
    sceneObject = sceneObject.parent
    ancestors.append(sceneObject)
  return ancestors


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
  nameOwnMesh(target)
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
    nameOwnMesh(sceneObject)
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
  """World units per texture repeat over the whole mesh."""
  areas = bridgeMeshAccess.textureAreas(sceneObject)
  if areas is None:
    return None
  density = bridgeMeshAccess.worldUnitsPerRepeat(areas.world.sum(), areas.uv.sum())
  return None if density is None else round(density, 3)


def getObjectDetail(name):
  sceneObject = bridgeMeshAccess.requireObject(name)
  if sceneObject.type == "MESH" or bridgeMeshAccess.isCollectionInstance(sceneObject):
    corners = bridgeMeshAccess.worldBoundsCorners(sceneObject, bpy.context.evaluated_depsgraph_get())
  else:
    corners = [sceneObject.matrix_world @ mathutils.Vector(corner) for corner in sceneObject.bound_box]
  detail = describeTransform(sceneObject) | {
    "type": sceneObject.type,
    "parent": sceneObject.parent.name if sceneObject.parent else None,
    "collections": [collection.name for collection in sceneObject.users_collection],
    "worldMinimum": roundVector([min(corner[axis] for corner in corners) for axis in range(3)]) if corners else None,
    "worldMaximum": roundVector([max(corner[axis] for corner in corners) for axis in range(3)]) if corners else None,
    "modifiers": [{"name": modifier.name, "type": modifier.type} for modifier in sceneObject.modifiers],
  }
  if sceneObject.type == "MESH":
    mesh = sceneObject.data
    faceMaterials = numpy.empty(len(mesh.polygons), dtype=numpy.int32)
    mesh.polygons.foreach_get("material_index", faceMaterials)
    detail |= bridgeMeshAccess.meshCounts(sceneObject) | {
      "mesh": mesh.name,
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
  surfaces = bridgeMeshAccess.PlayerSurfaces() if snapToSurface else None
  for point in points:
    if snapToSurface:
      hit = surfaces.footingBelow(mathutils.Vector(point), measureCastDistance)
      if hit is None:
        raise ValueError(f"No surface players stand on below {point}")
      measured.append(hit)
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
  "createRockFromOutline": (createRockFromOutline, True),
  "transformObjects": (transformObjects, True),
  "duplicateObjects": (duplicateObjects, True),
  "joinObjects": (joinObjects, True),
  "deleteObjects": (deleteObjects, True),
  "organize": (organize, True),
  "getObjectDetail": (getObjectDetail, False),
  "measure": (measure, False),
}
