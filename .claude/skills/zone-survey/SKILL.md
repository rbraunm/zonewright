---
name: zone-survey
description: Answer questions about existing EverQuest zones (size, polygons, layout, enclosure, water, textures, models, character) from the zonewright survey instead of raw client files, and interpret zones (type, character, areas, landmarks) into it by its procedure. Use whenever a question compares or characterizes EQ zones, when picking reference zones for scale or design, or when a zone's interpretation is missing or stale.
---

# Zone survey

The survey caches what zonewright knows about every zone in the EverQuest client (`EVERQUEST_CLIENT`). Answer zone questions from it; read raw zone files only when the survey cannot answer.

## Two lanes

**Technical lane** — measured by the server from the zone files. Cheap: the whole client takes minutes. Use it freely, for one zone or all of them.

| Group | Holds |
|---|---|
| `dimensions` | Terrain and all-geometry size, footprint, triangle and vertex counts, triangle density, EQTZP tile shape |
| `surfaces` | Area by surface kind (solid, invisible, passable, water, lava, cutout, translucent), solid area by slope band, walkable area and elevation percentiles |
| `verticality` | A 128 x 128 grid of vertical probes: share of columns with a floor, share of floors with geometry overhead (enclosed), share with 1, 2, 3, 4+ walkable levels |
| `content` | Texture count and top textures by area, placement count, distinct models, top placed models, missing models and asset archives |
| `regions` | Water, lava, zone-line, and other regions from the zone data; an EQG zone's zone lines each with its name, the number the client reads from it, center, size, and heading (a classic or EQ terrain zone's are `None`: not read) |
| `construction` | How the zone is built: terrain triangles and density, textures on the terrain, the share of terrain triangles in material islands of one or two triangles (vertices welded by position), the terrain's steep share, how much steep area is terrain rather than placed models, and how much ground is painted from palette maps or blended |

