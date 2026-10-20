"""The claudeperidot change ledger: every change to claudeperidot or to Claude's workstation side is written, staged and
committed before it runs, and its state is always read live, never stored."""
import argparse
import base64
import datetime
import hashlib
import json
import logging
import logging.handlers
import os
import re
import sqlite3
import struct
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ledgerFolder = Path(__file__).resolve().parent.parent
hostName = "claudeperidot"
mariadbContainer = "akk-stack-mariadb-1"
databaseName = "peq"
# Every write the ledger makes carries this GTID domain, so drift can tell the ledger's binary-log writes from all others.
ledgerGTIDDomain = 7
hostPaths = {
  "akkStack": "/opt/eqemu/akk-stack",
  "code": "/opt/eqemu/akk-stack/code",
  "quests": "/opt/eqemu/akk-stack/server/quests",
  "maps": "/opt/eqemu/akk-stack/server/maps",
  "dumps": "/opt/eqemu/baseline",
  "ledger": "/opt/eqemu/ledger",
}
peridotQuestsPath = "/opt/eqemu/akk-stack/server/quests"
# The host folders a files change may write: the server's maps, a checkout no remote Claude can push to carries.
fileRoots = (hostPaths["maps"] + "/",)

# EQEmu's player, state, login and bot groups (common/database_schema.h at ae4bd2833): play writes them constantly, and they
# hold accounts, logins and personal data.
playTables = frozenset("""
  account account_ip account_flags account_rewards adventure_details adventure_stats buyer buyer_buy_lines buyer_trade_items
  char_recipe_list character_activities character_alt_currency character_alternate_abilities character_auras
  character_bandolier character_bind character_buffs character_corpse_items character_corpses character_currency
  character_data character_disciplines character_enabledtasks character_expedition_lockouts character_exp_modifiers
  character_evolving_items character_inspect_messages character_instance_safereturns character_item_recast
  character_languages character_leadership_abilities character_material character_memmed_spells character_parcels
  character_parcels_containers character_pet_buffs character_pet_info character_pet_inventory character_pet_name
  character_peqzone_flags character_potionbelt character_skills character_spells character_stats_record
  character_task_timers character_tasks character_tribute completed_tasks data_buckets discovered_items faction_values
  friends guild_bank guild_members guild_permissions guild_ranks guild_relations guild_tributes guilds instance_list_player
  inventory inventory_snapshots keyring mail petitions player_titlesets quest_globals sharedbank spell_buckets spell_globals
  timers trader trader_audit zone_flags
  adventure_members banned_ips bug_reports bugs completed_shared_task_activity_state completed_shared_task_members
  completed_shared_tasks discord_webhooks dynamic_zone_lockouts dynamic_zone_members dynamic_zones gm_ips group_id
  group_leaders instance_list ip_exemptions lfguild merc_buffs merchantlist_temp mercs object_contents raid_details
  raid_leaders raid_members reports respawn_times saylink server_scheduled_events spawn2_disabled player_event_aa_purchase
  player_event_killed_npc player_event_killed_named_npc player_event_killed_raid_npc player_event_log_settings
  player_event_logs player_event_loot_items player_event_merchant_purchase player_event_merchant_sell player_event_npc_handin
  player_event_npc_handin_entries player_event_speech player_event_trade player_event_trade_entries
  shared_task_activity_state shared_task_dynamic_zones shared_task_members shared_tasks zone_state_spawns
  login_accounts login_api_tokens login_server_admins login_server_list_types login_world_servers
  bot_blocked_buffs bot_buffs bot_command_settings bot_create_combinations bot_data bot_heal_rotation_members
  bot_heal_rotation_targets bot_heal_rotations bot_inspect_messages bot_inventories bot_owner_options bot_pet_buffs
  bot_pet_inventories bot_pets bot_settings bot_spell_casting_chances bot_spell_settings bot_spells_entries bot_stances
  bot_timers
""".split())
# The low-churn security and configuration tables EQEmu files under state or login: only an admin writes them (ae4bd2833's
# boot writes to player_event_log_settings and login_server_admins add only what is missing), so drift watches them.
securityTables = frozenset("""
  login_api_tokens login_server_admins login_server_list_types discord_webhooks server_scheduled_events
  player_event_log_settings gm_ips banned_ips ip_exemptions
""".split())
# The tables world, zone, ucs and loginserver (ae4bd2833) and Spire (v4.24.2) write on their own, at start, on a timer or as
# play goes on; the README's Runtime tables gives each one's source. Their other start writes (rules, commands, log
# categories, player event settings, db_version, variables, Spire's settings) add only what is missing, so those stay watched.
runtimeTables = frozenset("""
  eqtime data_buckets group_id group_leaders raid_members raid_details raid_leaders guild_members inventory_snapshots trader
  buyer buyer_buy_lines buyer_trade_items bot_pet_buffs merchantlist_temp character_corpses character_corpse_items
  dynamic_zones dynamic_zone_members dynamic_zone_lockouts character_expedition_lockouts character_task_timers instance_list
  instance_list_player respawn_times spawn_condition_values spawn2_disabled shared_tasks shared_task_members
  shared_task_activity_state shared_task_dynamic_zones zone_state_spawns character_parcels player_event_logs
  player_event_loot_items player_event_merchant_sell player_event_merchant_purchase player_event_npc_handin
  player_event_npc_handin_entries player_event_trade player_event_trade_entries player_event_speech player_event_killed_npc
  player_event_killed_named_npc player_event_killed_raid_npc player_event_aa_purchase
  spawn_events quest_globals saylink
  mail chatchannels
  login_world_servers
  spire_server_database_connections
""".split())
contentTables = frozenset("""
  aa_ability aa_rank_effects aa_rank_prereqs aa_ranks adventure_template adventure_template_entry
  adventure_template_entry_flavor alternate_currency auras base_data blocked_spells books char_create_combinations
  char_create_point_allocations damageshieldtypes doors dynamic_zone_templates faction_association faction_base_data
  faction_list faction_list_mod fishing forage global_loot graveyard grid grid_entries ground_spawns horses items
  items_evolving_details ldon_trap_entries ldon_trap_templates lootdrop lootdrop_entries loottable loottable_entries
  merchantlist npc_emotes npc_faction npc_faction_entries npc_scale_global_base npc_spells npc_spells_effects
  npc_spells_effects_entries npc_spells_entries npc_types npc_types_tint object pets pets_beastlord_data pets_equipmentset
  pets_equipmentset_entries skill_caps spawn2 spawn_conditions spawnentry spawngroup spells_new start_zones starting_items
  task_activities tasks tasksets tradeskill_recipe tradeskill_recipe_entries traps tribute_levels tributes
  veteran_reward_templates zone zone_points
""".split())
sensitiveWordPattern = re.compile(r"^\w*(password|passwd|token|secret|webhook|key)s?$")

changeNumberPattern = r"\d{4}|\d{8}-\d{6}"
changeFolderPattern = re.compile(rf"^({changeNumberPattern})-([a-z][A-Za-z0-9]*)$")
authorPatterns = {"session": re.compile(r"^session_[A-Za-z0-9]+$"), "agent": re.compile(r"^a[0-9a-f]{16}$")}
templateFolderName = "template"
commonFields = ("title", "why", "kind", "scope", "disposition", "significance", "ownerClearance", "activation", "production",
  "commits", "author")
kindFields = {
  "db": (set(), {"schemaSteps"}),
  "quest": ({"repository", "source"}, set()),
  "config": ({"repository", "source"}, {"rendered", "render"}),
  "client": ({"steps", "undo", "checks"}, set()),
  "lab": ({"steps", "undo", "checks"}, set()),
  "files": ({"files"}, set()),
}
choices = {
  "disposition": ("fixture", "exploration", "production"),
  "significance": ("minor", "significant"),
}
productionForms = {"rows": ("contentSQL",), "schema": ("customMigration",), "quest": ("questCommit",),
  "config": ("runbook", "provisioner"), "client": (), "lab": (), "files": ()}
labTools = ("facet run", "sonicwall")
tableNamePattern = re.compile(r"^[A-Za-z0-9_]+$")
commitPattern = re.compile(r"^[0-9a-f]{7,40}$")
hostFilePattern = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./-]*$")
hostFolderPattern = re.compile(r"^/[A-Za-z0-9_./-]+$")
hostCommandPattern = re.compile(r"^/[A-Za-z0-9_./-]+( [A-Za-z0-9_./=-]+)*$")
keyNamePattern = re.compile(r"^[A-Za-z0-9_.-]+$")
clientPathPattern = re.compile(r"^([A-Za-z]:\\|~\\)")
credentialTargetPattern = re.compile(r"^[A-Za-z0-9_.:@/-]+$")
hashPattern = re.compile(r"^[0-9a-f]{64}$")
exportNamePattern = re.compile(r"^[A-Za-z0-9_?@$]+$")
encodedValuePattern = re.compile(r"^(N|E|V(?:[0-9A-F]{2})*)$")
stepPattern = re.compile(rf"^- \[( |x)\] (\d+)\. (.+?)(?: \[((?:{changeNumberPattern})(?:, (?:{changeNumberPattern}))*)\])?$")
mysqlErrorPattern = re.compile(r"^ERROR (\d+) \(([0-9A-Z]+)\)(?: at line (\d+))?: (.*)$")
quotedValuePattern = re.compile(r"'(?:[^'\\]|\\.|'')*'")

integerTypes = {"tinyint", "smallint", "mediumint", "int", "bigint"}
decimalTypes = {"decimal"}
floatTypes = {"float", "double"}
bitTypes = {"bit"}
binaryTypes = {"binary", "varbinary", "tinyblob", "blob", "mediumblob", "longblob"}
textTypes = {"char", "varchar", "tinytext", "text", "mediumtext", "longtext", "enum", "set"}
temporalTypes = {"date", "datetime", "timestamp", "time", "year"}
asciiCompatibleCharsets = {"ascii", "latin1", "utf8", "utf8mb3", "utf8mb4"}
numberPatterns = {
  "integer": re.compile(r"^-?\d+$"),
  "decimal": re.compile(r"^-?\d+(\.\d+)?$"),
  "float": re.compile(r"^-?\d+(\.\d+)?(e[+-]?\d+)?$", re.IGNORECASE),
}
rowTransactionKeywords = {"INSERT", "UPDATE", "DELETE", "REPLACE"}
dropTablePattern = re.compile(r"DROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?", re.IGNORECASE)
rebuiltSchemaObjects = frozenset("VIEW TRIGGER PROCEDURE FUNCTION EVENT".split())
schemaObjectWords = rebuiltSchemaObjects | frozenset("TABLE INDEX DATABASE SCHEMA SEQUENCE SERVER USER ROLE".split())
intents = ("applying", "reverting")
log = logging.getLogger("ledger")


class LedgerError(Exception):
  pass


class uniqueKeyLoader(yaml.SafeLoader):
  pass


def constructUniqueMapping(loader, node, deep=False):
  seenKeys = set()
  for keyNode, _ in node.value:
    key = loader.construct_object(keyNode, deep=deep)
    if key in seenKeys:
      raise LedgerError(f"duplicate key {key!r} on line {keyNode.start_mark.line + 1}")
    seenKeys.add(key)
  return loader.construct_mapping(node, deep)


uniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, constructUniqueMapping)


def readYAML(path):
  try:
    return yaml.load(path.read_text(encoding="utf-8"), Loader=uniqueKeyLoader)
  except yaml.YAMLError as error:
    raise LedgerError(f"{path} is not valid YAML: {error}") from error


def isOneLine(value):
  return isinstance(value, str) and bool(value.strip()) and "\n" not in value


def isWatched(table):
  return table not in runtimeTables and (table not in playTables or table in securityTables)


