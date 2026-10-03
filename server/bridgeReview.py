"""The open scene's zone gathered as the zone survey gathers a client zone, so it is measured by the same methods: the terrain
collection's meshes are its terrain, every other rendered mesh and collection instance is placed on it. Runs under Blender's Python."""
import os

import bpy
import mathutils
import numpy

import bridgeExport


def triangulated(sceneObject, depsgraph, matrix):
  """World positions, triangles, and each triangle's material name, from the mesh as evaluated."""
  evaluated = sceneObject.evaluated_get(depsgraph)
  mesh = evaluated.to_mesh()
  try:
    mesh.calc_loop_triangles()
    coordinates = numpy.empty(len(mesh.vertices) * 3)
    mesh.vertices.foreach_get("co", coordinates)
    positions = coordinates.reshape(-1, 3) @ numpy.array(matrix)[:3, :3].T + numpy.array(matrix)[:3, 3]
    triangles = numpy.empty(len(mesh.loop_triangles) * 3, dtype=numpy.int64)
    mesh.loop_triangles.foreach_get("vertices", triangles)
    materialIndices = numpy.empty(len(mesh.loop_triangles), dtype=numpy.int64)
    mesh.loop_triangles.foreach_get("material_index", materialIndices)
  finally:
    evaluated.to_mesh_clear()
  slotNames = [slot.material.name if slot.material else None for slot in sceneObject.material_slots]
  return positions, triangles.reshape(-1, 3), [slotNames[index] if index < len(slotNames) else None for index in materialIndices]


def collectConstruction(outputPath):
  """Writes the zone's triangles to outputPath (.npz) and returns its texture names and placement count."""
  scene = bpy.context.scene
  depsgraph = bpy.context.evaluated_depsgraph_get()
  terrainCollection = bpy.data.collections.get(bridgeExport.terrainCollectionName)
  if terrainCollection is None or not any(member.type == "MESH" for member in terrainCollection.all_objects):
    raise ValueError(f"The scene has no '{bridgeExport.terrainCollectionName}' collection with meshes; its meshes are the zone's terrain")
  terrainNames = {member.name for member in terrainCollection.all_objects}
  parts, placements = [], 0
  for sceneObject in scene.objects:
    if sceneObject.hide_render or sceneObject.type in ("LIGHT", "CAMERA"):
      continue
    if sceneObject.type == "MESH":
      isTerrain = sceneObject.name in terrainNames
      parts.append((not isTerrain, triangulated(sceneObject, depsgraph, sceneObject.matrix_world)))
      placements += not isTerrain
    elif sceneObject.type == "EMPTY" and sceneObject.instance_type == "COLLECTION" and sceneObject.instance_collection is not None:
      collection = sceneObject.instance_collection
      offset = mathutils.Matrix.Translation(-collection.instance_offset)
      for member in collection.all_objects:
        if member.type == "MESH" and not member.hide_render:
          parts.append((True, triangulated(member, depsgraph, sceneObject.matrix_world @ offset @ member.matrix_world)))
      placements += 1
  textureNames, vertexChunks, triangleChunks, textureChunks, objectChunks, vertexCount = {}, [], [], [], [], 0
  for isObject, (positions, triangles, materialNames) in parts:
    vertexChunks.append(positions)
    triangleChunks.append(triangles + vertexCount)
    textureChunks.append(numpy.array([-1 if name is None else textureNames.setdefault(name, len(textureNames)) for name in materialNames], dtype=numpy.int64))
    objectChunks.append(numpy.full(len(triangles), isObject))
    vertexCount += len(positions)
  os.makedirs(os.path.dirname(outputPath), exist_ok=True)
  numpy.savez(outputPath, vertices=numpy.concatenate(vertexChunks), triangles=numpy.concatenate(triangleChunks), triangleTextures=numpy.concatenate(textureChunks), triangleIsObject=numpy.concatenate(objectChunks))
  return {"arrays": outputPath, "textureNames": list(textureNames), "placements": placements}


commands = {
  "collectConstruction": (collectConstruction, False),
}
