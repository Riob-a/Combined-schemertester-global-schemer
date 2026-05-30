"""
master_config_reader.py
───────────────────────
Reads master_config.xlsx and reconstructs per-conservancy DataFrames
in the same shape the pipeline already expects.

Usage:
    from master_config_reader import load_master_config, iter_conservancy_frames

    frames = load_master_config(Path("master_config.xlsx"))
    for conservancy, df in iter_conservancy_frames(frames):
        # df has the same columns the pipeline reads from individual xlsx files
        ...

This is a drop-in replacement for pd.read_excel(per_conservancy_file).
The pipeline in run_build_final_cl.py can switch to this by replacing its
Excel-scanning loop with iter_conservancy_frames().
"""

from pathlib import Path
from typing import Iterator, Optional
import pandas as pd


# ─────────────────────────────────────────────────────────────
# LOAD
# ─────────────────────────────────────────────────────────────

def load_master_config(path: Path) -> dict[str, pd.DataFrame]:
    """
    Load master_config.xlsx and return both sheets as DataFrames.

    Returns
    -------
    {
        "schemas":             DataFrame (one row per event_value),
        "conservancy_mapping": DataFrame (one row per event_value × conservancy),
    }
    """
    if not path.exists():
        raise FileNotFoundError(f"Master config not found: {path}")

    sheets = pd.read_excel(
        path,
        sheet_name=["schemas", "conservancy_mapping"],
        dtype={"event_value": str, "event_category_id": str},
    )
    return {
        "schemas":             sheets["schemas"],
        "conservancy_mapping": sheets["conservancy_mapping"],
    }


# ─────────────────────────────────────────────────────────────
# RECONSTRUCT PER-CONSERVANCY DATAFRAMES
# ─────────────────────────────────────────────────────────────

def iter_conservancy_frames(
    frames: dict[str, pd.DataFrame],
    conservancies: Optional[list[str]] = None,
) -> Iterator[tuple[str, pd.DataFrame]]:
    """
    Yield (conservancy_name, df) pairs where each df matches the column
    shape the pipeline reads from individual schemas-*.xlsx files:

        event_value, schema_title, api_version, icon_id, image_url,
        n_fields, n_enums, event_name, event_category_display,
        event_category_id, event_is_active

    Parameters
    ----------
    frames:
        Output of load_master_config().
    conservancies:
        Optional list to restrict which conservancies are yielded.
        If None, all conservancies in conservancy_mapping are yielded.
    """
    df_schemas = frames["schemas"]
    df_mapping = frames["conservancy_mapping"]

    all_conservancies = sorted(df_mapping["conservancy"].dropna().unique())
    scope = conservancies if conservancies is not None else all_conservancies

    for conservancy in scope:
        mapping_rows = df_mapping[df_mapping["conservancy"] == conservancy].copy()

        # Join shared columns from schemas sheet
        merged = mapping_rows.merge(df_schemas, on="event_value", how="left")

        # Drop the conservancy column (implicit from context) and reorder
        merged = merged.drop(columns=["conservancy"])

        yield conservancy, merged.reset_index(drop=True)


# ─────────────────────────────────────────────────────────────
# QUICK SUMMARY
# ─────────────────────────────────────────────────────────────

def summarise(frames: dict[str, pd.DataFrame]) -> None:
    """Print a quick summary of what's in the master config."""
    df_schemas = frames["schemas"]
    df_mapping = frames["conservancy_mapping"]

    print(f"Master config summary")
    print(f"  schemas sheet:             {len(df_schemas)} unique event_values")
    print(f"  conservancy_mapping sheet: {len(df_mapping)} rows")
    print()
    counts = df_mapping.groupby("conservancy").size().sort_values(ascending=False)
    print("  Rows per conservancy:")
    for con, n in counts.items():
        active = df_mapping[
            (df_mapping["conservancy"] == con) &
            (df_mapping["event_is_active"] == True)
        ]
        print(f"    {con:<12} {n:>4} total  |  {len(active):>4} active")


# ─────────────────────────────────────────────────────────────
# CLI SMOKE TEST
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("master_config.xlsx")
    frames = load_master_config(path)
    summarise(frames)
    print()
    print("Sample per-conservancy frames:")
    for conservancy, df in iter_conservancy_frames(frames):
        print(f"  {conservancy}: {len(df)} rows, columns: {df.columns.tolist()}")
