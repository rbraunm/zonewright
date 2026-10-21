"""The claudeperidot change ledger's commands (claudeperidot/tools/ledger.py), run under the ledger's own Python."""
import re
import subprocess

from zoneDeployment import ledgerPython, repositoryRoot

ledgerScript = repositoryRoot / "claudeperidot" / "tools" / "ledger.py"
changeCommands = ("stage", "apply", "revert")
commands = ("status",) + changeCommands
changePattern = re.compile(r"[0-9][A-Za-z0-9-]*")


def runLedger(command, change):
  """The ledger's output lines for command (with change, for stage, apply and revert), or a ValueError with its error."""
  if command not in commands:
    raise ValueError(f"command is one of {list(commands)}, got {command!r}")
  if (command in changeCommands) != (change is not None):
    raise ValueError(f"change names the change for {list(changeCommands)} and is left out for status, got {change!r} for {command}")
  if change is not None and not changePattern.fullmatch(change):
    raise ValueError(f"change is a change's number or folder name, got {change!r}")
  arguments = [ledgerPython, str(ledgerScript), command] + ([change] if change is not None else [])
  completed = subprocess.run(arguments, cwd=repositoryRoot, stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8")
  if completed.returncode != 0:
    raise ValueError(f"ledger {command} failed: {completed.stderr.strip()[-2000:]}")
  return completed.stdout.splitlines()
