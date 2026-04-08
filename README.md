# Global Schema Builder

A Python toolkit for compiling, validating, and deploying EarthRanger event-type schemas across multiple conservancy servers (e.g. Borana, Mugie, Sosian, Suiyan, Catalyse).

---

## Overview

Each conservancy has its own EarthRanger server with its own event-type schemas. This project uses a set of Excel spreadsheets (one per conservancy) as a single source of truth to:

1. **Preview** — do a dry run across all conservancy files to see which schemas are deployable, without touching any server or writing any files.
2. **Export** — validate schemas against a live server JSON export, then write the output to disk as Excel and JSON files.
3. **Deploy** — same as Export — use this label to indicate a production deployment.

---

## File Structure

```
.
├── build_global_schemas_final_cl.py   # Core library (build logic, modes, schema templating)
├── run_build_final_cl.py              # Main CLI entry point — run this
├── build_global_schemas_final_test.py # Alternate/test version of the core library
├── run_build_final_test.py            # Alternate/test runner (uses test library)
├── global_schema_registry.xlsx        # Master registry of all schema IDs across servers
├── schemas-borana.xlsx                # Conservancy-specific schema definitions (root copies)
├── schemas-mugie.xlsx
├── schemas-sosian.xlsx
├── schemas-suiyan.xlsx
├── schemas-catalyse.xlsx
├── schemas-test.xlsx
├── excel files/                       # Working copies of the same schema spreadsheets
│   ├── schemas-borana.xlsx
│   ├── schemas-mugie.xlsx
│   ├── schemas-sosian.xlsx
│   ├── schemas-suiyan.xlsx
│   ├── schemas-catalyse.xlsx
│   └── schemas-test.xlsx
└── data/
    └── servers/
        └── <ServerName>/
            └── EarthRanger/
                └── Configuration/
                    └── eventtype_schemas_<timestamp>.json  # Exported from live server
```

> **Note:** The `excel files/` subfolder and the root-level `schemas-*.xlsx` files contain the same schema spreadsheets. When running the script, use `--folder "excel files"` (or provide the path interactively) to target the subfolder, or omit `--folder` to scan the project root.

---

## Server Configuration

The active server directories are defined at the top of `run_build_final_cl.py`:

```python
SERVER_CONFIG_DIRS = {
    "Borana": Path("data/servers/Borana/EarthRanger/Configuration"),
    "Mugie":  Path("data/servers/Mugie/EarthRanger/Configuration"),
}
```

Only **Borana** and **Mugie** have server-side JSON exports configured. Schemas for Sosian, Suiyan, Catalyse, and Test are processed from their Excel files but validated against the Borana or Mugie exports depending on which schema ID columns are populated. If no export is available for a source server, that server falls back to unvalidated (preview) mode automatically.

To add a new server, add an entry to `SERVER_CONFIG_DIRS` and place its JSON export under `data/servers/<ServerName>/EarthRanger/Configuration/`.

---

## Excel Schema Files

Each `schemas-<conservancy>.xlsx` file defines the schemas for that conservancy. The script reads the following columns:

| Column | Description |
|---|---|
| `event_value` | Unique identifier for the event type (e.g. `wildlife_sighting`) |
| `schema_title` | Human-readable title for the schema |
| `api_version` | API version string (e.g. `v1`) |
| `icon_id` | Icon identifier to associate with the event type |
| `event_is_active` | `True`/`False`/blank — only active rows are processed |
| `flag` | Set to `system` to skip a row (system-managed schemas) |
| `event_category_id` | Legacy schema ID (takes priority if present) |
| `borana_schema_id` | Schema ID from the Borana server |
| `mugie_schema_id` | Schema ID from the Mugie server |

**Priority logic for schema source:** If `event_category_id` is present, Borana is used as the source. If both `borana_schema_id` and `mugie_schema_id` are set, Borana is chosen and a warning is logged. Otherwise whichever single ID is present determines the source server.

---

## Setup

### Requirements

```bash
pip install pandas openpyxl colorama
```

