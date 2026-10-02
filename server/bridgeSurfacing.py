"""Materials and UVs within Phase 1 rules: diffuse and normal maps only, alpha-tested cutouts, world-scaled projection. Runs under Blender's Python."""
import os

import bpy
import numpy

import bridgeClientLight
import bridgeMeshAccess

projectionMethods = ("planar", "box")
uvLayerName = "UVMap"


def loadImage(path, colorSpace):
  if not os.path.isabs(path) or not os.path.isfile(path):
    raise FileNotFoundError(f"Texture '{path}' is not an existing absolute path")
  image = bpy.data.images.load(path, check_existing=True)
  image.colorspace_settings.name = colorSpace
  return image


def createMaterial(name, diffuseTexture, normalTexture, cutout, alphaThreshold):
  if bpy.data.materials.get(name) is not None:
    raise ValueError(f"A material named '{name}' already exists")
  if cutout and not 0 < alphaThreshold < 1:
    raise ValueError(f"alphaThreshold must be in (0, 1), got {alphaThreshold}")
  # Texture bytes stay raw: the preview lights them as the client does (bridgeClientLight).
  diffuseImage = loadImage(diffuseTexture, "Non-Color")
  normalImage = loadImage(normalTexture, "Non-Color") if normalTexture is not None else None
  material = bpy.data.materials.new(name)
  material.use_nodes = True
  nodes = material.node_tree.nodes
  links = material.node_tree.links
  diffuse = nodes.new("ShaderNodeTexImage")
  diffuse.image = diffuseImage
  diffuse.interpolation = "Linear"
  normal = None
  if normalImage is not None:
    normalNode = nodes.new("ShaderNodeTexImage")
    normalNode.image = normalImage
    normalMap = nodes.new("ShaderNodeNormalMap")
    links.new(normalNode.outputs["Color"], normalMap.inputs["Color"])
    normal = normalMap.outputs["Normal"]
  bridgeClientLight.surfaceOutput(material, diffuse.outputs["Color"], diffuse.outputs["Alpha"], "cutout" if cutout else "opaque", False, alphaThreshold, normal)
  return {"material": name, "diffuseTexture": diffuse.image.name, "normalTexture": os.path.basename(normalTexture) if normalTexture else None, "cutout": cutout}


def assignMaterial(objectName, materialName, selector):
  sceneObject = bridgeMeshAccess.requireMeshObject(objectName)
  material = bpy.data.materials.get(materialName)
  if material is None:
    raise ValueError(f"No material named '{materialName}'")
  mask = bridgeMeshAccess.evaluateSelector(selector, sceneObject, "faces")
  count = bridgeMeshAccess.requireSelection(mask, selector, sceneObject, "faces")
  slotIndex = next((index for index, slot in enumerate(sceneObject.material_slots) if slot.material == material), None)
  if slotIndex is None:
    sceneObject.data.materials.append(material)
    slotIndex = len(sceneObject.material_slots) - 1
  materialIndices = numpy.empty(len(sceneObject.data.polygons), dtype=numpy.int32)
  sceneObject.data.polygons.foreach_get("material_index", materialIndices)
  materialIndices[mask] = slotIndex
  sceneObject.data.polygons.foreach_set("material_index", materialIndices)
  sceneObject.data.update()
  return {"object": objectName, "material": materialName, "faces": count, "slot": slotIndex}


def planarAxes(direction):
  normal = numpy.array(direction, dtype=numpy.float64)
  normal /= numpy.linalg.norm(normal)
  helper = numpy.array([0, 0, 1.0]) if abs(normal[2]) < 0.9 else numpy.array([0, 1.0, 0])
  across = numpy.cross(helper, normal)
  across /= numpy.linalg.norm(across)
  return across, numpy.cross(normal, across)


def projectUVs(objectName, method, worldUnitsPerRepeat, selector, direction):
  if method not in projectionMethods:
    raise ValueError(f"method must be one of {list(projectionMethods)}, got '{method}'")
  if worldUnitsPerRepeat <= 0:
    raise ValueError(f"worldUnitsPerRepeat must be positive, got {worldUnitsPerRepeat}")
  if (method == "planar") != (direction is not None):
    raise ValueError("planar projection needs a direction, and only planar takes one")
  sceneObject = bridgeMeshAccess.requireMeshObject(objectName)
  mesh = sceneObject.data
  faceMask = bridgeMeshAccess.evaluateSelector(selector, sceneObject, "faces")
  bridgeMeshAccess.requireSelection(faceMask, selector, sceneObject, "faces")
  if uvLayerName not in mesh.uv_layers:
    mesh.uv_layers.new(name=uvLayerName)
  uvLayer = mesh.uv_layers[uvLayerName]
  mesh.uv_layers.active = uvLayer
  vertexPositions, _ = bridgeMeshAccess.readVertexArrays(sceneObject)
  _, faceNormals, _ = bridgeMeshAccess.readFaceArrays(sceneObject)
  uvs = numpy.empty(len(mesh.loops) * 2)
  uvLayer.data.foreach_get("uv", uvs)
  uvs = uvs.reshape(-1, 2)
  loopVertices = numpy.empty(len(mesh.loops), dtype=numpy.int64)
  mesh.loops.foreach_get("vertex_index", loopVertices)
  loopTotals = numpy.empty(len(mesh.polygons), dtype=numpy.int64)
  mesh.polygons.foreach_get("loop_total", loopTotals)
  loopFaces = numpy.repeat(numpy.arange(len(mesh.polygons)), loopTotals)
  selectedLoops = faceMask[loopFaces]
  if method == "planar":
    loopAxes = numpy.broadcast_to(numpy.array(planarAxes(direction)), (len(mesh.loops), 2, 3))
  else:
    boxAxes = numpy.array([planarAxes(numpy.eye(3)[axis]) for axis in range(3)])
    loopAxes = boxAxes[numpy.abs(faceNormals).argmax(axis=1)[loopFaces]]
  points = vertexPositions[loopVertices]
  projected = numpy.einsum("lj,laj->la", points, loopAxes) / worldUnitsPerRepeat
  uvs[selectedLoops] = projected[selectedLoops]
  uvLayer.data.foreach_set("uv", uvs.ravel())
  mesh.update()
  return {"object": objectName, "method": method, "faces": int(faceMask.sum()), "worldUnitsPerRepeat": worldUnitsPerRepeat}


commands = {
  "createMaterial": (createMaterial, True),
  "assignMaterial": (assignMaterial, True),
  "projectUVs": (projectUVs, True),
}
