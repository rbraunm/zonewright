import ctypes
import logging
import os
import re
import subprocess
from ctypes import wintypes

logger = logging.getLogger(__name__)
blenderVersionLinePattern = re.compile(r"^Blender (\d+\.\d+\.\d+)", re.MULTILINE)
blenderCommandTimeoutSeconds = 300
processQueryLimitedInformation = 0x1000
processIDCapacity = 4096
imagePathCapacity = 32768

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.K32EnumProcesses.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
kernel32.K32EnumProcesses.restype = wintypes.BOOL


def runBlender(executablePath, arguments):
  command = [str(executablePath), "--factory-startup", *arguments]
  logger.info("running %s", command)
  completed = subprocess.run(command, capture_output=True, encoding="utf-8", errors="replace", timeout=blenderCommandTimeoutSeconds)
  logger.info("exit %s stdout: %s stderr: %s", completed.returncode, completed.stdout.strip(), completed.stderr.strip())
  if completed.returncode != 0:
    raise RuntimeError(f"{command} exited {completed.returncode}: {completed.stdout.strip()[-500:]} {completed.stderr.strip()[-500:]}")
  return completed.stdout


def readBlenderVersion(executablePath):
  output = runBlender(executablePath, ["--version"])
  versionMatch = blenderVersionLinePattern.search(output)
  if versionMatch is None:
    raise RuntimeError(f"{executablePath} --version printed no 'Blender X.Y.Z' line: {output.strip()[:200]}")
  return versionMatch.group(1)


def listProcessImagePaths():
  processIDs = (wintypes.DWORD * processIDCapacity)()
  bytesReturned = wintypes.DWORD()
  if not kernel32.K32EnumProcesses(processIDs, ctypes.sizeof(processIDs), ctypes.byref(bytesReturned)):
    raise ctypes.WinError(ctypes.get_last_error())
  processCount = bytesReturned.value // ctypes.sizeof(wintypes.DWORD)
  if processCount == processIDCapacity:
    raise RuntimeError(f"More than {processIDCapacity} processes running; raise processIDCapacity")
  imagePaths = []
  for processID in processIDs[:processCount]:
    processHandle = kernel32.OpenProcess(processQueryLimitedInformation, False, processID)
    # System and protected processes refuse the handle; none of them can be a Blender we launched.
    if not processHandle:
      continue
    try:
      imagePath = ctypes.create_unicode_buffer(imagePathCapacity)
      pathLength = wintypes.DWORD(imagePathCapacity)
      if kernel32.QueryFullProcessImageNameW(processHandle, 0, imagePath, ctypes.byref(pathLength)):
        imagePaths.append(imagePath.value)
    finally:
      kernel32.CloseHandle(processHandle)
  return imagePaths


def findRunningBlenders(blenderRoot):
  rootPrefix = os.path.normcase(str(blenderRoot)) + os.sep
  return sorted(imagePath for imagePath in listProcessImagePaths() if os.path.normcase(imagePath).startswith(rootPrefix))
