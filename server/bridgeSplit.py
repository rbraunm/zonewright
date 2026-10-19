"""Splitting a mesh into parts the client draws as models, and joining them back. Runs under Blender's Python."""
import json
import math

import bmesh
import bpy
import mathutils
import mathutils.kdtree
import numpy

import bridgeCaveData
import bridgeMeshAccess
from buildTolerances import maxModelTriangles

# A cut's seam: the vertices on it carry, in this attribute, the number of the part they went to; joining welds the seam's vertices of
# different parts that lie together, and no other.
seamAttribute = "zonewrightSeam"
seamTolerance = 1e-5
# Each face of a mesh being split carries the number of the connected piece it came from in this attribute while it is cut.
pieceAttribute = "zonewrightPiece"
# A cut goes through the nearest row of vertices when one lies within this share of a face's width of it, so it leaves no sliver of a
# face beside a vertex.
snapShare = 0.25
# Vertices within this share of a face's width of a cut lie on it.
rowShare = 1e-3
# A piece too big is cut into this many slabs more than its size asks for, at most, before the split is refused.
extraSlabs = 4


def faceTriangles(face):
  return len(face.verts) - 2


def faceComponents(editor):
  """The mesh's faces in groups of those joined through shared vertices, in the order their first faces come."""
  editor.faces.index_update()
  seen, components = set(), []
  for start in editor.faces:
    if start.index in seen:
      continue
    seen.add(start.index)
    stack, component = [start], []
    while stack:
      face = stack.pop()
      component.append(face)
      for vertex in face.verts:
        for neighbor in vertex.link_faces:
          if neighbor.index not in seen:
            seen.add(neighbor.index)
            stack.append(neighbor)
    components.append(component)
  return components


def slabCuts(component, maxTriangles, count):
  """The axis a connected piece is cut across (its longest) and the `count` - 1 coordinates that divide its triangles evenly, each at
  the gap between the faces on either side where there is one (no face is cut), else through where the faces overlap."""
  positions = numpy.array([vertex.co[:] for face in component for vertex in face.verts])
  axis = int((positions.max(0) - positions.min(0)).argmax())
  extents = numpy.array([[min(vertex.co[axis] for vertex in face.verts), max(vertex.co[axis] for vertex in face.verts)] for face in component])
  centers = extents.mean(axis=1)
  order = centers.argsort()
  lowest, highest = extents[order, 0], extents[order, 1]
  cumulative = numpy.cumsum(numpy.array([faceTriangles(face) for face in component])[order])
  below = numpy.maximum.accumulate(highest)[:-1]
  above = numpy.minimum.accumulate(lowest[::-1])[::-1][1:]
  gaps = numpy.flatnonzero(below <= above + seamTolerance)
  coordinates = numpy.unique(positions[:, axis])
  snapLimit = snapShare * float(numpy.median(extents[:, 1] - extents[:, 0]))
  cuts = []
  for step in range(1, count):
    target = cumulative[-1] * step / count
    index = int(gaps[numpy.abs(cumulative[gaps] - target).argmin()]) if len(gaps) else min(int(numpy.searchsorted(cumulative, target)), len(centers) - 2)
    cut = float((below[index] + above[index]) / 2)
    nearest = float(coordinates[numpy.abs(coordinates - cut).argmin()])
    cuts.append(nearest if abs(nearest - cut) <= snapLimit else cut)
  return axis, sorted(set(cuts))


def planeTolerance(cut, faces, axis):
  """How far from a cut a vertex still lies on it: more than seamTolerance where the numbers are far from the origin (a vertex is
  stored in single precision, so one a cut put on the plane is as far off it as their spacing there), and more where the faces are
  wide, so a row of vertices that a turn or an export left a hair apart is one row to cut along, not a sliver of every face beside it."""
  widths = [max(vertex.co[axis] for vertex in face.verts) - min(vertex.co[axis] for vertex in face.verts) for face in faces]
  return max(seamTolerance, 8 * float(numpy.spacing(numpy.float32(max(abs(cut), 1.0)))), rowShare * float(numpy.median(widths)))


