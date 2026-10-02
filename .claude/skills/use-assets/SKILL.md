---
name: use-assets
description: Find and use EverQuest client assets from zonewright's asset catalog while building a zone: choosing textures for terrain and objects and applying them at the right scale, and placing lights and particle emitters. Use when surfacing terrain, texturing models, or lighting a zone.
---

# Using assets

Work from the catalog (`docs/assetCatalog.md`), not from guesses about file names. Every texture, light style, and emitter used in a zone should be found, looked at, and described in the catalog first (the `describe-assets` skill).

## Finding

- Ask in the catalog's words: `findAssets` with `categories`, `tags`, and `text` (`"red sandstone cliff"`). These search descriptions.
- Narrow by what was measured, for described and undescribed textures alike: `colors`, `minimumSide`, `tiles`, and `usedOn` (slope bands where the texture covers most of its area: `flat`, `slope`, `steep`, `vertical`, `overhang`).
- Learn from the client's own recipes: an EQ terrain zone's ecosystems (`findAssets` kind `ecosystem`) say which textures its artists put on which slopes and heights, and at what repeat.
- Look before choosing: `viewTextures` with the candidates, `tiled: true`, and `cellSide: 256` for the finalists.

## Applying textures

- `createMaterial` takes catalog ids directly (`texture/<name>@<hash>`) for the diffuse and its normal map (the diffuse's `pairsWith` or `materialRoles`).
- Assign by the terrain's own shape, as the client's recipes do: `assignMaterial` with `slope` selectors for rock on steep faces, `height` for river beds and ledges, `nearPath` for trails.
- Scale with `projectUVs`: the description's `worldUnitsPerRepeat`, else the measured `unitsPerRepeat` of its main use. Use `box` projection on cliffs so steep faces do not stretch.
- Where two ground materials meet, use a transition texture (Phase 1 has no shader blending): look for `category: transition` or textures that tile one way (`seamRatios` low across, high down).
- Judge in the client's light: `renderView` with the default client shading at eye height, not only in layout shading.

## Lights and emitters

- `placeLights` takes the colors and radii of a described light style that fits (`findAssets` kind `light`, category `torch`, `brazier`, ...). Space them as the style's zone did.
- `placeEmitters` takes a definition index from a described emitter (`findAssets` kind `emitter`); the lifespan most client lists use is 4000000.
- The preview does not draw lights or emitters yet; check their placement in layout views and through `getSceneSummary`.

## Closing the loop

`exportZone` writes the textures (DDS unchanged), the point lights into the .zon, and the emitters to `<zone>_EnvironmentEmitters.txt`. `surveyAssets` with the exported `path` measures the result like any client zone: compare its textures' `unitsPerRepeat` and slope use with what was intended.
