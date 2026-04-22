"""Rocket Money CSV ingest.

Safety model:
  - The source file's sha256 is recorded in `imports`.
  - Each row is fingerprinted as
        sha256(date | account_number | amount | name | description | occurrence)
    where `occurrence` is the 1-based index of this row among all rows with
    the same (date, account, amount, name, description). Rocket Money exports
    the same transactions in a stable order, so overlapping re-exports
    deterministically produce the same occurrence index. This preserves
    legitimately-identical purchases (e.g. two $2.06 gas-station stops on
    the same day) while keeping re-imports idempotent.
  - Import is wrapped in a transaction so a bad file cannot leave partial state.
"""
from __future__ import annotations

import csv
import hashlib
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

EXPECTED_HEADERS = [
    "Date",
    "Original Date",
    "Account Type",
    "Account Name",
    "Account Number",
    "Institution Name",
    "Name",
    "Custom Name",
    "Amount",
    "Description",
    "Category",
    "Note",
    "Ignored From",
    "Tax Deductible",
    "Transaction Tags",
]


@dataclass
class ImportResult:
    import_id: int
    rows_in_file: int
    rows_inserted: int
    rows_skipped_duplicate: int


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _basis(row: dict) -> str:
    return "|".join(
        [
            row.get("Date", "") or "",
            row.get("Account Number", "") or "",
            row.get("Amount", "") or "",
            row.get("Name", "") or "",
            row.get("Description", "") or "",
        ]
    )


def _fingerprint(basis: str, occurrence: int) -> str:
    return hashlib.sha256(f"{basis}|#{occurrence}".encode("utf-8")).hexdigest()


def import_csv(conn: sqlite3.Connection, path: Path, notes: str | None = None) -> ImportResult:
    path = Path(path)
    file_hash = _sha256_file(path)

    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames != EXPECTED_HEADERS:
            missing = set(EXPECTED_HEADERS) - set(reader.fieldnames or [])
            extra = set(reader.fieldnames or []) - set(EXPECTED_HEADERS)
            raise ValueError(
                f"Unexpected CSV headers. Missing: {sorted(missing)}. Extra: {sorted(extra)}."
            )
        rows = list(reader)

    conn.execute("BEGIN")
    try:
        cur = conn.execute(
            "INSERT INTO imports(filename, file_hash, row_count, notes) VALUES (?, ?, ?, ?)",
            (path.name, file_hash, len(rows), notes),
        )
        import_id = cur.lastrowid

        inserted = 0
        skipped = 0
        occurrence = defaultdict(int)
        for row in rows:
            basis = _basis(row)
            occurrence[basis] += 1
            fp = _fingerprint(basis, occurrence[basis])
            try:
                amount = float(row["Amount"]) if row["Amount"] else 0.0
            except ValueError:
                amount = 0.0
            try:
                conn.execute(
                    """
                    INSERT INTO transactions (
                        fingerprint, import_id, date, original_date, account_type,
                        account_name, account_number, institution_name, name, custom_name,
                        amount, description, category, note, ignored_from, tax_deductible,
                        transaction_tags
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        fp,
                        import_id,
                        row["Date"],
                        row["Original Date"] or None,
                        row["Account Type"],
                        row["Account Name"],
                        row["Account Number"],
                        row["Institution Name"],
                        row["Name"],
                        row["Custom Name"] or None,
                        amount,
                        row["Description"],
                        row["Category"],
                        row["Note"] or None,
                        row["Ignored From"] or None,
                        row["Tax Deductible"] or None,
                        row["Transaction Tags"] or None,
                    ),
                )
                inserted += 1
            except sqlite3.IntegrityError:
                skipped += 1

        conn.execute(
            "INSERT INTO audit_log(event_type, entity_type, entity_id, details) VALUES (?, ?, ?, ?)",
            (
                "import",
                "import",
                import_id,
                f'{{"filename":"{path.name}","inserted":{inserted},"skipped":{skipped}}}',
            ),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    return ImportResult(
        import_id=import_id,
        rows_in_file=len(rows),
        rows_inserted=inserted,
        rows_skipped_duplicate=skipped,
    )
