# How the client renders a zone

What `eqgame.exe` and `EQGraphicsDX9.dll` of the RoF2 client do to draw a zone, read from their code and the client's compiled effects (`RenderEffects\*.fxo`). The zone renderer reproduces this; calibration against screenshots checks it. Addresses are the RoF2 client's. Calibration screenshots come from the live client, whose renderer may have moved on since; differences that remain after matching these rules may be that.

## Effects

The DLL draws with Direct3D 9 effects from `RenderEffects\SPL` (single pass) and `RenderEffects\MPL` (multipass). Each effect has techniques for DX9, DX8, and DX6 hardware; a technique without a pixel shader leaves the pixel stage to the DLL's texture stage states, which set color to MODULATE (texture times vertex color) and never to MODULATE2X.

A classic (WLD) zone mesh draws with `SPL\RegionOldA.fxo` (technique `RegionOldA_DX6_VS1_PS0`): a vertex shader and no pixel shader.

## Classic zone lighting

`RegionOldA`'s vertex shader lights each vertex from its stored color (RGB baked light, alpha the share of scene light it receives) and normal:

    light = vertex.rgb + vertex.a * (ambient + bounce * max(N . D, 0) + sun * max(N . -D, 0))
          + specialAmbient + sum over three point lights of color * max(N . L, 0) * (1 - min((distance * k)^2, 1))

- N is the stored normal (bytes 0-255 mapped to -1..1), D the sun direction, L the direction to a point light; k is a per-light reciprocal range computed in the effect's preshader.
- The pixel is the texture times the light, clamped to 1 by the fixed-function stage, then fogged.
- Fog is exponential squared per vertex: `fog = exp(-(density * clamp(10 * (distance - fogStart) / (fogEnd - fogStart), 0, 10))^2)`, distance from the eye, and the pixel is `lerp(fogColor, pixel, fog)`. The 10 is the effect's own `fFogRange`; the DLL sets start, end, and density (default 0.33), so at the default density fog reaches 63% a third of the way from start to end and is total at the end.

So a WLD zone mesh's vertex colors are not a multiplier: in Plane of Knowledge they are near black with alpha 255 (lit by scene light), in Eastern Wastes black with alpha about 200.

## Where the lighting values come from

The DLL sets the effect parameters per draw (`0x10088cb5`) from its world object's environment (`0x1008b470`):

| Parameter | Source |
|---|---|
| `Ambient` | scene graph ambient, set by vtable `0xc8` from a D3DCOLOR (bytes / 255) |
| `SpecialAmbient` | the sum of two scene graph colors (vtable `0xcc` and `0xd0`), each channel clamped to 1 |
| `BounceColor` | scene graph bounce color (vtable `0xd4`) |
| `DirectionalColor`, `DirectionalNormal` | the world's sun object: color at `+0x5c`, direction at `+0x50` |
| `FogStart`, `FogEnd`, `FogDensity` | the device's fog settings (`+0xa9b8`, `+0xa9bc`, `+0xa9c0`) |

`eqgame.exe` sets the scene ambient each frame (`0x4942b0`):

- **Full bright** (a debug and vision mode): white.
- **Outdoor zones** (zone type 1, 2, or 5): the sky's ambient color when the zone has a sky object; otherwise hourly tables interpolated by the minute (`0xaf3260`, `0xaf32c0`, `0xaf3320`), red and green 0 at night rising to 0.9 at noon, blue 0.3 at night rising to 0.9.
- **Zone type 4**: 0.5 gray. **Other zone types**: 0.1 gray.
- Vision effects (infravision, ultravision) then scale it.

