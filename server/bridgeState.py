"""Bridge session state shared by the Blender-side command modules. Runs under Blender's Python."""
import math

import bmesh
import bpy
import mathutils


class BridgeState:
  def __init__(self):
    self.unsavedChanges = False
    self.resetNamespace()

  def resetNamespace(self):
    self.namespace = {"bpy": bpy, "bmesh": bmesh, "mathutils": mathutils, "math": math}


state = BridgeState()


def requireNoUnsavedChanges(discardUnsavedChanges, action):
  if state.unsavedChanges and not discardUnsavedChanges:
    raise ValueError(f"Refusing to {action}: the open file has unsaved changes; save it or pass discardUnsavedChanges")
