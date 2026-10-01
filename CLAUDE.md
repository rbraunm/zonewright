# zonewright

MCP server that gives Claude hands and eyes in Blender to build EverQuest zones the way a 3D environment artist would. The global `~/.claude/CLAUDE.md` standards apply; these rules add to them.

## Roles
- Claude does all the work. The owner supplies concept art, references, and approvals; never ask the owner to edit anything in Blender.
- The .blend is the source of truth. Python is how Claude's hands move, not a generator that rebuilds the scene. Any operation done by hand twice becomes an MCP tool.

## eqzones
`../eqzones` holds only what an artist would save: .blend work files with textures as separate files on relative paths, a kit library of .blend files with marked assets, and a ref folder of concept art and reference screenshots. No code or Claude config goes in eqzones; binaries follow its LFS rules.

## Server rules
- stdout is the MCP protocol channel; nothing else prints to it. Logs go to a rotating file under `%LOCALAPPDATA%\zonewright\logs`, per the global logging standard.
- Long operations (downloads, renders) send MCP progress notifications.
- Every image a tool returns comes back inline as MCP image content and is also written to a file under the tooling root, with the path in the result. Size images to stay well inside Claude Code's MCP output limits.
- Expected tool failures raise `ToolError`; any other exception reaches the client as a bare "Error executing tool" with the reason hidden.
- Blender 5.x changed parts of the Python API. Check the 5.2 API docs instead of relying on memory.
- Keep the README tool table current as tools land.

## EverQuest reference
- Scale and layout come only from the client's actual zone files. Brewall maps are design notes (place names), never geometry or scale.
- Client content (extracted zones, models, textures) stays under the tooling root, never in eqzones or git.
- Blender is 1 unit = 1 EQ unit, Z up; Phase 2 export converts to EQGZI's convention.

## Tests
Tests exercise the real pinned Blender through the real bridge. No mocking the code path under test.

## Phase 1 limits
Phase 1 makes zones look right in Blender; EQ export is Phase 2. Build nothing Phase 2 can't carry:
- Fidelity target is EverQuest around 2011 (RoF2 client).
- Materials use diffuse and normal maps only.
- Foliage is cut-out (alpha-tested) cards.
- Ground texture transitions use transition textures, not shader blending.
- Budgets stay plausible for the era, kept loose until Phase 2 measures a real zone.
