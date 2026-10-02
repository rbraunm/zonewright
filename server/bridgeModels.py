"""EQ models built from the server's model cache (model.npz plus textures) into Blender objects. Runs under Blender's Python."""
import math
import os

import bpy
import mathutils
import numpy

import bridgeClientLight
import bridgeMeshAccess
import bridgeObjects

alphaThreshold = 0.5
missingTextureColor = (1.0, 0.0, 1.0, 1.0)
untinted = 0xFFFFFF


def missingTextureMaterial(textureName):
  """Faces whose texture no linked archive holds: flat magenta, unlit and unfogged, so the gap is visible in every render."""
  materialName = f"eq_missing_{textureName}"
  material = bpy.data.materials.get(materialName)
  if material is None:
    material = bpy.data.materials.new(materialName)
    material.use_nodes = True
    nodes = material.node_tree.nodes
    for unused in [node for node in nodes if node.type == "BSDF_PRINCIPLED"]:
      nodes.remove(unused)
    emission = nodes.new("ShaderNodeEmission")
    emission.inputs["Color"].default_value = missingTextureColor
    output = next(node for node in nodes if node.type == "OUTPUT_MATERIAL")
    material.node_tree.links.new(emission.outputs["Emission"], output.inputs["Surface"])
  return material


def modelMaterial(folder, textureName, alphaMode, tint, lit):
  """One material per texture, alpha mode, tint, and lighting, reused across objects built from the same cache folder, drawn as the
  client draws it (bridgeClientLight). A tint other than white multiplies the texture's color, as the client tints hair (0xRRGGBB).
  A cutout keeps the pixels whose alpha passes the threshold; a blended material is as opaque as its alpha. A lit mesh is lit by its
  file's baked colors and normals."""
  materialName = f"eq_{os.path.basename(folder)}_{textureName}{'' if alphaMode == 'opaque' else f'_{alphaMode}'}{'' if tint == untinted else f'_{tint:06x}'}{'_lit' if lit else ''}"
  material = bpy.data.materials.get(materialName)
  texturePath = os.path.join(folder, textureName)
  if material is not None and material.node_tree.nodes["eqDiffuse"].image.filepath == texturePath:
    return material
  material = bpy.data.materials.new(materialName)
  material.use_nodes = True
  nodes = material.node_tree.nodes
  diffuse = nodes.new("ShaderNodeTexImage")
  diffuse.name = "eqDiffuse"
  diffuse.image = bpy.data.images.load(texturePath, check_existing=True)
  diffuse.image.colorspace_settings.name = "Non-Color"
  baseColor = diffuse.outputs["Color"]
  if tint != untinted:
    multiply = nodes.new("ShaderNodeMix")
    multiply.data_type = "RGBA"
    multiply.blend_type = "MULTIPLY"
    multiply.inputs["Factor"].default_value = 1.0
    colorInputs = [socket for socket in multiply.inputs if socket.type == "RGBA"]
    material.node_tree.links.new(diffuse.outputs["Color"], colorInputs[0])
    colorInputs[1].default_value = (*(((tint >> shift) & 0xFF) / 255 for shift in (16, 8, 0)), 1.0)
    baseColor = next(socket for socket in multiply.outputs if socket.type == "RGBA")
  bridgeClientLight.surfaceOutput(material, baseColor, diffuse.outputs["Alpha"], alphaMode, lit, alphaThreshold)
  return material


def buildModelMesh(folder, meshName):
  data = numpy.load(os.path.join(folder, "model.npz"))
  vertices, triangles, uvs = data["vertices"], data["triangles"], data["uvs"]
  mesh = bpy.data.meshes.new(meshName)
  mesh.vertices.add(len(vertices))
  mesh.vertices.foreach_set("co", vertices.astype(numpy.float32).ravel())
  mesh.loops.add(len(triangles) * 3)
  mesh.loops.foreach_set("vertex_index", triangles.astype(numpy.int32).ravel())
  mesh.polygons.add(len(triangles))
  mesh.polygons.foreach_set("loop_start", numpy.arange(0, len(triangles) * 3, 3, dtype=numpy.int32))
  mesh.polygons.foreach_set("loop_total", numpy.full(len(triangles), 3, dtype=numpy.int32))
  uvLayer = mesh.uv_layers.new(name="UVMap")
  uvLayer.data.foreach_set("uv", uvs[triangles.ravel()].astype(numpy.float32).ravel())
  lit = "colors" in data
  if lit:
    normals = mesh.attributes.new(bridgeClientLight.normalAttribute, "FLOAT_VECTOR", "POINT")
    normals.data.foreach_set("vector", data["normals"].astype(numpy.float32).ravel())
    baked = mesh.color_attributes.new(bridgeClientLight.bakedAttribute, "FLOAT_COLOR", "POINT")
    baked.data.foreach_set("color", (data["colors"].astype(numpy.float32) / 255).ravel())
  missing = {str(name) for name in data["missingTextures"]}
  slots = {}
  materialIndices = numpy.empty(len(triangles), dtype=numpy.int32)
  for index, (textureName, alphaMode, tint) in enumerate(zip(data["textureNames"], data["alphaModes"], data["tints"])):
    key = (str(textureName), str(alphaMode), int(tint), lit)
    if key not in slots:
      slots[key] = len(slots)
      mesh.materials.append(missingTextureMaterial(key[0]) if key[0] in missing else modelMaterial(folder, *key))
    materialIndices[index] = slots[key]
  mesh.polygons.foreach_set("material_index", materialIndices)
  mesh.update()
  mesh.validate()
  return mesh


def modelObject(folder, name, scale, location, rotationDegrees):
  """An object built from a model cache, scaled, turned rotationDegrees about Z (counter-clockwise from above), its model origin at `location`."""
  modelObjectInstance = bpy.data.objects.new(name, buildModelMesh(folder, name))
  modelObjectInstance.scale = (scale, scale, scale)
  modelObjectInstance.rotation_euler = (0, 0, math.radians(rotationDegrees))
  modelObjectInstance.location = location
  return modelObjectInstance


def placeModel(modelFolder, name, location, rotationDegrees, scale, avatarHeight, snapToGround, collection):
  """Place a cached EQ model with its origin at `location`; snapToGround instead stands the origin avatarHeight above the surface below, as the client stands a spawn."""
  bridgeObjects.requireNewName(name)
  origin = mathutils.Vector(location)
  ground = None
  if snapToGround:
    hit = bridgeMeshAccess.rayCast(origin + mathutils.Vector((0, 0, 1)), (0, 0, -1), 1000)
    if hit is None:
      raise ValueError(f"No ground below {list(location)} for '{name}'")
    ground = hit[0]
    origin = ground + mathutils.Vector((0, 0, avatarHeight))
  placed = modelObject(modelFolder, name, scale, origin, rotationDegrees)
  bridgeObjects.targetCollection(collection).objects.link(placed)
  bpy.context.view_layer.update()
  return bridgeObjects.describeTransform(placed) | {"ground": ground and bridgeObjects.roundVector(ground), "dimensions": bridgeObjects.roundVector(placed.dimensions)}


commands = {
  "placeModel": (placeModel, True),
}
