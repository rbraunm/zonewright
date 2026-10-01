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
- Fog is exponential squared: `fog = exp(-(density * clamp((distance - fogStart) * rangeInverse, 0, fogRange))^2)`, distance from the eye.

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

## To do

- The sun object's color and direction over the day, and the sky object's ambient.
- `SpecialAmbient` and `BounceColor` sources in `eqgame.exe`.
- Point lights: `lights.wld` and how the three per mesh are chosen.
- EQG zones (`RegionCBS1` and the other DX9 region effects) and EQ terrain (`Terrain_*`).