The zone header (the server's NewZone packet) carries zone type, sky type, fog colors and distances (four sets), clip distances, and whether fog and sky are on. The live dumps record it in `fields\zoneHeaders.tsv`.

## Zone files as the renderer reads them

- **Vertex colors** (WLD 0x36) are stored blue first (D3DCOLOR): Plane of Knowledge's torch-lit walls bake orange only read that way.
- **Normals** are signed bytes over 127.
- **Placed objects** (`objects.wld` 0x15): actor, position, a heading and a tilt in 512ths of a turn (the third rotation is always 0 in the zones read so far), and one scale. The renderer turns an object by its tilt about its Y axis, then its heading about Z as a spawn's heading turns it; screenshots check it.
- **Objects without vertex colors** (most placed objects) are drawn with no baked light and the full share of scene light. Where the client takes their color from is not traced.
- Zone and object textures are often DDS data under `.bmp` names.
- **Texture orientation:** WLD meshes and static (boneless) EQG models count v down from a texture's top, so the renderer flips them for Blender. The Neighborhood's map board and guild gate show upright only that way. Skinned EQG models (a Drakkin's face) show upright unflipped. Which DLL step makes the difference is not traced: the static model effects (`SModel*`) read texture coordinates as shorts over 256, the skinned ones (`SkinMesh*`) as floats.

## EQ terrain zones

An EQ terrain zone (`EQTZP`, such as the Neighborhood) is a grid of tiles in the zone's `.dat`, each a 17x17 height grid with two vertex colors per vertex and ecosystem layers (`.eco` files). `EQGraphicsDX9.dll` reads the tiles at `0x101004a0` (data versions 20 and 21 differ only in a water sheet block) and draws each ecosystem on a tile as its own pass with `SPL\Terrain_Bump<n>Detail.fxo` (`n` the ecosystem's texture layers, 1-3), technique `TerrainBump<n>Detail_DX9_VS1_PS20`. Where an ecosystem's first layer has no normal map it uses `Terrain_<n>Detail` instead, whose `DX9_VS1_PS11` technique clamps the doubled light and tint together in the vertex shader; the zones read so far all have normal maps.

### Vertices

The vertex buffer (`0x100ac170`) holds, per grid vertex:

- **Position:** x and y in whole units, height times 8, each truncated to a short; the shader multiplies the height by 0.125.
- **Normal:** from the heights beside the vertex (`0x100f2190`): `(h(x-1) - h(x+1), h(y-1) - h(y+1), 2 * unitsPerVertex)`, normalized, reaching into the neighboring tile at an edge and one-sided where there is none; packed as color bytes `trunc(255 * (n * 0.5 + 0.5))`.
- **COLOR0:** the tile's second color array: baked light in RGB and the share of scene light in alpha, as on a classic zone (`0xff000000`, black with the full share, by default).
- **COLOR1:** the first color array, a tint the shader doubles (`0x00808080`, neutral, by default).
- **TEXCOORD0:** across the tile's color map and detail mask, from the first texel's center to the last's, in 256ths.
- **TEXCOORD1:** 0 to 1 across the tile, times each detail layer's `DETAILREPEAT` in the shader.

A quad's flag `0x80` picks its diagonal: clear splits it from its (0, 0) corner to (1, 1), set from (1, 0) to (0, 1) (the height query, `0x100f2fc0`). Flag bits 1 and 4 mark quad kinds (`0x100f2100`) whose drawing is not traced; bit 2 reads back like no bit.

### The pass

    light  = COLOR0.rgb + COLOR0.a * (ambient + bounce * max(N . D, 0) + sun * max(N . -D, 0)) + specialAmbient   (clamped to 1)
    detail = mask.r * Detail0(uv1 * repeat0) + mask.g * Detail1(uv1 * repeat1) + mask.b * Detail2(uv1 * repeat2)
    pixel  = 2 * colorMap.rgb * detail * COLOR1.rgb * light,  written with alpha colorMap.a

plus point light 0 through the detail normal maps (not drawn). The passes' alphas add to 1 across a tile's ecosystems.

### Tile textures

Each ecosystem on a tile has a 32x32 color map and detail mask (A8R8G8B8, no mipmaps). The terrain system's distance table (`0x100ec12a`) gives 32 texels at every distance. They are computed once per tile (`0x100f3fd0`):

- **Height and slope per texel:** texel `t` samples the vertex grid at `t * 16 / 31` along each axis, bilinearly: the height, and the normal's z, whose slope in degrees comes from a table, `acos(round(z * 1000) / 1000)` (`0x100ec630`).
- **Layer weights (`0x100ee790`):** a texture layer covers a texel fully inside `MINHEIGHT`-`MAXHEIGHT` and `MINSLOPE`-`MAXSLOPE`, falling off linearly across `HEIGHTTOL` and `SLOPETOL` outside them (the two factors multiplied). The last layer takes `round(factor * 255)`; each earlier one, back to the second, takes its factor of what is left; the first layer takes the rest.
- **Color map RGB (`0x100eeed0`):** each layer's `COVERMAP` times its weight / 255, summed: a weight below 3 adds nothing and one of 253 or more takes the cover map whole. The cover map is read at 32x32 from the mip chain D3DX builds for it with a box filter (the cover maps ship without mips), so it repeats once per tile.
- **Coverage, the color map's alpha (`0x100f4690`):** each later ecosystem's 64x64 mask from the `.dat` (texel `t` reads mask texel `2t`) times each layer's weight, scaled by 1/65535 and summed. Working from the last ecosystem back to the second, each takes that share of what later ones left, snapped to 0 below 3 and 255 from 253. The first ecosystem takes the rest.
- **Detail mask (`0x100ac770`):** red, green, and blue are the first three layers' weights. A texel on the tile's edge is averaged with the neighboring tile's texel beside it where that tile has the same ecosystem (left, right, below, above, in that order).

`BLENDMAP` and `LAYERINGMAP` (`default.bmp` in the zones read so far) and child layers modify the weights and coverage; they are not read yet.

### Objects

- **Placements:** a tile's placements give a model (`.mod`, with `.lod` levels the client switches by distance), the ecosystem that placed it, a position from its own tile's origin with z above the ground beneath it, turns in degrees, and a scale. Most have z 0; hand-placed rocks sink a few units.
- **Object groups (`.tog`):** place their members' models relative to the group, each with a `.lit` file of baked light per vertex. The client ignores baked light whose count differs from the model's vertices (`0x100548d0` logs "LIT data has %d vertices, expected it to have %d vertices for %s, so LIT data is being ignored!"), so the Neighborhood's zone-out wall, whose `.lit` holds 2175 colors for 1788 vertices, draws without it.

Neighborhood calibration: the two day shots redraw within about 13.5 and 16.6 levels of 255, using the zone's header fog and the doors, ground items, and NPCs of the bristle behavior recording at each shot's moment. The Peridot server's door table agrees with the live dump for the gate in the second shot (`OBJ_GUILDGATE`, door 134). NPC equipment, radial flora (grass cards near the camera), the sky, and level-of-detail models are not drawn yet.

## EQG zones

An EQG zone is an `.eqg` archive whose `.zon` (EQGZ) places one `.ter` terrain mesh and many `.mod` objects. This is the export format. The import that draws client EQG zones is the first item of the [worklist](renderingWorklist.md). What is traced so far:

- **Which `.zon` loads (`0x10066230`):** the client first opens a loose `<archive>.zon` beside the `.eqg`. If it exists, that file is the zone: version 2 (`0x10065430`), or an EQ terrain zone. Otherwise the archive's own `.zon` loads, which must be version 1 (`0x10064da0`). In this client, all 166 loose `.zon` files are version 2. Of the 37 inside archives, 36 are version 1; the one version 2 file inside an archive cannot load that way. The RoF2 client confirms this for the Guild Lobby (below).
- **Baked light:** a version 1 placement takes it from `<placement name>.lit` in the archive (magic `EQGP`, a count, then a D3DCOLOR per vertex). A version 2 placement carries it inline. Either way it reaches the model instance (`0x100548d0`), which takes it only when its count equals the model's vertex count and otherwise logs "LIT data ... is being ignored!" to the client's `Logs\dbg.txt`.
- **The Guild Lobby in the RoF2 client** (MQPeridotEmu's dump with `dbg.txt`): the client loads the loose version 2 `.zon`. It places 63 candle torches as that file does, not the archive's 64, and none of the archive-only models; the 11 models no archive holds (`obj_aframe.mod`, `obj_window.mod`, `obp_streetlamp.mod`, ...) are not drawn. The 133 objects whose inline colors fit draw with exactly those colors. The terrain's inline colors (57912 for 61096 vertices) and those of the other 116 objects are ignored, and those objects end up with colors the client computes per placement: every one has alpha `0x19`, where colors taken from the file have alpha `0x00`. `AFRAME_`, `GUILD_DOOR_` and `TRANS_ENTRY` are the server's doors, not zone objects.
- **Terrain stands where its vertices are:** the `.zon` places its `.ter` turned and moved (most zones a quarter turn, -pi/2, and an offset), but the client draws the terrain at its own vertices and the placement goes unused. Measured in Broodlands, Crescent Reach, Freeport East, Highpass Hold, and the Arena: only so do the trees, posts, crates, and barrels placed on the terrain stand on it (Crescent Reach: 232 of 241 at no gap; with the placement applied, none), and RoF2 screenshots of Highpass Hold with MQPeridotEmu's camera match the import only so. Export places its terrain at the origin unturned, which is the same either way.
- **Placements:** model names lose their extension and take `_ACTORDEF`. Rotations are radians, turned into 512ths of a turn as (field 1, -field 2, field 3) times 512 / 2pi. A light's position reads as (field 2, -field 1, field 3).
- **Effects:** a material's shader name selects the effect family by mesh kind: `Opaque_MaxCB1.fx` draws zone terrain with `SPL\RegionCB1`, placed objects with `SModelCB1`, and skinned models with `SkinMeshCB1`. `RegionCB1`'s vertex shader lights each vertex exactly as `RegionOldA` does (baked RGB plus share times scene light, special ambient, and three point lights). Its normal map adds only point light 0.
- **Regions:** each is a name (its prefix says what it is: `AWT_` water, `ALV_` lava, `ATP_` zone line, `ASL_` ice, `APV`, `APK`) and nine floats: a box's center, three turns, and its half extents, in the terrain's own frame. The first turn holds -pi/2 or -128 in most client zones (the terrain placement's turn, in either unit); with the terrain drawn where its vertices are, the boxes line up with the water surfaces only when the turns are left unapplied. Export writes axis-aligned boxes with the turns 0, which reads the same either way. Whether the client applies the turns is not yet checked in game (swimming at a box's edge would show it).
- **Water** (`Opaque_MaxWater.fx`, terrain `SPL\RegionWater`, objects `SModelWater`; DX9 technique `ps_2_0`): the diffuse is not read. The color runs from `e_fWaterColor1` seen from straight above to `e_fWaterColor2` at grazing angles (by 1 - view·normal), lit by the vertex light like any surface, plus point light 0 by the normal. The normal is the normal map sampled at the texture coordinates and at twice them, each scrolled by a slide times time, summed, and flattened with distance until flat 300 units off. The environment cube map, looked up along the reflected view ray, adds `e_fReflectionAmount` times `e_fReflectionColor` times fresnel: `e_fFresnelBias` plus the rest times the grazing term to `e_fFresnelPower` (the constants holding 1 - bias and the color difference come from the effect's preshader and are taken to be those). Alpha is 2.9 times fresnel. The texture coordinates are read as shorts over 256, so the normal map repeats as often as the coordinates do; `e_fUVScale` defaults to 1 and no client material sets it. The effect's defaults match `Resources\WaterSwap\WaterSwap.ini`'s new water (fresnel bias 0.25, power 8, reflection 0.7). Highpass Hold's water drawn this way matches RoF2 screenshots in hue and ripple size.
- **Waterfalls** (`Opaque_MaxWaterFall.fx`, `SPL\RegionWaterFall`, DX8 technique `ps_1_1`): the diffuse at the texture coordinates scrolled by the first slide, times the vertex light, is the color; the diffuse's alpha at the coordinates scrolled by the second slide is the alpha, so a fall is as see-through as its texture.
- **Objects whose baked light doesn't fit:** the client computes per-vertex colors for them (above); how is not traced yet. Objects without any baked light take a different light setup per draw (`0x1009d670`), with the scene's ambient sources folded into one constant. MQPeridotEmu's dump records every object's colors (count, hash, first color), so a traced formula can be checked against the client.

## Camera

Measured by aligning renders to live client screenshots (Plane of Knowledge twice, Eastern Wastes once), each cropped about its center to 16:9:

- **Field of view:** 46.5 degrees vertical; the three shots scaled to 45.3-47.3 at 52.
- **Pitch:** 4.5 degrees below the pitch the screenshot's name records (-3.7 to -5.5 measured).
- **Heading:** as recorded (within 1.5 degrees).
- **Eye:** 0.66 above `/loc`'s z, from the Palatial Guild Hall shot; 2D alignment cannot separate eye height from pitch, so this rests on that shot.

The RoF2 client's camera, read by MQPeridotEmu in first person (the Grand Guild Hall and the Guild Lobby), differs: its half view angle is 45/512 of a turn horizontally, a 63.3-degree horizontal field of view whose vertical follows the window (37.7 degrees at 2560x1417, 38.3 at 16:9), and its eye sits 3.69 above the spawn's z for a human of height 3.75. The live measurements above stay the reference for live screenshots; RoF2's renders need RoF2's.

The Eastern Wastes shot at `-1994.17, 3027.87` matches in heading but sits about 15 degrees off in pitch; its recorded `/loc` or pitch likely was not taken at the moment of the screenshot, and it is left out.

## Calibration

`calibrateShot` renders a screenshot's view as passes (lit, texture, normal, baked light, share, distance) and solves the classic lighting and fog formula over a sample of the pixels: for each sun direction (and, without the zone header, each fog start and end at the client's density), the ambient, sun, bounce (and fog) colors are linear in every pixel and are solved by least squares per channel. Two ambiguities are settled by convention, not measured:

- Special ambient adds exactly like ambient where every surface takes the full share of scene light, so it is folded into ambient.
- A sun from one direction lights exactly as a bounce from the opposite one, so the sun is searched above the horizon and light from below is bounce.

Where no surface in view is fully fogged, a slightly nearer fog end with a slightly darker fog color draws nearly the same image, so a fitted fog is only as precise as the shot allows. The passes come back premultiplied by coverage and with negative values clamped to 0: the normal pass is encoded as normal * 0.5 + 0.5, and colors are divided by coverage before fitting.

Results: the two Plane of Knowledge night shots redraw within about 11-13 levels of 255 on average (no sky drawn), and the Eastern Wastes day shot, with its fog fitted (start near 0, end near 1900), within about 16.

## To do

What is not yet drawn as the client draws it is in [renderingWorklist.md](renderingWorklist.md).