def columnWords(name):
  return [word.lower() for word in re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+", name)]


def sensitiveReason(table, columnNames):
  """Why a table's rows are secrets or personal data, or None."""
  if table in playTables:
    return f"{table} is a player, state, login or bot table"
  for name in columnNames:
    if any(sensitiveWordPattern.match(word) for word in columnWords(name)):
      return f"{table}.{name} is named like a secret"
  return None


def changeForm(fields):
  if fields.get("kind") == "db":
    return "schema" if "schemaSteps" in fields else "rows"
  return fields.get("kind")


def clientCheckProblem(check):
  if isinstance(check, dict) and set(check) == {"credential"}:
    if isOneLine(check["credential"]) and credentialTargetPattern.match(check["credential"]):
      return None
    return f"a credential check names a Credential Manager target: {check!r}"
  if not isinstance(check, dict) or not isOneLine(check.get("path")) or not clientPathPattern.match(check["path"]):
    return f"a check needs a path that is absolute or starts with ~\\: {check!r}"
  keys = set(check)
  if keys == {"path", "exists"} and check["exists"] in ("file", "directory"):
    return None
  if keys == {"path", "exists", "target"} and check["exists"] == "junction" and isOneLine(check["target"]) \
      and clientPathPattern.match(check["target"]):
    return None
  if keys == {"path", "sha256"} and isinstance(check["sha256"], str) and hashPattern.match(check["sha256"]):
    return None
  if keys == {"path", "section", "key", "value"} and all(isinstance(check[name], str) and check[name] != ""
      for name in ("section", "key")) and isinstance(check["value"], str):
    return None
  if keys == {"path", "server", "character"} and all(isOneLine(check[name]) for name in ("server", "character")):
    return None
  if keys == {"path", "exports"} and isinstance(check["exports"], str) and exportNamePattern.match(check["exports"]):
    return None
  return (f"check {check!r} is not one of: exists file or directory; exists junction with a target; a lowercase sha256; "
    "an ini section, key and value; a credential; a login.db server and character; a name a DLL exports")


def labActionProblem(action, name):
  required = {"via", "host", "run"} | ({"expect"} if name == "checks" else set())
  if not isinstance(action, dict) or set(action) != required or action["via"] not in labTools \
      or not all(isinstance(action[key], str) and action[key].strip() for key in required):
    return (f"each of a lab change's {name} is {{via: facet run or sonicwall, host, run}}"
      + (" with expect" if name == "checks" else "") + f": {action!r}")
  return None


def renderedProblems(fields):
  rendered, render = fields.get("rendered"), fields.get("render")
  if rendered is None and render is None:
    return []
  if rendered is None or render is None:
    return ["rendered and render go together: the files rendered from templates, and the host command that renders them"]
  problems = []
  if not isOneLine(render) or not hostCommandPattern.match(render):
    problems.append("render must be one host command: an absolute path and plain arguments")
  if not isinstance(rendered, list) or not rendered:
    return problems + ["rendered must be a non-empty list"]
  for entry in rendered:
    if not isinstance(entry, dict) or set(entry) != {"template", "file", "filledKeys"} \
        or entry["template"] not in fields.get("scope", []) or not isinstance(entry["file"], str) \
        or not hostFolderPattern.match(entry["file"]) or not isinstance(entry["filledKeys"], list) or not entry["filledKeys"] \
        or not all(isinstance(key, str) and keyNamePattern.match(key) for key in entry["filledKeys"]):
      problems.append(f"a rendered entry is a scope template, the live file's absolute host path and the names of the keys the "
        f"host fills: {entry!r}")
  return problems


def scopeProblems(fields, form):
  scope = fields["scope"]
  if not isinstance(scope, list) or not scope:
    return ["scope must be a non-empty list"]
  problems = []
  for entry in scope:
    if form == "rows":
      if not isinstance(entry, dict) or set(entry) != {"table", "where"} or not isinstance(entry["table"], str) \
          or not tableNamePattern.match(entry["table"]) or not isinstance(entry["where"], str) or not entry["where"].strip():
        problems.append(f"a db scope entry is a table name and a where predicate: {entry!r}")
    elif form == "schema":
      if not isinstance(entry, dict) or set(entry) != {"table"} or not isinstance(entry["table"], str) \
          or not tableNamePattern.match(entry["table"]):
        problems.append(f"a schema scope entry is a table name: {entry!r}")
    elif form in ("quest", "config"):
      if not isinstance(entry, str) or not hostFilePattern.match(entry) or ".." in entry.split("/"):
        problems.append(f"a {form} scope entry is a file path inside the repository: {entry!r}")
    elif form == "lab":
      if not isOneLine(entry):
        problems.append(f"a lab scope entry names one thing the change touches: {entry!r}")
    elif form == "files":
      if not isinstance(entry, str) or not hostFolderPattern.match(entry) or ".." in entry.split("/") \
          or not entry.startswith(fileRoots):
        problems.append(f"a files scope entry is a host file under {', '.join(fileRoots)}: {entry!r}")
    elif not isinstance(entry, str) or not clientPathPattern.match(entry):
      problems.append(f"a client scope entry is a path that is absolute or starts with ~\\: {entry!r}")
  return problems


def fileEntriesProblems(fields):
  """A files change's entries: each {path, source, sha256}, a host path the scope names in order, and the workstation file
  that becomes it with that file's SHA-256, or source and sha256 both null for a file the change removes."""
  entries = fields["files"]
  if not isinstance(entries, list) or not entries:
    return ["files must be a non-empty list"]
  problems = []
  for entry in entries:
    if not isinstance(entry, dict) or set(entry) != {"path", "source", "sha256"}:
      problems.append(f"a files entry is {{path, source, sha256}}: {entry!r}")
    elif (entry["source"] is None) != (entry["sha256"] is None):
      problems.append(f"a files entry removes its file with source and sha256 both null, or gives both: {entry!r}")
    elif entry["source"] is not None and not (isOneLine(entry["source"]) and clientPathPattern.match(entry["source"])
        and isinstance(entry["sha256"], str) and hashPattern.match(entry["sha256"])):
      problems.append(f"a files entry's source is an absolute workstation path and its sha256 lowercase hex: {entry!r}")
  if not problems and [entry["path"] for entry in entries] != fields["scope"]:
    problems.append("a files change's scope lists its files' paths, in their order")
  if not problems and len(set(fields["scope"])) != len(fields["scope"]):
    problems.append("a files change names each path once")
  return problems


def clearanceProblem(clearance):
  if clearance == "pending":
    return None
  if isinstance(clearance, dict) and set(clearance) == {"ownerMessage"} and isOneLine(clearance["ownerMessage"]):
    return None
  return ("ownerClearance is pending, or {ownerMessage: the owner's message that cleared it, quoted with its date}; Claude never "
    "clears a change on its own judgment")


def authorProblem(author):
  """Why an author field is malformed, or None: the Claude session that wrote the change, and the subagent when one did."""
  if not (isinstance(author, dict) and "session" in author and set(author) <= set(authorPatterns)):
    return "author must be a mapping of session and, for a subagent, agent"
  bad = [name for name, value in author.items() if not (isinstance(value, str) and authorPatterns[name].match(value))]
  return f"author {', '.join(bad)} malformed" if bad else None


def validateChangeFields(fields):
  """The problems with a change.yaml's fields; an empty list means it is valid."""
  if not isinstance(fields, dict):
    return ["change.yaml must be a mapping of fields"]
  kind = fields.get("kind")
  problems = []
  if kind in kindFields:
    required, optional = kindFields[kind]
    allowed = set(commonFields) | required | optional
  else:
    problems.append(f"kind must be one of {', '.join(kindFields)}, not {kind!r}")
    required = set()
    allowed = set(commonFields).union(*(set(needed) | extra for needed, extra in kindFields.values()))
  missing = [name for name in (*commonFields, *sorted(required)) if name not in fields]
  if missing:
    problems.append(f"missing {', '.join(missing)}")
  unknown = sorted(set(fields) - allowed)
  if unknown:
    problems.append(f"unknown fields {', '.join(unknown)}")
  for name in ("title", "activation"):
    if name in fields and not isOneLine(fields[name]):
      problems.append(f"{name} must be one non-empty line")
  if "why" in fields and not (isinstance(fields["why"], str) and fields["why"].strip()):
    problems.append("why must be non-empty text")
  for name, allowedValues in choices.items():
    if name in fields and fields[name] not in allowedValues:
      problems.append(f"{name} must be one of {', '.join(allowedValues)}, not {fields[name]!r}")
  if "author" in fields and authorProblem(fields["author"]):
    problems.append(authorProblem(fields["author"]))
  if "ownerClearance" in fields and clearanceProblem(fields["ownerClearance"]):
    problems.append(clearanceProblem(fields["ownerClearance"]))
  if kind not in kindFields or problems:
    return problems
  form = changeForm(fields)
  if fields["disposition"] in ("fixture", "exploration") and fields["production"] != "none":
    problems.append(f"{fields['disposition']} changes never reach Peridot: production must be none")
  if fields["disposition"] == "production":
    if form in ("client", "lab", "files"):
      problems.append(f"a {form} change stays where it was made and cannot be production")
    elif fields["production"] not in productionForms[form]:
      problems.append(f"a production {'row' if form == 'rows' else form} change reaches Peridot as "
        f"{' or '.join(productionForms[form])}")
  commits = fields["commits"]
  if not isinstance(commits, list) or not all(isinstance(commit, str) and commitPattern.match(commit) for commit in commits):
    problems.append("commits must be a list of lowercase commit SHAs")
  elif kind in ("db", "client", "lab", "files") and commits:
    problems.append(f"a {kind} change has no commits")
  elif kind in ("quest", "config") and not commits:
    problems.append(f"a {kind} change names its commits, oldest first")
  if form == "schema" and (type(fields["schemaSteps"]) is not int or fields["schemaSteps"] < 1):
    problems.append("schemaSteps must be a whole number of steps, at least 1")
  if kind in ("quest", "config"):
    if not (isinstance(fields["repository"], str) and hostFolderPattern.match(fields["repository"])):
      problems.append("repository must be the checkout's absolute path on the host")
    if not (isOneLine(fields["source"]) and clientPathPattern.match(fields["source"])):
      problems.append("source must be the workstation clone the commits are made in, absolute or starting with ~\\")
  if kind == "config":
    problems.extend(renderedProblems(fields))
  if kind == "client":
    for name in ("steps", "undo"):
      if not isinstance(fields[name], list) or not fields[name] or not all(isinstance(step, str) and step.strip()
          for step in fields[name]):
        problems.append(f"{name} must be a non-empty list of PowerShell scripts")
    if not isinstance(fields["checks"], list) or not fields["checks"]:
      problems.append("checks must be a non-empty list")
    else:
      problems.extend(problem for problem in map(clientCheckProblem, fields["checks"]) if problem)
  if kind == "lab":
    for name in ("steps", "undo", "checks"):
      if not isinstance(fields[name], list) or not fields[name]:
        problems.append(f"{name} must be a non-empty list")
      else:
        problems.extend(problem for problem in (labActionProblem(action, name) for action in fields[name]) if problem)
  problems.extend(scopeProblems(fields, form))
  if kind == "files" and not problems:
    problems.extend(fileEntriesProblems(fields))
  return problems


@dataclass(frozen=True)
class Change:
  folder: Path
  fields: dict

  @property
  def identifier(self):
    return self.folder.name

  @property
  def number(self):
    return changeFolderPattern.match(self.folder.name).group(1)

  @property
  def name(self):
    return changeFolderPattern.match(self.folder.name).group(2)

  @property
  def kind(self):
    return self.fields["kind"]

  @property
  def form(self):
    return changeForm(self.fields)

  @property
  def steps(self):
    return self.fields["schemaSteps"] if self.form == "schema" else 1

  @property
  def tables(self):
    return [entry["table"] for entry in self.fields["scope"]]

  @property
  def isCleared(self):
    return self.fields["ownerClearance"] != "pending"

  @property
  def hostFolder(self):
    return f"{hostPaths['ledger']}/{self.identifier}"


def loadChange(folder):
  if not changeFolderPattern.match(folder.name):
    raise LedgerError(f"{folder.name} is not a change folder name (NNNN or YYYYMMDD-HHMMSS, then -drinkingCamelCaseName)")
  path = folder / "change.yaml"
  if not path.is_file():
    raise LedgerError(f"{folder.name} has no change.yaml")
  fields = readYAML(path)
  problems = validateChangeFields(fields)
  if problems:
    raise LedgerError(f"{folder.name}/change.yaml: " + "; ".join(problems))
  return Change(folder, fields)


def loadChanges(folder):
  changes = {}
  for changeFolder in sorted((folder / "changes").iterdir()):
    if changeFolder.name == templateFolderName:
      continue
    if not changeFolder.is_dir():
      raise LedgerError(f"changes/{changeFolder.name} is not a change folder")
    change = loadChange(changeFolder)
    if change.number in changes:
      raise LedgerError(f"{changes[change.number].identifier} and {change.identifier} share number {change.number}")
    changes[change.number] = change
  return list(changes.values())


def sqlPieces(text):
  """(kind, text) pieces of SQL in order: code, quoted, comment, versioned (a /*! or /*M! comment, which MariaDB runs), or
  separator (a semicolon)."""
  index = 0
  length = len(text)
  codeStart = 0
  while index < length:
    character = text[index]
    end = None
    kind = None
    if character in "'\"`":
      kind = "quoted"
      end = index + 1
      while True:
        if end >= length:
          raise LedgerError("an SQL quoted string or name is never closed")
        if text[end] == "\\" and character != "`":
          end += 2
        elif text[end] == character and text.startswith(character * 2, end):
          end += 2
        elif text[end] == character:
          end += 1
          break
        else:
          end += 1
    elif text.startswith("--", index) and (index + 2 == length or text[index + 2] in " \t\r\n") or character == "#":
      kind = "comment"
      end = text.find("\n", index)
      end = length if end == -1 else end
    elif text.startswith("/*", index):
      kind = "versioned" if text.startswith(("/*!", "/*M!"), index) else "comment"
      end = text.find("*/", index + 2)
      if end == -1:
        raise LedgerError("an SQL comment is never closed")
      end += 2
    elif character == ";":
      kind = "separator"
      end = index + 1
    if kind is None:
      index += 1
      continue
    if codeStart < index:
      yield "code", text[codeStart:index]
    yield kind, text[index:end]
    index = codeStart = end
  if codeStart < length:
    yield "code", text[codeStart:]


def firstKeyword(statement):
  for kind, text in sqlPieces(statement):
    if kind == "code" and text.strip():
      match = re.match(r"\s*([A-Za-z]+)", text)
      return match.group(1).upper() if match else ""
    if kind in ("quoted", "versioned"):
      return ""
  return ""


def splitStatements(text):
  """The statements of an SQL script, without their semicolons or comments, so each can be embedded or re-terminated."""
  statements = []
  current = []
  for kind, piece in sqlPieces(text):
    if kind == "versioned":
      raise LedgerError(f"MariaDB runs a versioned comment as code; write its content plainly instead of {piece[:40]}")
    if kind == "separator":
      statements.append("".join(current).strip())
      current = []
    else:
      current.append(" " if kind == "comment" else piece)
  statements.append("".join(current).strip())
  statements = [statement for statement in statements if statement]
  for statement in statements:
    if firstKeyword(statement) == "DELIMITER":
      raise LedgerError("DELIMITER is not supported in a change's SQL; write one statement per semicolon")
  return statements


def quoteIdentifier(name):
  return "`" + name.replace("`", "``") + "`"


def redactValues(text):
  return quotedValuePattern.sub("'?'", text)


@dataclass(frozen=True)
class Column:
  name: str
  dataType: str
  charset: str

  def asRecord(self):
    return {"name": self.name, "type": self.dataType, "charset": self.charset}


def columnFromRecord(record):
  return Column(record["name"], record["type"], record["charset"])


def fetchExpression(column):
  """An expression giving the column's value as N for NULL or V and the hex of its bytes (text and binary) or of a text form
  that converts back exactly: floats as doubles, which MariaDB prints with every digit they need. An ENUM holding its invalid
  value (index 0, which a write outside strict sql_mode leaves) is E."""
  name = quoteIdentifier(column.name)
  if column.dataType == "enum":
    return f"IF({name} IS NULL, 'N', IF({name} + 0 = 0, 'E', CONCAT('V', HEX(CONCAT({name})))))"
  if column.dataType in binaryTypes or column.dataType in textTypes - {"enum", "set"}:
    inner = f"HEX({name})"
  elif column.dataType == "set":
    inner = f"HEX(CONCAT({name}))"
  elif column.dataType in bitTypes:
    inner = f"HEX(CAST({name} + 0 AS CHAR))"
  elif column.dataType in floatTypes:
    inner = f"HEX(CAST({name} * 1e0 AS CHAR))"
  elif column.dataType in integerTypes | decimalTypes | temporalTypes:
    inner = f"HEX(CAST({name} AS CHAR))"
  else:
    raise LedgerError(f"column {column.name} has type {column.dataType}, which the ledger cannot save exactly")
  return f"IF({name} IS NULL, 'N', CONCAT('V', {inner}))"


def literal(column, encoded):
  """The SQL literal that stores exactly the value saved as encoded."""
  if encoded == "N":
    return "NULL"
  if encoded == "E":
    raise LedgerError(f"column {column.name} holds its ENUM's invalid value (index 0, which a write outside strict sql_mode "
      "leaves), which revert could not write back under strict sql_mode; correct the row first")
  data = bytes.fromhex(encoded[1:])
  if column.dataType in binaryTypes:
    return f"X'{data.hex().upper()}'"
  if column.dataType in textTypes:
    if not column.charset:
      raise LedgerError(f"text column {column.name} has no character set")
    if column.charset in asciiCompatibleCharsets and all(0x20 <= byte <= 0x7E and byte != 0x5C for byte in data):
      return "'" + data.decode("ascii").replace("'", "''") + "'"
    return f"_{column.charset} X'{data.hex().upper()}'"
  try:
    text = data.decode("ascii")
  except UnicodeDecodeError as error:
    raise LedgerError(f"column {column.name} ({column.dataType}) holds non-ASCII text") from error
  if column.dataType in integerTypes | bitTypes:
    pattern = numberPatterns["integer"]
  elif column.dataType in decimalTypes:
    pattern = numberPatterns["decimal"]
  elif column.dataType in floatTypes:
    pattern = numberPatterns["float"]
  elif column.dataType in temporalTypes:
    if not all(character not in "'\\" and " " <= character <= "~" for character in text):
      raise LedgerError(f"column {column.name} ({column.dataType}) holds a value that is not a date or time")
    return f"'{text}'"
  else:
    raise LedgerError(f"column {column.name} has type {column.dataType}, which the ledger cannot save exactly")
  if not pattern.match(text):
    raise LedgerError(f"column {column.name} ({column.dataType}) holds a value that is not a number")
  if column.dataType in floatTypes and "e" not in text.lower():
    return text + "e0"
  return text


def rowText(row):
  return ",".join(row)


def tableDigest(rows):
  """What SHA2(GROUP_CONCAT(row ORDER BY BINARY row SEPARATOR '|'), 256) gives on the server: None (NULL) for no rows."""
  if not rows:
    return None
  return hashlib.sha256("|".join(sorted(rowText(row) for row in rows)).encode("ascii")).hexdigest()


def scopeChecksum(tables):
  """One checksum of a scope's tables, column names and row digests."""
  lines = [f"{table['table']}\t{','.join(column['name'] for column in table['columns'])}\t{table['digest'] or 'NULL'}"
    for table in tables]
  return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def describeScope(tables):
  """The scope's tables with their row digests and counts in place of their rows."""
  return [{"table": table["table"], "where": table["where"], "columns": table["columns"], "digest": tableDigest(table["rows"]),
    "rowCount": len(table["rows"])} for table in tables]


def rowExpression(columns):
  return "CONCAT_WS(',', " + ", ".join(fetchExpression(column) for column in columns) + ")"


def scopeDigestQuery(table, where, columns):
  return (f"(SELECT SHA2(GROUP_CONCAT(ledgerRow ORDER BY BINARY ledgerRow SEPARATOR '|'), 256) FROM (SELECT "
    f"{rowExpression(columns)} AS ledgerRow FROM {quoteIdentifier(table)} WHERE ({where})) AS ledgerRows)")


def outsideDigestQuery(table, wheres, columns):
  """The count and the sums of the four 64-bit quarters of each row's SHA-256, over the rows no where matches: an exact
  digest of a whole table that needs no more memory than a few numbers."""
  quarters = ", ".join(f"SUM(CAST(CONV(SUBSTRING(ledgerRowHash, {start}, 16), 16, 10) AS UNSIGNED))" for start in (1, 17, 33, 49))
  outside = "(" + " OR ".join(f"({where})" for where in wheres) + ") IS NOT TRUE"
  return (f"(SELECT CONCAT_WS(':', COUNT(*), {quarters}) FROM (SELECT SHA2({rowExpression(columns)}, 256) AS ledgerRowHash "
    f"FROM {quoteIdentifier(table)} WHERE {outside}) AS ledgerRows)")


def entryDigestQuery(table):
  return scopeDigestQuery(table["table"], table["where"], [columnFromRecord(column) for column in table["columns"]])


def digestCondition(table, where, columns, digest):
  expected = "NULL" if digest is None else f"'{digest}'"
  return f"{scopeDigestQuery(table, where, columns)} <=> {expected}"


def guardBlock(condition, message):
  """A compound statement that fails the script, and so its open transaction, unless condition is true."""
  return ("DELIMITER //\nBEGIN NOT ATOMIC\n"
    f"  IF ({condition}) IS NOT TRUE THEN\n"
    f"    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{message}';\n"
    "  END IF;\nEND//\nDELIMITER ;\n")


def scopeGuards(tables, digests, message):
  return "".join(guardBlock(digestCondition(table["table"], table["where"], [columnFromRecord(column) for column in
    table["columns"]], digest), message) for table, digest in zip(tables, digests))


def scopeTables(tables):
  """Each distinct table of a scope with every where that names it and its columns."""
  grouped = {}
  for table in tables:
    grouped.setdefault(table["table"], ([], [columnFromRecord(column) for column in table["columns"]]))[0].append(table["where"])
  return [(name, wheres, columns) for name, (wheres, columns) in grouped.items()]


def outsideCapture(tables):
  return "".join(f"SET @ledgerOutside{index} = {outsideDigestQuery(name, wheres, columns)};\n"
    for index, (name, wheres, columns) in enumerate(scopeTables(tables)))


def outsideGuards(tables, message):
  return "".join(guardBlock(f"{outsideDigestQuery(name, wheres, columns)} <=> @ledgerOutside{index}", message)
    for index, (name, wheres, columns) in enumerate(scopeTables(tables)))


def checkQuery(checkStatement):
  return f"SELECT applied FROM ({checkStatement}) AS ledgerCheck"


def ledgerTag(change, action):
  return f"/* ledger {change.number} {action} */ "


def tagged(statement, tag):
  """statement with tag where MariaDB 10.5 keeps it in the binary log: first, except in a DROP TABLE, which the server
  rebuilds for the log keeping only a comment right after DROP TABLE [IF EXISTS]."""
  drop = dropTablePattern.match(statement)
  return statement[:drop.end()] + tag + statement[drop.end():] if drop else tag + statement


def taggedStatements(change, action, statements):
  return "".join(f"{tagged(statement, ledgerTag(change, action))};\n" for statement in statements)


def codeWords(statement):
  """The words of statement outside its quoted text and comments, in capitals."""
  return [word.upper() for kind, text in sqlPieces(statement) if kind == "code" for word in re.findall(r"[A-Za-z_]+", text)]


def untaggedLogReason(statement):
  """Why MariaDB 10.5 writes statement to the binary log without the ledger's tag, so drift could not tell the ledger made
  it, or None: it rebuilds views, triggers, routines and events with their definer and CREATE TABLE ... SELECT as a CREATE
  TABLE of its own, and keeps a DROP TABLE's tag only in its plain form."""
  words = codeWords(statement)
  if words[:1] == ["DROP"] and words[1:2] in (["TABLES"], ["TEMPORARY"]):
    return "MariaDB keeps the tag of a DROP TABLE only when it is written DROP TABLE [IF EXISTS] name"
  if words[:1] not in (["CREATE"], ["ALTER"]):
    return None
  kind = next((word for word in words[1:] if word in schemaObjectWords), None)
  if kind in rebuiltSchemaObjects:
    return f"MariaDB writes {words[0]} {kind} to the binary log rebuilt with its definer and without the tag"
  if words[0] == "CREATE" and kind == "TABLE" and "SELECT" in words:
    return "MariaDB writes CREATE TABLE ... SELECT to the binary log as a CREATE TABLE of its own, without the tag"
  return None


def revertScript(tables):
  lines = ["-- Written by ledger.py stage from the rows in saved.json; ledger.py revert runs it to put back exactly those rows."]
  for table in tables:
    columns = [columnFromRecord(record) for record in table["columns"]]
    lines.append(f"DELETE FROM {quoteIdentifier(table['table'])} WHERE ({table['where']});")
    if table["rows"]:
      names = ", ".join(quoteIdentifier(column.name) for column in columns)
      values = ",\n".join("  (" + ", ".join(literal(column, value) for column, value in zip(columns, row)) + ")"
        for row in table["rows"])
      lines.append(f"INSERT INTO {quoteIdentifier(table['table'])} ({names}) VALUES\n{values};")
  return "\n".join(lines) + "\n"


def unescapeBatchField(text):
  replacements = {"n": "\n", "t": "\t", "0": "\0", "\\": "\\"}
  return re.sub(r"\\(.)", lambda match: replacements.get(match.group(1), match.group(0)), text)


def containerShell(command):
  return f"sudo -n docker exec -i {mariadbContainer} sh -c '{command}'"


def mysqlCommand(database, force=False):
  return containerShell('export MYSQL_PWD="$MYSQL_ROOT_PASSWORD"; exec mysql --user=root --batch --skip-column-names --comments '
    + ("--force " if force else "") + f"--default-character-set=utf8mb4 {database}")


def binaryLogCommand(logFile, position):
  if not re.match(r"^[A-Za-z0-9_.-]+$", logFile) or type(position) is not int:
    raise LedgerError(f"baseline.yaml's binary log {logFile!r} at {position!r} is not a file name and a position")
  return containerShell('export MYSQL_PWD="$MYSQL_ROOT_PASSWORD"; exec mysqlbinlog --read-from-remote-server '
    f'--host=localhost --user=root --base64-output=decode-rows --verbose --start-position={position} --to-last-log {logFile}')


def asRepositoryOwner(repository, command):
  return f"sudo -n -u \"$(stat -c %U {repository})\" sh -c 'cd {repository} && {command}'"


readPreamble = ["SET NAMES utf8mb4", "SET time_zone = '+00:00'", "SET SESSION group_concat_max_len = 4294967295"]
writePreamble = ["SET SESSION sql_mode = CONCAT_WS(',', NULLIF(@@sql_mode, ''), 'NO_AUTO_VALUE_ON_ZERO')",
  "SET SESSION TRANSACTION ISOLATION LEVEL SERIALIZABLE"]


class Server:
  """claudeperidot, reached over an SSH connection whose execute(command, standardInput, timeoutSeconds) returns
  (exitCode, stdoutBytes, stderrBytes) and whose streamCommand(command, writeOutput, standardInput, timeoutSeconds) hands
  stdout to writeOutput as it arrives and returns (exitCode, stderrBytes)."""

  def __init__(self, connection, database=databaseName, domain=ledgerGTIDDomain):
    self.connection = connection
    self.database = database
    self.domain = domain

  def run(self, command, standardInput="", timeoutSeconds=120, sensitive=False):
    data = standardInput if isinstance(standardInput, bytes) else standardInput.encode("utf-8")
    exitCode, output, errors = self.connection.execute(command, data, timeoutSeconds)
    if exitCode != 0:
      message = errors.decode("utf-8", "replace").strip()[-2000:]
      raise LedgerError(f"{hostName}: {command[:100]} exited {exitCode}: {redactValues(message) if sensitive else message}")
    return output.decode("utf-8")

  def stream(self, command, consumeLine, timeoutSeconds=600):
    """Runs command and hands each line of its output to consumeLine as it arrives, never holding the whole output."""
    pending = bytearray()

    def receive(chunk):
      pending.extend(chunk)
      while (end := pending.find(b"\n")) != -1:
        consumeLine(bytes(pending[:end]).decode("utf-8", "replace").rstrip("\r"))
        del pending[:end + 1]

    exitCode, errors = self.connection.streamCommand(command, receive, b"", timeoutSeconds)
    if exitCode != 0:
      raise LedgerError(f"{hostName}: {command[:100]} exited {exitCode}: {errors.decode('utf-8', 'replace').strip()[-2000:]}")
    if pending:
      consumeLine(bytes(pending).decode("utf-8", "replace").rstrip("\r"))

  def preamble(self, writes):
    return readPreamble + ([f"SET SESSION gtid_domain_id = {self.domain}"] + writePreamble if writes else [])

  def sql(self, script, writes=False, timeoutSeconds=120, sensitive=False):
    text = "".join(f"{statement};\n" for statement in self.preamble(writes)) + script
    output = self.run(mysqlCommand(self.database), text, timeoutSeconds, sensitive)
    return [line.split("\t") for line in output.splitlines()]

  def sqlByStatement(self, statements):
    """Runs statements with the client's --force, so one failing statement leaves the others running; returns the rows and
    each failed statement's redacted error by its index."""
    preamble = "".join(f"{statement};\n" for statement in self.preamble(False))
    starts = []
    line = preamble.count("\n") + 1
    for statement in statements:
      starts.append(line)
      line += statement.count("\n") + 1
    script = preamble + "".join(f"{statement};\n" for statement in statements)
    exitCode, output, errors = self.connection.execute(mysqlCommand(self.database, force=True), script.encode("utf-8"), 120)
    failures = {}
    for errorLine in errors.decode("utf-8", "replace").splitlines():
      match = mysqlErrorPattern.match(errorLine)
      if not match or match.group(3) is None or int(match.group(3)) < starts[0]:
        raise LedgerError(f"{hostName}: the checks failed: {redactValues(errorLine)[-2000:]}")
      index = max(index for index, start in enumerate(starts) if start <= int(match.group(3)))
      failures[index] = redactValues(f"ERROR {match.group(1)} ({match.group(2)}): {match.group(4)}")
    if exitCode != 0 and not failures:
      raise LedgerError(f"{hostName}: the checks exited {exitCode} without naming a failed statement")
    return [line.split("\t") for line in output.decode("utf-8").splitlines()], failures

  def writeHostFile(self, path, text):
    folder = path.rsplit("/", 1)[0]
    self.run(f"sudo -n sh -c 'umask 077 && mkdir -p {folder} && cat > {path}'", text)

  def readHostFile(self, path):
    return self.run(f"sudo -n cat {path}", sensitive=True)


def runPowerShell(script):
  """Runs a PowerShell 5.1 script handed over on standard input, so nothing it holds reaches a command line."""
  # Errors are written to the console directly: PowerShell serializes its own error stream as CLIXML when it is redirected.
  wrapped = ("$ErrorActionPreference = 'Stop'\n$ProgressPreference = 'SilentlyContinue'\ntry {\n" + script + "\n} catch {\n"
    "  [Console]::Error.WriteLine(($_ | Out-String).Trim())\n  exit 1\n}\n")
  bootstrap = ("$ErrorActionPreference = 'Stop'; Invoke-Expression ([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String("
    "[Console]::In.ReadToEnd())))")
  result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command",
    bootstrap], input=base64.b64encode(wrapped.encode("utf-8")).decode("ascii"), capture_output=True, text=True)
  if result.returncode != 0:
    raise LedgerError(f"a PowerShell step failed (exit {result.returncode}): {(result.stderr or result.stdout).strip()[-2000:]}")
  return result.stdout


