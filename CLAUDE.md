# zonewright

MCP server that gives Claude hands and eyes in Blender to build EverQuest zones the way a 3D environment artist would. The global `~/.claude/CLAUDE.md` standards apply; these rules add to them.

## Roles
- Claude does all the work. The owner supplies concept art, references, and approvals; never ask the owner to edit anything in Blender.
- The .blend is the source of truth. Work like an artist through the toolkit's purpose-built tools, not a generator that rebuilds the scene. `runPython` is the fallback for what no tool covers yet; when it covers the same kind of operation twice, propose a tool for it (its calls are logged and counted in `getToolingStatus`).

## Work files
zonewright is a standalone MCP server; it is not tied to any particular work repository. Tools take full paths to .blend files wherever they live. A work file holds only what an artist would save: textures are separate files on paths relative to the .blend, never packed.

## Server rules
- stdout is the MCP protocol channel; nothing else prints to it. Logs go to a rotating file under `%LOCALAPPDATA%\zonewright\logs`, per the global logging standard.
- Long operations (downloads, renders) send MCP progress notifications.
- Every image a tool returns comes back inline as MCP image content and is also written to a file under the tooling root, with the path in the result. Size images to stay well inside Claude Code's MCP output limits.
- Expected tool failures raise `ToolError`; any other exception reaches the client as a bare "Error executing tool" with the reason hidden.
- Blender 5.x changed parts of the Python API. Check the 5.2 API docs instead of relying on memory.
- Keep the README tool table current as tools land.
- Per-machine performance settings (GPU backend, worker counts) are discovered by tools during setup (`syncTooling` profiles the machine), never hard-coded or hand-configured.
- No stale code: the server refuses tools once its own source changed (reconnect with /mcp); the bridge restarts Blender on changed bridge code when the open file is saved.

## EverQuest reference
- Scale and layout come only from the client's actual zone files. Brewall maps are design notes (place names), never geometry or scale.
- Client content (extracted zones, models, textures) stays under the tooling root, never in a work repository or git.
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