def cutPiece(editor, tag, axis, cuts):
  """Cut the faces tagged `tag` across `axis` at each of `cuts`; the faces cut that way and the vertices lying on the cuts."""
  layer = editor.faces.layers.int[pieceAttribute]
  normal = mathutils.Vector([1.0 if index == axis else 0.0 for index in range(3)])
  faces = [face for face in editor.faces if face[layer] == tag]
  geometry = faces + list({edge for face in faces for edge in face.edges}) + list({vertex for face in faces for vertex in face.verts})
  tolerance = max(planeTolerance(cut, faces, axis) for cut in cuts)
  for cut in cuts:
    geometry = bmesh.ops.bisect_plane(editor, geom=geometry, dist=tolerance, plane_co=normal * cut, plane_no=normal)["geom"]
  faces = [face for face in editor.faces if face[layer] == tag]
  return faces, {vertex for face in faces for vertex in face.verts if any(abs(vertex.co[axis] - cut) <= tolerance for cut in cuts)}


def slabsOf(faces, axis, cuts):
  """The faces in groups by the slab between the cuts that their middles lie in, lowest first."""
  groups = [[] for _ in range(len(cuts) + 1)]
  for face in faces:
    groups[int(numpy.searchsorted(cuts, face.calc_center_median()[axis]))].append(face)
  return groups


def partsOf(editor, maxTriangles):
  """A mesh's faces in parts of at most maxTriangles triangles each, in order: the mesh's connected pieces, each in a part to itself
  when it fills one and with the pieces next to it when it does not, and a piece of more cut across its longest axis into slabs that
  each fill one. Returns the mesh cut that way (editor, replaced) with the parts' faces by index and the vertices on the cuts."""
  editor.faces.index_update()
  tag = editor.faces.layers.int.new(pieceAttribute)
  components = faceComponents(editor)
  for number, component in enumerate(components, 1):
    for face in component:
      face[tag] = number
  sizes = {number: sum(faceTriangles(face) for face in component) for number, component in enumerate(components, 1)}
  slabCounts = {number: math.ceil(size / maxTriangles) for number, size in sizes.items() if size > maxTriangles}
  plans = {number: slabCuts(components[number - 1], maxTriangles, count) for number, count in slabCounts.items()}
  for attempt in range(extraSlabs + 1):
    cut = editor.copy()
    cutTag = cut.faces.layers.int[pieceAttribute]
    units, seam, tooBig = [], set(), []
    cut.faces.index_update()
    uncut = [[face for face in cut.faces if face[cutTag] == number] for number in sizes if number not in plans]
    for number in sizes:
      if number in plans:
        axis, cuts = plans[number]
        faces, onCuts = cutPiece(cut, number, axis, cuts)
        seam |= onCuts
        for slab in slabsOf(faces, axis, cuts):
          if slab:
            units.append(slab)
            if sum(faceTriangles(face) for face in slab) > maxTriangles:
              tooBig.append(number)
      else:
        units.append(uncut.pop(0))
    if not tooBig:
      break
    cut.free()
    if attempt == extraSlabs:
      raise ValueError(f"A cut piece of the mesh stays over {maxTriangles} triangles in a slab after {extraSlabs} more cuts than its size asks for")
    for number in set(tooBig):
      plans[number] = slabCuts(components[number - 1], maxTriangles, slabCounts[number] + attempt + 1)
  cut.faces.index_update()
  cut.verts.index_update()
  parts, current, filled = [], [], 0
  for unit in units:
    size = sum(faceTriangles(face) for face in unit)
    if current and filled + size > maxTriangles:
      parts.append(current)
      current, filled = [], 0
    current.extend(face.index for face in unit)
    filled += size
  parts.append(current)
  return cut, parts, {vertex.index for vertex in seam}


def partMesh(cut, faces, seam, number, meshName, materials):
  """A new mesh of the faces of `cut` with the given indices, its seam vertices marked with the part's number."""
  editor = cut.copy()
  editor.faces.index_update()
  editor.verts.ensure_lookup_table()
  marks = editor.verts.layers.int.get(seamAttribute) or editor.verts.layers.int.new(seamAttribute)
  for index in seam:
    editor.verts[index][marks] = number
  wanted = set(faces)
  bmesh.ops.delete(editor, geom=[face for face in editor.faces if face.index not in wanted], context="FACES")
  editor.faces.layers.int.remove(editor.faces.layers.int[pieceAttribute])
  mesh = bpy.data.meshes.new(meshName)
  editor.normal_update()
  editor.to_mesh(mesh)
  editor.free()
  for material in materials:
    mesh.materials.append(material)
  return mesh


