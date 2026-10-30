"""zonewright's front: the process Claude Code starts. It serves the tools of a worker process running zonewrightServer.py and
passes each call to it; reloadZonewright replaces the worker with one running the code now on disk, hands the running Blender from
the old worker to the new one so the open scene survives, and tells Claude Code the tool list changed. Claude Code cannot restart a
server on its own, so the front keeps the connection and stays small enough that it rarely changes itself. It also starts Blender on
the worker's behalf (launcher), since a process the worker started would die with it."""
import json
import logging
import os
import sys
import tempfile
from pathlib import Path

import anyio
import mcp_types as types
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.server.lowlevel.server import NotificationOptions, Server
from mcp.server.stdio import stdio_server

import launcher
import toolingLog
import toolingStatus

reloadToolName = "reloadZonewright"
callToolName = "callZonewright"
describeToolName = "describeZonewrightTool"
frontTools = (reloadToolName, callToolName, describeToolName)
handOverToolName = "handOverBridge"
adoptToolName = "adoptBridge"
frontOnlyTools = (handOverToolName, adoptToolName)
serverDirectory = Path(__file__).resolve().parent
zonewrightWorker = [str(serverDirectory / "zonewrightServer.py")]
errorTailCharacters = 2000
reloadTool = types.Tool(name=reloadToolName, input_schema={"type": "object", "properties": {}}, description=(
  "Restarts zonewright on the server code now on disk, after a change to it, keeping the running Blender and its open scene (the old"
  " server hands Blender to the new one, unsaved changes included), and returns the tools added, removed and changed and whether Blender"
  " was handed over. Refused while other zonewright calls are running. If the new code fails to start, the running server stays and the"
  " failure is returned. Changed Blender-side code (bridge*.py) still restarts Blender on the next call once the open file is saved."
  " Tool lists refresh; a change to zonewrightFront.py or to the server instructions reaches Claude only after the user reconnects"
  " zonewright in /mcp, and so does a tool the reload added: until then callZonewright calls it by name."))
callTool = types.Tool(name=callToolName, input_schema={
  "type": "object", "properties": {"name": {"type": "string"}, "arguments": {"type": "object"}}, "required": ["name", "arguments"],
}, description=(
  "Calls a zonewright tool by name with its arguments, exactly as calling it directly would, for a tool reloadZonewright added, which"
  " this session's tool list shows only after the user reconnects zonewright in /mcp (describeZonewrightTool gives its description and"
  " input schema). Refused for the front's own tools and for a tool the running zonewright lacks."))
