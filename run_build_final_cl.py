import logging
import pandas as pd
import glob
import os
import argparse
from datetime import datetime
from collections import defaultdict
from pathlib import Path
from colorama import init, Fore, Style
import json

from build_global_schemas_final_cl import build_global_schemas, BuildMode, latest_schema_export

# ─────────────────────────────────────────────
# CONFIGURE YOUR SERVER DIRECTORIES HERE
# ─────────────────────────────────────────────
SERVER_CONFIG_DIRS = {
    "Borana": Path("data/servers/Borana/EarthRanger/Configuration"),
    "Mugie": Path("data/servers/Mugie/EarthRanger/Configuration"),
}

# ─────────────────────────────────────────────

init(autoreset=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s: %(message)s",
)


def safe_input(message: str) -> str:
    """Wrapper around input() that allows global escape keywords."""
    value = input(message).strip().lower()
    if value in {"exit", "quit", "q"}:
        print(Fore.RED + "\n⚠ Execution manually aborted by user.")
        raise SystemExit(0)
    return value


def print_dry_run_detail(grouped_deployed: dict) -> None:
    """Print per-conservancy schema breakdown for spot-checking."""
    for conservancy in sorted(grouped_deployed.keys()):
        schemas = grouped_deployed[conservancy]
        if not schemas:
            continue
        print(Style.BRIGHT + Fore.BLUE + f"\n--- {conservancy.upper()} ---")
        print(f"Deployable: {len(schemas)}")
        print("---------------------------")
        for schema in sorted(schemas, key=lambda x: x.get("event_value", "")):
            event_value = schema.get("event_value", "N/A")
            source_server = schema.get("schema_source_server", "N/A")
            schema_id = schema.get("schema_source_id", "N/A")
            schema_title = schema.get("schema_title", "N/A")
            print(f"{event_value} | {source_server} | {schema_id} | {schema_title}")


def main():
    parser = argparse.ArgumentParser(description="Global schema builder")
    parser.add_argument("--mode", choices=["manual", "auto"], default="auto")
    parser.add_argument("--file", type=str, default=None)
    parser.add_argument("--no-confirm", action="store_true")
    parser.add_argument("--deploy", action="store_true")
    parser.add_argument("--prompt-deploy", action="store_true")
    parser.add_argument("--escape", action="store_true")
    parser.add_argument("--dry-only", action="store_true")
    parser.add_argument("--folder", type=str, default=None, help="Folder to scan for .xlsx files in auto mode")
    parser.add_argument(
        "--use-export-only",
        action="store_true",
        help="Ignore Excel; build global schemas using server JSON exports only"
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print per-schema breakdown in dry run summary",
    )

    args = parser.parse_args()

