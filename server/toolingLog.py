import json
import logging
import logging.handlers

logRetentionDays = 90
runPythonLoggerName = "runPython"


def rotatingHandler(logPath, formatter):
  handler = logging.handlers.TimedRotatingFileHandler(logPath, when="midnight", backupCount=logRetentionDays, encoding="utf-8")
  handler.setFormatter(formatter)
  return handler


def configureLogging(toolingRoot):
  logDirectory = toolingRoot / "logs"
  logDirectory.mkdir(parents=True, exist_ok=True)
  rootLogger = logging.getLogger()
  rootLogger.handlers = [rotatingHandler(logDirectory / "zonewright.log", logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))]
  rootLogger.setLevel(logging.INFO)
  runPythonLogger = logging.getLogger(runPythonLoggerName)
  runPythonLogger.handlers = [rotatingHandler(logDirectory / "runPython.log", logging.Formatter("%(message)s"))]
  runPythonLogger.propagate = False


def recordRunPython(code):
  """One JSON line per runPython call, so repeated raw scripting can be spotted and turned into tools."""
  logging.getLogger(runPythonLoggerName).info(json.dumps({"code": code}))


def countRunPython(toolingRoot):
  logPaths = sorted((toolingRoot / "logs").glob("runPython.log*"))
  return {"calls": sum(len(logPath.read_text(encoding="utf-8").splitlines()) for logPath in logPaths), "logFiles": [str(logPath) for logPath in logPaths]}