describeTool = types.Tool(name=describeToolName, input_schema={"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
  description=("A running zonewright tool's description and input schema, for one reloadZonewright added that this session's tool list"
    " does not show yet; callZonewright calls it."))
log = logging.getLogger("zonewrightFront")


class WorkerError(Exception):
  pass


class Worker:
  """One worker process and the MCP session to it, held open by serve() in its own task until stop is set."""

  def __init__(self, arguments, environment):
    self.parameters = StdioServerParameters(command=sys.executable, args=arguments, env=dict(os.environ) | environment)
    self.ready = anyio.Event()
    self.stop = anyio.Event()
    self.finished = anyio.Event()
    self.failure = None
    self.errors = tempfile.TemporaryFile("w+", encoding="utf-8", errors="replace")

  def errorTail(self):
    self.errors.flush()
    self.errors.seek(0)
    text = self.errors.read().strip()
    return text[-errorTailCharacters:]

  async def serve(self):
    try:
      async with stdio_client(self.parameters, self.errors) as (readStream, writeStream), \
          ClientSession(readStream, writeStream) as session:
        initialized = await session.initialize()
        listing = await session.list_tools()
        if listing.next_cursor is not None:
          raise WorkerError("the worker pages its tool list; the front reads one page")
        if any(tool.name == reloadToolName for tool in listing.tools):
          raise WorkerError(f"the worker has its own {reloadToolName}")
        missing = [name for name in frontOnlyTools if not any(tool.name == name for tool in listing.tools)]
        if missing:
          raise WorkerError(f"the worker lacks the front's tools {', '.join(missing)}")
        self.session = session
        self.instructions = initialized.instructions
        self.tools = {tool.name: tool for tool in listing.tools}
        self.ready.set()
        await self.stop.wait()
    except Exception as error:
      self.failure = error
    finally:
      self.ready.set()
      self.finished.set()

  def close(self):
    self.errors.close()


def described(error):
  if isinstance(error, BaseExceptionGroup):
    return "; ".join(described(inner) for inner in error.exceptions)
  return f"{type(error).__name__}: {error}"


def failed(text):
  return types.CallToolResult(content=[types.TextContent(type="text", text=text)], is_error=True)


def resultText(result):
  return "\n".join(content.text for content in result.content if content.type == "text")


def toolChanges(oldTools, newTools):
  same = oldTools.keys() & newTools.keys()
  return {"added": sorted(newTools.keys() - oldTools.keys()), "removed": sorted(oldTools.keys() - newTools.keys()),
    "changed": sorted(name for name in same if oldTools[name] != newTools[name])}


class Front:
  def __init__(self, taskGroup, workerArguments=zonewrightWorker):
    self.taskGroup = taskGroup
    self.workerArguments = workerArguments
    self.launcher = launcher.Launcher()
    self.worker = None
    self.lock = anyio.Lock()
    self.running = 0

  async def startWorker(self):
    worker = Worker(self.workerArguments, self.launcher.environment())
    self.taskGroup.start_soon(worker.serve)
    await worker.ready.wait()
    if worker.failure is not None:
      message = f"zonewright did not start: {described(worker.failure)}; its stderr ends: {worker.errorTail() or '(empty)'}"
      worker.close()
      raise WorkerError(message)
    return worker

  async def start(self):
    await self.launcher.serve(self.taskGroup)
    self.worker = await self.startWorker()
    log.info("zonewright worker started with %d tools; the launcher listens on port %s", len(self.worker.tools), self.launcher.port)

  async def close(self):
    """Stops the worker, which shuts its Blender down on the way out, and kills any Blender left behind."""
    self.worker.stop.set()
    await self.worker.finished.wait()
    self.worker.close()
    survivors = self.launcher.killSurvivors()
    if survivors:
      log.warning("killed Blender processes the worker left running: %s", survivors)

  def tools(self):
    return [tool for name, tool in self.worker.tools.items() if name not in frontOnlyTools] + [reloadTool, callTool, describeTool]

  def describe(self, name):
    if name in frontTools or name in frontOnlyTools:
      return failed(f"{name} is the front's own")
    if name not in self.worker.tools:
      return failed(f"zonewright has no tool {name}")
    tool = self.worker.tools[name]
    described = {"name": name, "description": tool.description, "inputSchema": tool.input_schema}
    return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(described))], structured_content=described)

  async def call(self, name, arguments, notifyToolsChanged, reportProgress=None):
    if name == reloadToolName:
      return await self.reload(notifyToolsChanged)
    if name in (callToolName, describeToolName):
      if not isinstance(arguments, dict) or not isinstance(arguments.get("name"), str):
        return failed(f"{name} takes the name of a zonewright tool")
      if name == describeToolName:
        return self.describe(arguments["name"])
      if arguments["name"] in frontTools:
        return failed(f"{arguments['name']} is the front's own: call it directly")
      if not isinstance(arguments.get("arguments"), dict):
        return failed(f"{callToolName} takes the tool's arguments as an object")
      return await self.call(arguments["name"], arguments["arguments"], notifyToolsChanged, reportProgress)
    if name in frontOnlyTools:
      return failed(f"{name} is the front's own; {reloadToolName} uses it")
    async with self.lock:
      worker = self.worker
      if name not in worker.tools:
        return failed(f"zonewright has no tool {name}")
      self.running += 1
    try:
      return await worker.session.call_tool(name, arguments, progress_callback=reportProgress)
    except Exception as error:
      log.error("zonewright call %s failed in the worker: %s", name, error)
      return failed(f"the zonewright worker failed during {name}: {described(error)}; its stderr ends: "
        f"{worker.errorTail() or '(empty)'}; {reloadToolName} starts a new one")
    finally:
      self.running -= 1

  async def handOverBlender(self, oldWorker, newWorker):
    """The old worker's Blender, handed to the new worker; why it was not when it could not be."""
    try:
      handed = await oldWorker.session.call_tool(handOverToolName, {})
      if handed.is_error:
        return {"handedOver": False, "reason": f"the old server could not hand Blender over: {resultText(handed)}"}
      record = json.loads(resultText(handed))["record"]
      if record is None:
        return {"handedOver": False, "reason": "no Blender was running"}
      adopted = await newWorker.session.call_tool(adoptToolName, {"record": record})
    except Exception as error:
      return {"handedOver": False, "reason": f"the handover failed: {described(error)}"}
    if adopted.is_error:
      return {"handedOver": False, "reason": f"the new server could not adopt Blender (pid {record['pid']}): {resultText(adopted)}"}
    return {"handedOver": True} | json.loads(resultText(adopted))

  async def reload(self, notifyToolsChanged):
    async with self.lock:
      if self.running:
        return failed(f"{self.running} zonewright calls are running; reload when they finish")
      try:
        newWorker = await self.startWorker()
      except WorkerError as error:
        log.error("reload refused: %s", error)
        return failed(f"{error}; the running zonewright stays")
      oldWorker = self.worker
      blender = await self.handOverBlender(oldWorker, newWorker)
      self.worker = newWorker
    oldWorker.stop.set()
    await oldWorker.finished.wait()
    oldWorker.close()
    changes = toolChanges(oldWorker.tools, newWorker.tools)
    changes["instructionsChanged"] = oldWorker.instructions != newWorker.instructions
    changes["blender"] = blender
    await notifyToolsChanged()
    log.info("zonewright reloaded: %s", changes)
    return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(changes))], structured_content=changes)


async def main():
  toolingLog.configureLogging(toolingStatus.resolveToolingRoot())
  async with anyio.create_task_group() as taskGroup:
    front = Front(taskGroup)
    await front.start()

    async def listTools(context, parameters):
      return types.ListToolsResult(tools=front.tools())

    async def callTool(context, parameters):
      progressToken = (context.meta or {}).get("progress_token")

      async def reportProgress(progress, total, message):
        await context.session.send_progress_notification(progressToken, progress, total, message, related_request_id=context.request_id)

      return await front.call(parameters.name, parameters.arguments, context.session.send_tool_list_changed,
        reportProgress if progressToken is not None else None)

    instructions = (f"{front.worker.instructions} After a change to zonewright's server code, {reloadToolName} restarts it on that code"
      " without the user, keeping Blender and the open scene.")
    server = Server("zonewright", instructions=instructions, on_list_tools=listTools, on_call_tool=callTool)
    async with stdio_server() as (readStream, writeStream):
      await server.run(readStream, writeStream, server.create_initialization_options(NotificationOptions(tools_changed=True)))
    await front.close()
    taskGroup.cancel_scope.cancel()


if __name__ == "__main__":
  anyio.run(main)
