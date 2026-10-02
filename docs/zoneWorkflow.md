# Building a zone

How a zone is built with zonewright, and what the tools need to support it. A zone is not built in one pass: each part goes from rough to refined to detailed, and every pass is followed by looking at the result and deciding what to change, including taking work back out.

## Principles

- **Coarse to fine.** Each pass works at its own scale and resolution: big forms on a coarse grid, then refinement, then breakup and detail. Nothing is detailed before its position and shape are settled, because detail on something that moves is wasted.
- **Look after every pass.** A pass is judged from where players stand (eye-height views along the routes) and from above (layout), against the reference and against EQ scale (a 6-unit character, walkable slopes, run distances). The judgment decides the next pass, which may be a correction, a reduction, or a removal.
- **Passes stay adjustable.** A terrain pass can be turned up, turned down, or removed after later passes are made, so a bad pass costs one change, not a rebuild. Arrangements keep their transforms as data and can be regenerated or re-settled after the ground under them changes.
- **Removing is a way of adding.** Carving a path, lowering a basin, deleting a face, or dialing a noise pass back to half are as normal as raising and placing. Shapes often read better after something is taken away.
- **Layers of the zone are independent.** Terrain shape, surfaces, and placed objects change separately: re-sculpting re-seats the props on it, and retexturing does not touch shape.

## Passes

| Pass | Purpose | Works on |
|---|---|---|
| 1. Layout | The playable space at gameplay scale: size, routes, landmarks, zone lines, sightlines. Fast and disposable. | A coarse terrain grid (about 32 units), route paths, proxy blocks for landmarks and buildings |
| 2. Primary forms | Hills, valleys, ridges, cliffs, basins, and the routes graded into them; proxies replaced by building masses | Terrain at its working resolution (8 to 16 units), large brushes |
| 3. Secondary forms | Breaking regularity: no perfect cones or circles, uneven ridgelines, gullies down slopes, ledges, rock outcrops | Warp and medium-scale noise in their own passes; outcrops placed and sunk |
| 4. Surfacing | Materials by region, slope, height, and route; transitions between them; texture scale kept even | Material assignment by masks, UV projection |
| 5. Dressing | Props and clusters that make places: explicit sets placed by hand and generated sets (rows, rings, scatter) with varied scale, heading, and tilt | Linked copies of models and kit assets, settled on the ground |
| 6. Detail | Fine breakup on terrain away from routes; small props and debris | Fine noise passes masked by slope and distance from routes |
| 7. Review | Problems (floating or buried props, unwalkable route slopes, texture stretch), budgets, export round trip | Checks and views |

Any pass can send the work back to an earlier one.

## What the tools need

### Terrain passes

Each shaping operation writes into a named pass on the terrain (a Blender shape key over the terrain's base shape). Passes are listed, and each can be set to any strength, muted, removed, or collapsed into the base. Export draws the terrain as the passes combine. A change of resolution (subdividing) collapses the passes first, since a pass holds one offset per vertex.

- **Rough:** large forms from a few inputs: a mound or basin at a point, a ridge or valley along a path with a cross-section profile, a plateau.
- **Refine:** the point and path brushes (raise, lower, smooth, flatten, crease, carve), and grading a route so it reads as a path.
- **Breakup:** warp (moving vertices sideways by smooth noise, so round shapes stop being round) and roughen (fractal noise up and down, by feature size, amplitude, and octaves), each in its own pass and limited by a mask.
- **Masks** shared by every terrain and surfacing tool: slope range, height range, distance from a route, and the existing selectors.

The usual sequence for natural ground: rough forms, look, warp, look, medium roughen, look, smooth where it went too far, carve routes and channels, fine roughen masked off the routes, look. Any pass that does not help is turned down or removed.

### Terrain that is not a heightfield

EQG terrain is a mesh, not a height grid, so a zone can have near-vertical walls, overhangs, caves, and arches, and the tools have to make them:

- **Resolution where it is needed:** a grid stretched down a steep wall becomes a few long triangles, so steep areas are refined (more vertices on the wall) before they are shaped.
- **Shaping along the surface:** brushes, warp, and roughen move vertices along the surface normal, which on a wall is sideways: that is how ledges, alcoves, and overhangs are made, and how cliffs get breakup.
- **Caves:** cut into a wall along a path with an irregular cutter, then shaped and roughened inside.
- **Arches and bridges:** a rough cross-section swept along a path, then broken up, kept in the terrain so it is walked on and exported as ground.

A pass holds one offset per vertex, so refining and cutting, which change the vertices, first collapse the passes into the base. That fits coarse to fine: forms are settled on the coarse grid, collapsed, refined, and shaped further in new passes.

### Arrangement

- **Explicit sets:** one call places many linked copies of a model or kit asset, each with its own position, heading, pitch, roll, and scale, which are exactly what an EQ placement holds.
- **Generated sets:** rows (between two points, by count or spacing), grids, rings, along a route, and scatter over a region, each with ranges for heading, pitch, roll, scale, and position jitter, and a seed. The result is the same per-copy list, so a generated set can be edited copy by copy.
- **Settling:** dropping copies onto whatever is below them from above the whole scene, by footprint rather than by origin: sunk to the lowest ground under the footprint (plus an optional depth), optionally tilted toward the ground's slope by a fraction, or seated on top of a named object. Settling again after the terrain changes puts everything back on the ground.

### Review

- Framing a view on given objects, walk views at eye height along a route, and before-and-after comparisons of the same view across passes.
- Checks: props floating above or buried in the ground, route segments steeper than walkable, texture stretch and uneven texture scale, triangle and object counts.

### Surfacing and lighting from the catalog

Textures, light styles, and particle emitters come from the asset catalog ([assetCatalog.md](assetCatalog.md)): surveyed from client zones, looked at on contact sheets, and described once in a shared vocabulary, so a need ("red layered rock for steep faces") finds them and each use starts from how the client's own zones used them. EQ terrain zones' ecosystems are recipes for the surfacing pass: which texture goes on which slopes and heights, at what repeat.

### Later

Route-aware surfacing (a material along a route at a width, with the terrain cut along its edges and a transition strip) and building pieces (gable and hip roofs, openings, stairs, walls and fences that follow the ground).
