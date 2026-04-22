"""CLI for first-time / batch CSV imports.

Usage:
    python scripts/import_csv.py data/raw/<file>.csv [--notes "..."]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db as db_module  # noqa: E402
from app import importer as importer_module  # noqa: E402
from app import rules as rules_module  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path", type=Path)
    ap.add_argument("--notes", default=None)
    ap.add_argument("--no-apply-rules", action="store_true")
    args = ap.parse_args()

    if not args.path.exists():
        print(f"File not found: {args.path}", file=sys.stderr)
        return 2

    conn = db_module.connect()
    db_module.init_schema(conn)
    result = importer_module.import_csv(conn, args.path, notes=args.notes)
    print(
        f"Imported {result.rows_inserted} rows (skipped {result.rows_skipped_duplicate} dupes) "
        f"from {args.path.name}. import_id={result.import_id}"
    )
    if not args.no_apply_rules:
        r = rules_module.apply_rules(conn)
        print(
            f"Rules applied: {r['changed']} changed, {r['unchanged']} unchanged, "
            f"{r['manual_skipped']} manual preserved."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
