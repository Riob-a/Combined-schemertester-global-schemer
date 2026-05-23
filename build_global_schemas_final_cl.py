import logging
from typing import List, Dict, Optional, Any, Tuple
import pandas as pd
import os
import json
import copy
from glob import glob
from pathlib import Path
from enum import Enum

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# BUILD MODE ENUM
# ─────────────────────────────────────────────────────────────

class BuildMode(str, Enum):
    PREVIEW = "preview"
    EXPORT = "export"
    DEPLOY = "deploy"


# ─────────────────────────────────────────────────────────────
# BOOLEAN PARSING
# ─────────────────────────────────────────────────────────────

def parse_bool(value) -> Optional[bool]:
    if pd.isna(value):
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        val_lower = value.strip().lower()
        if val_lower in {"true", "yes", "1"}:
            return True
        elif val_lower in {"false", "no", "0"}:
            return False
    return None


# ─────────────────────────────────────────────────────────────
# ENUM TEMPLATING
# ─────────────────────────────────────────────────────────────

CHOICE_PREFIX = "enum___"
CHOICE_VALUES_SUFFIX = "___values"
CHOICE_NAMES_SUFFIX = "___names"


def make_choice_core(prop_name: str) -> str:
    return prop_name


def _templatise_properties(props: Dict[str, Any], prop_path: str = "") -> None:
    """Recursively templatise enum fields at any nesting depth."""
    for prop_name, field in props.items():
        if not isinstance(field, dict):
            continue

        full_name = f"{prop_path}.{prop_name}" if prop_path else prop_name

        if "enum" in field:
            core = make_choice_core(full_name)
            key = f"{CHOICE_PREFIX}{core}"
            field["enum"] = f"{{{{{key}{CHOICE_VALUES_SUFFIX}}}}}"
            field["enumNames"] = f"{{{{{key}{CHOICE_NAMES_SUFFIX}}}}}"
            field.pop("enumImages", None)
            field.pop("inactive_enum", None)

        # Recurse into sub-object properties
        if "properties" in field and isinstance(field["properties"], dict):
            _templatise_properties(field["properties"], full_name)

        # Recurse into array item properties
        if (
            field.get("type") == "array"
            and isinstance(field.get("items"), dict)
            and isinstance(field["items"].get("properties"), dict)
        ):
            _templatise_properties(field["items"]["properties"], f"{full_name}[]")


def templatised_schema(schema: Dict[str, Any]) -> Dict[str, Any]:
    sch = copy.deepcopy(schema)

    # Remove host-bound fields
    sch.pop("id", None)
    sch.pop("image_url", None)

    props = sch.get("properties", {})
    if not isinstance(props, dict):
        return sch

    _templatise_properties(props)
    return sch


# ─────────────────────────────────────────────────────────────
# SERVER SCHEMA LOADING
# ─────────────────────────────────────────────────────────────

def latest_schema_export(config_dir: Path) -> Optional[Path]:
    pattern = str(config_dir / "eventtype_schemas_*.json")
    candidates = glob(pattern)
    if not candidates:
        return None

    return sorted(
        (Path(p) for p in candidates),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )[0]


def load_server_schemas(config_dir: Path) -> Dict[str, Dict]:
    latest = latest_schema_export(config_dir)
    if latest is None:
        raise FileNotFoundError(f"No schema export found in {config_dir}")

    with latest.open("r", encoding="utf-8") as f:
        records = json.load(f)

    by_schema_id = {}
    by_value = {}

    for rec in records:
        val = rec.get("value")
        sch = rec.get("schema") or {}
        schema_id = sch.get("id")

        if val:
            by_value[val] = rec
        if schema_id:
            by_schema_id[schema_id] = rec

    return {
        "records": records,
        "by_schema_id": by_schema_id,
        "by_value": by_value,
    }


# ─────────────────────────────────────────────────────────────
# SAFE JSON WRITER
# ─────────────────────────────────────────────────────────────

