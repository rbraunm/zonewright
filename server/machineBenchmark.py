"""Blender-side GPU benchmark for machine profiling: renders a small multi-material EEVEE scene and reports the GPU used. Run with blender --python."""
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bpy
import gpu

import bridgeProtocol

benchmarkMarker = "ZONEWRIGHT_BENCHMARK"
renderCount = 4
materialCount = 8

bpy.context.preferences.system.gpu_shader_workers = int(os.environ[bridgeProtocol.shaderWorkersVariable])
bpy.ops.wm.read_homefile(use_empty=True)
scene = bpy.context.scene
for index in range(materialCount):
  bpy.ops.mesh.primitive_cube_add(size=4, location=((index % 4) * 6 - 9, (index // 4) * 6 - 3, 2))
  material = bpy.data.materials.new(f"benchmark{index}")
  material.use_nodes = True
  nodes = material.node_tree.nodes
  noise = nodes.new("ShaderNodeTexNoise")
  noise.inputs["Scale"].default_value = 2.0 + index
  ramp = nodes.new("ShaderNodeValToRGB")
  material.node_tree.links.new(noise.outputs["Fac"], ramp.inputs["Fac"])
  material.node_tree.links.new(ramp.outputs["Color"], nodes["Principled BSDF"].inputs["Base Color"])
  bpy.context.object.data.materials.append(material)
bpy.ops.mesh.primitive_plane_add(size=60)
camera = bpy.data.objects.new("benchmarkCamera", bpy.data.cameras.new("benchmarkCamera"))
scene.collection.objects.link(camera)
camera.location = (0, -30, 18)
camera.rotation_euler = (1.1, 0, 0)
scene.camera = camera
sun = bpy.data.objects.new("benchmarkSun", bpy.data.lights.new("benchmarkSun", "SUN"))
scene.collection.objects.link(sun)
sun.rotation_euler = (0.7, 0, 0.5)
scene.render.engine = "BLENDER_EEVEE"
scene.render.resolution_x, scene.render.resolution_y = 640, 360
scene.eevee.taa_render_samples = 4
scene.view_layers[0].use_pass_mist = True
renderSeconds = []
with tempfile.TemporaryDirectory() as outputDirectory:
  scene.render.filepath = os.path.join(outputDirectory, "benchmark.png")
  for _ in range(renderCount):
    start = time.perf_counter()
    bpy.ops.render.render(write_still=True)
    renderSeconds.append(round(time.perf_counter() - start, 4))
print(benchmarkMarker + " " + json.dumps({
  "backend": gpu.platform.backend_type_get(),
  "vendor": gpu.platform.vendor_get(),
  "renderer": gpu.platform.renderer_get(),
  "deviceType": gpu.platform.device_type_get(),
  "renderSeconds": renderSeconds,
}), flush=True)