def expandClientPath(path):
  return str(Path.home()) + path[1:] if path.startswith("~\\") else path


def normalizedWindowsPath(path):
  for prefix in ("\\\\?\\", "\\??\\"):
    if path.startswith(prefix):
      path = path[len(prefix):]
  return os.path.normcase(os.path.normpath(path))


def iniValue(path, section, key):
  if not path.is_file():
    return None
  currentSection = None
  for line in path.read_bytes().decode("latin-1").splitlines():
    sectionMatch = re.match(r"^\[(.+)\]\s*$", line)
    if sectionMatch:
      currentSection = sectionMatch.group(1)
    elif currentSection is not None and currentSection.lower() == section.lower() and "=" in line:
      name, value = line.split("=", 1)
      if name.strip().lower() == key.lower():
        return value
  return None


def credentialExists(target):
  """Whether Credential Manager holds a generic credential named target; the password is never read."""
  import ctypes
  from ctypes import wintypes
  advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
  advapi32.CredReadW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p))
  advapi32.CredReadW.restype = wintypes.BOOL
  advapi32.CredFree.argtypes = (ctypes.c_void_p,)
  credential = ctypes.c_void_p()
  if advapi32.CredReadW(target, 1, 0, ctypes.byref(credential)):
    advapi32.CredFree(credential)
    return True
  error = ctypes.get_last_error()
  if error == 1168:
    return False
  raise LedgerError(f"reading Credential Manager for {target} failed with Windows error {error}")


