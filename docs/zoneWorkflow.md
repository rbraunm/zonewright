# Building a zone

How a zone is built with zonewright, and what the tools need to support it. A zone is not built in one pass: each part goes from rough to refined to detailed, and every pass is followed by looking at the result and deciding what to change, including taking work back out.

## Principles

- **Intent before tools, region by region.** Each area of a zone is planned for what it is to become, from the concept and references, and kept as a region with its intent. Shaping, surfacing, and dressing are then done region by region at any scale. A slope or height recipe belongs to an area, as the client's terrain ecosystems do: they put rock on steep ground across most of a zone, but each painted area has its own palette and thresholds, height bands target features, and edges blend softly. Noise and scatter likewise work inside a chosen area. One hard rule with one palette across the whole zone reads as generated.
- **Steer by the EQ worlds.** How the client's own zones are built is measured (the zone survey's `construction` group) and the zone in progress is compared with them after each pass (`compareWithClientZones`), so judgment rests on EQ's practice rather than on taste alone.
- **Coarse to fine.** Each pass works at its own scale and resolution: big forms on a coarse grid, then refinement, then breakup and detail. Nothing is detailed before its position and shape are settled, because detail on something that moves is wasted.
- **Look after every pass.** Building a zone is visual iteration: a pass is judged by looking at pictures of it, from where players stand (eye-height views along the routes), from above (layout), and close on the part being worked from several sides, against the reference and against EQ scale (a 6-unit character, walkable slopes, run distances). Measurements check what a picture shows; they never replace it. The judgment decides the next pass, which may be a correction, a reduction, or a removal.
- **Passes stay adjustable.** A terrain pass can be turned up, turned down, or removed after later passes are made, so a bad pass costs one change, not a rebuild. Arrangements keep their transforms as data and can be regenerated or re-settled after the ground under them changes.
- **Removing is a way of adding.** Carving a path, lowering a basin, deleting a face, or dialing a noise pass back to half are as normal as raising and placing. Shapes often read better after something is taken away.
- **Layers of the zone are independent.** Terrain shape, surfaces, and placed objects change separately: re-sculpting re-seats the props on it, and retexturing does not touch shape.

## Passes

| Pass | Purpose | Works on |
|---|---|---|
| 1. Layout | The playable space at gameplay scale: size, routes, landmarks, zone lines, sightlines; then the regions, each with its intent. Fast and disposable. | A coarse terrain grid (about 32 units), route paths, proxy blocks for landmarks and buildings, regions |
| 2. Primary forms | Hills, valleys, ridges, cliffs, basins, and the routes graded into them; proxies replaced by building masses | Terrain at its working resolution (8 to 16 units), large brushes |
| 3. Secondary forms | Breaking regularity: no perfect cones or circles, uneven ridgelines, gullies down slopes, ledges, rock outcrops | Warp and medium-scale noise in their own passes; outcrops placed and sunk |
| 4. Surfacing | Materials painted by intent, region by region: each region's ground, strata, paths, accents; edges shaped deliberately; transitions between them; texture scale kept even | Surfacing layers painted by region and stroke, edge edits, UV projection |
| 5. Dressing | Props and clusters that make places: explicit sets placed by hand and generated sets (rows, rings, scatter) with varied scale, heading, and tilt | Linked copies of models and kit assets, settled on the ground |
| 6. Detail | Fine breakup on terrain away from routes; small props and debris | Fine noise passes masked by slope and distance from routes |
| 7. Review | Problems (floating or buried props, unwalkable route slopes, texture stretch), budgets, export round trip | Checks and views |

Any pass can send the work back to an earlier one.

## What the tools need

### Regions

A region is a vertical prism over an outline with its intent written on it (`createRegion`): "north guild terrace: packed earth, dwellings carved into the back wall". Regions are seen in Blender and never rendered or exported. The `{"region": name}` selector confines every tool to one, so shaping, surfacing, and dressing happen inside a decided area.

### Sketches

