# Server files

An EQEmu server loads three map files per zone from its `maps` folder: the collision map `base/<short>.map`, the region map `water/<short>.wtr` and the nav mesh `nav/<short>.nav`. Peridot's are EQEmu's public map pack, made with zone-utilities (azone, awater, map_edit). zonewright writes the `.map` and `.wtr` itself and reads all three, in `server/serverMapFiles.py` (pure Python); `server/serverMapDrawing.py` draws them. Every server file derives from the zone archive's bytes, the archive the client loads. Line references are to EQEmu 4aceae1 and zone-utilities b361e63, read for behaviour only: no code is copied or translated from either (GPL).

## Frames

| Frame | x | y | z |
|---|---|---|---|
| Zone (Blender, `.zon`, `.ter`, `.mod`, `.wtr` records, `.map` model vertices and placements) | x | y | z up |
| Server (`.map` collidable vertices and the rebuilt collision) | zone y | zone x | z |
| Recast and Detour (`.nav`) | zone y | zone z | zone x |

`inZoneAxes` and `inRecastAxes` turn server-axis points into the other two.

## How each server input fails, and what answers it

| Input | Quiet failure | Answer here |
|---|---|---|
| `base/<short>.map` | The inflate result is unchecked (map.cpp:456); a placement naming no model is skipped (:621); with no file, line of sight always passes, Z is never fixed, NPCs move straight, and fear dereferences null | `mapBytes` derives every size; `readMap` refuses a stream that ends before its last block, bytes after it, and an inflated size other than the header's, and decodes the payload to the last byte; `mapCollision` refuses a placement naming no model |
| `water/<short>.wtr` | "Loaded Water Map" is logged when parsing failed and the map was dropped (water_map.cpp:48-52, 59-63); no file or a bad one turns nav pathing off (mob_movement_manager.cpp:1035) | `waterBytes([])` is the valid 18-byte empty file; `readWater` decodes to the exact length and refuses a file cut short |
| `nav/<short>.nav` | A zero tile reference or size drops the whole mesh (pathfinder_nav_mesh.cpp:473-491) | `readNav` refuses either, naming the tile and its offset |

## Collision map (`.map` V2)

**File:** uint32 `0x02000000`, uint32 compressed size, uint32 inflated size, then a zlib stream, all little-endian. `mapFile` deflates at zlib's default level; the server inflates any valid stream.

**Payload, in order** (azone map.cpp:54-370):

1. **Header, 40 bytes:** uint32 collidable vertex count, collidable index count, non-collidable vertex count, non-collidable index count, model count, placement count, then placement groups, terrain tiles and quads per tile (all 0 for an EQG zone), and float32 units per vertex 0.0.
2. **Collidable vertices** (float32[3], server axes) **and indices** (uint32, three per triangle).
3. **Non-collidable vertices and indices,** which the server skips (map.cpp:527).
4. **Models,** bytewise ascending by name (azone keeps them in a `std::map`). Each is its map name and a 0 byte, uint32 vertex and polygon counts, float32[3] vertices in model space as the `.mod` stores them, then 13-byte polygons: uint32 v1, v2, v3 and uint8 `vis`, 0 when the triangle's flags have 0x1 (players pass through it), else 1. Only models some non-terrain placement places are written.
5. **Placements,** in `.zon` order, terrain left out. Each is its model's map name and a 0 byte, float32 x, y, z from the `.zon`, the turns about X, Y and Z (the `.zon` fields 2, 1 and 0), and the scale three times.

**Map names.** A model's map name is the `.zon` model entry's own spelling of its file name with `)` written `_` (eqg_loader.cpp:103-106), and each placement carries its entry's (:138), so `eqgFiles.parseZone` gives each placement its entry's spelling (`modelFileName`) beside the lowercased name. The server matches placements to models by that string exactly and skips a mismatch (map.cpp:621). Two entries spelling one file differently are two models of the same shape, as azone writes them. Highpass Hold holds `OBJ_Forge_Bellowss_A_.MOD`.