# ─────────────────────────────────────────────
# EXPORT-ONLY MODE (RUN FIRST AND EXIT)
# ─────────────────────────────────────────────
    if args.use_export_only:
        print(Fore.CYAN + "\nUsing server JSON export only (skipping Excel)...")

        # FIX 3: Use latest_schema_export() instead of a hardcoded filename,
        # so this always picks up the most recently generated export file.
        borana_config_dir = SERVER_CONFIG_DIRS["Borana"]
        export_file = latest_schema_export(borana_config_dir)

        if export_file is None:
            raise FileNotFoundError(
                f"No eventtype_schemas_*.json found in {borana_config_dir}"
            )

        print(Fore.CYAN + f"Using export file: {export_file.name}")

        # Load JSON records
        with export_file.open("r", encoding="utf-8") as f:
            export_records = json.load(f)

        # Prepare one row per schema (not per property)
        export_rows = []
        for record in export_records:
            sch = record.get("schema", {})
            export_rows.append({
                "event_value": record.get("value"),
                "api_version": record.get("api_version", "v1"),
                "schema_title": sch.get("title", ""),
                "icon_id": sch.get("icon_id", ""),
                "event_is_active": True,
                "flag": "",
                "event_category_id": "",
                "borana_schema_id": sch.get("id", record.get("value")),
                "mugie_schema_id": ""
            })

        df_export = pd.DataFrame(export_rows)

        if df_export.empty:
            print(Fore.RED + "No deployable schemas found in export. Aborting.")
            return

        print(Fore.GREEN + f"Prepared {len(df_export)} deployable schema rows.")

        # Deploy schemas
        deployed = build_global_schemas(
            df_export,
            file_path=str(export_file),
            server_config_dirs=SERVER_CONFIG_DIRS,
            mode=BuildMode.EXPORT,
            output_dir=Path("deploy_output"),
        )

        print(Fore.GREEN + f"\nExport test complete. Generated {len(deployed)} schemas.")
        return

    # ---- Escape flag ----
    if args.escape:
        print(Style.BRIGHT + Fore.YELLOW + "⚠ Escape flag detected. Stopping execution.")
        return

    # ---- Auto mode confirmation ----
    if args.mode == "auto" and not args.no_confirm:
        print(Fore.YELLOW + "\nSelect mode [type exit, quit, or q to stop]:")
        print("  1 - Auto   (scan a folder for all .xlsx files)")
        print("  2 - Manual (select a single .xlsx file)")
        mode_choice = safe_input("Enter choice (1/2): ")

        if mode_choice == "2":
            args.mode = "manual"
        else:
            # Auto mode — ask for folder
            folder_input = safe_input("Enter folder path (or press Enter for current directory): ")
            search_dir = Path(folder_input).resolve() if folder_input else Path.cwd()

            if not search_dir.is_dir():
                print(Fore.RED + f"Folder not found: {search_dir}")
                return

            excel_files = [
                str(p) for p in search_dir.glob("*.xlsx")
                if not p.name.startswith("~$")
            ]

            if not excel_files:
                print(Fore.RED + "No .xlsx files found in that folder.")
                return

    # ---- Manual file selection ----
    if args.mode == "manual" and not args.file:
        last_dir = os.getcwd()
        while True:
            file_input = safe_input(f"Enter Excel file path [{last_dir}]: ")
            if not file_input:
                print(Style.BRIGHT + Fore.CYAN + "Please provide a valid Excel file path.")
                continue
            file_input = os.path.expanduser(file_input)
            if not os.path.isabs(file_input):
                file_input = os.path.join(last_dir, file_input)
            if os.path.isfile(file_input):
                args.file = file_input
                last_dir = os.path.dirname(file_input)
                break
            else:
                print(Style.BRIGHT + Fore.RED + f"File not found: {file_input}")

    # ---- Collect Excel files ----
    if args.mode == "manual":
        if not args.file or not os.path.isfile(args.file):
            raise FileNotFoundError(f"Manual mode requires a valid --file argument.")
        excel_files = [args.file]
    else:
        # excel_files = [f for f in glob.glob("*.xlsx") if not f.startswith("~$")]
        search_dir = Path(args.folder).resolve() if args.folder else Path.cwd()
        excel_files = [
            str(p) for p in search_dir.glob("*.xlsx")
            if not p.name.startswith("~$")
        ]

        if not excel_files:
            print(Fore.RED + "No .xlsx files found in current directory.")
            return

    grouped_deployed = defaultdict(list)
    total_rows = 0

    print(Fore.MAGENTA + "\n==============================")
    print(Fore.MAGENTA + "GLOBAL DRY RUN START")
    print(Fore.MAGENTA + "==============================")

    # ---- Process each Excel file ----
    for file_path in excel_files:
        print(Fore.CYAN + f"\nProcessing: {file_path}")
        df = pd.read_excel(file_path)
        total_rows += len(df)

        conservancy = os.path.splitext(os.path.basename(file_path))[0].replace("schemas-", "")

        deployed = build_global_schemas(
            df,
            file_path=file_path,
            server_config_dirs=SERVER_CONFIG_DIRS,
            mode=BuildMode.PREVIEW,
        )
        grouped_deployed[conservancy].extend(deployed)

    # ---- Global summary ----
    total_deployed = sum(len(v) for v in grouped_deployed.values())
    print(Fore.MAGENTA + "\n==============================")
    print(Fore.MAGENTA + "GLOBAL DRY RUN SUMMARY")
    print(Fore.MAGENTA + "==============================")
    print(Fore.GREEN + f"Total schemas processed: {total_rows}")
    print(Fore.GREEN + f"Total deployable schemas: {total_deployed}")

    for conservancy in sorted(grouped_deployed.keys()):
        schemas = grouped_deployed[conservancy]
        if not schemas:
            continue
        print(Style.BRIGHT + Fore.BLUE + f"\n--- {conservancy.upper()} ---")
        print(f"Deployable: {len(schemas)}")
        print("---------------------------")

        for schema in sorted(schemas, key=lambda x: x.get("event_value", "")):
            event_value = schema.get("event_value", "N/A")
            source_server = schema.get("schema_source_server", "N/A")
            schema_id = schema.get("schema_source_id", "N/A")
            schema_title = schema.get("schema_title", "N/A")
            print(f"{event_value} | {source_server} | {schema_id} | {schema_title}")

    if args.dry_only:
        print(Fore.YELLOW + "\nDry run complete (--dry-only).")
        return

# ---- Deployment stage prompt ----
    if grouped_deployed:
        # Prompt user after displaying summaries
        confirm = safe_input(Style.BRIGHT + Fore.CYAN + "\nDo you want to generate Excel/JSON files for deployable schemas? (y/n): ")

        if confirm == "y":
            output_dir = Path("deploy_output")
            output_dir.mkdir(exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

            merged_schemas = []

            # ---- Per-conservancy Excel and enrich schema with server info ----
            for conservancy, schemas in grouped_deployed.items():
                if not schemas:
                    continue

                for schema in schemas:
                    # Add source server info if not present
                    if "schema_source_server" not in schema:
                        schema["schema_source_server"] = "Unknown"
                    if "schema_source_path" not in schema:
                        schema["schema_source_path"] = str(
                            SERVER_CONFIG_DIRS.get(conservancy, Path("Unknown"))
                        )

                df_out = pd.DataFrame(schemas)
                output_path = output_dir / f"{conservancy}_deployed_schemas_{timestamp}.xlsx"
                df_out.to_excel(output_path, index=False)
                print(Fore.GREEN + f"Generated: {output_path}")

                # ---- Per-conservancy JSON ----
                conservancy_json_path = output_dir / f"{conservancy}_deployed_schemas_{timestamp}.json"
                with open(conservancy_json_path, "w", encoding="utf-8") as f:
                    json.dump(schemas, f, ensure_ascii=False, indent=2)
                print(Fore.GREEN + f"Generated JSON: {conservancy_json_path}")

                merged_schemas.extend(schemas)

            # ---- Global JSON including server info ----
            global_json_path = output_dir / f"global_deployed_schemas_{timestamp}.json"
            with open(global_json_path, "w", encoding="utf-8") as f:
                json.dump(merged_schemas, f, ensure_ascii=False, indent=2)
            print(Fore.GREEN + f"Generated global JSON: {global_json_path}")

        else:
            print(Fore.YELLOW + "\n⚠ Deployment skipped by user.")

# ---- Graceful Ctrl+C handling ----
if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(Fore.RED + "\n\n⚠ Execution cancelled by user (Ctrl+C).")
        raise SystemExit(0)
