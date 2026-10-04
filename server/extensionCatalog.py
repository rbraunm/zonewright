import json
import urllib.parse

from mcp.server.mcpserver.exceptions import ToolError

import toolingSync

catalogURL = "https://extensions.blender.org/api/v1/extensions/"
catalogPlatform = "windows-x64"
archiveHashPrefix = "sha256:"


def resolveExtension(extensionID, blenderVersion):
  query = urllib.parse.urlencode({"blender_version": blenderVersion, "platform": catalogPlatform})
  catalog = toolingSync.fetchWithRetries(f"{catalogURL}?{query}", lambda response: json.loads(response.read()))
  matches = [entry for entry in catalog["data"] if entry["id"] == extensionID]
  if len(matches) != 1:
    raise ToolError(f"extensions.blender.org lists {len(matches)} entries for '{extensionID}' compatible with Blender {blenderVersion} on {catalogPlatform}")
  entry = matches[0]
  if not entry["archive_hash"].startswith(archiveHashPrefix):
    raise ToolError(f"{extensionID}: archive_hash '{entry['archive_hash']}' is not a SHA-256 hash")
  return {"version": entry["version"], "url": entry["archive_url"], "sha256": entry["archive_hash"][len(archiveHashPrefix):]}