**Terrain.** A placement whose name starts `TER` or whose map name ends `.ter` (both case-sensitive, azone map.cpp:660) is baked into the lists and not placed; its position is ignored, as the client ignores it. Its triangles go in file order, corners swapped into server axes: those flagged 0x1 to the non-collidable list, the rest to the collidable list. Each list keeps each distinct float32 position once, in first-seen order, -0.0 the same as 0.0 and the first spelling kept (azone's `AddFace`). Boundaries (material -1, flag 0) are collidable, as the client's walls are.

**Turns.** The writer stores each turn in radians exactly as the `.zon` stores it. azone converted through degrees with pi as 3.14159, and Peridot's maps hold its result: `degrees = turn x float32(180 / 3.14159)`, then `degrees x float32(3.14159 / 180)`, each one float32 multiply by a float32 constant. Its build folded `* 3.14159f / 180.0f` into one multiply; computed as written, in float32 or in double, it misses over a quarter of the fields. That arithmetic reproduces every turn in Peridot's Highpass Hold map bit for bit (3,489 fields, 1,026 of which differ from the `.zon`'s by up to 2.4e-7 rad), and in thulehouse2's, freeporteast's and freeportwest's. The tests pin it there; the writer does not repeat the error.

**Refused,** naming the placement or model:

- a placement whose model the zone's files do not hold (azone would drop it, and the server would have no collision for it);
- two models whose map names coincide (the server would place one for both);
- a placement named `TER...` of a model that is not a terrain (azone would bake it unplaced where the client places it), and a terrain whose file ends `.TER` placed under another name (azone would place it where the client draws it at its own vertices);
- a number that is not finite.

**Zone files.** `zoneFilesOf(archiveBytes, looseZon)` gathers the `.zon` (the archive's one `.zon`, or the loose `.zon` the client loads in its place) and every model file it names that the archive holds. Models only another archive holds are not gathered yet, so a zone placing them is refused: guildhall's loose `.zon` places six.

**Against Peridot.** From the client's archive and loose `.zon`, `mapBytes` gives Highpass Hold's, thulehouse2's, freeporteast's and freeportwest's maps with the same inflated size and every payload byte identical but the turns, which equal azone's round trip of ours.

## Server-style rebuild

`mapCollision(mapBytes)` gives the triangles the server collides with (map.cpp LoadV2), float32 in server axes: the collidable list as stored, then each placement's `vis` polygons turned about X, then Y, then Z by its three turns, scaled, moved and swapped into server axes, one float32 operation at a time. A placement naming no model raises where the server would skip it. It is written from the format; nothing is translated from EQEmu.

On Highpass Hold it gives 272,230 triangles in the order the zone reader (`eqgFiles.placeVertices`) gives them: the 73,565 terrain triangles exactly, the placed ones within 6.7e-5 from our map and within 3.23e-4 from Peridot's (azone's turns).

## Region map (`.wtr` V2)

**Layout:** `EQEMUWATER`, uint32 version 2, uint32 count, then 52-byte records: uint32 type, float32 position[3], rotation[3] (degrees), scale[3] and half extents[3], all in zone axes. The server tests a point against the boxes in file order and takes the first that holds it, so a zone line inside a swim box reads as water.

**Records** (`waterBytes(regions)`, the `.zon` regions as `eqgFiles.parseZone` reads them, in their order): the center, rotation 0, scale 1, and the half extents as stored. Ours are positive; client files hold negative ones, which the server swaps into its box's low and high (oriented_bounding_box.cpp:74), so signs pass through.

**Types,** by prefix, from zonewright's own table (`waterRegionTypes`), never awater's (which writes an unknown prefix as Water). Prefixes match in their case, as awater matches them (water_map.cpp:258); how the client reads a prefix in another case is untraced, so `awt_` is refused as unknown.

| Prefix | Type | Evidence |
|---|---|---|
| `AWT_` | 1 Water | zonewright's swim boxes; Peridot's Highpass Hold, thulehouse2 and freeport files |
| `ALV_` | 2 Lava | zonewright's lava swim boxes |
| `ATP_` | 3 ZoneLine | zonewright's zone lines; Peridot's files |
| `APK_` | 4 PvP | freeporteast.zon's `APK_01` is type 4 in Peridot's freeporteast.wtr |

**Refused,** naming the region: an unknown prefix, a turned region (zonewright writes none, and the `.zon` turn's unit is unsettled), a zero half extent, and a number that is not finite. With no regions the file is 18 bytes and valid, which keeps nav pathing on.

**Against Peridot.** Highpass Hold's (382 bytes), thulehouse2's, freeporteast's and freeportwest's `.wtr` files are byte-identical to ours from their client `.zon` once every region's turn is written 0, as the awater that wrote them did; guildhall's is the empty file.

`readWater` recognizes and refuses a V1 file (the BSP tree of an S3D zone, such as qeynos2's).

## Nav mesh container (`.nav`)

**Container:** `EQNAVMESH`, uint32 version 2, uint32 compressed size, uint32 inflated size, then zlib of: uint32 tile count, `dtNavMeshParams` (origin[3], tile width and height, maximum tiles and polygons; 28 bytes), then per tile uint32 tile reference, int32 size and the raw Detour tile.

**Tile** (Detour DNAV version 7): a 100-byte header, then vertices, polygons (32 bytes, six vertices), links (12 bytes), detail meshes, detail vertices, detail triangles, bounding-volume nodes and off-mesh connections. A polygon's area is its `areaAndType` & 0x3F.

`readNav` decodes all of it to the last byte and refuses a zero tile reference or size, naming the tile; `navPayload` and `navFile` encode it back. Peridot's six reference navs re-encode to identical payloads; Highpass Hold's has origin (-839.33337, -410.35071, -1650.574), 409.6-unit tiles, 64 maximum tiles, 65,536 maximum polygons, and 28 tiles whose polygons are 7,692 Normal (0), 452 Water (1) and 53 Disabled (11).

## Pictures

`serverMapDrawing.drawCollision` draws a collision plan north up, as renderSketch's plans are drawn: the highest collision over each pixel shaded by height and by slope away from a north-west light (faces seen edge-on from above, such as upright walls, show only as the edges between heights), the `.wtr` boxes outlined in their type's color and numbered in file order, a grid, a scale bar and north. `drawCollisionComparison` sets two collisions side by side in one frame, each with its own `.wtr`'s boxes, with a third plan of the triangles that differ by more than a tolerance (matched in order) in red over the first faded: blank when they agree.

## Reference files

Tests compare against Peridot's own files: EQEmu/maps at `fbd3b191286e0d232a7103dff4a451d2e51a819f`, which Peridot's maps are byte-identical to. `tests/serverReference.py` fetches each file `tests/serverReference.json` lists (path, size, SHA-256) into `%LOCALAPPDATA%\zonewrightTests\serverReference\fbd3b191\<path>` when missing, under a lock, and fails on any size or hash that differs. They are server data, so they stay under the test root and never go into git. Their tests are the `serverMaps` group of the client data tier, which also reads the client's archives and loose `.zon` files in place; a whole-suite run takes it when the server map code, the zone readers it builds on (`eqgFiles`, `eqArchive`), or the fixture changed.