An artist sketches a layout before and while building it, to see what might work: where buildings stand and face, the yards and lanes between them, the arrival point, a dungeon's rooms. `sketch` draws shapes on named sheets (areas, footprints with heights and facings, paths with widths, points, notes) and measures each against the ground under it (its slope, cut and fill to its floor, water, overlaps and gaps to its neighbors), and `renderSketch` draws the sheets with the regions, plots, and water over a grey relief map. A footprint with a height stands in views as a plain block, so a layout can be judged at eye height before anything is built. Sketches stay in the file and are revised like any other work, but they are aids, not the plan: nothing holds the zone to them, export leaves them out, and players do not stand on them. Reach for them when the next step or how to improve an area is unclear.

### Surfacing layers

A terrain's face materials are composed from named, ordered layers (`addSurfaceLayer`): each face shows the topmost unmuted layer covering it, else the materials it had before layering. A layer is painted by region, along a stroke, or at a point (`paintSurface`), with noise on its edge so it wanders as a painted edge does; its edges are grown, shrunk, smoothed, or cleaned of small islands and holes (`editSurface`); it is erased, muted, reordered, or removed to take a decision back. This matches how most of the client's EQG terrains are surfaced: per-face materials in clean regions whose borders follow modeled edges. A painted border can only follow the mesh's edges, so it is brought onto edges along a smooth line (`conformSurfaceEdges`: vertices off creases slide along their edges onto the evened border, in every shaping pass, carrying UVs), and where a band or strip must end at a set height or distance from a border, the faces are cut there first (`cutContours`). A transition texture goes on as a strip along a border (`paintTransition`), its bottom edge on the border and repeating along it.

### Taking an area back

`resetRegion` takes shaping passes back inside an area, faded at its edge; `rebuildRegion` spans the area's heights smoothly from the ground around it (a blank slate that already fits) or levels it; `clearRegion` does both kinds at once along with erasing its surfacing and deleting the objects placed in it. Every EQG zone has one terrain mesh, so areas are reworked inside it rather than split into separate meshes.

### Measured against the client

The zone survey's `construction` group measures every client zone: terrain triangle density, textures on the terrain, the share of terrain triangles in one- or two-triangle material islands, steep share, steep area on terrain rather than placed models, painted share, and placements. `compareWithClientZones` measures the open scene the same way and places each measure among chosen client zones by percentile. The `author-zone` skill lists the EQG distributions.

### Terrain passes