def loginPersonaExists(path, server, character):
  """Whether MacroQuest's login.db at path holds a persona row for character on server; only the key columns are read."""
  if not path.is_file():
    return False
  try:
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    try:
      row = connection.execute("SELECT EXISTS (SELECT 1 FROM personas JOIN characters ON personas.character_id = characters.id "
        "WHERE characters.server = lower(?) AND characters.character = lower(?))", (server, character)).fetchone()
    finally:
      connection.close()
  except sqlite3.Error as error:
    raise LedgerError(f"{path} cannot be read as MacroQuest's login.db: {error}") from error
  return row[0] == 1


def exportedNames(path):
  """The names in a PE image's export table (PE32 or PE32+)."""
  data = path.read_bytes()
  try:
    if data[:2] != b"MZ":
      raise ValueError("it has no MZ header")
    header = struct.unpack_from("<I", data, 0x3C)[0]
    if data[header:header + 4] != b"PE\0\0":
      raise ValueError("it has no PE signature")
    sectionCount, = struct.unpack_from("<H", data, header + 6)
    optionalSize, = struct.unpack_from("<H", data, header + 20)
    optional = header + 24
    magic, = struct.unpack_from("<H", data, optional)
    directoryOffsets = {0x10B: 96, 0x20B: 112}
    if magic not in directoryOffsets:
      raise ValueError(f"its optional header magic is {magic:#x}, neither PE32 nor PE32+")
    exportAddress, _ = struct.unpack_from("<II", data, optional + directoryOffsets[magic])
    if exportAddress == 0:
      return set()
    sections = [struct.unpack_from("<IIII", data, optional + optionalSize + index * 40 + 8) for index in range(sectionCount)]

    def fileOffset(address):
      for virtualSize, virtualAddress, rawSize, rawPointer in sections:
        if virtualAddress <= address < virtualAddress + max(virtualSize, rawSize):
          return address - virtualAddress + rawPointer
      raise ValueError(f"the address {address:#x} lies in no section")

    exportTable = fileOffset(exportAddress)
    nameCount, = struct.unpack_from("<I", data, exportTable + 24)
    nameAddresses = fileOffset(struct.unpack_from("<I", data, exportTable + 32)[0]) if nameCount else 0
    names = set()
    for index in range(nameCount):
      start = fileOffset(struct.unpack_from("<I", data, nameAddresses + index * 4)[0])
      names.add(data[start:data.index(b"\0", start)].decode("ascii"))
    return names
  except (ValueError, struct.error, UnicodeDecodeError) as error:
    raise LedgerError(f"{path} cannot be read as a PE image: {error}") from error


def clientCheckPasses(check):
  if "credential" in check:
    return credentialExists(check["credential"])
  path = Path(expandClientPath(check["path"]))
  if check.get("exists") == "file":
    return path.is_file() and not path.is_junction()
  if check.get("exists") == "directory":
    return path.is_dir() and not path.is_junction()
  if check.get("exists") == "junction":
    return path.is_junction() and normalizedWindowsPath(os.readlink(path)) == normalizedWindowsPath(
      expandClientPath(check["target"]))
  if "sha256" in check:
    return path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == check["sha256"]
  if "server" in check:
    return loginPersonaExists(path, check["server"], check["character"])
  if "exports" in check:
    return path.is_file() and check["exports"] in exportedNames(path)
  return iniValue(path, check["section"], check["key"]) == check["value"]


@dataclass(frozen=True)
class ChangeState:
  change: Change
  state: str
  done: int
  total: int
  note: str = ""

  @property
  def isApplied(self):
    return self.state == "applied"

  @property
  def label(self):
    text = {"applied": "applied", "notApplied": "not applied", "partlyApplied": f"partly applied ({self.done} of {self.total})",
      "checkFailed": "check failed", "recorded": "recorded"}
    return text[self.state] + (f"; {self.note}" if self.note else "")


def stateFromCount(change, done, total, note=""):
  if not 0 <= done <= total:
    return ChangeState(change, "checkFailed", 0, total, f"it reports {done} of {total}")
  state = "applied" if done == total else "notApplied" if done == 0 else "partlyApplied"
  return ChangeState(change, state, done, total, note)


@dataclass(frozen=True)
class PlanStep:
  line: int
  number: int
  ticked: bool
  text: str
  changes: tuple

  def describe(self):
    return f"{self.number}. {self.text}" + (f" [{', '.join(self.changes)}]" if self.changes else "")


def readPlanSteps(text):
  steps = []
  for index, line in enumerate(text.split("\n")):
    match = stepPattern.match(line)
    if match:
      changes = tuple(match.group(4).split(", ")) if match.group(4) else ()
      steps.append(PlanStep(index, int(match.group(2)), match.group(1) == "x", match.group(3), changes))
  return steps


def sectionSpan(lines, heading):
  try:
    start = lines.index(f"## {heading}")
  except ValueError as error:
    raise LedgerError(f"plan.md has no '## {heading}' section") from error
  end = next((index for index in range(start + 1, len(lines)) if lines[index].startswith("## ")), len(lines))
  return start, end


def planSection(text, heading):
  lines = text.split("\n")
  start, end = sectionSpan(lines, heading)
  return "\n".join(lines[start + 1:end]).strip()


def replaceSection(text, heading, body):
  lines = text.split("\n")
  start, end = sectionSpan(lines, heading)
  return "\n".join(lines[:start + 1] + ["", body, ""] + lines[end:])


def rewritePlan(text, appliedByNumber, affectedNumbers):
  """plan.md with each step that names an affected change ticked when all its changes are applied, and the In progress
  and Next sections naming the first two unticked steps."""
  lines = text.split("\n")
  for step in readPlanSteps(text):
    if affectedNumbers & set(step.changes):
      ticked = all(appliedByNumber[number] for number in step.changes)
      lines[step.line] = f"- [{'x' if ticked else ' '}] " + step.describe()
  text = "\n".join(lines)
  remaining = [step for step in readPlanSteps(text) if not step.ticked]
  text = replaceSection(text, "In progress", remaining[0].describe() if remaining else "None: every step is done.")
  return replaceSection(text, "Next", remaining[1].describe() if len(remaining) > 1 else "None.")


@dataclass
class BinaryLogTransaction:
  gtid: str
  domain: int
  time: str
  position: str
  writes: dict = field(default_factory=dict)
  statements: list = field(default_factory=list)


eventHeaderPattern = re.compile(r"^#(\d{6}\s+\d{1,2}:\d{2}:\d{2})\s+server id \d+\s+end_log_pos \d+\b(.*)$")
gtidPattern = re.compile(r"\sGTID (\d+)-(\d+)-(\d+)\b")
queryEventPattern = re.compile(r"\sQuery\s+thread_id=")
annotateEventPattern = re.compile(r"\sAnnotate_rows:")
rowEventPattern = re.compile(r"^### (INSERT INTO|UPDATE|DELETE FROM) `([^`]+)`\.`([^`]+)`$")
tagPattern = re.compile(rf"^(\s*|DROP TABLE (?:IF EXISTS )?)/\* ledger ({changeNumberPattern}) (apply|revert) \*/\s*")
versionedCommentPattern = re.compile(r"/\*M?!\d{5,6} ?(.*?)\*/", re.DOTALL)
clientCommandCommentPattern = re.compile(r"/\*!\\C [^*]*\*/")
# mysqlbinlog names a statement's session database, not the one it writes, and prints a use line only when that changes to a
# database: None is a session with no database, which writes peq only by naming it, and a statement that names peq counts as
# peq's whatever database it ran in. MariaDB logs CREATE, ALTER and DROP DATABASE without their session's database, so no use
# line comes before them either: they count as the database they name.
databaseQualifierPattern = re.compile(rf"(?<![\w$`.])(?:`{databaseName}`|{databaseName})\s*\.")
ignoredQueryStatements = re.compile(r"^(SET\b(?!\s+(?:PASSWORD|DEFAULT\s+ROLE)\b)|use\b|BEGIN\b|COMMIT\b|START TRANSACTION\b|"
  r"ROLLBACK\b|XA\b)", re.IGNORECASE)
identifierText = r"(?:`(?:[^`]|``)+`|[\w$]+)"
tableStatementPatterns = {
  "TRUNCATE": re.compile(rf"^TRUNCATE\s+(?:TABLE\s+)?(?:({identifierText})\s*\.\s*)?({identifierText})", re.IGNORECASE),
  "ALTER TABLE": re.compile(rf"^ALTER\s+(?:ONLINE\s+)?(?:IGNORE\s+)?TABLE\s+(?:IF\s+EXISTS\s+)?(?:({identifierText})\s*\.\s*)?"
    rf"({identifierText})", re.IGNORECASE),
}
databaseStatementPattern = re.compile(rf"^(?:CREATE(?:\s+OR\s+REPLACE)?|ALTER|DROP)\s+(?:DATABASE|SCHEMA)\s+"
  rf"(?:IF\s+(?:NOT\s+)?EXISTS\s+)?(?!(?:CHARACTER|CHARSET|DEFAULT|COLLATE|COMMENT)\b)({identifierText})", re.IGNORECASE)


def isAccountStatement(statement):
  """True for a statement that changes the server's users, roles or privileges, or flushes: it writes no peq table."""
  words = codeWords(statement)
  if words[:1] in (["GRANT"], ["REVOKE"], ["FLUSH"]) or words[:2] == ["SET", "PASSWORD"] \
      or words[:3] == ["SET", "DEFAULT", "ROLE"]:
    return True
  subject = words[3:4] if words[1:3] == ["OR", "REPLACE"] else words[1:2]
  return words[:1] in (["CREATE"], ["DROP"], ["ALTER"], ["RENAME"]) and subject in (["USER"], ["ROLE"])


def unquoteIdentifier(text):
  return text[1:-1].replace("``", "`") if text.startswith("`") else text


def withoutComments(statement):
  return "".join(" " if kind == "comment" else piece for kind, piece in sqlPieces(statement)).strip()


def namedDatabase(statement):
  """The database a CREATE, ALTER or DROP DATABASE names, or None."""
  match = databaseStatementPattern.match(withoutComments(statement))
  return unquoteIdentifier(match.group(1)) if match else None


def runtimeStatement(statement, database):
  """(table, operation) for a statement the servers or Spire run in peq on their own, or None: a TRUNCATE of a runtime table
  (world empties the trader and buyer tables at boot), or Spire's migration of one of its own spire_ tables at start."""
  text = withoutComments(statement)
  for operation, pattern in tableStatementPatterns.items():
    match = pattern.match(text)
    if not match:
      continue
    table = unquoteIdentifier(match.group(2))
    if (unquoteIdentifier(match.group(1)) if match.group(1) else database) != databaseName:
      return None
    if operation == "TRUNCATE" and table in runtimeTables or operation == "ALTER TABLE" and table.startswith("spire_"):
      return table, operation
    return None
  return None


def splitTag(text):
  """The change number a statement's ledger tag names (or None) and the statement without the tag: the tag leads it, or
  follows DROP TABLE [IF EXISTS] in a DROP TABLE the server rebuilt."""
  match = tagPattern.match(text or "")
  return (match.group(2), match.group(1).lstrip() + text[match.end():]) if match else (None, text)


class BinaryLogParser:
  """Reads mysqlbinlog --base64-output=decode-rows --verbose output line by line and hands each transaction, with its row
  writes per table and the ledger tag of the statement that made them, and its statements with their tags and the database
  each ran in, to onTransaction."""

  def __init__(self, onTransaction):
    self.onTransaction = onTransaction
    self.current = None
    self.position = ""
    self.inQuery = False
    self.inAnnotate = False
    self.annotate = None
    self.queryLines = []
    self.database = None

  def feed(self, line):
    if line.startswith("# at "):
      self.position = line[5:].strip()
      self.inQuery = self.inAnnotate = False
      self.queryLines = []
      return
    header = eventHeaderPattern.match(line)
    if header:
      gtid = gtidPattern.search(header.group(2))
      if gtid:
        self.finish()
        self.current = BinaryLogTransaction(f"{gtid.group(1)}-{gtid.group(2)}-{gtid.group(3)}", int(gtid.group(1)),
          header.group(1), self.position)
        self.annotate = None
      self.inQuery = bool(queryEventPattern.search(header.group(2)))
      self.inAnnotate = bool(annotateEventPattern.search(header.group(2)))
      if self.inAnnotate:
        self.annotate = None
      self.queryLines = []
      return
    if self.inAnnotate and line.startswith("#Q> "):
      if self.annotate is None:
        self.annotate = line[4:]
      return
    rowEvent = rowEventPattern.match(line)
    if rowEvent and self.current:
      key = (rowEvent.group(2), rowEvent.group(3), rowEvent.group(1).split()[0], splitTag(self.annotate)[0])
      self.current.writes[key] = self.current.writes.get(key, 0) + 1
      return
    if self.inQuery and self.current and not line.startswith("#"):
      if line.endswith("/*!*/;"):
        statement = "\n".join(self.queryLines + [line[:-len("/*!*/;")]])
        self.queryLines = []
        statement = versionedCommentPattern.sub(lambda match: match.group(1), clientCommandCommentPattern.sub(" ", statement))
        tag, text = splitTag(statement.strip())
        text = text.strip()
        use = re.match(r"^use `([^`]+)`$", text)
        if use:
          self.database = use.group(1)
        elif text and not ignoredQueryStatements.match(text):
          self.current.statements.append((tag, text, self.database))
      else:
        self.queryLines.append(line)

  def finish(self):
    if self.current is not None:
      self.onTransaction(self.current)
      self.current = None


def parseBinaryLog(text):
  transactions = []
  parser = BinaryLogParser(transactions.append)
  for line in text.splitlines():
    parser.feed(line)
  parser.finish()
  return transactions


class DriftAccount:
  """Sorts binary-log transactions into what the ledger wrote within its changes' scopes, the runtime tables' writes, the
  server account changes, and everything else drift reports."""

  def __init__(self, changes):
    self.changes = {change.number: change for change in changes}
    self.problems = []
    self.runtime = {}
    self.accountChanges = []

  def describe(self, transaction, parts):
    return f"{transaction.time} GTID {transaction.gtid} at {transaction.position}: " + "; ".join(parts)

  def countRuntime(self, table, operation, count):
    self.runtime[(table, operation)] = self.runtime.get((table, operation), 0) + count

  def add(self, transaction):
    if transaction.domain == ledgerGTIDDomain:
      self.addLedger(transaction)
      return
    parts = []
    for (database, table, operation, _), count in sorted(transaction.writes.items(), key=lambda item: item[0][:3]):
      if database != databaseName:
        continue
      if table in runtimeTables:
        self.countRuntime(table, operation, count)
      elif isWatched(table):
        parts.append(f"{operation} {table} x{count}")
    accounts = []
    for _, text, session in transaction.statements:
      database = namedDatabase(text) or session
      shown = redactValues(" ".join(text.split()))[:200]
      runtime = runtimeStatement(text, database)
      if isAccountStatement(text):
        accounts.append(shown)
      elif runtime:
        self.countRuntime(*runtime, 1)
      elif database == databaseName or databaseQualifierPattern.search(text):
        parts.append(shown)
    if accounts:
      self.accountChanges.append(self.describe(transaction, accounts))
    if parts:
      self.problems.append("unaccounted write: " + self.describe(transaction, parts))

  def addLedger(self, transaction):
    parts = []
    for (database, table, operation, tag), count in sorted(transaction.writes.items(), key=lambda item: (item[0][:3],
        item[0][3] or "")):
      change = self.changes.get(tag)
      if change is None or change.kind != "db":
        whose = f"by {tag}, which is not a db change in the ledger" if tag else "with no ledger tag"
      elif database != databaseName or table not in change.tables:
        whose = f"by {tag}, whose scope is {', '.join(sorted(set(change.tables)))}"
      else:
        continue
      parts.append(f"{operation} {database}.{table} x{count} {whose}")
    for tag, text, database in transaction.statements:
      change = self.changes.get(tag)
      if change is None or change.form != "schema" or database != databaseName:
        where = "" if database == databaseName else f" in {database or 'no database'}"
        parts.append(redactValues(" ".join(text.split()))[:200] + where + (f" by {tag}, which is not a schema change in the "
          "ledger" if tag else " with no ledger tag"))
    if parts:
      self.problems.append("ledger write outside its change's scope: " + self.describe(transaction, parts))

  def runtimeLines(self):
    return [f"runtime (the servers' own writes, not drift): {operation} {table} x{count}"
      for (table, operation), count in sorted(self.runtime.items())]

  def accountLines(self):
    return [f"server account change (not a peq write): {line}" for line in self.accountChanges]


