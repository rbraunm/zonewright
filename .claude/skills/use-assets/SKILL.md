---
name: use-assets
description: Find and use EverQuest client assets from zonewright's asset catalog while building a zone: choosing textures for terrain and objects and applying them at the right scale, and placing lights and particle emitters. Use when surfacing terrain, texturing models, or lighting a zone.
---

# Using assets

Work from the catalog (`docs/assetCatalog.md`), not from guesses about file names. Every texture, light style, and emitter used in a zone should be found, looked at, and described in the catalog first (the `describe-assets` skill).

## Finding

- Ask in the catalog's words: `findAssets` with `categories`, `tags`, and `text` (`"red sandstone cliff"`). These search descriptions.
- Narrow by what was measured, for described and undescribed textures alike: `colors`, `minimumSide`, `tiles`, and `usedOn` (slope bands where the texture covers most of its area: `flat`, `slope`, `steep`, `vertical`, `overhang`).
- Learn from the client's own recipes: an EQ terrain zone's ecosystems (`findAssets` kind `ecosystem`) say which textures its artists put on which slopes and heights, and at what repeat. Its artists painted which ecosystem covers which area; the slope and height rules only apply inside it, and that is how to use them.
- Look before choosing: `viewTextures` with the candidates, `tiled: true`, and `cellSide: 256` for the finalists.

## Applying textures

- `createMaterial` takes catalog ids directly (`texture/<name>@<hash>`) for the diffuse and its normal map (the diffuse's `pairsWith` or `materialRoles`).
- Surface terrain by intent, region by region (the `author-zone` skill): a surfacing layer per decision (`addSurfaceLayer`), painted by region and by stroke (`paintSurface` with `{"region": ...}`, `nearPath` strokes, points), with `edgeNoise` so edges wander, and edges evened or moved with `editSurface`. A region's recipe (rock on its steep faces, a band at chosen heights) is painted with the region and a slope or height mask, as the client's ecosystems are applied; never one hard rule and one palette across the whole terrain. `assignMaterial` is for objects and blockout only.
- Scale with `projectUVs`: the description's `worldUnitsPerRepeat`, else the measured `unitsPerRepeat` of its main use. Use `box` projection on cliffs so steep faces do not stretch.
- Where two ground materials meet, use a transition texture (Phase 1 has no shader blending): look for `category: transition` or textures that tile one way (`seamRatios` low across, high down). Lay it as a strip: cut a contour at the strip's width from the border (`cutContours` with `distanceFrom`, and `onlyAbove` on both at a wall's foot), then `paintTransition`, which maps the texture's bottom edge onto the border and repeats it along the border; `projectUVs` would tile it down as well as across.
- Judge in the client's light: `renderView` with the default client shading at eye height, not only in layout shading.
- Block out with `createMaterial` greys made with `blockout: true`, so a game export refuses any left on exported faces. Before handing work off, read `checkExport` (purpose `test`) beside `renderView` with `shading: "coverage"`: they show where the base material shows, borders lack a transition strip, textures stretch or collapse, and blockout remains.

## Lights and emitters

- `placeLights` takes the colors and radii of a described light style that fits (`findAssets` kind `light`, category `torch`, `brazier`, ...). Space them as the style's zone did.
- `placeEmitters` takes a definition index from a described emitter (`findAssets` kind `emitter`); the lifespan most client lists use is 4000000. White water on a fall, river, or pool goes on with `sprayWater` instead, which keeps the emitters with the body.
- Client-shaded views draw lights and emitters as the client does: a light's pool on what it reaches (terrain takes only `LIB_` lights; name a light for terrain `LIB_`), an emitter's particles. Judge their placement at eye level, at night and by day (`setZoneProperties` `sky` hour), and through `getSceneSummary`.

## Closing the loop

`exportZone` (from the saved file, with a purpose) writes the textures (DDS unchanged), the point lights into the .zon, and the emitters to `<zone>_EnvironmentEmitters.txt`. `surveyAssets` with the exported `path` measures the result like any client zone: compare its textures' `unitsPerRepeat` and slope use with what was intended.
