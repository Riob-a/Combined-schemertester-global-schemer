"""
er_schema_diff.py
─────────────────
Diff engine for EarthRanger schema dry runs.

Given:
  - A server export (JSON file from data/servers/<name>/EarthRanger/Configuration/)
  - A desired state (rows from a conservancy Excel file)

Produces a list of DiffResult objects classifying every event type as:
  CREATE     — in desired state, not on server
  UPDATE     — on server and in desired state, but something changed
  DEACTIVATE — on server and active, but marked inactive/system/absent in desired state
  NO_CHANGE  — on server and in desired state, nothing differs

Each UPDATE result carries a human-readable list of what changed.

Choice list diffing (diff_choice_lists / ChoiceListDiffResult) compares the
enum values and display names for every property within a schema, reporting
values added, removed, or whose display label changed.
"""

import copy
import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Any

import pandas as pd

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# DIFF ACTION ENUM
# ─────────────────────────────────────────────────────────────

class DiffAction(str, Enum):
    CREATE     = "CREATE"
    UPDATE     = "UPDATE"
    DEACTIVATE = "DEACTIVATE"
    NO_CHANGE  = "NO_CHANGE"


# ─────────────────────────────────────────────────────────────
# DIFF RESULT
# ─────────────────────────────────────────────────────────────

@dataclass
class DiffResult:
    event_value:   str
    action:        DiffAction
    schema_title:  Optional[str]  = None
    schema_id:     Optional[str]  = None
    changes:       List[str]      = field(default_factory=list)
    # populated for CREATE/UPDATE — the schema that would be pushed
    desired_schema: Optional[Dict] = field(default=None, repr=False)
    # populated for UPDATE/DEACTIVATE/NO_CHANGE — what's currently on server
    current_record: Optional[Dict] = field(default=None, repr=False)


# ─────────────────────────────────────────────────────────────
# SCHEMA NORMALISER
# Strip host-bound, volatile, or irrelevant fields before comparing.
# ─────────────────────────────────────────────────────────────

_STRIP_SCHEMA_KEYS = {"id", "image_url", "$schema"}
_STRIP_PROP_KEYS   = {"enumImages", "inactive_enum"}


def _normalise_property(prop: Dict[str, Any]) -> Dict[str, Any]:
    """Strip volatile keys from a single property for comparison."""
    p = {k: v for k, v in prop.items() if k not in _STRIP_PROP_KEYS}
    return p


