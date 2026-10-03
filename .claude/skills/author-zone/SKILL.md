---
name: author-zone
description: Build or rework an EverQuest zone in zonewright the way a 3D environment artist does - planned in regions by intent, shaped and surfaced by hand pass by pass, judged in views, and steered by how the client's own zones are built - instead of applying procedural rules across the zone. Use for any zone layout, terrain shaping, surfacing, dressing, or rework, at any scale.
---

# Authoring a zone

zonewright gives you an artist's hands; use them as an artist would. The procedural tools (masks, noise, scatter, recipes) are brushes and helpers inside an area you have decided on. They are never the design. A zone surfaced by one slope rule or roughened by one noise pass everywhere reads as generated, because it was. `docs/zoneWorkflow.md` holds the method; this is the working procedure.

## The creed

- **Intent before tools.** Know what each area is to become, from the concept and references, before touching it. Write it down as a region (`createRegion`, with its intent).
- **Region by region, at any scale.** A large zone is many regions, each with its own forms, palette, and dressing. Global operations are for blocking out only, and are then broken up per region.
- **Recipes per region, decisions by hand.** The client's terrain zones put rock on steep slopes nearly everywhere, but as part of each painted area's recipe: each area has its own palette and slope thresholds, height bands target features (a sulfur band, river beds below the water line, grass above a height), and edges blend softly. Do the same: a slope or height recipe painted inside a region (`paintSurface` with the region and a mask), targeted bands, softened edges; and strokes and boundary edits for everything a rule cannot decide (`paintSurface`, `editSurface`).
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

### How the client's terrain zones use recipes

Over the 52 EQ terrain zones: a median of 5 painted ecosystems per zone (10th to 90th percentile 2 to 10); in 40 of them a slope rule applies on over 90% of tiles, with thresholds varying by ecosystem (Goru'kar Mesa 1, 25, 30, and 43 degrees; Sunderock 8, 40, and 45); height bands target features (Sunderock's sulfur ground between z 340 and 380, its river rocks below set heights; Sunrise Hills' grass above z 500); each rule has a slope and height tolerance and painted cover maps blend the result.

## Procedure

1. **References.** The concept art (`ref/<zone>/concept` in the work repository); reference zones of a similar kind found with the zone survey and looked at with `importZone`; the asset catalog's measured use of candidate textures. Name the reference zones you will compare against.
2. **Plan (layout).** Block the space out on a coarse grid; then `createRegion` for every area with a distinct intent (each terrace, the bay, each canyon wall section, the river, the plateau rim). `getRegions` is the plan; keep intents current with `editRegion`.
3. **Primary forms, per region.** Draw rock forms as the client's zones build them: broad planes, straight cliff runs, sharp corners, few strong ledges, the fine detail left to the texture. Carve, fill, and sculpt the big shapes inside each region, drawing cliff lines, slots, mesas, and terraces as outlines with straight runs and jogs (`sculptOutline`) rather than as even offsets of a path; stepped profiles (strata, ledges, terraces) take `conformBreaks` so their edges run clean. Refine the terrain where it is steep or detailed (`subdivide` with a slope selector inside the region, passes collapsed first) before shaping it further. Keep every major form in its own shaping pass. Forms with an underside, which ground shaped from above cannot hold (a natural arch, an overhanging lip, a ledge off a wall), are rock masses (`createRockMass`), their ends run into the rock they grow from; whether a form is its own piece or part of the ground depends on the landform, and either way it must read as part of the rock around it, shaped and surfaced with that rock's recipe.
4. **Secondary forms, per region.** Break regularity deliberately: jogs drawn into outlines, rock pressed into facets (`facet`), and only then a little warp or roughen confined to the region (`{"region": name}` with a `fadeDistance`), at the region's own scale; smooth noise everywhere reads as ripples, not rock. Check `foldedFaces`; turn passes down where they overdo it.
5. **Surfacing, per region.** Choose the palette from the catalog against the concept (`use-assets`). Add a surfacing layer per decision (the region's ground, a stratum band, a path, accents). Lay each region's recipe first (its ground; rock on its steep faces above that region's threshold; height bands for features), then paint by stroke what no rule decides, with `edgeNoise` so edges wander; smooth and grow or shrink edges with `editSurface` so no speckle remains near a threshold; put transition textures where materials meet.
6. **Dressing, per region.** Place sets by hand and generated sets inside regions, varied in scale, heading, and tilt, settled on the ground; hundreds of placements is normal for EQ.
7. **Review.** `compareWithClientZones` against the reference zones; walk the routes at eye height and with `walkRoute` (decks, ramps, ledges, the ways into each area: slope, footing, headroom); fix what reads wrong, region by region.

## Starting an area over

- `resetRegion`: take shaping passes back in an area, faded at its edge.
- `rebuildRegion`: span the area's heights from its surroundings (a blank slate that already fits), or level it.
- `clearRegion`: all at once, with its surfacing erased and the objects placed in it removed.
- `eraseSurface`, `setSurfaceLayer` (mute), `removeSurfaceLayer`: take surfacing decisions back.

## What not to do

- One hard slope or height rule and one palette across the whole terrain, with no areas, no bands, and speckled edges at the threshold.
- One noise pass over everything at one scale, or rock left soft and rippled by warps and roughening where it should be planes and edges.
- Many thin ledges at even offsets running parallel to a path; strata belong to heights and come and go.
- Perfect circles, straight lines, and symmetric forms left as made.
- Material edges that follow the grid; speckled faces near a threshold.
- Declaring a pass done without looking at it from where a player stands.
- Building at full detail before the layout is settled.
