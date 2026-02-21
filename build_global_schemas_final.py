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

class BuildMode(str, Enum):
    PREVIEW = "preview"   # Excel-only, no validation, no writes
    EXPORT = "export"     # Validate against server, write output
    DEPLOY = "deploy"     # Same as export but intended for prod

# ─────────────────────────────────────────────────────────────
# Boolean parsing (unchanged from yours)
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
# ENUM TEMPLATING (from original)
# ─────────────────────────────────────────────────────────────

CHOICE_PREFIX = "enum___"
CHOICE_VALUES_SUFFIX = "___values"
CHOICE_NAMES_SUFFIX = "___names"


def make_choice_core(prop_name: str) -> str:
    return prop_name


def templatised_schema(schema: Dict[str, Any]) -> Dict[str, Any]:
    sch = copy.deepcopy(schema)

    # Remove host-bound fields
    sch.pop("id", None)
    sch.pop("image_url", None)

    props = sch.get("properties", {})
    if not isinstance(props, dict):
        return sch

    for prop_name, field in props.items():
        if not isinstance(field, dict):
            continue

        if "enum" in field:
            core = make_choice_core(prop_name)
            key = f"{CHOICE_PREFIX}{core}"

            field["enum"] = f"{{{{{key}{CHOICE_VALUES_SUFFIX}}}}}"
            field["enumNames"] = f"{{{{{key}{CHOICE_NAMES_SUFFIX}}}}}"

            field.pop("enumImages", None)
            field.pop("inactive_enum", None)

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
        raise RuntimeError(
            "Refusing to write global schemas without confirm=True"
        )

    with output_path.open("w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)

    logger.info("Wrote %d schemas to %s", len(records), output_path)


# ─────────────────────────────────────────────────────────────
# MAIN BUILD FUNCTION (MERGED VERSION)
# ─────────────────────────────────────────────────────────────

def build_global_schemas(
    df: pd.DataFrame,
    *,
    server_config_dirs: Dict[str, Path],
    mode: BuildMode = BuildMode.PREVIEW,
    output_dir: Optional[Path] = None,
    file_path=None,
) -> List[Dict]:

    deployed: List[Dict] = []
    missing: List[Tuple[str, str]] = []
    ambiguous: List[Tuple[str, str]] = []

    false_count = 0
    blank_count = 0
    system_count = 0

    # ─────────────────────────────────────────────
    # Preload all servers (Hybrid Mode)
    # ─────────────────────────────────────────────

    server_cache = {}
    server_mode = {}  # validated | fallback

    for server, config_dir in server_config_dirs.items():
        if mode == BuildMode.PREVIEW:
            server_cache[server] = {
                "records": [],
                "by_schema_id": {},
                "by_value": {},
            }
            server_mode[server] = "fallback"
            logger.info("Validation disabled — %s running in Excel-only mode.", server)
            continue

        try:
            server_cache[server] = load_server_schemas(config_dir)
            server_mode[server] = "validated"
            logger.info("Loaded schemas for %s (validated mode)", server)   

        except FileNotFoundError:
            logger.warning(
                f"No schema export found in {config_dir} — "
                f"{server} running in Excel fallback mode."
            )

            server_cache[server] = {
                "records": [],
                "by_schema_id": {},
                "by_value": {},
            }

            server_mode[server] = "fallback"

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

        # Support both old and new Excel formats

        legacy_id = (row.get("event_category_id", "") or "").strip()
        borana_id = (row.get("borana_schema_id", "") or "").strip()
        mugie_id = (row.get("mugie_schema_id", "") or "").strip()

        if legacy_id:
            # Old format — assume Borana source
            source_server = "Borana"
            # source_server = conservancy_name
            schema_id = legacy_id

        elif borana_id and mugie_id:
            source_server = "Borana"
            # source_server = conservancy_name
            schema_id = borana_id
            ambiguous.append(
                (event_value, "Both Borana and Mugie present; Borana chosen")
            )

        elif borana_id:
            source_server = "Borana"
            # source_server = conservancy_name
            schema_id = borana_id

        elif mugie_id:
            source_server = "Mugie"
            # source_server = concervancy_name
            schema_id = mugie_id

        else:
            missing.append((event_value, "No schema_id provided"))
            continue


        cache = server_cache.get(source_server, {})
        mode = server_mode.get(source_server, "fallback")

        rec = cache.get("by_schema_id", {}).get(schema_id)

        # ─────────────────────────────────────────────
        # VALIDATED MODE (server export exists)
        # ─────────────────────────────────────────────
        if mode == "validated":

            if rec is None:
                missing.append((event_value, f"Not found in {source_server}"))
                continue

            raw_schema = rec.get("schema") or {}

        # ─────────────────────────────────────────────
        # FALLBACK MODE (no server export)
        # ─────────────────────────────────────────────
        else:
            if not schema_id:
                missing.append((event_value, "No schema_id provided"))
                continue

            # Minimal schema placeholder
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

        deployed.append(
            {
                "event_value": event_value,
                "api_version": api_version or rec.get("api_version"),
                "schema_source_server": source_server,
                "schema_source_id": schema_id,
                "icon_id": gen_schema.get("icon_id"),
                "schema_title": gen_schema.get("title"),
                "schema": gen_schema,
            }
        )

    # ─────────────────────────────────────────────
    # DRY RUN SUMMARY
    # ─────────────────────────────────────────────
    conservancy = os.path.splitext(os.path.basename(file_path))[0].replace("schemas-", "")

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

    if mode == BuildMode.PREVIEW:
        logger.info("PREVIEW MODE — no files written.")
        return deployed

    if mode in {BuildMode.EXPORT, BuildMode.DEPLOY}:
        if output_dir is None:
            raise ValueError("output_dir must be provided in EXPORT/DEPLOY mode")
    else:
        return deployed

    output_dir.mkdir(parents=True, exist_ok=True)
    ts = pd.Timestamp.now().strftime("%Y%m%d-%H%M%S")

    out_json = output_dir / f"global_event_schemas_{ts}.json"
    write_global_json(deployed, out_json, confirm=True)

    if missing:
        miss_file = output_dir / f"global_event_schemas_missing_{ts}.txt"
        with miss_file.open("w") as f:
            for ev, reason in missing:
                f.write(f"{ev}\t{reason}\n")

    if ambiguous:
        amb_file = output_dir / f"global_event_schemas_ambiguous_{ts}.txt"
        with amb_file.open("w") as f:
            for ev, reason in ambiguous:
                f.write(f"{ev}\t{reason}\n")

    return deployed