Each shaping operation writes into a named pass on the terrain (a Blender shape key over the terrain's base shape). Passes are listed, and each can be set to any strength, muted, removed, or collapsed into the base. Export draws the terrain as the passes combine. A change of resolution (subdividing) collapses the passes first, since a pass holds one offset per vertex.

- **Rough:** large forms from a few inputs: a mound or basin at a point, a ridge or valley along a path with a cross-section profile, a plateau.
- **Refine:** the point and path brushes (raise, lower, smooth, flatten, crease, carve), and grading a route so it reads as a path.
- **Breakup:** warp (moving vertices sideways by smooth noise, so round shapes stop being round) and roughen (fractal noise up and down, by feature size, amplitude, and octaves), each in its own pass and limited by a mask.
- **Masks** shared by every terrain and surfacing tool: slope range, height range, distance from a route, regions, and the existing selectors; they pick within an area chosen by intent.

The usual sequence for natural ground: rough forms, look, warp, look, medium roughen, look, smooth where it went too far, carve routes and channels, fine roughen masked off the routes, look. Any pass that does not help is turned down or removed.

### Terrain that is not a heightfield

EQG terrain is a mesh, not a height grid, so a zone can have near-vertical walls, overhangs, caves, and arches, and the tools have to make them:

- **Resolution where it is needed:** a grid stretched down a steep wall becomes a few long triangles, so steep areas are refined (more vertices on the wall) before they are shaped.
- **Shaping along the surface:** brushes, warp, and roughen move vertices along the surface normal, which on a wall is sideways: that is how ledges, alcoves, and overhangs are made, and how cliffs get breakup.
- **Caves:** cut into a wall along a path with an irregular cutter, then shaped and roughened inside.
- **Arches and bridges:** what is left of the layered rock where the gorge cut under it, not something laid across: the top is the plateau's own surface, the sides are the gorge walls carried on, the strata run through it and into the walls, and the opening is cut from below as one broad curve, the lintel thickening into the abutments. It is its own piece in the terrain, so it is walked on and exported as ground, with its hidden parts running into the rock around it.

A pass holds one offset per vertex, so refining and cutting, which change the vertices, first collapse the passes into the base. That fits coarse to fine: forms are settled on the coarse grid, collapsed, refined, and shaped further in new passes.

### Water

Water is laid in body by body, each a named object rebuilt from what it was made from whenever it changes, against the ground as it is then: a pool floods from a point to a level over the ground below it; a river follows a path of falling levels over the ground below its level within reach of the path, cut square at its ends; a fall hangs from a lip, turning over the edge and arcing out as it drops. Their surfaces reach a little under their banks so no seam shows at the waterline. A broad tool makes a starting point (a flood, a river along a whole channel); strokes and edits shape it (a removed stroke across a leak, a raised level, a moved lip), with a look after each. Where players swim is designed apart from the surfaces, once the water and its bed settle: swim volumes are boxes (the `.zon`'s `AWT_` water and `ALV_` lava regions) that a generator starts for a body (`buildSwimVolumes`) and an artist adjusts or places by hand, and they need not match any surface (a floating pool is design). Export writes them as they stand and derives none; a body changed since its boxes were accepted, or with no boxes and not marked not swimmable, is listed. Sections (`renderSection`) show boxes against the surface and the bed. The surfaces export with the client's own water and waterfall shaders.

### Housing

A zone decides its housing before any plot exists: whether it has any, how central it is, what it is for, where the server hosts it (the public zone or instanced neighborhoods), how many plots, how they are priced, and its main routes. Plots are then placed one at a time, each an outline with the client's own border model as players will see it, graded level in its own shaping pass, assessed where it lies (the ground, drops and walls around it, water, view, seclusion, how much of the main routes see it), and priced from the zone's rules and its features. Export writes the zone's housing file in the shape Peridot's housing reads (plots, their border doors, prices, capacities) and lists the border models' archive for the zone.

### Arrangement

Planned; today placeOnSurface, duplicateObjects, and scatterInRegion cover part of it.

- **Explicit sets:** one call places many linked copies of a model or kit asset, each with its own position, heading, pitch, roll, and scale, which are exactly what an EQ placement holds.
- **Generated sets:** rows (between two points, by count or spacing), grids, rings, along a route, and scatter over a region, each with ranges for heading, pitch, roll, scale, and position jitter, and a seed. The result is the same per-copy list, so a generated set can be edited copy by copy.
- **Settling:** dropping copies onto whatever is below them from above the whole scene, by footprint rather than by origin: sunk to the lowest ground under the footprint (plus an optional depth), optionally tilted toward the ground's slope by a fraction, or seated on top of a named object. Settling again after the terrain changes puts everything back on the ground.

### Review

Views come first: renderView at eye height, from above, close, and framed on given objects; views all the way round them (renderOrbit); plans (renderSketch) and sections (renderSection); and the same view before and after a change with what changed (compareRenders). Checks confirm what the views show: walkRoute for slope, footing, and headroom along a route, compareWithClientZones for construction against the client's zones. Planned: walk views along a route, and checks for props floating above or buried in the ground, texture stretch, and uneven texture scale.

### Surfacing and lighting from the catalog

Textures, light styles, and particle emitters come from the asset catalog ([assetCatalog.md](assetCatalog.md)): surveyed from client zones, looked at on contact sheets, and described once in a shared vocabulary, so a need ("red layered rock for steep faces") finds them and each use starts from how the client's own zones used them. EQ terrain zones' ecosystems are recipes for the surfacing pass: which texture goes on which slopes and heights, at what repeat. Their artists painted which ecosystem covers which area, and the rules apply only inside it.

### Later

Route-aware surfacing (a material along a route at a width, with the terrain cut along its edges and a transition strip) and building pieces (gable and hip roofs, openings, stairs, walls and fences that follow the ground).