def git(folder, *arguments):
  result = subprocess.run(["git", "-C", str(folder), *arguments], capture_output=True, text=True, encoding="utf-8")
  if result.returncode != 0:
    raise LedgerError(f"git {' '.join(arguments)} in {folder} failed: {result.stderr.strip()}")
  return result.stdout


def gitAnswers(folder, *arguments):
  """True or False for a git command that answers by exiting 0 or 1."""
  result = subprocess.run(["git", "-C", str(folder), *arguments], capture_output=True, text=True, encoding="utf-8")
  if result.returncode not in (0, 1):
    raise LedgerError(f"git {' '.join(arguments)} in {folder} failed: {result.stderr.strip()}")
  return result.returncode == 0


def pushBranch(folder):
  branch = git(folder, "rev-parse", "--abbrev-ref", "HEAD").strip()
  if branch in ("HEAD", "main", "master"):
    raise LedgerError(f"the ledger pushes only a working branch such as claude, and HEAD is {branch}")
  git(folder, "push", "--quiet", "origin", branch)


def writeJSON(path, value):
  path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def readJSON(path, missing):
  if not path.is_file():
    raise LedgerError(missing)
  return json.loads(path.read_text(encoding="utf-8"))


renderCheckScript = r'''
import json
import sys


def flatten(value, prefix, keys):
  if isinstance(value, dict):
    for name, inner in value.items():
      flatten(inner, prefix + [str(name)], keys)
  else:
    keys[".".join(prefix)] = value


def load(path):
  with open(path, encoding="utf-8") as handle:
    text = handle.read()
  if path.endswith(".json"):
    keys = {}
    flatten(json.loads(text), [], keys)
    return keys
  return dict(line.split("=", 1) for line in text.splitlines() if "=" in line and not line.lstrip().startswith("#"))


template = load(sys.argv[1])
live = load(sys.argv[2])
for key in sys.argv[3:]:
  if key not in template or template[key] not in ("", None) or live.get(key) in ("", None):
    print("unfilled " + key)
  else:
    live[key] = template[key]
print("match" if live == template else "differ")
'''