def write_global_json(
    records: List[Dict],
    output_path: Path,
    *,
    confirm: bool = False,
) -> None:
    if not confirm:
        raise RuntimeError("Refusing to write global schemas without confirm=True")

    with output_path.open("w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)

    logger.info("Wrote %d schemas to %s", len(records), output_path)


# ─────────────────────────────────────────────────────────────
# MAIN BUILD FUNCTION
# ─────────────────────────────────────────────────────────────

def build_global_schemas(
    df: pd.DataFrame,
    *,
    server_config_dirs: Dict[str, Path],
    mode: BuildMode = BuildMode.PREVIEW,
    output_dir: Optional[Path] = None,
    file_path: Optional[str] = None,
) -> List[Dict]:

    deployed: List[Dict] = []
    missing: List[Tuple[str, str]] = []
    ambiguous: List[Tuple[str, str]] = []

    false_count = 0
    blank_count = 0
    system_count = 0

    # ─────────────────────────────────────────────
    # PRELOAD SERVER EXPORTS
    # ─────────────────────────────────────────────

    server_cache = {}
    server_validation_mode = {}

    for server, config_dir in server_config_dirs.items():
        if mode == BuildMode.PREVIEW:
            server_cache[server] = {"records": [], "by_schema_id": {}, "by_value": {}}
            server_validation_mode[server] = "fallback"
            continue

        try:
            server_cache[server] = load_server_schemas(config_dir)
            server_validation_mode[server] = "validated"
            logger.info("Loaded schemas for %s (validated mode)", server)

        except FileNotFoundError:
            logger.warning(
                "No schema export found in %s — %s running in fallback mode.",
                config_dir,
                server,
            )
            server_cache[server] = {"records": [], "by_schema_id": {}, "by_value": {}}
            server_validation_mode[server] = "fallback"

    # ─────────────────────────────────────────────
    # PROCESS ROWS
    # ─────────────────────────────────────────────

    for _, row in df.iterrows():

        event_value = row.get("event_value", "")
        schema_title = row.get("schema_title", "")
        api_version = row.get("api_version", "")
        icon_id = row.get("icon_id", "")

        is_active = parse_bool(row.get("event_is_active", ""))
        flag = str(row.get("flag", "")).strip().lower()

        if is_active is not True:
            if is_active is False:
                false_count += 1
            else:
                blank_count += 1
            continue

        if flag == "system":
            system_count += 1
            continue

        legacy_id = str(row.get("event_category_id") or "").strip()
        borana_id = str(row.get("borana_schema_id") or "").strip()
        mugie_id = str(row.get("mugie_schema_id") or "").strip()

        source_server = None
        schema_id = None

        if legacy_id:
            source_server = "Borana"
            schema_id = legacy_id

        elif borana_id and mugie_id:
            source_server = "Borana"
            schema_id = borana_id
            ambiguous.append(
                (event_value, "Both Borana and Mugie present; Borana chosen")
            )

        elif borana_id:
            source_server = "Borana"
            schema_id = borana_id

        elif mugie_id:
            source_server = "Mugie"
            schema_id = mugie_id

        else:
            missing.append((event_value, "No schema_id provided"))
            continue

        cache = server_cache.get(source_server, {})
        validation_mode = server_validation_mode.get(source_server, "fallback")

        rec = cache.get("by_schema_id", {}).get(schema_id)

        # ─────────────────────────────────────────
        # VALIDATED MODE
        # ─────────────────────────────────────────

        if validation_mode == "validated":
            if rec is None:
                missing.append((event_value, f"Not found in {source_server}"))
                continue
            raw_schema = rec.get("schema") or {}

        else:
            raw_schema = {
                "id": schema_id,
                "title": schema_title,
                "type": "object",
                "properties": {},
            }

        gen_schema = templatised_schema(raw_schema)

        if schema_title:
            gen_schema["title"] = schema_title
        if icon_id:
            gen_schema["icon_id"] = icon_id

        api_version_final = api_version
        if not api_version_final and rec:
            api_version_final = rec.get("api_version")
        if not api_version_final:
            logger.warning(
                "Missing api_version for event_value=%r (schema_id=%s, server=%s)",
                event_value,
                schema_id,
                source_server,
            )

        deployed.append(
            {
                "event_value": event_value,
                "api_version": api_version_final,
                "schema_source_server": source_server,
                "schema_source_id": schema_id,
                "icon_id": gen_schema.get("icon_id"),
                "schema_title": gen_schema.get("title"),
                "schema": gen_schema,
            }
        )

    # ─────────────────────────────────────────────
    # SUMMARY
    # ─────────────────────────────────────────────

    conservancy = "UNKNOWN"
    if file_path:
        conservancy = (
            os.path.splitext(os.path.basename(file_path))[0]
            .replace("schemas-", "")
        )

    logger.info(
        "\n----------------------------------"
        f"\nBUILD SUMMARY - {conservancy.upper()}\n"
        "----------------------------------\n"
        f"Total rows: {len(df)}\n"
        f"Deployed: {len(deployed)}\n"
        f"Inactive (False): {false_count}\n"
        f"Inactive (Blank): {blank_count}\n"
        f"System flagged: {system_count}\n"
        f"Missing: {len(missing)}\n"
        f"Ambiguous: {len(ambiguous)}"
    )

    # ─────────────────────────────────────────────
    # WRITE OUTPUT (EXPORT / DEPLOY)
    # ─────────────────────────────────────────────

    if mode == BuildMode.PREVIEW:
        return deployed

    if mode in {BuildMode.EXPORT, BuildMode.DEPLOY}:
        if output_dir is None:
            raise ValueError("output_dir required in EXPORT/DEPLOY mode")

        output_dir.mkdir(parents=True, exist_ok=True)
        ts = pd.Timestamp.now().strftime("%Y%m%d-%H%M%S")
        out_json = output_dir / f"global_event_schemas_{ts}.json"
        write_global_json(deployed, out_json, confirm=True)

    return deployed