def _normalise_properties(props: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively normalise all properties."""
    out = {}
    for k, v in props.items():
        if not isinstance(v, dict):
            out[k] = v
            continue
        normed = _normalise_property(v)
        # recurse into sub-object properties
        if "properties" in normed and isinstance(normed["properties"], dict):
            normed["properties"] = _normalise_properties(normed["properties"])
        # recurse into array item properties
        if (
            normed.get("type") == "array"
            and isinstance(normed.get("items"), dict)
            and isinstance(normed["items"].get("properties"), dict)
        ):
            normed["items"]["properties"] = _normalise_properties(
                normed["items"]["properties"]
            )
        out[k] = normed
    return out


def normalise_schema(schema: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy of schema with volatile/host-bound fields stripped."""
    s = {k: v for k, v in schema.items() if k not in _STRIP_SCHEMA_KEYS}
    if "properties" in s and isinstance(s["properties"], dict):
        s["properties"] = _normalise_properties(s["properties"])
    return s


# ─────────────────────────────────────────────────────────────
# CHANGE DETECTOR
# Returns a list of human-readable change descriptions.
# Empty list → no meaningful difference.
# ─────────────────────────────────────────────────────────────

def _detect_changes(
    current_rec: Dict[str, Any],
    desired_title: str,
    desired_icon: str,
    desired_schema_norm: Dict[str, Any],
) -> List[str]:
    changes = []

    current_schema_raw = current_rec.get("schema") or {}
    current_schema_norm = normalise_schema(current_schema_raw)

    # Title change
    current_title = current_rec.get("schema_title") or current_schema_raw.get("title") or ""
    if desired_title and desired_title.strip() != current_title.strip():
        changes.append(f"title: '{current_title}' → '{desired_title}'")

    # Icon change
    current_icon = current_rec.get("event_icon_id") or current_schema_raw.get("icon_id") or ""
    if desired_icon and desired_icon.strip() != current_icon.strip():
        changes.append(f"icon_id: '{current_icon}' → '{desired_icon}'")

    # Property-level diff
    current_props = set(current_schema_norm.get("properties", {}).keys())
    desired_props = set(desired_schema_norm.get("properties", {}).keys())

    added   = desired_props - current_props
    removed = current_props - desired_props

    if added:
        changes.append(f"properties added: {sorted(added)}")
    if removed:
        changes.append(f"properties removed: {sorted(removed)}")

    # For shared properties, check if their definitions changed
    for prop in current_props & desired_props:
        c = current_schema_norm["properties"][prop]
        d = desired_schema_norm["properties"][prop]
        if c != d:
            # Summarise what changed within the property
            c_keys = set(c.keys())
            d_keys = set(d.keys())
            changed_keys = {
                k for k in c_keys | d_keys
                if c.get(k) != d.get(k)
            }
            changes.append(
                f"property '{prop}' changed: {sorted(changed_keys)}"
            )

    return changes


# ─────────────────────────────────────────────────────────────
# LOAD SERVER STATE
# ─────────────────────────────────────────────────────────────

def load_server_state(export_path: Path) -> Dict[str, Dict]:
    """
    Load a server JSON export and index by event_value.
    Returns dict of {event_value: full_record}.
    """
    with export_path.open("r", encoding="utf-8") as f:
        records = json.load(f)

    by_value = {}
    by_category_id = {}

    for rec in records:
        val = rec.get("value")
        cat_id = rec.get("event_category_id")
        if val:
            by_value[val] = rec
        if cat_id:
            # multiple event_values can share a category_id
            by_category_id.setdefault(cat_id, []).append(rec)

    return {
        "records":        records,
        "by_value":       by_value,
        "by_category_id": by_category_id,
    }


# ─────────────────────────────────────────────────────────────
# MAIN DIFF FUNCTION
# ─────────────────────────────────────────────────────────────

def diff_against_server(
    df: pd.DataFrame,
    server_state: Dict[str, Any],
    *,
    conservancy_name: str = "UNKNOWN",
) -> List[DiffResult]:
    """
    Compare desired state (Excel DataFrame) against current server state.

    For each row in df:
      - If event_value exists on server and desired is active → UPDATE or NO_CHANGE
      - If event_value does not exist on server and desired is active → CREATE
      - If event_value exists on server and desired is inactive/system → DEACTIVATE

    For each server record not mentioned in df at all:
      - Not included (those are unmanaged schemas; don't touch them)

    Returns list of DiffResult.
    """
    from build_global_schemas_final_cl import parse_bool, templatised_schema

    results: List[DiffResult] = []
    by_value = server_state.get("by_value", {})

    # Track which event_values we've seen from the desired state
    desired_values = set()

    for _, row in df.iterrows():
        raw_value    = row.get("event_value")
        event_value  = "" if (raw_value is None or (isinstance(raw_value, float) and raw_value != raw_value)) else str(raw_value).strip()
        schema_title = str(row.get("schema_title") or "").strip()
        icon_id      = str(row.get("icon_id") or "").strip()
        is_active    = parse_bool(row.get("event_is_active", ""))
        flag         = str(row.get("flag") or "").strip().lower()

        if not event_value or event_value.lower() == "nan":
            continue

        desired_values.add(event_value)
        current_rec = by_value.get(event_value)

        # ── Desired: INACTIVE or SYSTEM-flagged ──────────────────────
        if is_active is not True or flag == "system":
            if current_rec and current_rec.get("event_is_active"):
                results.append(DiffResult(
                    event_value    = event_value,
                    action         = DiffAction.DEACTIVATE,
                    schema_title   = schema_title or current_rec.get("schema_title"),
                    changes        = [
                        "inactive/system in config — would deactivate on server"
                    ],
                    current_record = current_rec,
                ))
            # If already inactive on server or not present — nothing to do
            continue

        # ── Desired: ACTIVE ──────────────────────────────────────────

        # Build the desired schema (templatised for structural comparison)
        # We use event_category_id as the schema source id for lookup
        schema_source_id = str(row.get("event_category_id") or "").strip()

        # Build a minimal desired schema for comparison purposes
        # (in a full pipeline this would come from master config;
        #  here we use the server's own schema as baseline and compare
        #  title/icon/property-set changes driven by the Excel)
        if current_rec:
            raw_schema = copy.deepcopy(current_rec.get("schema") or {})
        else:
            raw_schema = {
                "title":      schema_title,
                "type":       "object",
                "properties": {},
            }

        desired_schema = templatised_schema(raw_schema)
        if schema_title:
            desired_schema["title"] = schema_title
        if icon_id:
            desired_schema["icon_id"] = icon_id

        desired_schema_norm = normalise_schema(desired_schema)

        # ── CREATE ───────────────────────────────────────────────────
        if current_rec is None:
            results.append(DiffResult(
                event_value    = event_value,
                action         = DiffAction.CREATE,
                schema_title   = schema_title,
                schema_id      = schema_source_id or None,
                changes        = ["not found on server — would be created"],
                desired_schema = desired_schema,
            ))
            continue

        # ── UPDATE or NO_CHANGE ──────────────────────────────────────
        changes = _detect_changes(
            current_rec,
            desired_title       = schema_title,
            desired_icon        = icon_id,
            desired_schema_norm = desired_schema_norm,
        )

        if changes:
            results.append(DiffResult(
                event_value    = event_value,
                action         = DiffAction.UPDATE,
                schema_title   = schema_title or current_rec.get("schema_title"),
                schema_id      = schema_source_id or None,
                changes        = changes,
                desired_schema = desired_schema,
                current_record = current_rec,
            ))
        else:
            results.append(DiffResult(
                event_value    = event_value,
                action         = DiffAction.NO_CHANGE,
                schema_title   = schema_title or current_rec.get("schema_title"),
                current_record = current_rec,
            ))

    return results


# ─────────────────────────────────────────────────────────────
# DIFF SUMMARY PRINTER
# ─────────────────────────────────────────────────────────────

def print_diff_summary(
    results: List[DiffResult],
    conservancy: str,
    *,
    verbose: bool = False,
) -> None:
    from colorama import Fore, Style

    counts = {a: 0 for a in DiffAction}
    for r in results:
        counts[r.action] += 1

    total_changes = (
        counts[DiffAction.CREATE]
        + counts[DiffAction.UPDATE]
        + counts[DiffAction.DEACTIVATE]
    )

    print(Style.BRIGHT + Fore.MAGENTA + f"\n{'='*50}")
    print(Style.BRIGHT + Fore.MAGENTA + f"  DIFF SUMMARY — {conservancy.upper()}")
    print(Style.BRIGHT + Fore.MAGENTA + f"{'='*50}")
    print(
        Fore.GREEN  + f"  CREATE    : {counts[DiffAction.CREATE]}"
    )
    print(
        Fore.YELLOW + f"  UPDATE    : {counts[DiffAction.UPDATE]}"
    )
    print(
        Fore.RED    + f"  DEACTIVATE: {counts[DiffAction.DEACTIVATE]}"
    )
    print(
        Fore.CYAN   + f"  NO CHANGE : {counts[DiffAction.NO_CHANGE]}"
    )
    print(
        Style.BRIGHT + f"  ─────────────────────────────────────"
    )
    print(
        Style.BRIGHT + f"  Total changes: {total_changes} "
        f"({counts[DiffAction.CREATE]} create, "
        f"{counts[DiffAction.UPDATE]} update, "
        f"{counts[DiffAction.DEACTIVATE]} deactivate)"
    )

    if not verbose:
        return

    # ── Verbose: show per-action detail ─────────────────────────────

    action_order = [
        DiffAction.CREATE,
        DiffAction.UPDATE,
        DiffAction.DEACTIVATE,
        DiffAction.NO_CHANGE,
    ]
    action_colors = {
        DiffAction.CREATE:     Fore.GREEN,
        DiffAction.UPDATE:     Fore.YELLOW,
        DiffAction.DEACTIVATE: Fore.RED,
        DiffAction.NO_CHANGE:  Fore.CYAN,
    }

    by_action = {a: [] for a in DiffAction}
    for r in results:
        by_action[r.action].append(r)

    for action in action_order:
        group = sorted(by_action[action], key=lambda r: r.event_value)
        if not group:
            continue

        color = action_colors[action]
        print(color + f"\n  [{action.value}]")
        print(color + f"  {'─'*46}")

        for r in group:
            title = r.schema_title or "—"
            print(color + f"  {r.event_value:<45} {title}")
            if r.changes and action != DiffAction.NO_CHANGE:
                for change in r.changes:
                    print(color + f"      ↳ {change}")


# ─────────────────────────────────────────────────────────────
# CHOICE LIST DIFF
# ─────────────────────────────────────────────────────────────

@dataclass
class ChoiceListDiffResult:
    """
    Diff result for a single enum property within a schema.

    Attributes
    ----------
    event_value:    The schema's event_value (e.g. "aip_polygon")
    prop_name:      The property name containing the enum (e.g. "invasivealienplants_alienplantspecies")
    values_added:   Enum value keys present in desired but not on server
    values_removed: Enum value keys on server but absent from desired
    labels_changed: {value_key: (old_label, new_label)} for display-name changes
    has_changes:    True when any of the above lists/dicts are non-empty
    """
    event_value:     str
    prop_name:       str
    values_added:    List[str]           = field(default_factory=list)
    values_removed:  List[str]           = field(default_factory=list)
    labels_changed:  Dict[str, tuple]    = field(default_factory=dict)

    @property
    def has_changes(self) -> bool:
        return bool(self.values_added or self.values_removed or self.labels_changed)


def _extract_enum_props(schema: Dict[str, Any], *, prop_path: str = "") -> Dict[str, Dict]:
    """
    Walk a schema's properties recursively and return a flat dict of
    { "prop_path": {"enum": [...], "enumNames": ...} }
    for every property that carries an "enum" key.

    Handles:
      - Top-level properties
      - Nested object properties  (type=object with properties)
      - Array item properties     (type=array with items.properties)
    """
    result = {}
    props = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(props, dict):
        return result

    for name, prop in props.items():
        if not isinstance(prop, dict):
            continue

        full_path = f"{prop_path}.{name}" if prop_path else name

        if "enum" in prop and isinstance(prop["enum"], list):
            result[full_path] = {
                "enum":      prop["enum"],
                "enumNames": prop.get("enumNames"),
            }

        # Recurse into sub-object properties
        if isinstance(prop.get("properties"), dict):
            result.update(_extract_enum_props(prop, prop_path=full_path))

        # Recurse into array item properties
        if (
            prop.get("type") == "array"
            and isinstance(prop.get("items"), dict)
            and isinstance(prop["items"].get("properties"), dict)
        ):
            result.update(_extract_enum_props(prop["items"], prop_path=f"{full_path}[]"))

    return result


def _normalise_enum_names(enum_names: Any, enum_values: List[str]) -> Dict[str, str]:
    """
    Normalise enumNames to a {value_key: display_label} dict regardless of
    whether the server stores it as a dict or a parallel list.
    Returns an empty dict if enumNames is absent or malformed.
    """
    if isinstance(enum_names, dict):
        return {str(k): str(v) for k, v in enum_names.items()}
    if isinstance(enum_names, list) and len(enum_names) == len(enum_values):
        return {str(k): str(v) for k, v in zip(enum_values, enum_names)}
    return {}


def diff_choice_lists(
    server_state: Dict[str, Any],
    *,
    conservancy_name: str = "UNKNOWN",
    event_values: Optional[List[str]] = None,
) -> List[ChoiceListDiffResult]:
    """
    Compare choice lists (enum values + display names) between the desired
    state extracted from the server export and a modified desired state.

    In the current pipeline the "desired state" for choice lists IS the
    server export itself — there is no separate choice-list input yet
    (that belongs to the master-config gap).  This function therefore
    compares each schema's enum properties against themselves for
    structural correctness, and is designed so that once the master
    config is introduced you can pass in a ``desired_props`` dict to
    compare against.

    Parameters
    ----------
    server_state:
        Output of ``load_server_state()``.
    conservancy_name:
        Used only for logging/display.
    event_values:
        Optional list of event_values to restrict the diff to.
        If None, all schemas in the export are checked.

    Returns
    -------
    List of ChoiceListDiffResult — one per (event_value, prop_name) pair
    where a change is detected.  Results with no changes are omitted.
    """
    by_value: Dict[str, Dict] = server_state.get("by_value", {})
    scope = event_values if event_values is not None else list(by_value.keys())

    results: List[ChoiceListDiffResult] = []

    for ev in scope:
        rec = by_value.get(ev)
        if rec is None:
            logger.debug("diff_choice_lists: event_value %r not in server state", ev)
            continue

        schema = rec.get("schema") or {}
        server_props = _extract_enum_props(schema)

        # Until master config supplies desired choice lists,
        # we compare server against server — no changes expected.
        # Swap ``desired_props`` below once master config is wired in.
        desired_props = server_props  # placeholder — replace with config-driven data

        all_prop_names = set(server_props) | set(desired_props)

        for prop_name in sorted(all_prop_names):
            server_entry  = server_props.get(prop_name)
            desired_entry = desired_props.get(prop_name)

            # Property added (not on server, in desired)
            if server_entry is None:
                result = ChoiceListDiffResult(
                    event_value    = ev,
                    prop_name      = prop_name,
                    values_added   = list(desired_entry["enum"]),
                )
                if result.has_changes:
                    results.append(result)
                continue

            # Property removed (on server, not in desired)
            if desired_entry is None:
                result = ChoiceListDiffResult(
                    event_value    = ev,
                    prop_name      = prop_name,
                    values_removed = list(server_entry["enum"]),
                )
                if result.has_changes:
                    results.append(result)
                continue

            # Both present — compare values and labels
            server_values  = server_entry["enum"]
            desired_values = desired_entry["enum"]

            server_set  = set(str(v) for v in server_values)
            desired_set = set(str(v) for v in desired_values)

            added   = sorted(desired_set - server_set)
            removed = sorted(server_set  - desired_set)

            server_names  = _normalise_enum_names(server_entry.get("enumNames"),  server_values)
            desired_names = _normalise_enum_names(desired_entry.get("enumNames"), desired_values)

            labels_changed = {}
            for val in server_set & desired_set:
                old_label = server_names.get(val, "")
                new_label = desired_names.get(val, "")
                if old_label != new_label:
                    labels_changed[val] = (old_label, new_label)

            result = ChoiceListDiffResult(
                event_value    = ev,
                prop_name      = prop_name,
                values_added   = added,
                values_removed = removed,
                labels_changed = labels_changed,
            )
            if result.has_changes:
                results.append(result)

    return results


def print_choice_list_diff_summary(
    results: List[ChoiceListDiffResult],
    conservancy: str,
    *,
    verbose: bool = False,
) -> None:
    """Print a summary of choice list diff results to stdout."""
    from colorama import Fore, Style

    schemas_with_changes = len({r.event_value for r in results})
    total_props          = len(results)
    total_added          = sum(len(r.values_added)   for r in results)
    total_removed        = sum(len(r.values_removed) for r in results)
    total_relabelled     = sum(len(r.labels_changed) for r in results)

    print(Style.BRIGHT + Fore.MAGENTA + f"\n{'='*50}")
    print(Style.BRIGHT + Fore.MAGENTA + f"  CHOICE LIST DIFF — {conservancy.upper()}")
    print(Style.BRIGHT + Fore.MAGENTA + f"{'='*50}")
    print(Fore.GREEN  + f"  Schemas with changes : {schemas_with_changes}")
    print(Fore.GREEN  + f"  Properties affected  : {total_props}")
    print(Fore.GREEN  + f"  Values added         : {total_added}")
    print(Fore.RED    + f"  Values removed       : {total_removed}")
    print(Fore.YELLOW + f"  Labels changed       : {total_relabelled}")

    if not verbose or not results:
        return

    # Group by event_value for readable output
    by_schema: Dict[str, List[ChoiceListDiffResult]] = {}
    for r in results:
        by_schema.setdefault(r.event_value, []).append(r)

    for ev in sorted(by_schema):
        print(Fore.CYAN + f"\n  {ev}")
        for r in sorted(by_schema[ev], key=lambda x: x.prop_name):
            print(Fore.CYAN + f"    [{r.prop_name}]")
            for v in r.values_added:
                print(Fore.GREEN  + f"      + {v}")
            for v in r.values_removed:
                print(Fore.RED    + f"      - {v}")
            for val, (old, new) in sorted(r.labels_changed.items()):
                print(Fore.YELLOW + f"      ~ {val}: '{old}' → '{new}'")
