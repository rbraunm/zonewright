# zonewright

MCP server through which Claude manages and works in Blender to build EverQuest zones.

## Install (Windows)

Requires Python 3.14 on PATH as `python`, git, and Claude Code.

From the root of a clone of this repository:

```
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m pytest
claude
```

Run Claude Code from the repository root, since `.mcp.json` launches the server with paths relative to it. On first launch, accept the workspace trust prompt and approve the `zonewright` server. `claude mcp list` then shows `zonewright` as connected.

## Tooling

`toolingManifest.json` pins Blender and every extension: version, download URL, and SHA-256. `syncTooling` makes `%LOCALAPPDATA%\zonewright` match it. Blender runs in portable mode (a `portable` folder next to `blender.exe`), so it never reads or writes the config of any Blender installed for personal use. Logs rotate daily under `%LOCALAPPDATA%\zonewright\logs` and are kept 90 days.

To upgrade Blender, update the version, URL, and SHA-256 (from the release's published `.sha256` file) and run `syncTooling`.

| Tool | Does |
|---|---|
| `getToolingStatus` | Reports Blender (`missing`, `broken`, `versionMismatch`, `installed`), each pinned extension (`missing`, `versionMismatch`, `installed`), and installed extensions that are not pinned |
| `syncTooling` | Installs the pinned Blender after verifying its SHA-256, removes other Blender versions, and installs, upgrades, or removes extensions to match the manifest. Fails if Blender is running from the tooling root, or if the pinned install is `broken` or `versionMismatch` (clear it by hand) |
| `addExtension` | Pins the newest extensions.blender.org release of an extension compatible with the pinned Blender, then syncs. Fails if a different `version` is requested |
| `removeExtension` | Unpins an extension, then syncs to uninstall it |

## eqzones

Claude Code sessions started here also get `../eqzones` as a working directory (`.claude/settings.json`). eqzones holds only artist files:

| Path | Holds |
|---|---|
| `zones/<zoneName>/<zoneName>.blend` | Zone work file |
| `zones/<zoneName>/textures/` | Zone-only textures, on relative paths |
| `library/kits/<kitName>.blend` | Kit libraries of marked assets, linked into zones |
| `library/textures/<textureName>/` | Shared textures: `diffuse.png`, `normal.png`, `source.txt` (CC0 attribution) |
| `ref/<zoneName>/concept/` | Concept art |
| `ref/<zoneName>/screenshots/` | Reference screenshots |
| `ref/common/` | References shared across zones |