def splitMesh(sceneObject, maxTriangles):
  """Replace an object by parts of at most maxTriangles triangles each: the first part keeps its name, object, and place, and the
  others are copies of it named <name>.part<number>, in its collections and under its parent. Returns the parts, first first."""
  mesh = sceneObject.data
  editor = bmesh.new()
  editor.from_mesh(mesh)
  cut, parts, seam = partsOf(editor, maxTriangles)
  editor.free()
  materials = list(mesh.materials)
  names = [sceneObject.name] + [f"{sceneObject.name}.part{number}" for number in range(2, len(parts) + 1)]
  meshes = [partMesh(cut, faces, seam, number, name, materials) for number, (faces, name) in enumerate(zip(parts, names), 1)]
  cut.free()
  objects = [sceneObject]
  for name, partData in zip(names[1:], meshes[1:]):
    part = sceneObject.copy()
    part.name = name
    part.data = partData
    for collection in sceneObject.users_collection:
      collection.objects.link(part)
    part.parent = sceneObject.parent
    part.matrix_parent_inverse = sceneObject.matrix_parent_inverse.copy()
    part.matrix_world = sceneObject.matrix_world.copy()
    objects.append(part)
  sceneObject.data = meshes[0]
  bpy.data.meshes.remove(mesh)
  meshes[0].name = sceneObject.name
  for number, part in enumerate(objects, 1):
    part[bridgeMeshAccess.splitProperty] = json.dumps({"whole": sceneObject.name, "index": number, "count": len(objects)})
  return objects


def splitBody(sceneObject):
  """Split a water body's surface into parts when it has more triangles than the client draws in a model: the parts carry its
  definition, so they are water as it is; the parts, or none."""
  if bridgeMeshAccess.triangleCount(sceneObject) <= maxModelTriangles:
    return []
  return splitMesh(sceneObject, maxModelTriangles)


def removeParts(sceneObject):
  """Take the parts split off an object away, and the object's own mark, so it is whole as it stands."""
  for part in bridgeMeshAccess.splitParts(sceneObject)[1:]:
    data = part.data
    bpy.data.objects.remove(part)
    if data.users == 0:
      bpy.data.meshes.remove(data)
  if bridgeMeshAccess.splitProperty in sceneObject:
    del sceneObject[bridgeMeshAccess.splitProperty]


def renameWhole(sceneObject, newName):
  """Give a split object's whole a new name: the first part takes it and the others are named for it, each part's mark naming it."""
  parts = bridgeMeshAccess.splitParts(sceneObject)
  if sceneObject is not parts[0]:
    mark = bridgeMeshAccess.splitMark(sceneObject)
    raise ValueError(f"'{sceneObject.name}' is part {mark['index']} of '{mark['whole']}'; rename the whole by its name, or joinParts first")
  names = [newName] + [f"{newName}.part{number}" for number in range(2, len(parts) + 1)]
  for name in names:
    if bpy.data.objects.get(name) is not None:
      raise ValueError(f"An object named '{name}' already exists")
  for part, name in zip(parts, names):
    mark = bridgeMeshAccess.splitMark(part) | {"whole": newName}
    part.name = name
    part.data.name = name
    part[bridgeMeshAccess.splitProperty] = json.dumps(mark)


def describeParts(parts):
  return [{"name": part.name} | bridgeMeshAccess.meshCounts(part) for part in parts]


