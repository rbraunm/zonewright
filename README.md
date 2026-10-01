# zonewright

MCP server through which Claude manages and works in Blender to build EverQuest zones.

## Install (Windows)

Requires Python 3.13 on PATH as `python`, git, and Claude Code.

From the root of a clone of this repository:

```
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m pytest
claude
```

Run Claude Code from the repository root, since `.mcp.json` launches the server with paths relative to it. On first launch, accept the workspace trust prompt and approve the `zonewright` server. `claude mcp list` then shows `zonewright` as connected.

## Tooling

`toolingManifest.json` pins every tool zonewright manages. Installs live under `%LOCALAPPDATA%\zonewright`.

| Tool | Does |
|---|---|
| `getToolingStatus` | Reports each managed tool's state against its pin: `missing`, `broken`, `versionMismatch`, or `installed` |