class Ledger:
  def __init__(self, folder, openServer, push):
    """openServer() returns the Server; push(folder) publishes the branch of the repository folder is in."""
    self.folder = folder
    self.openServer = openServer
    self.push = push
    self.connectedServer = None

  @property
  def server(self):
    if self.connectedServer is None:
      self.connectedServer = self.openServer()
    return self.connectedServer

  @property
  def planPath(self):
    return self.folder / "plan.md"

  @property
  def baselinePath(self):
    return self.folder / "baseline.yaml"

  @property
  def tableChecksumsPath(self):
    return self.folder / "tableChecksums.json"

  def find(self, identifier):
    matches = [change for change in loadChanges(self.folder) if identifier in (change.number, change.identifier)]
    if not matches:
      raise LedgerError(f"no change {identifier} in {self.folder / 'changes'}")
    return matches[0]

  def relative(self, path):
    return path.relative_to(self.folder).as_posix()

  def record(self, paths, message):
    """Commits paths (and only them) and pushes; nothing to commit is not an error, so a re-run converges."""
    names = [self.relative(path) for path in paths if path.exists()
      or git(self.folder, "ls-files", "--", self.relative(path)).strip()]
    if names:
      git(self.folder, "add", "-A", "--", *names)
    if names and git(self.folder, "status", "--porcelain", "--", *names).strip():
      git(self.folder, "commit", "-q", "-m", message, "--", *names)
    self.push(self.folder)
    log.info("recorded and pushed: %s", message)

  def requireCommitted(self, change):
    paths = [self.relative(change.folder), self.relative(self.planPath), self.relative(self.tableChecksumsPath)]
    pending = git(self.folder, "status", "--porcelain", "--", *paths).strip()
    if pending:
      raise LedgerError(f"{change.identifier}, plan.md or tableChecksums.json has uncommitted edits; stage the change (and "
        f"commit plan.md) first, so what runs is what the ledger holds:\n{pending}")

  def readIntent(self, change):
    path = change.folder / "intent.json"
    if not path.is_file():
      return None
    intent = json.loads(path.read_text(encoding="utf-8"))
    if intent.get("intent") not in intents:
      raise LedgerError(f"{change.identifier}/intent.json holds {intent!r}, not applying or reverting")
    return intent

  def writeIntent(self, change, intent, **details):
    """Commits and pushes the change's intent before anything is written, so an interrupted apply or revert is known."""
    writeJSON(change.folder / "intent.json", {"intent": intent, **details})
    self.record([change.folder], f"{intent.capitalize()} {change.identifier}: {change.fields['title']}")

  def applyStatements(self, change):
    statements = splitStatements(self.readFile(change, "apply.sql"))
    if not statements:
      raise LedgerError(f"{change.identifier}/apply.sql holds no statements")
    if change.form == "rows":
      outside = [statement for statement in statements if firstKeyword(statement) not in rowTransactionKeywords]
      if outside:
        raise LedgerError(f"{change.identifier} changes rows in one transaction, so apply.sql holds only INSERT, UPDATE, "
          f"DELETE and REPLACE; it also has: {outside[0][:120]}")
    elif len(statements) != change.steps:
      raise LedgerError(f"{change.identifier} has schemaSteps {change.steps} but apply.sql holds {len(statements)} statements")
    else:
      self.requireTaggedInLog(change, "apply.sql", statements)
    return statements

  @staticmethod
  def requireTaggedInLog(change, name, statements):
    for statement in statements:
      reason = untaggedLogReason(statement)
      if reason:
        raise LedgerError(f"{change.identifier}/{name}: {reason}, so drift could not tell the ledger made it: "
          f"{statement[:120]}")

  def checkStatement(self, change):
    statements = splitStatements(self.readFile(change, "check.sql"))
    if len(statements) != 1 or firstKeyword(statements[0]) not in ("SELECT", "WITH"):
      raise LedgerError(f"{change.identifier}/check.sql must be one SELECT giving one row with a column named applied")
    return statements[0]

  def revertStatements(self, change, saved=None):
    if change.form == "rows" and saved["sensitive"]:
      text = self.server.readHostFile(f"{change.hostFolder}/revert.sql")
      if hashlib.sha256(text.encode("utf-8")).hexdigest() != saved["hostCopy"]["revertSHA256"]:
        raise LedgerError(f"{change.identifier}: revert.sql on {hostName} is not the one stage wrote")
    else:
      text = self.readFile(change, "revert.sql")
    statements = splitStatements(text)
    if change.form == "schema" and len(statements) != change.steps:
      raise LedgerError(f"{change.identifier}/revert.sql must hold {change.steps} statements, the inverse of each apply step in "
        f"reverse order; it holds {len(statements)}")
    if change.form == "schema":
      self.requireTaggedInLog(change, "revert.sql", statements)
    return statements

  def readFile(self, change, name):
    path = change.folder / name
    if not path.is_file():
      raise LedgerError(f"{change.identifier} has no {name}")
    return path.read_text(encoding="utf-8")

  def readSaved(self, change):
    return readJSON(change.folder / "saved.json", f"{change.identifier} is not staged: run stage {change.number} first")

  def readApplied(self, change):
    return readJSON(change.folder / "applied.json", f"{change.identifier} has no applied.json: it was not applied through the "
      "ledger")

  def readTableChecksums(self):
    return json.loads(self.tableChecksumsPath.read_text(encoding="utf-8")) if self.tableChecksumsPath.is_file() else {}

  def updateTableChecksums(self, checksums, by):
    """Records each table's checksum (or absent) after a ledger write, the value drift expects until the next one."""
    recorded = self.readTableChecksums()
    recorded.update({table: {"checksum": checksum, "by": by} for table, checksum in checksums.items()})
    writeJSON(self.tableChecksumsPath, dict(sorted(recorded.items())))

  def databaseStates(self, changes):
    """Each db change's state from its check.sql, all in one script; a check that fails or gives no whole number marks
    only its own change."""
    states = {}
    runnable = []
    for change in changes:
      try:
        runnable.append((change, self.checkStatement(change)))
      except LedgerError as error:
        states[change.number] = ChangeState(change, "checkFailed", 0, change.steps, str(error))
    if runnable:
      rows, failures = self.server.sqlByStatement([f"SELECT '{change.number}', applied FROM ({check}) AS ledgerCheck"
        for change, check in runnable])
      results = {}
      for row in rows:
        if len(row) != 2 or row[0] not in {change.number for change, _ in runnable}:
          raise LedgerError(f"the checks gave the row {row!r}")
        results.setdefault(row[0], []).append(row[1])
      for index, (change, _) in enumerate(runnable):
        note = "" if (change.folder / "saved.json").is_file() else "not staged"
        values = results.get(change.number, [])
        if index in failures:
          states[change.number] = ChangeState(change, "checkFailed", 0, change.steps, failures[index])
        elif len(values) != 1 or not numberPatterns["integer"].match(values[0]):
          problem = "gave no row" if not values else "gave more than one row" if len(values) > 1 else f"gave {values[0]}"
          states[change.number] = ChangeState(change, "checkFailed", 0, change.steps, f"check.sql {problem}, not a whole number")
        else:
          states[change.number] = stateFromCount(change, int(values[0]), change.steps, note)
    return states

  def repositoryHashes(self, change, commit=None):
    """Each scope file's SHA-256 in the host checkout, or at commit when given; None for a file that is absent."""
    repository = change.fields["repository"]
    files = " ".join(change.fields["scope"])
    if commit is None:
      loop = f'for f in {files}; do if [ -f "$f" ]; then sha256sum -- "$f"; else echo "absent  $f"; fi; done'
    else:
      loop = (f'git cat-file -e "{commit}^{{commit}}" 2>/dev/null || {{ echo "commit {commit} is not in the checkout" >&2; '
        f'exit 3; }}; for f in {files}; do if git cat-file -e {commit}:"$f" 2>/dev/null; then '
        f'echo "$(git show {commit}:"$f" | sha256sum | cut -d" " -f1)  $f"; else echo "absent  $f"; fi; done')
    hashes = {}
    for line in self.server.run(asRepositoryOwner(repository, loop)).splitlines():
      digest, path = line.split("  ", 1)
      hashes[path] = None if digest == "absent" else digest
    if set(hashes) != set(change.fields["scope"]):
      raise LedgerError(f"{change.identifier}: the host reported files {sorted(hashes)}, not the scope")
    return hashes

  def fetchHost(self, change, commits=None):
    """Fetches the host checkout's origin and fails unless every commit is then in it."""
    commits = commits or change.fields["commits"]
    script = ("git fetch --quiet origin && for c in " + " ".join(commits) + '; do git cat-file -e "$c^{commit}" 2>/dev/null || '
      'echo "$c"; done')
    missing = self.server.run(asRepositoryOwner(change.fields["repository"], script)).split()
    if missing:
      raise LedgerError(f"{change.identifier}: {', '.join(missing)} is not in {change.fields['repository']} on {hostName} even "
        "after fetching its origin: push it from the source repository")

  def renderedProblems(self, change):
    """The rendered files that do not follow their templates with the host's keys filled in; values are never read back."""
    problems = []
    for entry in change.fields.get("rendered", []):
      template = f"{change.fields['repository']}/{entry['template']}"
      lines = self.server.run(f"sudo -n python3 - {template} {entry['file']} {' '.join(entry['filledKeys'])}",
        renderCheckScript).split()
      if lines[-1:] != ["match"] or len(lines) > 1:
        unfilled = [line for line in lines if line not in ("match", "differ", "unfilled")]
        problems.append(f"{entry['file']} " + (f"lacks {', '.join(unfilled)}" if unfilled else "does not follow its template"))
    return problems

  def repositoryState(self, change):
    savedPath = change.folder / "saved.json"
    if not savedPath.is_file():
      return ChangeState(change, "notApplied", 0, len(change.fields["scope"]), "not staged, so its files cannot be judged")
    saved = json.loads(savedPath.read_text(encoding="utf-8"))
    current = self.repositoryHashes(change)
    rendered = self.renderedProblems(change)
    done = sum(current[path] == saved["after"][path] for path in current)
    if done == len(current) and not rendered:
      return ChangeState(change, "applied", done, done)
    if all(current[path] == saved["before"][path] for path in current) and not rendered:
      return ChangeState(change, "notApplied", 0, len(current))
    return ChangeState(change, "partlyApplied", done, len(current), "; ".join(rendered)
      or "some files are neither as committed nor as staged")

  def clientState(self, change):
    checks = change.fields["checks"]
    return stateFromCount(change, sum(clientCheckPasses(check) for check in checks), len(checks))

  def states(self, changes):
    states = self.databaseStates([change for change in changes if change.kind == "db"])
    for change in changes:
      if change.kind in ("quest", "config"):
        states[change.number] = self.repositoryState(change)
      elif change.kind == "client":
        states[change.number] = self.clientState(change)
      elif change.kind == "files":
        states[change.number] = self.filesState(change)
      elif change.kind == "lab":
        states[change.number] = ChangeState(change, "recorded", 0, len(change.fields["checks"]), "its checks run through "
          + " and ".join(sorted({check["via"] for check in change.fields["checks"]})) + ", by hand")
    return {change.number: states[change.number] for change in changes}

  def state(self, change):
    return self.states([change])[change.number]

  def requireState(self, change, expected, action, state=None):
    state = state or self.state(change)
    if state.state != expected:
      raise LedgerError(f"{action} needs {change.identifier} {'applied' if expected == 'applied' else 'not applied'}; "
        f"it is {state.label}")
    return state

  def scopeColumns(self, change):
    """Each scope table's stored columns; refuses a view, a table that is missing, or one that is not InnoDB, since a row
    change is one transaction."""
    tables = sorted(set(change.tables))
    names = ", ".join(f"'{table}'" for table in tables)
    kinds = {row[0]: (row[1], row[2]) for row in self.server.sql("SELECT TABLE_NAME, TABLE_TYPE, ENGINE FROM "
      f"information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME IN ({names});\n")}
    for table in tables:
      if table not in kinds:
        raise LedgerError(f"{change.identifier}: {table} is not a table in {self.server.database}")
      if kinds[table] != ("BASE TABLE", "InnoDB"):
        raise LedgerError(f"{change.identifier}: {table} is a {kinds[table][0]} with engine {kinds[table][1]}; a row change "
          "runs in one transaction, so its tables must be InnoDB")
    columns = {table: [] for table in tables}
    for row in self.server.sql("SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE, IFNULL(CHARACTER_SET_NAME, ''), EXTRA FROM "
        f"information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME IN ({names}) "
        "ORDER BY TABLE_NAME, ORDINAL_POSITION;\n"):
      if len(row) != 5:
        raise LedgerError(f"information_schema gave the column row {row!r}")
      if "GENERATED" not in row[4].upper():
        columns[row[0]].append(Column(row[1], row[2].lower(), row[3]))
    return columns

  def fetchScope(self, change):
    """The scope's columns and rows, each value encoded as N (NULL) or V and the hex of its bytes or text form."""
    columns = self.scopeColumns(change)
    tables = change.tables
    script = "".join(f"SELECT '{index}', {', '.join(fetchExpression(column) for column in columns[entry['table']])} FROM "
      f"{quoteIdentifier(entry['table'])} WHERE ({entry['where']});\n" for index, entry in enumerate(change.fields["scope"]))
    rows = {index: [] for index in range(len(tables))}
    for row in self.server.sql(script, sensitive=True):
      index = int(row[0])
      values = row[1:]
      if len(values) != len(columns[tables[index]]) or not all(encodedValuePattern.match(value) for value in values):
        raise LedgerError(f"{change.identifier}: a row of {tables[index]} came back with {len(values)} values, not "
          f"{len(columns[tables[index]])} encoded ones")
      rows[index].append(values)
    return [{"table": entry["table"], "where": entry["where"], "columns": [column.asRecord() for column in
      columns[entry["table"]]], "rows": sorted(rows[index], key=rowText)} for index, entry in enumerate(change.fields["scope"])]

  def scopeDigests(self, tables, sensitive=False):
    """Each scope entry's row digest, computed on the server; None for no rows."""
    script = "".join(f"SELECT '{index}', {entryDigestQuery(table)};\n" for index, table in enumerate(tables))
    digests = {}
    for row in self.server.sql(script, sensitive=sensitive):
      if len(row) != 2 or not (row[1] == "NULL" or hashPattern.match(row[1])):
        raise LedgerError(f"a scope digest came back as {row!r}")
      digests[int(row[0])] = None if row[1] == "NULL" else row[1]
    if set(digests) != set(range(len(tables))):
      raise LedgerError(f"the scope digests answered for {sorted(digests)}, not {len(tables)} entries")
    return [digests[index] for index in range(len(tables))]

  def tableChecksums(self, tables):
    if not tables:
      return {}
    checksums = {}
    for row in self.server.sql(f"CHECKSUM TABLE {', '.join(quoteIdentifier(table) for table in tables)} EXTENDED;\n",
        timeoutSeconds=1800):
      if len(row) != 2 or not numberPatterns["integer"].match(row[1]):
        raise LedgerError(f"CHECKSUM TABLE gave {row!r}")
      checksums[row[0].split(".", 1)[1]] = int(row[1])
    if set(checksums) != set(tables):
      raise LedgerError(f"CHECKSUM TABLE answered for {sorted(checksums)}, not {sorted(tables)}")
    return checksums

  def schemaFingerprint(self, change, createFolder=None):
    """Each scope table's definition (without its AUTO_INCREMENT counter) and checksum, or absent."""
    tables = change.tables
    names = ", ".join(f"'{table}'" for table in sorted(set(tables)))
    existing = {row[0] for row in self.server.sql("SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = "
      f"DATABASE() AND TABLE_NAME IN ({names});\n")}
    present = [table for table in tables if table in existing]
    rows = self.server.sql("".join(f"SHOW CREATE TABLE {quoteIdentifier(table)};\n" for table in present)) if present else []
    if len(rows) != len(present):
      raise LedgerError(f"SHOW CREATE TABLE gave {len(rows)} rows for {len(present)} tables")
    checksums = self.tableChecksums(present)
    fingerprint = {}
    for table, row in zip(present, rows):
      create = re.sub(r" AUTO_INCREMENT=\d+", "", unescapeBatchField(row[1]))
      fingerprint[table] = {"create": hashlib.sha256(create.encode("utf-8")).hexdigest(), "checksum": checksums[table]}
      if createFolder is not None:
        createFolder.mkdir(exist_ok=True)
        (createFolder / f"{table}.sql").write_text(create + ";\n", encoding="utf-8")
    for table in tables:
      fingerprint.setdefault(table, "absent")
    return fingerprint

  def rowsChangedSinceApply(self, changes):
    """The numbers of applied row changes whose rows differ from those apply left."""
    entries = []
    for change in changes:
      saved = self.readSaved(change)
      entries += [(change, index, table, digest) for index, (table, digest) in enumerate(zip(saved["tables"],
        self.readApplied(change)["digests"]))]
    if not entries:
      return set()
    script = "".join(f"SELECT '{change.number}', '{index}', {entryDigestQuery(table)};\n" for change, index, table, _ in entries)
    current = {(row[0], int(row[1])): None if row[2] == "NULL" else row[2] for row in self.server.sql(script)}
    return {change.number for change, index, _, digest in entries if current[(change.number, index)] != digest}

  def status(self):
    changes = loadChanges(self.folder)
    states = self.states(changes)
    planText = self.planPath.read_text(encoding="utf-8")
    steps = readPlanSteps(planText)
    lines = [f"In progress (plan.md): {planSection(planText, 'In progress')}", f"Next (plan.md): {planSection(planText, 'Next')}",
      ""]
    unticked = [step for step in steps if not step.ticked]
    derived = unticked[0].describe() if unticked else "None: every step is done."
    if planSection(planText, "In progress") != derived:
      lines.append(f"plan.md: the In progress section does not name the first unticked step, {derived}")
    for step in steps:
      unknown = [number for number in step.changes if number not in states]
      if unknown:
        lines.append(f"plan.md: step {step.number} names {', '.join(unknown)}, which is not in the ledger")
        continue
      if not step.changes:
        continue
      allApplied = all(states[number].isApplied for number in step.changes)
      if allApplied and not step.ticked:
        lines.append(f"plan.md: step {step.number}'s changes are applied but it is not ticked: an apply stopped before its "
          "commit; run apply again to finish it")
      elif step.ticked and not allApplied:
        lines.append(f"plan.md: step {step.number} is ticked but not all of its changes are applied")
    changed = self.rowsChangedSinceApply([change for change in changes if change.form == "rows"
      and states[change.number].isApplied and (change.folder / "applied.json").is_file()])
    width = max((len(change.identifier) for change in changes), default=0)
    for change in changes:
      notes = [states[change.number].label]
      intent = self.readIntent(change)
      if intent:
        verb = intent["intent"][:-3]
        notes.append(f"an interrupted {verb} (intent.json): run {verb} {change.number} again to finish it")
      if change.number in changed:
        notes.append("rows changed since apply")
      lines.append(f"{change.identifier:<{width}}  {change.kind:<6}  {change.fields['disposition']:<11}  "
        f"{'; '.join(notes)}")
    fixtures = [change.number for change in changes if change.fields["disposition"] == "fixture"]
    explorations = [change.number for change in changes if change.fields["disposition"] == "exploration"
      and states[change.number].state != "notApplied"]
    uncleared = [change.number for change in changes if change.fields["disposition"] == "production" and not change.isCleared]
    lines += ["", "Closing a session:", f"  fixtures (kept): {', '.join(fixtures) or 'none'}",
      f"  exploration changes still applied: {', '.join(explorations) or 'none'}",
      f"  production changes the owner has not cleared: {', '.join(uncleared) or 'none'}",
      "  drift: run drift; it must be clean"]
    return lines

  def stage(self, identifier):
    change = self.find(identifier)
    notes = []
    if change.kind == "lab":
      raise LedgerError(f"{change.identifier} is a lab change: its steps run through facet or the sonicwall MCP, and its record "
        "is its change.yaml")
    if change.kind == "db":
      self.applyStatements(change)
      self.checkStatement(change)
      if change.form == "schema":
        self.revertStatements(change)
      state = self.state(change)
      if state.state != "notApplied":
        raise LedgerError(f"{change.identifier} is {state.label}; stage saves the rows of a change that has not run")
      if change.form == "rows":
        notes = self.stageRows(change)
      else:
        fingerprint = self.schemaFingerprint(change, change.folder / "showCreate")
        writeJSON(change.folder / "saved.json", {"checksum": hashlib.sha256(json.dumps(fingerprint, sort_keys=True).encode(
          "utf-8")).hexdigest(), "tables": fingerprint})
    elif change.kind in ("quest", "config"):
      source = Path(expandClientPath(change.fields["source"]))
      self.requireSourceCommits(change, source)
      self.push(source)
      self.fetchHost(change)
      before = self.repositoryHashes(change)
      after = self.repositoryHashes(change, change.fields["commits"][-1])
      unchanged = [path for path in before if before[path] == after[path]]
      if unchanged:
        raise LedgerError(f"{change.identifier}: the commits leave {', '.join(unchanged)} as it is on the host now; either the "
          "change is applied already or those files are not its scope")
      writeJSON(change.folder / "saved.json", {"before": before, "after": after})
    elif change.kind == "files":
      self.stageFiles(change)
    self.record([change.folder], f"Stage {change.identifier}: {change.fields['title']}")
    return [f"staged {change.identifier}: committed and pushed before it runs"] + notes

  def stageRows(self, change):
    tables = self.fetchScope(change)
    reasons = [reason for reason in (sensitiveReason(table["table"], [column["name"] for column in table["columns"]])
      for table in tables) if reason]
    described = describeScope(tables)
    saved = {"sensitive": bool(reasons), "checksum": scopeChecksum(described), "tables": described}
    revert = revertScript(tables)
    if reasons:
      full = json.dumps({"checksum": saved["checksum"], "tables": tables}, indent=2) + "\n"
      self.server.writeHostFile(f"{change.hostFolder}/saved.json", full)
      self.server.writeHostFile(f"{change.hostFolder}/revert.sql", revert)
      saved["hostCopy"] = {"folder": change.hostFolder, "savedSHA256": hashlib.sha256(full.encode("utf-8")).hexdigest(),
        "revertSHA256": hashlib.sha256(revert.encode("utf-8")).hexdigest(), "because": reasons[0]}
      (change.folder / "revert.sql").unlink(missing_ok=True)
      writeJSON(change.folder / "saved.json", saved)
      return [f"{reasons[0]}: its saved rows and revert.sql stay on {hostName} in {change.hostFolder}; only their digests are "
        "committed"]
    for table, full in zip(saved["tables"], tables):
      table["rows"] = full["rows"]
    writeJSON(change.folder / "saved.json", saved)
    (change.folder / "revert.sql").write_text(revert, encoding="utf-8")
    return []

  def requireHostCopy(self, change, saved):
    if not saved["sensitive"]:
      return
    text = self.server.readHostFile(f"{change.hostFolder}/saved.json")
    if hashlib.sha256(text.encode("utf-8")).hexdigest() != saved["hostCopy"]["savedSHA256"]:
      raise LedgerError(f"{change.identifier}: the saved rows on {hostName} are not the ones stage wrote; stage it again")

  def requireSourceCommits(self, change, source):
    if not (source / ".git").exists():
      raise LedgerError(f"{change.identifier}: {source} is not a git repository")
    for commit in change.fields["commits"]:
      if not gitAnswers(source, "cat-file", "-e", f"{commit}^{{commit}}"):
        raise LedgerError(f"{change.identifier}: {commit} is not in {source}")
      if not gitAnswers(source, "merge-base", "--is-ancestor", commit, "HEAD"):
        raise LedgerError(f"{change.identifier}: {commit} is not on {source}'s checked-out branch")

  def apply(self, identifier):
    change = self.find(identifier)
    self.requireCommitted(change)
    intent = self.readIntent(change)
    if intent and intent["intent"] == "reverting" and self.state(change).state == "notApplied":
      raise LedgerError(f"{change.identifier} has an interrupted revert (intent.json): run revert {change.number} again to "
        "finish it first")
    log.info("applying %s", change.identifier)
    if change.form == "rows":
      finished = self.applyRows(change, intent)
    elif change.form == "schema":
      finished = self.applySchema(change, intent)
    elif change.kind in ("quest", "config"):
      finished = self.applyRepository(change, intent)
    elif change.kind == "client":
      finished = self.applyClient(change, intent)
    elif change.kind == "files":
      finished = self.applyFiles(change, intent)
    else:
      raise LedgerError(f"{change.identifier} is a lab change: its steps run through facet or the sonicwall MCP")
    (change.folder / "intent.json").unlink()
    self.updatePlan(change)
    self.record([change.folder, self.planPath, self.tableChecksumsPath], f"Apply {change.identifier}: {change.fields['title']}")
    return [f"applied {change.identifier}" + (" (finished the interrupted apply)" if finished else ""),
      f"activation: {change.fields['activation']}"]

  @staticmethod
  def finishing(state, intent, expected):
    """Whether an interrupted apply (expected applied) or revert (expected notApplied) already got there and only its
    record is left to write."""
    operation = "applying" if expected == "applied" else "reverting"
    return intent is not None and intent["intent"] == operation and state.state == expected

  def applyRows(self, change, intent):
    saved = self.readSaved(change)
    state = self.state(change)
    if self.finishing(state, intent, "applied"):
      self.recordAppliedRows(change, saved)
      return True
    self.requireState(change, "notApplied", "apply", state)
    self.requireHostCopy(change, saved)
    staged = [table["digest"] for table in saved["tables"]]
    if self.scopeDigests(saved["tables"], saved["sensitive"]) != staged:
      raise LedgerError(f"the rows of {change.identifier} changed since stage, so revert.sql would put back stale rows: "
        "stage it again")
    self.writeIntent(change, "applying")
    script = ("START TRANSACTION;\n"
      + scopeGuards(saved["tables"], staged, f"ledger: {change.number} rows changed since stage")
      + outsideCapture(saved["tables"])
      + taggedStatements(change, "apply", self.applyStatements(change))
      + outsideGuards(saved["tables"], f"ledger: {change.number} apply wrote rows outside its scope")
      + guardBlock(f"({checkQuery(self.checkStatement(change))}) = 1", f"ledger: {change.number} check is not 1 after apply")
      + "COMMIT;\n")
    self.server.sql(script, writes=True, timeoutSeconds=600, sensitive=saved["sensitive"])
    self.recordAppliedRows(change, saved)
    return False

  def recordAppliedRows(self, change, saved):
    digests = self.scopeDigests(saved["tables"], saved["sensitive"])
    after = [dict(table, digest=digest) for table, digest in zip(saved["tables"], digests)]
    writeJSON(change.folder / "applied.json", {"checksum": scopeChecksum(after), "digests": digests})
    self.updateTableChecksums(self.tableChecksums(sorted(set(change.tables))), f"apply {change.identifier}")

  def applySchema(self, change, intent):
    statements = self.applyStatements(change)
    saved = self.readSaved(change)
    state = self.state(change)
    if self.finishing(state, intent, "applied"):
      self.recordAppliedSchema(change, "apply")
      return True
    if state.state == "checkFailed":
      raise LedgerError(f"apply needs {change.identifier}'s check; it is {state.label}")
    if state.done == 0 and self.schemaFingerprint(change) != saved["tables"]:
      raise LedgerError(f"the tables of {change.identifier} changed since stage: stage it again")
    self.writeIntent(change, "applying")
    failure = None
    try:
      self.server.sql(taggedStatements(change, "apply", statements[state.done:]), writes=True, timeoutSeconds=3600)
    except LedgerError as error:
      failure = error
    after = self.state(change)
    if failure is not None or not after.isApplied:
      raise LedgerError(f"{change.identifier}: {after.done} of {after.total} steps are done; fix the cause and run apply again "
        "to resume from the next step" + (f" ({failure})" if failure else "")) from failure
    self.recordAppliedSchema(change, "apply")
    return False

  def recordAppliedSchema(self, change, action):
    fingerprint = self.schemaFingerprint(change)
    if action == "apply":
      writeJSON(change.folder / "applied.json", {"checksum": hashlib.sha256(json.dumps(fingerprint, sort_keys=True).encode(
        "utf-8")).hexdigest(), "tables": fingerprint})
    self.updateTableChecksums({table: "absent" if value == "absent" else value["checksum"] for table, value in
      fingerprint.items()}, f"{action} {change.identifier}")

  def applyRepository(self, change, intent):
    self.readSaved(change)
    self.fetchHost(change)
    state = self.repositoryState(change)
    if self.finishing(state, intent, "applied"):
      return True
    self.requireState(change, "notApplied", "apply", state)
    self.writeIntent(change, "applying")
    self.server.run(asRepositoryOwner(change.fields["repository"], f"git merge --ff-only --quiet {change.fields['commits'][-1]}"))
    if "render" in change.fields:
      self.server.run(f"sudo -n {change.fields['render']}")
    self.requireState(change, "applied", "after its merge, apply")
    return False

  def applyClient(self, change, intent):
    state = self.clientState(change)
    if self.finishing(state, intent, "applied"):
      return True
    self.requireState(change, "notApplied", "apply", state)
    self.writeIntent(change, "applying")
    for step in change.fields["steps"]:
      runPowerShell(step)
    self.requireState(change, "applied", "after its steps, apply")
    return False

  def revert(self, identifier):
    change = self.find(identifier)
    self.requireCommitted(change)
    intent = self.readIntent(change)
    if intent and intent["intent"] == "applying" and self.state(change).isApplied:
      raise LedgerError(f"{change.identifier} has an interrupted apply (intent.json): run apply {change.number} again to finish "
        "it first")
    log.info("reverting %s", change.identifier)
    sourceRevert = None
    if change.form == "rows":
      finished = self.revertRows(change, intent)
    elif change.form == "schema":
      finished = self.revertSchema(change, intent)
    elif change.kind in ("quest", "config"):
      finished, sourceRevert = self.revertRepository(change, intent)
    elif change.kind == "client":
      finished = self.revertClient(change, intent)
    elif change.kind == "files":
      finished = self.revertFiles(change, intent)
    else:
      raise LedgerError(f"{change.identifier} is a lab change: its undo runs through facet or the sonicwall MCP")
    (change.folder / "applied.json").unlink(missing_ok=True)
    (change.folder / "intent.json").unlink()
    self.updatePlan(change)
    self.record([change.folder, self.planPath, self.tableChecksumsPath], f"Revert {change.identifier}: {change.fields['title']}"
      + (f" (source revert {sourceRevert[:12]})" if sourceRevert else ""))
    return [f"reverted {change.identifier}" + (" (finished the interrupted revert)" if finished else ""),
      f"activation: {change.fields['activation']}"]

  def requireSavedRows(self, change, saved):
    if self.scopeDigests(saved["tables"], saved["sensitive"]) != [table["digest"] for table in saved["tables"]]:
      raise LedgerError(f"{change.identifier}: revert committed, but the rows differ from those saved at stage")

  def revertRows(self, change, intent):
    saved = self.readSaved(change)
    state = self.state(change)
    if self.finishing(state, intent, "notApplied"):
      self.requireSavedRows(change, saved)
      self.updateTableChecksums(self.tableChecksums(sorted(set(change.tables))), f"revert {change.identifier}")
      return True
    self.requireState(change, "applied", "revert", state)
    applied = self.readApplied(change)
    current = self.scopeDigests(saved["tables"], saved["sensitive"])
    if current != applied["digests"]:
      changed = [table["table"] for table, digest, appliedDigest in zip(saved["tables"], current, applied["digests"])
        if digest != appliedDigest]
      raise LedgerError(f"the rows of {change.identifier} changed since apply ({', '.join(changed)}); revert refuses to "
        "overwrite them")
    statements = self.revertStatements(change, saved)
    self.writeIntent(change, "reverting")
    message = f"ledger: {change.number} "
    script = ("START TRANSACTION;\n"
      + scopeGuards(saved["tables"], applied["digests"], message + "rows changed since apply")
      + outsideCapture(saved["tables"])
      + taggedStatements(change, "revert", statements)
      + scopeGuards(saved["tables"], [table["digest"] for table in saved["tables"]], message + "revert did not restore the "
        "saved rows")
      + outsideGuards(saved["tables"], message + "revert wrote rows outside its scope")
      + guardBlock(f"({checkQuery(self.checkStatement(change))}) = 0", message + "check is not 0 after revert")
      + "COMMIT;\n")
    self.server.sql(script, writes=True, timeoutSeconds=600, sensitive=saved["sensitive"])
    self.requireSavedRows(change, saved)
    self.updateTableChecksums(self.tableChecksums(sorted(set(change.tables))), f"revert {change.identifier}")
    return False

  def revertSchema(self, change, intent):
    saved = self.readSaved(change)
    state = self.state(change)
    if self.finishing(state, intent, "notApplied"):
      if self.schemaFingerprint(change) != saved["tables"]:
        raise LedgerError(f"{change.identifier}: after revert.sql the tables are not as saved at stage")
      self.recordAppliedSchema(change, "revert")
      return True
    if state.state in ("notApplied", "checkFailed"):
      raise LedgerError(f"revert needs {change.identifier} applied or partly applied; it is {state.label}")
    if state.isApplied and self.schemaFingerprint(change) != self.readApplied(change)["tables"]:
      raise LedgerError(f"the tables of {change.identifier} changed since apply; revert refuses to overwrite them")
    statements = self.revertStatements(change)
    self.writeIntent(change, "reverting")
    failure = None
    try:
      self.server.sql(taggedStatements(change, "revert", statements[change.steps - state.done:]), writes=True,
        timeoutSeconds=3600)
    except LedgerError as error:
      failure = error
    after = self.state(change)
    if failure is not None or after.state != "notApplied":
      raise LedgerError(f"{change.identifier}: {after.done} of {after.total} steps are still done; fix the cause and run "
        "revert again to undo the rest" + (f" ({failure})" if failure else "")) from failure
    if self.schemaFingerprint(change) != saved["tables"]:
      raise LedgerError(f"{change.identifier}: after revert.sql the tables are not as saved at stage")
    self.recordAppliedSchema(change, "revert")
    return False

  def revertRepository(self, change, intent):
    """Undoes a quest or config change with a revert commit made in the source repository and fast-forwarded on the host, so
    the host never holds a commit the source lacks."""
    self.readSaved(change)
    state = self.repositoryState(change)
    if self.finishing(state, intent, "notApplied"):
      return True, intent.get("sourceRevert")
    self.requireState(change, "applied", "revert", state)
    source = Path(expandClientPath(change.fields["source"]))
    sourceRevert = intent.get("sourceRevert") if intent else None
    if sourceRevert is None:
      self.requireSourceCommits(change, source)
      if git(source, "status", "--porcelain").strip():
        raise LedgerError(f"{change.identifier}: {source} has uncommitted edits; the revert commit is made there")
      self.writeIntent(change, "reverting")
      git(source, "revert", "--no-commit", *reversed(change.fields["commits"]))
      git(source, "commit", "-q", "-m", f"Revert {change.identifier}: {change.fields['title']}")
      sourceRevert = git(source, "rev-parse", "HEAD").strip()
      self.writeIntent(change, "reverting", sourceRevert=sourceRevert)
    self.push(source)
    self.fetchHost(change, [sourceRevert])
    self.server.run(asRepositoryOwner(change.fields["repository"], f"git merge --ff-only --quiet {sourceRevert}"))
    if "render" in change.fields:
      self.server.run(f"sudo -n {change.fields['render']}")
    self.requireState(change, "notApplied", "after merging its revert commit, revert")
    return False, sourceRevert

  def revertClient(self, change, intent):
    state = self.clientState(change)
    if self.finishing(state, intent, "notApplied"):
      return True
    if not state.isApplied:
      raise LedgerError(f"revert needs every check of {change.identifier} passing, as apply left it; it is {state.label}, so "
        "what it touched changed since apply: put that right by hand")
    self.writeIntent(change, "reverting")
    for step in change.fields["undo"]:
      runPowerShell(step)
    self.requireState(change, "notApplied", "after its undo, revert")
    return False

  def hostFileHashes(self, paths):
    """Each host file's SHA-256, or None for one that is absent."""
    loop = ('for f in ' + " ".join(paths) + '; do if [ -f "$f" ]; then sha256sum < "$f" | cut -c1-64; else echo absent; fi; '
      'done')
    digests = self.server.run(f"sudo -n sh -c '{loop}'").split()
    if len(digests) != len(paths) or not all(digest == "absent" or hashPattern.match(digest) for digest in digests):
      raise LedgerError(f"{hostName} reported {digests!r} for the files {paths}")
    return {path: None if digest == "absent" else digest for path, digest in zip(paths, digests)}

  def filesState(self, change):
    """applied when every file is as the change leaves it; not applied when every file is as stage found it (or, before
    stage, when none is as the change leaves it)."""
    entries = change.fields["files"]
    current = self.hostFileHashes([entry["path"] for entry in entries])
    done = sum(current[entry["path"]] == entry["sha256"] for entry in entries)
    savedPath = change.folder / "saved.json"
    if done == len(entries):
      return ChangeState(change, "applied", done, done)
    if not savedPath.is_file():
      return ChangeState(change, "notApplied" if done == 0 else "partlyApplied", done, len(entries), "not staged")
    before = json.loads(savedPath.read_text(encoding="utf-8"))["before"]
    if current == before:
      return ChangeState(change, "notApplied", 0, len(entries))
    return ChangeState(change, "partlyApplied", done, len(entries), "some files are neither as staged nor as the change leaves them")

  @staticmethod
  def requireSources(change):
    for entry in change.fields["files"]:
      if entry["source"] is None:
        continue
      source = Path(expandClientPath(entry["source"]))
      if not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest() != entry["sha256"]:
        raise LedgerError(f"{change.identifier}: {source} is not the file with SHA-256 {entry['sha256']} the change installs")

  def keptPath(self, change, index):
    return f"{change.hostFolder}/previous/{index}"

  def stageFiles(self, change):
    """Saves each host file the change replaces or removes as it stands, in the change's host folder (root only), and their
    hashes before and after in saved.json."""
    self.requireSources(change)
    state = self.filesState(change)
    if state.state != "notApplied":
      raise LedgerError(f"{change.identifier} is {state.label}; stage saves the files of a change that has not run")
    entries = change.fields["files"]
    before = self.hostFileHashes([entry["path"] for entry in entries])
    kept = {}
    for index, entry in enumerate(entries):
      if before[entry["path"]] is None:
        continue
      kept[entry["path"]] = self.keptPath(change, index)
      self.server.run(f"sudo -n sh -c 'umask 077 && mkdir -p {change.hostFolder}/previous && cp -p {entry['path']} "
        f"{kept[entry['path']]}'")
    self.requireKept(change, before, kept)
    writeJSON(change.folder / "saved.json", {"before": before, "after": {entry["path"]: entry["sha256"] for entry in entries},
      "kept": kept})

  def requireKept(self, change, before, kept):
    if not kept:
      return
    hashes = self.hostFileHashes(list(kept.values()))
    for path, copy in kept.items():
      if hashes[copy] != before[path]:
        raise LedgerError(f"{change.identifier}: the copy of {path} kept on {hostName} at stage ({copy}) is gone or changed")

  def applyFiles(self, change, intent):
    saved = self.readSaved(change)
    state = self.filesState(change)
    if self.finishing(state, intent, "applied"):
      return True
    self.requireState(change, "notApplied", "apply", state)
    self.requireKept(change, saved["before"], saved["kept"])
    self.requireSources(change)
    self.writeIntent(change, "applying")
    incoming = f"{change.hostFolder}/incoming"
    for index, entry in enumerate(change.fields["files"]):
      path = entry["path"]
      if entry["source"] is None:
        self.server.run(f"sudo -n rm -f -- {path}")
        continue
      data = Path(expandClientPath(entry["source"])).read_bytes()
      self.server.run(f"sudo -n sh -c 'umask 077 && mkdir -p {incoming} && cat > {incoming}/{index}'", data, timeoutSeconds=600)
      self.server.run(f"sudo -n sh -c 'test \"$(sha256sum < {incoming}/{index} | cut -c1-64)\" = {entry['sha256']} && "
        f"cp {incoming}/{index} {path}.ledger && chown --reference={path.rsplit('/', 1)[0]} {path}.ledger && "
        f"chmod 0644 {path}.ledger && mv -f {path}.ledger {path}'")
    self.server.run(f"sudo -n rm -rf -- {incoming}")
    self.requireState(change, "applied", "after its files, apply")
    return False

  def revertFiles(self, change, intent):
    saved = self.readSaved(change)
    state = self.filesState(change)
    if self.finishing(state, intent, "notApplied"):
      return True
    self.requireState(change, "applied", "revert", state)
    self.requireKept(change, saved["before"], saved["kept"])
    self.writeIntent(change, "reverting")
    for entry in change.fields["files"]:
      path = entry["path"]
      if saved["before"][path] is None:
        self.server.run(f"sudo -n rm -f -- {path}")
      else:
        self.server.run(f"sudo -n sh -c 'cp -p {saved['kept'][path]} {path}.ledger && mv -f {path}.ledger {path}'")
    self.requireState(change, "notApplied", "after putting its files back, revert")
    return False

  def updatePlan(self, change):
    text = self.planPath.read_text(encoding="utf-8")
    named = sorted({number for step in readPlanSteps(text) if change.number in step.changes for number in step.changes})
    changes = {candidate.number: candidate for candidate in loadChanges(self.folder)}
    unknown = [number for number in named if number not in changes]
    if unknown:
      raise LedgerError(f"plan.md names {', '.join(unknown)}, which is not in the ledger")
    states = self.states([changes[number] for number in named]) if named else {}
    applied = {number: state.isApplied for number, state in states.items()}
    self.planPath.write_text(rewritePlan(text, applied, {change.number}), encoding="utf-8")

  def readBaseline(self):
    if not self.baselinePath.is_file():
      raise LedgerError("there is no baseline.yaml yet: seed claudeperidot first (plan step 7)")
    baseline = readYAML(self.baselinePath)
    missing = [name for name in ("dump", "dbVersion", "binaryLog", "deployed", "tableChecksums") if name not in baseline]
    if missing:
      raise LedgerError(f"baseline.yaml lacks {', '.join(missing)}")
    return baseline

  def baseTables(self):
    return [row[0] for row in self.server.sql("SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() "
      "AND TABLE_TYPE = 'BASE TABLE' ORDER BY TABLE_NAME;\n")]

  def requireBinaryLog(self):
    settings = self.server.sql("SELECT @@log_bin, @@binlog_format, @@binlog_annotate_row_events;\n")
    if settings != [["1", "ROW", "1"]]:
      raise LedgerError(f"drift reads the binary log, which needs log_bin 1, binlog_format ROW and binlog_annotate_row_events 1; "
        f"the server has {settings!r}")

  def masterStatus(self):
    rows = self.server.sql("SHOW MASTER STATUS;\n")
    if len(rows) != 1:
      raise LedgerError(f"SHOW MASTER STATUS gave {rows!r}; the binary log must be on")
    return {"file": rows[0][0], "position": int(rows[0][1])}

  def driftReport(self, readPosition=False):
    """drift's lines, plus the binary-log position read between the table checksums and the log, and those checksums."""
    baseline = self.readBaseline()
    self.requireBinaryLog()
    changes = loadChanges(self.folder)
    dbChanges = [change for change in changes if change.kind == "db"]
    states = self.databaseStates(dbChanges)
    lines = []
    for change in dbChanges:
      state = states[change.number]
      intent = self.readIntent(change)
      if state.state == "checkFailed":
        lines.append(f"{change.identifier}: {state.label}")
      elif intent:
        verb = intent["intent"][:-3]
        lines.append(f"{change.identifier} has an interrupted {verb} (intent.json): run {verb} {change.number} again to "
          "finish it")
      elif state.state != "notApplied" and not (change.folder / "applied.json").is_file():
        lines.append(f"{change.identifier} is {state.label} but has no applied.json: it ran outside the ledger")
    expected = {table: (value, "the baseline") for table, value in baseline["tableChecksums"].items() if isWatched(table)}
    for table, value in self.readTableChecksums().items():
      if not isWatched(table):
        continue
      if value["checksum"] == "absent":
        expected.pop(table, None)
      else:
        expected[table] = (value["checksum"], value["by"])
    current = self.tableChecksums([table for table in self.baseTables() if isWatched(table)])
    position = self.masterStatus() if readPosition else None
    for table in sorted(set(expected) | set(current)):
      if table not in current:
        lines.append(f"{table}: expected from {expected[table][1]} but gone now")
      elif table not in expected:
        lines.append(f"{table}: new since the baseline, and no change accounts for it")
      elif expected[table][0] != current[table]:
        lines.append(f"{table}: checksum {current[table]}, expected {expected[table][0]} from {expected[table][1]}")
    account = DriftAccount(changes)
    parser = BinaryLogParser(account.add)
    binaryLog = baseline["binaryLog"]
    self.server.stream(binaryLogCommand(binaryLog["file"], binaryLog["position"]), parser.feed, timeoutSeconds=600)
    parser.finish()
    lines += account.problems
    return (lines or ["drift: clean"]) + account.accountLines() + account.runtimeLines(), position, current

  def drift(self):
    return self.driftReport()[0]

  def advance(self):
    lines, position, current = self.driftReport(readPosition=True)
    if lines[0] != "drift: clean":
      raise LedgerError("advance moves the baseline only after a clean drift:\n" + "\n".join(lines))
    baseline = self.readBaseline()
    baseline["binaryLog"] = position
    baseline["tableChecksums"] = current
    self.baselinePath.write_text(yaml.safe_dump(baseline, sort_keys=False), encoding="utf-8")
    self.tableChecksumsPath.unlink(missing_ok=True)
    self.record([self.baselinePath, self.tableChecksumsPath], f"Advance the baseline to binary log {position['file']}:"
      f"{position['position']} after a clean drift")
    return lines + [f"baseline advanced to {position['file']}:{position['position']}"]

  def promote(self, identifier, manifestVersion=None):
    change = self.find(identifier)
    if change.fields["disposition"] != "production":
      raise LedgerError(f"{change.identifier} is {change.fields['disposition']}; only a production change is promoted")
    if not change.isCleared:
      raise LedgerError(f"{change.identifier} waits for the owner's clearance: ownerClearance names no message of the owner's")
    pending = [candidate.identifier for candidate in loadChanges(self.folder) if candidate.fields["disposition"] == "production"
      and candidate.fields["significance"] == "significant" and not candidate.isCleared]
    if pending:
      raise LedgerError(f"promote waits for the owner to clear the significant changes {', '.join(pending)}")
    production = change.fields["production"]
    saved = self.readSaved(change) if production == "contentSQL" else None
    if saved is not None and saved["sensitive"]:
      raise LedgerError(f"{change.identifier}: {saved['hostCopy']['because']}, so promote never writes it; carry it to Peridot "
        "by hand with the owner")
    self.requireState(change, "applied", "promote")
    if production == "contentSQL":
      check = self.checkStatement(change)
      header = (f"-- {change.identifier}: {change.fields['title']}\n-- Written by ledger.py promote. Run it with the mysql "
        "command-line client (mysql peq < content.sql), whose DELIMITER command its guards use. It fails and rolls back unless\n"
        "-- Peridot still holds the rows the change was made against, unless it writes only those rows, and unless its check "
        "gives 1 after it.\n")
      outputs = {"content.sql": header + "".join(f"{statement};\n" for statement in readPreamble + writePreamble)
        + "START TRANSACTION;\n"
        + scopeGuards(saved["tables"], [table["digest"] for table in saved["tables"]], f"{change.number}: Peridot rows differ "
          "from those the change was made against")
        + outsideCapture(saved["tables"])
        + "".join(f"{statement};\n" for statement in self.applyStatements(change))
        + outsideGuards(saved["tables"], f"{change.number}: the content SQL wrote rows outside its scope")
        + guardBlock(f"({checkQuery(check)}) = 1", f"{change.number}: check is not 1 after the content SQL")
        + "COMMIT;\n", "check.sql": checkQuery(check) + ";\n"}
    elif production == "customMigration":
      outputs = {"customMigration.cpp": self.manifestEntry(change, manifestVersion)}
    elif production == "questCommit":
      outputs = {"cherryPick.sh": f"#!/bin/sh\nset -e\ncd {peridotQuestsPath}\ngit cherry-pick "
        f"{' '.join(change.fields['commits'])}\n"}
    else:
      outputs = {"edit.md": f"# {change.identifier}: carry into the {production}\n\n{change.fields['title']}\n\n"
        f"{change.fields['why']}\n\n- Files: {', '.join(change.fields['scope'])}\n- Commits: "
        f"{', '.join(change.fields['commits'])}\n- Activation: {change.fields['activation']}\n"}
    outputFolder = change.folder / "peridot"
    outputFolder.mkdir(exist_ok=True)
    for name, text in outputs.items():
      (outputFolder / name).write_text(text, encoding="utf-8", newline="\n")
    self.record([change.folder], f"Promote {change.identifier}: {change.fields['title']}")
    return [f"promoted {change.identifier}: {self.relative(outputFolder)}"]

  def manifestEntry(self, change, manifestVersion):
    if type(manifestVersion) is not int or manifestVersion < 1:
      raise LedgerError("a custom migration needs --manifestVersion, the next version in Peridot's "
        "database_update_manifest_custom.cpp")
    statements = self.applyStatements(change)
    if any(')"' in statement or not statement.isascii() for statement in statements):
      raise LedgerError(f"{change.identifier}: a statement holds )\" (which would end a C++ raw string) or non-ASCII text")
    check = " ".join(f"{checkQuery(self.checkStatement(change))} WHERE applied = {change.steps}".split())
    check = check.replace("\\", "\\\\").replace('"', '\\"')
    pieces = "\n\t\t\t".join(f'R"({statement};\n)"' for statement in statements)
    contentUpdate = "true" if all(table in contentTables for table in change.tables) else "false"
    return ("\tManifestEntry{\n"
      f"\t\t.version = {manifestVersion},\n"
      f"\t\t.description = \"{datetime.date.today():%Y_%m_%d}_{change.name}\",\n"
      f"\t\t.check = \"{check}\",\n"
      "\t\t.condition = \"empty\",\n"
      "\t\t.match = \"\",\n"
      f"\t\t.sql =\n\t\t\t{pieces},\n"
      f"\t\t.content_schema_update = {contentUpdate},\n"
      "\t},\n")

  def seedCopy(self, copyScript):
    if self.baselinePath.is_file():
      raise LedgerError("baseline.yaml exists: claudeperidot is seeded; to seed again, remove it in a commit first")
    script = Path(copyScript)
    if not script.is_file():
      raise LedgerError(f"{script} is not a file")
    commands = {".ps1": ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script)],
      ".py": [sys.executable, str(script)]}
    if script.suffix.lower() not in commands:
      raise LedgerError(f"the copy script must be .ps1 or .py, not {script.name}")
    log.info("seeding with %s", script)
    result = subprocess.run(commands[script.suffix.lower()])
    if result.returncode != 0:
      raise LedgerError(f"the copy script {script.name} failed with exit code {result.returncode}")
    return ["copied: boot world once (its migrations and rule injection), then run seed --baseline"]

  def seedBaseline(self):
    if self.baselinePath.is_file():
      raise LedgerError("baseline.yaml exists already")
    self.requireBinaryLog()
    dumps = [name for name in self.server.run(f"sudo -n ls -1t {hostPaths['dumps']}").split()
      if re.match(r"^peq-[\w.-]+\.sql\.gz$", name)]
    if not dumps:
      raise LedgerError(f"no peq-*.sql.gz in {hostPaths['dumps']}: run seed with the copy script first")
    dumpPath = f"{hostPaths['dumps']}/{dumps[0]}"
    dumpTime = datetime.datetime.fromtimestamp(int(self.server.run(f"sudo -n stat -c %Y {dumpPath}").strip()), datetime.UTC)
    versionRows = self.server.sql("SELECT version, bots_version, custom_version FROM db_version;\n")
    if len(versionRows) != 1:
      raise LedgerError(f"db_version gave {versionRows!r}; the baseline needs one db_version row")
    tableChecksums = self.tableChecksums([table for table in self.baseTables() if isWatched(table)])
    binaryLog = self.masterStatus()
    deployed = {name: self.server.run(f"sudo -n git -c safe.directory='*' -C {hostPaths[name]} rev-parse HEAD").strip()
      for name in ("akkStack", "code", "quests", "maps")}
    baseline = {
      "dump": {"path": dumpPath, "sha256": self.server.run(f"sudo -n sha256sum {dumpPath}").split()[0],
        "time": dumpTime.strftime("%Y-%m-%dT%H:%M:%SZ")},
      "dbVersion": {"version": int(versionRows[0][0]), "botsVersion": int(versionRows[0][1]),
        "customVersion": int(versionRows[0][2])},
      "binaryLog": binaryLog,
      "deployed": deployed,
      "tableChecksums": tableChecksums,
    }
    self.baselinePath.write_text(yaml.safe_dump(baseline, sort_keys=False), encoding="utf-8")
    self.record([self.baselinePath], f"Baseline: dump {baseline['dump']['sha256'][:12]}, db_version {versionRows[0][0]}, "
      f"binary log {binaryLog['file']}:{binaryLog['position']}")
    return [f"baseline written: {len(tableChecksums)} table checksums; now take the Proxmox baseline snapshot"]