def splitObject(name, maxTriangles):
  if maxTriangles is None:
    maxTriangles = maxModelTriangles
  sceneObject = bridgeMeshAccess.requireEditableMesh(name, "split it")
  if bridgeMeshAccess.waterProperty in sceneObject:
    raise ValueError(f"'{name}' is a water body, which splits itself into parts when editWater builds it over {maxModelTriangles} triangles")
  if bridgeMeshAccess.splitProperty in sceneObject:
    raise ValueError(f"'{name}' is already split into {bridgeMeshAccess.splitMark(sceneObject)['count']} parts; joinParts puts them back as one")
  if sceneObject.modifiers:
    raise ValueError("Apply or remove modifiers before splitting; a split cuts the base mesh")
  if sceneObject.data.users > 1:
    raise ValueError(f"'{name}' shares its mesh with {sceneObject.data.users - 1} other objects, which a split would leave whole; make its mesh its own first")
  bridgeMeshAccess.requireNoShapingPasses(sceneObject, "split it")
  bridgeCaveData.requireNoCaves(sceneObject, "split it")
  if not isinstance(maxTriangles, int) or isinstance(maxTriangles, bool) or maxTriangles < 1:
    raise ValueError(f"maxTriangles is a whole number of triangles, 1 or more, got {maxTriangles!r}")
  before = bridgeMeshAccess.triangleCount(sceneObject)
  if before <= maxTriangles:
    raise ValueError(f"'{name}' has {before} triangles, within {maxTriangles}; there is nothing to split")
  for number in range(2, math.ceil(before / maxTriangles) + 1):
    if bpy.data.objects.get(f"{name}.part{number}") is not None:
      raise ValueError(f"An object named '{name}.part{number}' already exists")
  parts = splitMesh(sceneObject, maxTriangles)
  bpy.context.view_layer.update()
  return {"whole": name, "triangles": before, "maxTriangles": maxTriangles, "parts": describeParts(parts)}


def weldSeams(sceneObject):
  """Weld the seam vertices of different parts that lie together, and take the seam marks off the mesh."""
  mesh = sceneObject.data
  if seamAttribute not in mesh.attributes:
    return 0
  editor = bmesh.new()
  editor.from_mesh(mesh)
  marks = editor.verts.layers.int[seamAttribute]
  seam = [vertex for vertex in editor.verts if vertex[marks] > 0]
  tree = mathutils.kdtree.KDTree(len(seam))
  for index, vertex in enumerate(seam):
    tree.insert(vertex.co, index)
  tree.balance()
  targets = {}
  for index, vertex in enumerate(seam):
    if vertex in targets:
      continue
    for _, other, _ in tree.find_range(vertex.co, seamTolerance):
      partner = seam[other]
      if partner is not vertex and partner[marks] != vertex[marks] and partner not in targets:
        targets[partner] = vertex
  bmesh.ops.weld_verts(editor, targetmap=targets)
  editor.verts.layers.int.remove(editor.verts.layers.int[seamAttribute])
  editor.to_mesh(mesh)
  editor.free()
  return len(targets)


def joinParts(name):
  """Join the parts of a split object back into one: the first part's object keeps the name, origin, and transform, the others go,
  and the vertices along each cut are one again."""
  sceneObject = bridgeMeshAccess.requireMeshObject(name)
  mark = bridgeMeshAccess.splitMark(sceneObject)
  if mark is None:
    raise ValueError(f"'{name}' is not split into parts")
  parts = bridgeMeshAccess.splitParts(sceneObject)
  first = parts[0]
  if bridgeMeshAccess.waterProperty in first:
    raise ValueError(f"'{mark['whole']}' is a water body; editWater builds it again whole, and splits it again if it needs it")
  for part in parts:
    bridgeMeshAccess.requireEditableMesh(part.name, "join it")
    if part.modifiers:
      raise ValueError("Apply or remove modifiers before joining; a join merges the base meshes")
    bridgeMeshAccess.requireNoShapingPasses(part, "join it")
    bridgeCaveData.requireNoCaves(part, "join it")
    if part is not first and part.children:
      raise ValueError(f"'{part.name}' has {[child.name for child in part.children]} parented under it; a join removes it, so unparent them first")
  missing = sorted(set(range(1, mark["count"] + 1)) - {bridgeMeshAccess.splitMark(part)["index"] for part in parts})
  if len(parts) > 1:
    with bpy.context.temp_override(active_object=first, object=first, selected_objects=parts, selected_editable_objects=parts):
      result = bpy.ops.object.join()
    if result != {"FINISHED"}:
      raise RuntimeError(f"join returned {result}")
  welded = weldSeams(first)
  del first[bridgeMeshAccess.splitProperty]
  first.data.name = first.name
  bpy.context.view_layer.update()
  return {"whole": first.name, "joined": len(parts), "missingParts": missing, "weldedVertices": welded} | bridgeMeshAccess.meshCounts(first)


commands = {
  "splitObject": (splitObject, True),
  "joinParts": (joinParts, True),
}
