# Rendering fidelity worklist

What the preview does not yet draw as the client (RoF2) does, ordered by what it blocks. How the client renders what is done is in [clientRendering.md](clientRendering.md); `calibrateShot` measures the remaining difference against live screenshots, and `getToolingStatus` lists every screenshot's latest run.

Live-only content (models, looks, and textures the RoF2 client lacks) is not rendered: placement lists it and moves on. A difference that remains for RoF2-era content whose data matches the Peridot server's database is a renderer bug.

## Standing

| Shots | Mean difference (of 255) | Largest causes |
|---|---|---|
| Plane of Knowledge, two night shots | about 13.0 and 10.4 | No sky; no spawns or doors placed |
| Eastern Wastes, one fogged day shot | about 15.8 | Fog fitted (no zone header); no sky |
| Neighborhood, two day shots with the recording | about 13.5 and 16.6 | No sky; NPCs without equipment; no grass tufts |

## First: what exported zones need

Blender-built zones drawn as the client would draw them depend on these.

1. **EQG zones.** Import client EQG zones following the loader rules in [clientRendering.md](clientRendering.md#eqg-zones): the loose version 2 `.zon` over the archive's version 1 (confirmed in the RoF2 client), inline or `.lit` baked light used only when its count fits, and the effect families. Still to trace: the colors the client computes for objects whose baked light doesn't fit (checkable against MQPeridotEmu's dump), how objects and terrain without baked light are lit (`0x1009d670`), and the `SModel` effects.
2. **Point lights.** The shaders' three point lights per mesh: the zone's lights (`lights.wld`, EQG zone lights), which three the client picks per mesh, and their falloff (each light's reciprocal range from the effect's preshader). Baked vertex light is already drawn.
3. **Sky.** The client draws a sky dome (stars and moon at night) for the zone header's sky type; the preview shows the fog color. Includes the sun object's color and direction over the day and the sky object's ambient.
4. **Texture orientation in the DLL.** Static EQG models need v flipped and skinned ones do not; this is measured from screenshots, not traced. Find the vertex buffer step that differs (static effects read texture coordinates as shorts over 256, skinned ones as floats) so export writes what the client expects.
5. **Additive blending.** `EQGraphicsDX9.dll` has one additive pass (source and destination factor one, fog off, `0x10088ac5`), chosen through a descriptor per shader type (`0x1009a3ec`). Which shader types take it, and so whether `AddAlpha` does, is not traced; `AddAlpha` draws opaque.

## Next: matching client zones in calibration

6. **Character lighting.** Spawns are lit like zone meshes; the client lights them with the `SkinMesh*` effects, not yet read.
7. **Equipment.** NPCs and players draw without equipment (Luclin bodies show bare skin); Drakkin armor pieces and layers are not drawn either. The recordings carry each spawn's equipment and armor colors.
8. **Levels of detail.** The client switches `.lod` models by distance (for example `OBJ_pinetree` 100, 200, 500, 5000 units); every object draws at full detail.
9. **Radial flora.** EQ terrain zones draw grass cards around the camera (`radialfloradefs.rfd`, the ecosystems' flora layers).
10. **Water.** EQ terrain water sheets (`water.dat`) and EQG water materials.
11. **EQ terrain remainder.** Quad kinds 1 and 4; `BLENDMAP` and `LAYERINGMAP`; child layers; tiles without ecosystems (the client's default texture); a `.lit` file whose count does not fit its model (drawn without baked light, inferred from a screenshot).
12. **Scene light sources.** `SpecialAmbient` and `BounceColor` sources in `eqgame.exe`; the calibration fit folds special ambient into ambient.
13. **Cover map mips.** D3DX recompresses each generated mip level of a terrain cover map to DXT5; the preview box filters without recompressing.
14. **Spawn looks not read.** Appearance values some EQG models carry (an ALA's face style and heritage) are refused rather than guessed.

## Validation

Rules the renderer uses that are not yet confirmed against the RoF2 client itself. Code traces read what the DLL does but can miss a step. Calibration screenshots so far come from the live client, which has moved on since RoF2. Each of these needs RoF2 evidence: screenshots taken in the RoF2 client on Peridot, its `Logs\dbg.txt` (the graphics DLL logs there, including every baked light it ignores), or a read of the running client. MQPeridotEmu's `/peridotemu dump` (`PhoenixCampfire/mqperidotemu`) reads the running client: the zone in MQ2PeridotLive's dump format, plus the graphics camera (position, orientation, view angle, clip planes, projection scales), every actor in the scene (definition, position, orientation, scale, and a simple actor's vertex count and baked light colors), and the engine's property tree (sky, fog colors, time of day). A dump taken with a screenshot settles most of the rules below.

- **Which variant a zone loads.** Some zones ship both a classic `.s3d` and an EQG `.eqg` with a `.zon` (`arena`, `tutorialb`, ...); the import takes the classic one, an untested choice.
- **Texture orientation** of static EQG models (checked against live screenshots only).
- **The camera:** the RoF2 client's field of view and eye height, read from the client, differ from those measured against live screenshots (clientRendering.md, Camera); the renderer needs a camera per client. The pitch offset is not yet checked.
- **EQ terrain:** 32 texels per tile (the DLL's default distance table; `eqgame.exe` may set others); quad kind bit 2 drawn as ordinary; placement z above the ground; tilt order; the cover map's mip filtering.
- **Objects without baked light** (most placed objects in classic zones): drawn with no baked light and the full share of scene light, an assumption (`0x1009d670` is the client's path). EQG objects whose baked light doesn't fit get colors the client computes (clientRendering.md, EQG zones).
- **Tilt order** of EQG zone placements.

## Parked

- **Particle effects** (flames, smoke, spell clouds): not drawn until the owner asks; placements count WLD particle clouds in `particleCloudsNotDrawn`.