def openServer(host=hostName):
  """The Server for a host of facet's inventory: claudeperidot for every ledger command; zoneDeploy's read-only slot reads
  also reach Peridot."""
  from facetClient.inventory import InventoryError, defaultInventoryPath, loadInventory
  from facetClient.sshConnection import SSHConnection
  inventoryPath = defaultInventoryPath
  try:
    hosts = loadInventory(inventoryPath)
  except (InventoryError, OSError) as error:
    raise LedgerError(f"facet's inventory {inventoryPath} cannot be used: {error}") from error
  if host not in hosts:
    raise LedgerError(f"{host} is not in {inventoryPath}: enroll it in facet first")
  return Server(SSHConnection(hosts[host]))


def configureLogging():
  localAppData = os.environ.get("LOCALAPPDATA")
  if not localAppData:
    raise LedgerError("LOCALAPPDATA is not set, so the ledger has nowhere to log")
  logFolder = Path(localAppData) / "claudeperidot" / "logs"
  logFolder.mkdir(parents=True, exist_ok=True)
  handler = logging.handlers.TimedRotatingFileHandler(logFolder / "ledger.log", when="midnight", backupCount=90,
    encoding="utf-8")
  handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
  logging.getLogger().addHandler(handler)
  logging.getLogger().setLevel(logging.INFO)
  logging.getLogger("paramiko").setLevel(logging.WARNING)


