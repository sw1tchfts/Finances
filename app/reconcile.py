"""Reconciliation math for the partnership.

The core question: within the partnership window,
    (business income) - (business expenses + shared expenses) = net profit

If net profit <= 0, no 50/50 profit split was ever owed to the partner.

Every shared/business expense is further attributed to whoever paid it so we
can show a per-partner contribution balance.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from . import db as db_module


@dataclass
class Bucket:
    count: int = 0
    total: float = 0.0

    def add(self, amount: float) -> None:
        self.count += 1
        self.total += amount


@dataclass
class Summary:
    start: str | None
    end: str | None
    partner_user_name: str
    partner_other_name: str

    # Top-level buckets (all within window).
    business_income: Bucket = field(default_factory=Bucket)
    business_expense: Bucket = field(default_factory=Bucket)
    shared_expense: Bucket = field(default_factory=Bucket)
    personal: Bucket = field(default_factory=Bucket)
    transfer: Bucket = field(default_factory=Bucket)
    excluded: Bucket = field(default_factory=Bucket)
    unclassified: Bucket = field(default_factory=Bucket)

    # Income is stored as a negative number in the CSV (money in). We flip sign
    # in `gross_income` so downstream math reads naturally.
    @property
    def gross_income(self) -> float:
        return -self.business_income.total

    @property
    def total_expenses(self) -> float:
        return self.business_expense.total + self.shared_expense.total

    @property
    def net_profit(self) -> float:
        return self.gross_income - self.total_expenses

    # What each partner is entitled to under a 50/50 profit-split if there is
    # profit. If loss, neither is entitled to a distribution from the pool.
    @property
    def partner_profit_share(self) -> float:
        return max(self.net_profit, 0.0) / 2.0


def _window(conn: sqlite3.Connection) -> tuple[str | None, str | None]:
    start = db_module.get_setting(conn, "partnership_start") or None
    end = db_module.get_setting(conn, "partnership_end") or None
    return start or None, end or None


def _in_window(date: str, start: str | None, end: str | None) -> bool:
    if start and date < start:
        return False
    if end and date > end:
        return False
    return True


def build_summary(conn: sqlite3.Connection) -> Summary:
    start, end = _window(conn)
    s = Summary(
        start=start,
        end=end,
        partner_user_name=db_module.get_setting(conn, "partner_user_name") or "You",
        partner_other_name=db_module.get_setting(conn, "partner_other_name") or "Partner",
    )

    rows = conn.execute(
        """
        SELECT t.id, t.date, t.amount,
               COALESCE(c.classification, 'unclassified') AS classification
        FROM transactions t
        LEFT JOIN classifications c
          ON c.transaction_id = t.id AND c.superseded_at IS NULL
        """
    ).fetchall()

    for r in rows:
        if not _in_window(r["date"], start, end):
            continue
        bucket = getattr(s, r["classification"], None)
        if bucket is None:
            bucket = s.unclassified
        bucket.add(r["amount"])

    return s


def by_account_and_class(conn: sqlite3.Connection) -> list[dict]:
    """Breakdown by (account, classification) within the window. Useful for
    the dashboard's contribution table."""
    start, end = _window(conn)
    params: list = []
    clauses = []
    if start:
        clauses.append("t.date >= ?")
        params.append(start)
    if end:
        clauses.append("t.date <= ?")
        params.append(end)
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    rows = conn.execute(
        f"""
        SELECT
            t.account_name,
            t.account_number,
            t.institution_name,
            COALESCE(c.classification, 'unclassified') AS classification,
            COUNT(*) AS n,
            SUM(t.amount) AS total
        FROM transactions t
        LEFT JOIN classifications c
          ON c.transaction_id = t.id AND c.superseded_at IS NULL
        {where}
        GROUP BY t.account_name, t.account_number, t.institution_name, classification
        ORDER BY t.institution_name, t.account_name, classification
        """,
        params,
    ).fetchall()
    return [dict(r) for r in rows]
