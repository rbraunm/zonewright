# Server files

An EQEmu server loads three map files per zone from its `maps` folder: the collision map `base/<short>.map`, the region map `water/<short>.wtr` and the nav mesh `nav/<short>.nav`. Peridot's are EQEmu's public map pack, made with zone-utilities (azone, awater, map_edit). zonewright writes the `.map` and `.wtr` itself and reads all three, in `server/serverMapFiles.py` (pure Python); it builds the `.nav` from them with the Recast helper and inspects it as the server loads and searches it, in `server/serverNav.py`; `server/serverMapDrawing.py` draws them. Every server file derives from the zone archive's bytes, the archive the client loads. Line references are to EQEmu 4aceae1 and zone-utilities b361e63, read for behaviour only: no code is copied or translated from either (GPL).

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
| `nav/<short>.nav` | A zero tile reference or size drops the whole mesh (pathfinder_nav_mesh.cpp:473-491); a tile `addTile` rejects is lost without a word; Detour reads and writes a tile by its header's counts, never checking them against its size; map_edit drops a triangle outside its bounds or at or below z -15000, and marks a Normal region with Recast's null area, cutting a hole | `readNav` refuses a zero reference or size, a tile whose size is not the one its counts make, a count below zero, and bytes after the last tile or the zlib stream, naming the tile and its offset; `navFile` refuses to write a payload `readNav` would refuse; the helper refuses each of map_edit's drops, a region type no nav area maps, a turned region, and a failed `dtCreateNavMeshData` or `addTile`; inspect loads each tile at its stored reference, as the server does |
| NPC search | Peridot searches 1,024 nodes (Pathing:MaxNavmeshNodes); an NPC stands on the nearest polygon within (5, 100, 5), and a path's goal is the nearest within (10, 200, 10), so an NPC can snap onto mesh up to 100 above or below it | `inspectNav` reports every island apart from the safe point's piece with its snap risk, and probes paths from the safe point with the server's filter and limits; each failed probe is a finding |

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

## Nav mesh (`.nav`)

**Container:** `EQNAVMESH`, uint32 version 2, uint32 compressed size, uint32 inflated size, then zlib of: uint32 tile count, `dtNavMeshParams` (origin[3], tile width and height, maximum tiles and polygons; 28 bytes), then per tile uint32 tile reference, int32 size and the raw Detour tile.

**Tile** (Detour DNAV version 7): a 100-byte header, then vertices, polygons (32 bytes, six vertices), links (12 bytes), detail meshes, detail vertices, detail triangles, bounding-volume nodes and off-mesh connections. A polygon's area is its `areaAndType` & 0x3F.

**Reading and writing.** `readNav` decodes all of it to the last byte. It refuses a zero tile reference or size, naming the tile; a tile whose size is not the one its header's counts make (Detour's `addTile` reads and writes by the counts and never checks them), and a count below zero; and bytes after the last tile or the zlib stream. `navPayload` and `navFile` encode it back, and `navFile` first reads the payload as `readNav` does, so nothing is written that the reader would refuse. Peridot's six reference navs re-encode to identical payloads; Highpass Hold's has origin (-839.33337, -410.35071, -1650.574), 409.6-unit tiles, 64 maximum tiles, 65,536 maximum polygons, and 28 tiles whose polygons are 7,692 Normal (0), 452 Water (1) and 53 Disabled (11).

### Building