- `surveyZones(zones, groups, sortBy, limit)` returns rows for the named zones, or every zone when `zones` is omitted, in zone name order, or sorted descending by `sortBy`, a dotted path into a requested group such as `dimensions.triangleCount`. A name that is no zone in the client comes back in `unknownZones`, the rest answered. Request only the groups the question needs.
- `getZoneSurvey(zone)` returns every measured group of the zone's variants, and its interpretation with its state.
- `getZoneNotes(zone, text)` returns Brewall map labels: those on the zone by map layer, each with the scene position it roughly marks (scene x, y = map -y, -x), and those off it (the map's legend and credits, at made-up positions) counted and named. `text` keeps the labels holding each of its words, such as `to` for the zone lines' destinations. They are design notes only; Brewall maps are never geometry or scale.
- `importZone(zone)` brings an EQG zone's zone lines in as guides (`getZoneLines`; `renderSketch` and `renderSection` draw them in their `zoneLines` layer), with no target: the zone's files never say where a zone line leads.

Each zone variant is keyed `zone:format` (`wld`, `eqgz`, `eqtzp`); some zones ship both a classic and an EQG version. A row with `error` is a variant whose files the parsers cannot read; report it, do not guess its values.

**Interpretive lane** — Claude's own reading of a zone, judged from screenshots and checked against the technical lane: what kind of zone it is, its character, its areas and how they connect, its landmarks, what defines it, and numbers worth keeping that the technical lane lacks. It is written by the procedure below and kept with `recordZoneInterpretation`. It is token-heavy. Run it on your own judgment when a question needs it for a zone or a few zones. Before running it on a large sample, warn the user that it will take significant time and tokens through the MCP, and wait for them to agree. A pilot on a few contrasting zones, measuring tokens, time, and screenshots per zone, comes before any larger run.

## Caching

Measured values are cached per zone variant, keyed by the SHA-256 of every source file and by each group's own version. Changing one group's method (its version in `server/surveyFields.py`) recomputes only that group; the others keep their cached values.

An interpretation is of the variant `importZone` draws: the classic one where a zone has one, else the EQ terrain one, else the EQG one. It is kept under the tooling root in `survey\interpretations\<zone>`, with copies of its screenshots, the SHA-256 of that variant's source files, and the version of the procedure it followed. `getZoneSurvey` reports its state:

- `current`: the zone's files and the procedure's version are as they were when it was recorded.
- `stale`: `staleBecause` names the zone files that changed (`zoneFilesChanged`) and the procedure version raised since (`procedureChanged`). It is still returned, for what it is worth, but it is never current again until it is recorded anew; nothing re-records it on its own.
- `none`: none was recorded.

## Interpretive procedure

Interpretive procedure version: 3

Raise this version with any change to the procedure, the zone types, or the fields `recordZoneInterpretation` takes. Every interpretation recorded under an older version then reads as stale, so batch changes.

### 1. Read the measurements

`getZoneSurvey(zone)` gives the variant the interpretation is of, the zone's size (`dimensions`), how enclosed it is and how many levels it stacks (`verticality`), its water and lava (`surfaces`, `regions`), the textures and models that dominate it (`content`), and how its ground is built (`construction`). `getZoneNotes(zone)` gives its place names and roughly where they are. These say where to look; the pictures say what the zone is.

### 2. Open the zone in a scratch scene

1. `newFile`. If the open file holds unsaved work, save it first; discard only the scratch scene of an earlier survey.
2. `importZone(zone)`. If it refuses the zone (a part of the client's files the renderer does not draw yet), stop: record nothing, and report the refusal as it reads.
3. `setZoneProperties` with the survey's stand-in view, since fog and view distance are the server's zone row, not the client's files: `sky` `{"type": <zone>, "hour": 13, "minute": 0}` (the client's own sky for the zone, or its `default`; the result's sky chain says which), `specialAmbientColor` [0, 0, 0], `fogOn` false, `fogStart` 0, `fogDensity` 0.33, `minClip` the zone's longest `allGeometrySize` side, `fogEnd` and `maxClip` twice that, and `newEngineZone` false (EQEmu servers send false for every zone).

The views are daylit and unfogged. An indoor zone is darker in the client: judge its mood by its baked light, textures, and forms, not by how bright the views are, and say nothing of its fog unless it is known.

### 3. Look

Directions follow the game's compass, as `/loc` and the in-game map do: north is the scene's +X and west its +Y. Map views and plans draw north up, as the in-game map does: +X up and +Y to the left, so a map's `width` runs along y.

1. **Overview.** A `renderView` map of the whole zone (`center` the middle of its extent, `width` its y size or 16/9 of its x size, whichever is more, and a margin, since a map is 16 wide to 9 high) in client shading, and the same in layout shading (`bandHeight` about a tenth of its height range) for its shape. `getZoneNotes` can hold hundreds of labels, most of them creatures and credits; plot only the place names that matter as sketch points (`sketch` sheet `brewall`, each at its label's `scenePosition`, named in letters, digits, and `_`, with the label's text as its `label`) and draw them with `renderSketch` over the same frame. For a large zone, add a `renderSketch` plan of each part: its spot heights show where the floors are and how high. Spot heights and sketch points give the highest ground players could stand on at a point: where floors are open to the sky, one `sketch` call with a point at the middle of each room (on a sheet of its own) returns each floor with no picture, but where roofs or the zone's shell lie over its floors, it returns the roof. There, take each floor's height from `pick` on a view inside the space, or from a section.
2. **Find the areas.** An area is a part players experience as a place of its own: a valley, a town, a courtyard, a cave, a shore, a dungeon wing or level. Read them off the overview and the place names; `verticality` says whether to expect enclosed ground and stacked levels. A run of rooms alike (chambers repeated or mirrored along a wing) is one area, its views taken in different rooms of it; a corridor or tunnel too narrow to look down into is a connection, not an area.
3. **Each area:** an oblique view from above onto its middle (`eye` back from it by about its width and raised by 0.6 to 1 times that, 30 to 45 degrees down, `target` its middle), showing its layout and how it meets its neighbours; and an eye-level view inside it facing its main feature or along its main route. A large or varied area takes more.
4. **Each landmark:** an oblique or eye-level view that shows it clearly; the area views often do. `pick` on the view that shows it gives its exact location.
5. **Stacked levels:** a `renderSection` along the zone's main run and one across it, where `verticality` reports levels over one another; `walkRoute` settles which way a ramp or stair runs.

Placing the cameras:

- Walls, cliffs, and ceilings are drawn from inside only, so an oblique from outside a canyon, cave, or building looks through its walls and shows floors floating in the sky. In a walled area, set the oblique's `eye` inside the space, above the floor and below the rims, or straight over an open part looking down into it.
- Some enclosed zones model their rooms' outsides as well (roofs over halls, the outer faces of walls): maps and plans of them show the roofs, and an oblique from outside shows a shell. Take every oblique from inside the room: the `eye` in a corner, about two thirds of the way from the floor to the ceiling, looking across to the far side and down to its middle.
- `standAt` [x, y] stands on the highest ground at that point, which can be a rim, a roof, or a boulder's top, and finds none over a gap between canyons. Give [x, y, z] wherever floors stack, roofs cover them, or walls are near: it searches from 3 above z to 50 below, so take z from a pick or a section, never a guess.
- A view refused for no ground, or for no room to walk the scale figure ahead, names why: move the point onto the floor, or turn along the floor. A view that misses (a camera in rock or outside the walls, its subject hidden, a face of bare rock) is retaken, never recorded. In a walled zone, expect to retake about a third of the first views.

Look at every screenshot as it comes back (renderView returns it inline; otherwise Read its file) and judge from it: an interpretation is judged from the pictures, not from the data alone. The numbers confirm scale and give what pictures cannot.

| Zone | Areas | Screenshots |
|---|---|---|
| Small | 1-3 | 6-10 |
| Medium | 4-8 | 12-24 |
| Large | 9 or more, or many levels | 25-40 or more: a map of each part and two views per area |

### 4. Connect

Work out how players get from each area to the others (gates, tunnels, ramps, bridges, stairs, drops they cannot climb back, water, teleports), which areas are hubs and which dead ends, what each overlooks, and where the zone lines lead (the `regions` group's zone lines and Brewall's "to <zone>" labels). The zone's own files do not say where a zone line leads (the server's zone points do), and Brewall names the zone by its title: `surveyZones` with a candidate short name confirms the client has it, and a destination the client lacks is left out. Look again wherever a connection is unclear: an oblique view along it, or `walkRoute`.

### 5. Write and record

Write the interpretation from the pictures, then keep it with `recordZoneInterpretation(zone, interpretation)`. It refuses an interpretation that lacks a field or a view the procedure requires, listing every problem at once. `getZoneSurvey(zone)` then shows it `current`. Positions are the scene's coordinates, as the tools report them. Leave the scratch scene unsaved.

| Field | Holds |
|---|---|
| `zoneType` | One of the zone types below: how most of the zone's walkable space is organized. Its other parts go in `areas` and `definingCharacteristics` |
| `character` | `description`: the mood and setting as the pictures show them (light, palette, materials, weather, who seems to live there); `tags`: camelCase terms for its setting (forest, swamp, desert, tundra, mountains, canyon, coast, jungle, volcanic, underground, urban, otherworldly), its mood (dark, foreboding, peaceful, eerie, majestic, desolate, bustling, sacred, menacing), and the built style or inhabitants the pictures show (ruins, castle, village, temple, camp, undead, orcs, giants, bandits). Reuse these words before coining one |
| `areas` | Each `name` (Brewall's, where it has one), `center` [x, y, z], `where` (its compass position and height in the zone), `what` (what it is, what it looks like, how big), and `connections` [{`to`, `by`}]: `to` another area's name or `zone:<short name>` for a zone line, `by` what way |
| `landmarks` | Each `name`, `location` [x, y, z], `what` (what it is and looks like), and `significance` (why it matters: a meeting point, a route marker, the skyline, a gate) |
| `definingCharacteristics` | Three to eight statements of what makes the zone itself: layout, scale, routes, skyline, palette, how it is built |
| `screenshots` | Each `path` (the PNG renderView, renderSketch, or renderSection wrote), `view` (`map`, `oblique`, `eyeLevel`, or `section`), `caption` (what it shows and from where), and `shows` (the areas and landmarks in it). It needs a map view, an oblique and an eye-level view of every area, and an oblique or eye-level view of every landmark |
| `structuredValues` | Optional: numbers worth keeping that the technical lane lacks, each `{value, unit, how}` with how it was measured: a gate's opening, a main route's length (walkRoute), a wall's height, the longest sight line. Never repeat the measured groups' sizes, counts, or textures |

### Zone types

| Type | Meaning |
|---|---|
| `openOutdoor` | Broad open land under the sky, roamed in any direction, with few structures: plains, desert, tundra, open forest |
| `channeledOutdoor` | Outdoor ground divided by cliffs, ridges, or walls into valleys, passes, canyons, or trails that set the routes |
| `outdoorWithRuins` | Outdoor ground organized around ruined or abandoned structures that hold most of what is there |
| `outdoorWithSettlement` | Outdoor ground anchored by a lived-in village, town, camp, or keep that takes up a smaller part of it |
| `city` | A settlement filling most of the zone: streets, buildings, districts |
| `fortress` | One built complex (a castle, keep, temple, or tower) walked through as courtyards, halls, and floors |
| `tightDungeon` | Enclosed, with narrow corridors and small rooms, short sight lines, and a few levels at most |
| `bigDungeon` | Enclosed and large: many wings or levels, long routes, rooms of varied size |
| `caveNetwork` | Natural caverns: irregular tunnels and chambers in rock |
| `undergroundLandscape` | A vast enclosed space that reads as a landscape: open ground under a ceiling far overhead |
| `islands` | Separate landmasses over water, sky, or void, reached by boat, bridge, swimming, or teleport |
| `underwater` | Mostly under water: swimming is the main way through |
| `plane` | An otherworldly realm not bound by natural geography: floating ground, impossible architecture, a god's domain |
| `hub` | A small gathering place built around services or travel: a lobby, bazaar, guild hall, arena, or tutorial |

## Growing the survey

If answering questions keeps requiring raw zone files for the same kind of fact, propose adding it to the survey: a technical group in `server/surveyFields.py` when it can be measured, an interpreted field when it needs judgment (a change to the procedure, so its version rises).
