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

## Camera

Measured by aligning renders to live client screenshots (Plane of Knowledge twice, Eastern Wastes once), each cropped about its center to 16:9:

- **Field of view:** 46.5 degrees vertical; the three shots scaled to 45.3-47.3 at 52.
- **Pitch:** 4.5 degrees below the pitch the screenshot's name records (-3.7 to -5.5 measured).
- **Heading:** as recorded (within 1.5 degrees).
- **Eye:** 0.66 above `/loc`'s z, from the Palatial Guild Hall shot; 2D alignment cannot separate eye height from pitch, so this rests on that shot.

The Eastern Wastes shot at `-1994.17, 3027.87` matches in heading but sits about 15 degrees off in pitch; its recorded `/loc` or pitch likely was not taken at the moment of the screenshot, and it is left out.

## Calibration

`calibrateShot` renders a screenshot's view as passes (lit, texture, normal, baked light, share, distance) and solves the classic lighting and fog formula over a sample of the pixels: for each sun direction (and, without the zone header, each fog start and end at the client's density), the ambient, sun, bounce (and fog) colors are linear in every pixel and are solved by least squares per channel. Two ambiguities are settled by convention, not measured:

- Special ambient adds exactly like ambient where every surface takes the full share of scene light, so it is folded into ambient.
- A sun from one direction lights exactly as a bounce from the opposite one, so the sun is searched above the horizon and light from below is bounce.

Where no surface in view is fully fogged, a slightly nearer fog end with a slightly darker fog color draws nearly the same image, so a fitted fog is only as precise as the shot allows. The passes come back premultiplied by coverage and with negative values clamped to 0: the normal pass is encoded as normal * 0.5 + 0.5, and colors are divided by coverage before fitting.

Results: the two Plane of Knowledge night shots redraw within about 11-13 levels of 255 on average (no sky drawn), and the Eastern Wastes day shot, with its fog fitted (start near 0, end near 1900), within about 16.

## To do

- The sky: the client draws a sky dome (stars and moon at night) where the preview shows the fog color.
- Point lights: the shader's three point lights per mesh (dynamic lights such as a player's light source) are not drawn; the static torch light on Plane of Knowledge's walls is baked into their vertex colors and is drawn.
- The sun object's color and direction over the day, and the sky object's ambient.
- `SpecialAmbient` and `BounceColor` sources in `eqgame.exe`.
- Point lights: `lights.wld` and how the three per mesh are chosen.
- EQG zones (`RegionCBS1` and the other DX9 region effects) and EQ terrain (`Terrain_*`).