`serverNav.navBytes(mapBytes, waterBytes)` builds the `.nav` map_edit would build from a zone's `.map` and `.wtr`: `navFromCollision` over the collision `mapCollision` rebuilds and the records `readWater` reads. The Recast helper (README, Recast helper) builds the tiles with map_edit's defaults (`serverNavSettings`, recovered from Highpass Hold's header and project; they describe the NPC, never the player):

| Setting | Value |
|---|---|
| cell size / cell height | 0.8 / 0.4 |
| agent height / radius / climb | 6.55 / 1.31 / 6.55 |
| max slope | 60 |
| region minimum / merge | 8 / 20 |
| edge length / error | 12 / 1.3 |
| vertices per polygon | 6 |
| detail sample distance / error | 18 / 1 |
| tile size / border | 512 cells / 5 cells |
| partitioning | watershed |

The bounds are the collidable extents. The grid is map_edit's: (int)(extent / cell size + 0.5) cells each way, in tiles of 512; the tile bits are counted over the whole grid, not the tiles that hold polygons (Highpass Hold: 5 x 9 = 45 grid tiles, 6 tile bits, 16 polygon bits, 28 tiles built). Each `.wtr` record marks a volume after erosion: Water 1, Lava 2, PvP 4, Slime 5, Ice 6, VWater 7, GeneralArea 8 and PreferPathing as Prefer 10; a zone line becomes Disabled 11, as map_edit has no ZoneLine case. A Normal or DisableNavMesh record, an unknown type, and a turned record are refused. Islands are kept, and no bounding-volume tree is built. Tiles build on parallel threads and are added in (ty, tx) order, so tile references, file order and bytes are the same run to run; map_edit added tiles as its threads finished, so Peridot's references and order are never compared, and tiles are matched by their header's (x, y, layer).

**Against Peridot.** From Peridot's own `.map` and `.wtr`, Highpass Hold's 28 tiles and thulehouse2's 22 match Peridot's tile for tile: the parameters, every header but its detail counts, the vertices, the polygons, and the links between them. The detail meshes differ: the pinned Recast (EQEmu `710dabe`) carries upstream `13dc549` ("Improve triangulateHull"), which the 2017 copy that built Peridot's navs lacked, so 4,709 of Highpass Hold's 8,197 polygons and 1,865 of thulehouse2's 3,560 have another detail triangulation. thulehouse2's nav was built from a map_edit project (`thulehouse2.navprj`) whose bounds' top was lowered by hand to 255.2, below the collision's 270.25, so map_edit dropped the two triangles reaching above it, which also set the collision's x and y extents; its test gives the helper the project's bounds and the triangles map_edit kept. Exports always take the collidable extents.

From our own Highpass Hold `.map` (the client's archive and loose `.zon`), the nav has Peridot's parameters and 28 tiles, and the number of tiles whose polygons differ from Peridot's is 0: the file is byte-identical to the nav of Peridot's `.map`. Our turns are the `.zon`'s and Peridot's are azone's round trip of them (up to 2.4e-7 rad apart, which moves 33,184 of the 272,230 collision triangles by up to 3.2e-4), and no voxel changes. Every built tile holds placed-model triangles (five hold nothing else), so no tile's input is bit-identical to Peridot's, and all 28 are compared.

### Inspecting

`serverNav.inspectNav(navFile, safePoint, targets)` loads a `.nav` as the server does (each tile at the slot its stored reference names) and labels its polygons into components across links, cross-tile links included, under the server's ground filter: Disabled and ZoneLine polygons belong to none. The main piece is the component of the polygon nearest the safe point within (5, 100, 5), the search that decides the polygon an NPC stands on; when no polygon lies within it, the largest component stands in and the result says so. Every other component is an island, numbered by area, with its bounds, its center (the middle of its largest polygon) and its snap risk: some main-piece polygon lies within 100 vertically over its footprint, where the server's nearest-polygon search can put an NPC onto it.

A path probe runs a Detour search with Peridot's limits (1,024 nodes, a 256-polygon path, the server's area costs, goals the nearest polygon the filter allows within (10, 200, 10)) from the safe point to each target. A partial or failed path is a finding: "NPCs cannot path from the safe point to X within Peridot's 1,024 search nodes", with the reason.

On Highpass Hold, from the safe point (zone -148, -219, -24), our nav and Peridot's give the same answer: of 8,197 polygons, 53 are Disabled; the main piece holds 3,677 polygons, and there are 640 islands, 297 of them at snap risk; the two largest are the backdrop mountains west and east of the pass. Of the five zone lines, records 4 and 5 are reached, records 2 and 3 at the pass's far ends are past what 1,024 nodes search (partial paths of 46 and 145 polygons), and record 6's center lies more than 10 across from any polygon the filter walks, its own slab being Disabled.

## Pictures

`serverMapDrawing.drawCollision` draws a collision plan north up, as renderSketch's plans are drawn: the highest collision over each pixel shaded by height and by slope away from a north-west light (faces seen edge-on from above, such as upright walls, show only as the edges between heights), the `.wtr` boxes outlined in their type's color and numbered in file order, a grid, a scale bar and north. `drawCollisionComparison` sets two collisions side by side in one frame, each with its own `.wtr`'s boxes, with a third plan of the triangles that differ by more than a tolerance (matched in order) in red over the first faded: blank when they agree.

`drawNav` sets nav plans side by side on one frame, each titled and with its legend, from `inspectNav`'s polygons: by nav area (`areaPanel`), by component with the main piece green and the islands orange and numbered (`componentPanel`), or as a difference against another nav (`differencePanel`): polygons the other lacks red, polygons only the other has outlined purple, tiles matched by (x, y, layer) and polygons by area and outline; blank when they agree.

## Reference files

Tests compare against Peridot's own files: EQEmu/maps at `fbd3b191286e0d232a7103dff4a451d2e51a819f`, which Peridot's maps are byte-identical to. `tests/serverReference.py` fetches each file `tests/serverReference.json` lists (path, size, SHA-256) into `%LOCALAPPDATA%\zonewrightTests\serverReference\fbd3b191\<path>` when missing, under a lock, and fails on any size or hash that differs. They are server data, so they stay under the test root and never go into git. Their tests are two groups of the client data tier, which also read the client's archives and loose `.zon` files in place: `serverMaps`, taken by a whole-suite run when the server map code, the zone readers it builds on (`eqgFiles`, `eqArchive`), or the fixture changed; and `serverNav`, taken when the nav code, the Recast helper's sources, the server map code and readers it builds on, the drawing, or the helper's pin, build and thread count changed.