def main(arguments=None):
  parser = argparse.ArgumentParser(description="The claudeperidot change ledger.")
  commands = parser.add_subparsers(dest="command", required=True)
  commands.add_parser("status", help="every change's state, read live, beside plan.md's step in progress")
  for name, text in (("stage", "save the rows or files a change touches and commit and push it before it runs"),
      ("apply", "run a staged change, check it, rewrite plan.md, commit and push; finishes an interrupted apply"),
      ("revert", "undo an applied change unless what it touched changed since apply; finishes an interrupted revert")):
    commands.add_parser(name, help=text).add_argument("change", help="the change's number or folder name")
  commands.add_parser("drift", help="table checksums against the baseline and the ledger's writes, and unaccounted "
    "binary-log writes")
  commands.add_parser("advance", help="after a clean drift, move the baseline to the binary log's end and today's checksums")
  promote = commands.add_parser("promote", help="write a production change's Peridot form")
  promote.add_argument("change")
  promote.add_argument("--manifestVersion", type=int, help="the custom migration version, for a schema change")
  seed = commands.add_parser("seed", help="stream Peridot's database into claudeperidot, then write baseline.yaml")
  seedMode = seed.add_mutually_exclusive_group(required=True)
  seedMode.add_argument("--copyScript", help="the operations repository's database copy script (.ps1 or .py)")
  seedMode.add_argument("--baseline", action="store_true", help="after world has booted once, write baseline.yaml")
  options = parser.parse_args(arguments)
  ledger = Ledger(ledgerFolder, openServer, pushBranch)
  try:
    configureLogging()
    if options.command == "status":
      lines = ledger.status()
    elif options.command in ("stage", "apply", "revert"):
      lines = getattr(ledger, options.command)(options.change)
    elif options.command == "drift":
      lines = ledger.drift()
    elif options.command == "advance":
      lines = ledger.advance()
    elif options.command == "promote":
      lines = ledger.promote(options.change, options.manifestVersion)
    else:
      lines = ledger.seedBaseline() if options.baseline else ledger.seedCopy(options.copyScript)
  except LedgerError as error:
    log.error("%s failed: %s", options.command, error)
    print(f"ledger: {error}", file=sys.stderr)
    return 1
  print("\n".join(lines))
  return 0


if __name__ == "__main__":
  sys.exit(main())
