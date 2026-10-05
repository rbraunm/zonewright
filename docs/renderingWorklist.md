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

1. **EQG zones.** `importZone` reads client EQG zones by the loader rules in [clientRendering.md](clientRendering.md#eqg-zones) (the loose version 2 `.zon` over the archive's version 1, baked light only where its count fits), and `importZoneFile` reads exported ones. Still to draw: the colors the client computes for placements whose baked light doesn't fit (listed by the import; checkable against MQPeridotEmu's dump), how objects and terrain without baked light are lit (`0x1009d670`), and the effect families (`SModel`). The preview also lights a `createMaterial` normal map with the scene light, where the client's region effect (terrain) uses a normal map only for point light 0; the object effects are not traced.
2. **Point lights and emitters remainder.** Both draw ([clientRendering.md](clientRendering.md#point-lights), [Particle emitters](clientRendering.md#particle-emitters)); what each section lists as not settled remains: the region of influence and the split of EQG terrain into draws (the preview chooses lights per vertex), the particles' near clip plane, the draw order between emitters, and whether blended zone surfaces hide particles behind them. No RoF2 screenshot of a lit night scene or of emitters near the camera exists yet.
3. **Sky remainder.** The dome, sun and moon, and horizon band draw from the client's sky files, and a zone with a sky takes its light and fog color from it ([clientRendering.md](clientRendering.md#sky)). Still to draw: clouds, stars, the backdrop ring, and the dome's and band's colors by azimuth. Not traced: when the moon lights the scene, the satellites' turn and fade, and whether the sky draws before or after the world.
4. **Texture orientation in the DLL.** Static EQG models need v flipped and skinned ones do not; this is measured from screenshots, not traced. Find the vertex buffer step that differs (static effects read texture coordinates as shorts over 256, skinned ones as floats) so export writes what the client expects.
5. **Additive blending.** `EQGraphicsDX9.dll` has one additive pass (source and destination factor one, fog off, `0x10088ac5`), chosen through a descriptor per shader type (`0x1009a3ec`). Which shader types take it, and so whether `AddAlpha` does, is not traced; `AddAlpha` draws opaque.

## Next: matching client zones in calibration

6. **Character lighting.** Spawns are lit like zone meshes; the client lights them with the `SkinMesh*` effects, not yet read.
7. **Equipment.** NPCs and players draw without equipment (Luclin bodies show bare skin); Drakkin armor pieces and layers are not drawn either. The recordings carry each spawn's equipment and armor colors.
8. **Levels of detail.** The client switches `.lod` models by distance (for example `OBJ_pinetree` 100, 200, 500, 5000 units); every object draws at full detail.
9. **Radial flora.** EQ terrain zones draw grass cards around the camera (`radialfloradefs.rfd`, the ecosystems' flora layers).
10. **Liquids.** EQG water, waterfall, and lava materials draw by their effects ([clientRendering.md](clientRendering.md#eqg-zones)), at the effect time a view sets, with these stand-ins: opaque water where the client's alpha is fresnel times 2.9 and more; see-through liquids (falls, waterfall ribbons) layering among themselves in Blender's order of their objects' origins, which for water bodies is the world's, so two overlapping in a view may layer wrongly; no point light 0 term on water or lava, so lava's crust takes point light 0 through the vertex light with falloff where the client lights it through the normal maps without falloff, and an object's lava glows at once its second diffuse where point light 0 brightens it up to twice. Not settled: that an empty environment sampler reads black (an RoF2 screenshot of Argath's or Sepulcher's pools, 2D `coverage_cool`, at a grazing angle, and `Logs\dbg.txt` after entering showing `CreateCUBETexture(): unable to load`); the cube faces' orientation, D3D's standard table being assumed and borne out by one streak in Highpass Hold (an RoF2 screenshot of Crescent Reach's Bear Cave pond from (-150, -2030) facing east at eye level, midday, against the preview's from the same camera); why Highpass Hold's water reads 2 to 10 levels darker in green and blue than its RoF2 screenshots; whether water without `e_TextureDiffuse0` draws (1,600 triangles, Buried Sea's isles' lakes and Brell's Rest's `regwater`; the import leaves them out; an RoF2 screenshot of Blacksail Folly's lake); lava in game at all (no RoF2 screenshot of lava exists; Brell's Rest or Underquarry at eye level). EQ terrain water sheets (`water.dat`) are not drawn.
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
- **Back faces.** The preview draws both sides of every face and lights each by its own normal, as the vertex shaders light a vertex whichever side is seen, so a face turned inside out draws darker, as it does once exported. Whether the client draws back faces at all is not traced: it needs the cull mode set for zone and object meshes (`D3DRS_CULLMODE` in `EQGraphicsDX9.dll`'s draw setup, or a `CullMode` state in the effects' passes), or an RoF2 screenshot of an exported zone with a box turned inside out, seen from outside. Until then the preview does not cull.

## Parked

- **Other particle effects** (spell effects, actor emitters from `ActorEmittersNew.edd`, WLD particle clouds): not drawn until the owner asks; a zone's environment emitters draw. Placements count WLD particle clouds in `particleCloudsNotDrawn`.
