import logging
import logging.handlers

logRetentionDays = 90


def configureLogging(toolingRoot):
  logDirectory = toolingRoot / "logs"
  logDirectory.mkdir(parents=True, exist_ok=True)
  handler = logging.handlers.TimedRotatingFileHandler(
    logDirectory / "zonewright.log",
    when="midnight",
    backupCount=logRetentionDays,
    encoding="utf-8",
  )
  handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
  rootLogger = logging.getLogger()
  rootLogger.handlers = [handler]
  rootLogger.setLevel(logging.INFO)
