---
name: author-zone
description: Build or rework an EverQuest zone in zonewright the way a 3D environment artist does - planned in regions by intent, shaped and surfaced by hand pass by pass, judged in views, and steered by how the client's own zones are built - instead of applying procedural rules across the zone. Use for any zone layout, terrain shaping, surfacing, dressing, or rework, at any scale.
---

# Authoring a zone

zonewright gives you an artist's hands; use them as an artist would. The procedural tools (masks, noise, scatter, recipes) are brushes and helpers inside an area you have decided on. They are never the design. A zone surfaced by one slope rule or roughened by one noise pass everywhere reads as generated, because it was. `docs/zoneWorkflow.md` holds the method; this is the working procedure.

## The creed

- **Intent before tools.** Know what each area is to become, from the concept and references, before touching it. Write it down as a region (`createRegion`, with its intent).
- **Region by region, at any scale.** A large zone is many regions, each with its own forms, palette, and dressing. Global operations are for blocking out only, and are then broken up per region.
- **Decide by hand, use rules as helpers.** Slope, height, and route masks pick faces inside a region you chose; paint strokes and boundary edits make the decisions (`paintSurface`, `editSurface`).
- **Rough to fine, look after every pass.** `renderView` at eye height (`standAt`) and from above (`map` with `shading: layout`); judge, then change, reduce, or remove.
- **Removing is a way of adding.** Mute or turn down passes and layers, take an area back (`resetRegion`, `rebuildRegion`, `clearRegion`, `eraseSurface`), and do it again better.
- **Steer by the EQ worlds, not taste alone.** `compareWithClientZones` after each pass (named reference zones of a similar kind work best); the asset catalog for what the client's artists used where.

## How the client's own zones are built

Measured over the 201 EQG zones of the RoF2 client (the zone survey's `construction` group):

| Measure | 10th | Median | 90th percentile |
|---|---|---|---|
| Terrain meshes per zone | 1 | 1 | 1 |
| Terrain textures | 4 | 13 | 34 |
| Terrain triangles per 10,000 square units | 8 | 244 | 5,083 |
| Terrain triangles in one- or two-triangle material islands | 0% | 0.06% | 1% |
| Terrain area steeper than 50 degrees | 44% | 72% | 82% |
| Steep area that is terrain rather than placed models | 11% | 86% | 99% |
| Terrain area painted from palette maps or blended | 0% | 0% | 11% |
| Placed objects | 1 | 378 | 2,342 |

One terrain mesh, its walls modeled into it; per-face materials laid out in clean regions whose borders follow edges the artist modeled; a few hundred to a few thousand placements. Large open zones sit at the sparse end of density; the client's busiest are dense where it matters. Painted ground (palette and blend shaders, as Wall of Slaughter does) is the exception and is outside Phase 1.

## Procedure

1. **References.** The concept art (`ref/<zone>/concept` in the work repository); reference zones of a similar kind found with the zone survey and looked at with `importZone`; the asset catalog's measured use of candidate textures. Name the reference zones you will compare against.
2. **Plan (layout).** Block the space out on a coarse grid; then `createRegion` for every area with a distinct intent (each terrace, the bay, each canyon wall section, the river, the plateau rim). `getRegions` is the plan; keep intents current with `editRegion`.
3. **Primary forms, per region.** Carve, fill, and sculpt the big shapes inside each region; stepped profiles (strata, ledges, terraces) take `conformBreaks` so their edges run clean. Refine the terrain where it is steep or detailed (`subdivide` with a slope selector inside the region, passes collapsed first) before shaping it further. Keep every major form in its own shaping pass.
4. **Secondary forms, per region.** Break regularity with warp and roughen confined to the region (`{"region": name}` with a `fadeDistance`), at the region's own scale; check `foldedFaces`; turn passes down where they overdo it.
5. **Surfacing, per region.** Choose the palette from the catalog against the concept (`use-assets`). Add a surfacing layer per decision (the region's ground, a stratum band, a path, accents). Paint by region and by stroke, with `edgeNoise` so edges wander; smooth and grow or shrink edges with `editSurface`; put transition textures where materials meet. Masks only help pick faces inside the stroke or region.
6. **Dressing, per region.** Place sets by hand and generated sets inside regions, varied in scale, heading, and tilt, settled on the ground; hundreds of placements is normal for EQ.
7. **Review.** `compareWithClientZones` against the reference zones; walk the routes at eye height; fix what reads wrong, region by region.

## Starting an area over

- `resetRegion`: take shaping passes back in an area, faded at its edge.
- `rebuildRegion`: span the area's heights from its surroundings (a blank slate that already fits), or level it.
- `clearRegion`: all at once, with its surfacing erased and the objects placed in it removed.
- `eraseSurface`, `setSurfaceLayer` (mute), `removeSurfaceLayer`: take surfacing decisions back.

## What not to do

- One slope or height rule surfacing the whole terrain.
- One noise pass over everything at one scale.
- Perfect circles, straight lines, and symmetric forms left as made.
- Material edges that follow the grid; speckled faces near a threshold.
- Declaring a pass done without looking at it from where a player stands.
- Building at full detail before the layout is settled.
