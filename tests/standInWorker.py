"""A worker for the front's tests: its tools and instructions come from the JSON file named on its command line, read at start, so
rewriting the file and reloading stands in for changed code. Each tool answers its name and version; slow waits until the file named by
its release argument exists; progressing reports progress twice; handOverBridge returns the definition's record, and adoptBridge writes
the record it is given to the definition's adoptedPath."""
import json
import os
import sys
import time

from mcp.server.mcpserver import Context, MCPServer

with open(sys.argv[1], encoding="utf-8") as definitionFile:
  definition = json.load(definitionFile)
if definition.get("failToStart"):
  raise SystemExit("this build does not start")
server = MCPServer(name="standIn", instructions=definition["instructions"])


def answering(name):
  def tool(text: str = "") -> str:
    return f"{name} {definition['version']} {text}".strip()
  return tool


for toolName in definition["tools"]:
  server.add_tool(answering(toolName), name=toolName, description=f"{toolName} of version {definition['version']}")


def slow(release: str) -> str:
  while not os.path.exists(release):
    time.sleep(0.05)
  return "released"


async def progressing(context: Context) -> str:
  await context.report_progress(1, 3, "one")
  await context.report_progress(2, 3, "two")
  return "progressed"


def handOverBridge() -> str:
  return json.dumps({"record": definition.get("record")})


def adoptBridge(record: dict) -> str:
  with open(definition["adoptedPath"], "w", encoding="utf-8") as adoptedFile:
    json.dump(record, adoptedFile)
  return json.dumps({"adopted": record})


server.add_tool(slow, name="slow")
server.add_tool(progressing, name="progressing")
server.add_tool(handOverBridge, name="handOverBridge")
server.add_tool(adoptBridge, name="adoptBridge")
server.run()