### Server Data

To run in validated (Export/Deploy) mode, you need JSON schema exports from each live EarthRanger server. Place them at:

```
data/servers/<ServerName>/EarthRanger/Configuration/eventtype_schemas_<timestamp>.json
```

The script automatically picks up the **most recently modified** export file in each directory.

---

## Running the Script

The main entry point is `run_build_final_cl.py`. Run it from the project root directory.

### Interactive dry run (default)

```bash
python run_build_final_cl.py
```

Without flags, the script prompts you to choose **Auto** (scan a folder for all `.xlsx` files) or **Manual** (select a single file). In Auto mode you are also prompted for a folder path — press Enter to use the current directory, or type a path such as `excel files`.

### Skip the confirmation prompt

```bash
python run_build_final_cl.py --no-confirm
```

### Scan a specific folder for `.xlsx` files

```bash
python run_build_final_cl.py --folder "excel files"
```

This is the non-interactive equivalent of typing the folder path at the Auto-mode prompt.

### Dry run only (no deployment prompt at the end)

```bash
python run_build_final_cl.py --dry-only
```

### Verbose dry run (print per-schema breakdown)

```bash
python run_build_final_cl.py --verbose
```

### Process a single file manually

```bash
python run_build_final_cl.py --mode manual --file schemas-borana.xlsx
```

### Export using server JSON only (skip Excel)

```bash
python run_build_final_cl.py --use-export-only
```

Ignores all Excel files and builds schemas directly from the **Borana** server's JSON export (the path defined in `SERVER_CONFIG_DIRS["Borana"]`). Useful for a quick audit of what is currently live on Borana.

---

## Build Modes

| Mode | What it does |
|---|---|
| `PREVIEW` | Dry run — no server validation, no files written. Enum fields use placeholder templates. |
| `EXPORT` | Validates each schema ID against the live server JSON export. Writes output files to `deploy_output/`. |
| `DEPLOY` | Same as Export — use this label to indicate a production deployment. |

In `PREVIEW` mode, schemas are returned with templated placeholders for enum fields (e.g. `{{enum___species___values}}`). In `EXPORT`/`DEPLOY` mode, the actual schema JSON is pulled from the server export.

---

## Output Files

After a dry run, if you confirm deployment at the prompt, the script creates a `deploy_output/` folder containing:

- **`<conservancy>_deployed_schemas_<timestamp>.xlsx`** — one Excel file per conservancy with full schema details.
- **`<conservancy>_deployed_schemas_<timestamp>.json`** — one JSON file per conservancy with the same data.
- **`global_deployed_schemas_<timestamp>.json`** — a merged JSON file with all deployable schemas across all conservancies.

---

## Schema Templating

The core library strips host-specific fields (`id`, `image_url`) from schemas and replaces any `enum` field values with template placeholders:

```
{{enum___<property_name>___values}}
{{enum___<property_name>___names}}
```

This makes schemas portable across servers — the enum values can be injected per-deployment without modifying the schema structure itself.

---

## Logging & Warnings

After each conservancy is processed, a build summary is logged:

```
----------------------------------
BUILD SUMMARY - BORANA
----------------------------------
Total rows: 42
Deployed: 30
Inactive (False): 5
Inactive (Blank): 4
System flagged: 2
Missing: 1
Ambiguous: 0
```

**Missing** means a schema ID was expected but not found in the server export (only relevant in validated mode). **Ambiguous** means both a Borana and Mugie ID were provided — Borana is used and a warning is logged.

---

## Aborting

At any input prompt, type `exit`, `quit`, or `q` to safely abort. `Ctrl+C` is also handled gracefully.

---

## `_test` vs `_cl` files

The project contains two parallel sets of files:

- `build_global_schemas_final_cl.py` + `run_build_final_cl.py` — the **current/clean** version, intended for regular use.
- `build_global_schemas_final_test.py` + `run_build_final_test.py` — an **experimental/test** variant used for trying changes before merging them into the main files.

Use the `_cl` pair for all normal operations.