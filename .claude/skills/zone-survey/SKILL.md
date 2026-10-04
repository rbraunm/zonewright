---
name: zone-survey
description: Answer questions about existing EverQuest zones (size, polygons, layout, enclosure, water, textures, models, character) from the zonewright survey instead of raw client files. Use whenever a question compares or characterizes EQ zones, or when picking reference zones for scale or design.
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
| `regions` | Water, lava, zone-line, and other regions from the zone data |
| `construction` | How the zone is built: terrain triangles and density, textures on the terrain, the share of terrain triangles in material islands of one or two triangles (vertices welded by position), the terrain's steep share, how much steep area is terrain rather than placed models, and how much ground is painted from palette maps or blended |

- `surveyZones(zones, groups, sortBy, limit)` returns rows for the named zones, or every zone when `zones` is omitted, sorted descending by a dotted path such as `dimensions.triangleCount`. Request only the groups the question needs.
- `getZoneSurvey(zone)` returns every group for one zone, both lanes.
- `getZoneNotes(zone)` returns Brewall map labels: place names for design notes only. Brewall maps are never geometry or scale.

Each zone variant is keyed `zone:format` (`wld`, `eqgz`, `eqtzp`); some zones ship both a classic and an EQG version. A row with `error` is a variant whose files the parsers cannot read; report it, do not guess its values.

**Interpretive lane** — Claude's own reading of a zone from renders and the technical data: zone type, character, areas, landmarks. It is token-heavy. Run it on your own judgment when a question needs it for a zone or a few zones. Before running it on a large sample, warn the user that it will take significant time and tokens through the MCP, and wait for them to agree. Its procedure is not written yet: zone rendering exists (importZone, renderView), and a pilot on three or four contrasting zones measuring tokens, time, and screenshots per zone comes before any larger run.

## Caching

Values are cached per zone variant, keyed by the SHA-256 of every source file and by each group's own version. Changing one group's method (its version in `server/surveyFields.py`) recomputes only that group; the other groups, and every interpreted group, keep their cached values.

## Growing the survey

If answering questions keeps requiring raw zone files for the same kind of fact, propose adding it to the survey as a new group or field: a technical group in `server/surveyFields.py` when it can be measured, an interpreted field when it needs judgment.
