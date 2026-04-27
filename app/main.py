"""Flask entry point."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path

from flask import (
    Flask,
    abort,
    flash,
    g,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from werkzeug.utils import secure_filename

from . import db as db_module
from . import importer as importer_module
from . import reconcile as reconcile_module
from . import rules as rules_module

ROOT = Path(__file__).resolve().parent.parent
RECEIPTS_DIR = ROOT / "data" / "receipts"
RAW_DIR = ROOT / "data" / "raw"
ALLOWED_RECEIPT_EXT = {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".heic", ".gif", ".tif", ".tiff"}
MAX_RECEIPT_BYTES = 25 * 1024 * 1024

CLASSIFICATION_LABELS = {
    "business_income": "Business Income",
    "business_expense": "Business Expense",
    "shared_expense": "Shared Expense",
    "personal": "Personal",
    "transfer": "Transfer (excluded)",
    "excluded": "Excluded",
    "unclassified": "Unclassified",
}


def create_app() -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.secret_key = os.environ.get("FINANCES_SECRET", "dev-secret-change-me")
    app.config["MAX_CONTENT_LENGTH"] = MAX_RECEIPT_BYTES

    @app.before_request
    def _open_db():
        g.db = db_module.connect()
        db_module.init_schema(g.db)

    @app.teardown_request
    def _close_db(_exc):
        db = g.pop("db", None)
        if db is not None:
            db.close()

    app.jinja_env.globals["classification_labels"] = CLASSIFICATION_LABELS

    @app.template_filter("money")
    def money(value):
        if value is None:
            return "—"
        try:
            return f"${float(value):,.2f}"
        except (TypeError, ValueError):
            return str(value)

    @app.template_filter("signed_money")
    def signed_money(value):
        """Format amount using expense convention: positive CSV values are
        outflows so shown as-is; negative CSV values are inflows shown with a
        minus sign."""
        if value is None:
            return "—"
        try:
            v = float(value)
            return f"${v:,.2f}"
        except (TypeError, ValueError):
            return str(value)

    # ---- Summary --------------------------------------------------------

    @app.get("/")
    def index():
        summary = reconcile_module.build_summary(g.db)
        breakdown = reconcile_module.by_account_and_class(g.db)
        tx_count = g.db.execute("SELECT COUNT(*) AS n FROM transactions").fetchone()["n"]
        return render_template(
            "summary.html",
            summary=summary,
            breakdown=breakdown,
            tx_count=tx_count,
        )

    # ---- Transactions list ---------------------------------------------

    @app.get("/transactions")
    def transactions():
        q = request.args
        clauses = []
        params: list = []
        if q.get("account"):
            clauses.append("t.account_number = ?")
            params.append(q["account"])
        if q.get("classification"):
            clauses.append("COALESCE(c.classification, 'unclassified') = ?")
            params.append(q["classification"])
        if q.get("from"):
            clauses.append("t.date >= ?")
            params.append(q["from"])
        if q.get("to"):
            clauses.append("t.date <= ?")
            params.append(q["to"])
        if q.get("search"):
            clauses.append("(t.name LIKE ? OR t.description LIKE ? OR t.category LIKE ?)")
            needle = f"%{q['search']}%"
            params.extend([needle, needle, needle])
        in_window = q.get("in_window") == "1"
        if in_window:
            start = db_module.get_setting(g.db, "partnership_start")
            end = db_module.get_setting(g.db, "partnership_end")
            if start:
                clauses.append("t.date >= ?")
                params.append(start)
            if end:
                clauses.append("t.date <= ?")
                params.append(end)

        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        limit = min(int(q.get("limit", 200)), 2000)

        sort_columns = {
            "date":           "t.date",
            "account":        "t.institution_name, t.account_name",
            "name":           "t.name",
            "category":       "t.category",
            "amount":         "t.amount",
            "classification": "COALESCE(c.classification, 'unclassified')",
        }
        sort_key = q.get("sort") if q.get("sort") in sort_columns else "date"
        direction = "ASC" if q.get("dir") == "asc" else "DESC"
        order_by = f"{sort_columns[sort_key]} {direction}, t.id {direction}"

        rows = g.db.execute(
            f"""
            SELECT t.*,
                   COALESCE(c.classification, 'unclassified') AS classification,
                   c.split_user_pct, c.split_partner_pct, c.source, c.rule_id,
                   (SELECT COUNT(*) FROM receipts r WHERE r.transaction_id = t.id) AS receipt_count
            FROM transactions t
            LEFT JOIN classifications c
              ON c.transaction_id = t.id AND c.superseded_at IS NULL
            {where}
            ORDER BY {order_by}
            LIMIT ?
            """,
            (*params, limit),
        ).fetchall()

        accounts = g.db.execute(
            "SELECT DISTINCT account_number, account_name, institution_name "
            "FROM transactions ORDER BY institution_name, account_name"
        ).fetchall()

        return render_template(
            "transactions.html",
            rows=rows,
            accounts=accounts,
            q=q,
            in_window=in_window,
            classifications=list(CLASSIFICATION_LABELS.keys()),
            sort_key=sort_key,
            sort_dir=direction.lower(),
        )

    # ---- Transaction detail --------------------------------------------

    @app.get("/transactions/<int:tx_id>")
    def transaction_detail(tx_id: int):
        tx = g.db.execute("SELECT * FROM transactions WHERE id = ?", (tx_id,)).fetchone()
        if tx is None:
            abort(404)
        history = g.db.execute(
            "SELECT * FROM classifications WHERE transaction_id = ? ORDER BY created_at DESC",
            (tx_id,),
        ).fetchall()
        receipts = g.db.execute(
            "SELECT * FROM receipts WHERE transaction_id = ? ORDER BY uploaded_at DESC",
            (tx_id,),
        ).fetchall()
        current = rules_module.current_classification(g.db, tx_id)
        return render_template(
            "transaction_detail.html",
            tx=tx,
            history=history,
            receipts=receipts,
            current=current,
        )

    @app.post("/transactions/<int:tx_id>/classify")
    def classify_tx(tx_id: int):
        classification = request.form["classification"]
        su = request.form.get("split_user_pct") or None
        sp = request.form.get("split_partner_pct") or None
        note = request.form.get("note") or None
        rules_module.set_classification(
            g.db,
            tx_id,
            classification,
            source="manual",
            split_user_pct=float(su) if su else None,
            split_partner_pct=float(sp) if sp else None,
            note=note,
        )
        flash("Classification updated.", "success")
        return redirect(url_for("transaction_detail", tx_id=tx_id))

    # ---- Rules ----------------------------------------------------------

    @app.get("/rules")
    def rules_index():
        all_rules = rules_module.list_rules(g.db)
        return render_template("rules.html", rules=all_rules)

    @app.get("/rules/new")
    def rules_new():
        # Optional prefill from query string (used by "Create rule from filter")
        defaults = {
            "name": request.args.get("name", ""),
            "match_account_numbers": request.args.get("match_account_numbers", ""),
            "match_name_regex": request.args.get("match_name_regex", ""),
            "match_category_regex": request.args.get("match_category_regex", ""),
            "match_date_from": request.args.get("match_date_from", ""),
            "match_date_to": request.args.get("match_date_to", ""),
            "match_sign": request.args.get("match_sign", ""),
            "classification": request.args.get("classification", ""),
        }
        return render_template("rule_edit.html", rule=None, defaults=defaults)

    @app.get("/rules/<int:rule_id>")
    def rule_edit(rule_id: int):
        rule = rules_module.get_rule(g.db, rule_id)
        if rule is None:
            abort(404)
        return render_template("rule_edit.html", rule=rule, defaults={})

    def _rule_form_payload(form) -> dict:
        def opt_float(k):
            v = form.get(k)
            return float(v) if v else None
        accounts = [a.strip() for a in (form.get("match_account_numbers") or "").split(",") if a.strip()]
        return {
            "name": form["name"].strip(),
            "priority": int(form.get("priority") or 100),
            "enabled": form.get("enabled") == "on",
            "match_account_numbers": accounts or None,
            "match_name_regex": form.get("match_name_regex") or None,
            "match_category_regex": form.get("match_category_regex") or None,
            "match_amount_min": opt_float("match_amount_min"),
            "match_amount_max": opt_float("match_amount_max"),
            "match_date_from": form.get("match_date_from") or None,
            "match_date_to": form.get("match_date_to") or None,
            "match_sign": form.get("match_sign") or None,
            "classification": form["classification"],
            "split_user_pct": opt_float("split_user_pct"),
            "split_partner_pct": opt_float("split_partner_pct"),
            "note": form.get("note") or None,
        }

    @app.post("/rules")
    def rules_create():
        payload = _rule_form_payload(request.form)
        rule_id = rules_module.create_rule(g.db, payload)
        if request.form.get("apply_now") == "on":
            rules_module.apply_rules(g.db)
        flash(f"Rule '{payload['name']}' created.", "success")
        return redirect(url_for("rule_edit", rule_id=rule_id))

    @app.post("/rules/<int:rule_id>")
    def rules_update(rule_id: int):
        payload = _rule_form_payload(request.form)
        rules_module.update_rule(g.db, rule_id, payload)
        if request.form.get("apply_now") == "on":
            rules_module.apply_rules(g.db)
        flash("Rule updated.", "success")
        return redirect(url_for("rule_edit", rule_id=rule_id))

    @app.post("/rules/<int:rule_id>/disable")
    def rules_disable(rule_id: int):
        rules_module.delete_rule(g.db, rule_id)
        flash("Rule disabled.", "success")
        return redirect(url_for("rules_index"))

    @app.post("/rules/apply")
    def rules_apply():
        result = rules_module.apply_rules(g.db, overwrite_manual=False)
        flash(
            f"Applied rules: {result['changed']} changed, {result['unchanged']} unchanged, "
            f"{result['manual_skipped']} manual (left alone).",
            "success",
        )
        return redirect(request.referrer or url_for("rules_index"))

    # ---- Settings -------------------------------------------------------

    @app.route("/settings", methods=["GET", "POST"])
    def settings():
        if request.method == "POST":
            for key in [
                "partnership_start",
                "partnership_end",
                "partner_user_name",
                "partner_other_name",
                "default_split_user_pct",
                "default_split_partner_pct",
            ]:
                if key in request.form:
                    db_module.set_setting(g.db, key, request.form[key].strip())
            flash("Settings saved.", "success")
            return redirect(url_for("settings"))
        values = {
            k: db_module.get_setting(g.db, k, "")
            for k in [
                "partnership_start",
                "partnership_end",
                "partner_user_name",
                "partner_other_name",
                "default_split_user_pct",
                "default_split_partner_pct",
            ]
        }
        return render_template("settings.html", values=values)

    # ---- Import ---------------------------------------------------------

    @app.get("/import")
    def import_form():
        imports = g.db.execute(
            "SELECT * FROM imports ORDER BY imported_at DESC"
        ).fetchall()
        available = []
        if RAW_DIR.exists():
            for p in sorted(RAW_DIR.glob("*.csv")):
                available.append(p.name)
        return render_template("import.html", imports=imports, available=available)

    @app.post("/import")
    def import_run():
        filename = request.form.get("filename")
        if not filename:
            flash("Select a file from data/raw/.", "error")
            return redirect(url_for("import_form"))
        path = RAW_DIR / filename
        if not path.exists():
            flash(f"File not found: {path}", "error")
            return redirect(url_for("import_form"))
        result = importer_module.import_csv(g.db, path, notes=request.form.get("notes") or None)
        rules_module.apply_rules(g.db)
        flash(
            f"Imported {result.rows_inserted} new row(s); skipped {result.rows_skipped_duplicate} duplicate(s).",
            "success",
        )
        return redirect(url_for("import_form"))

    # ---- Receipts -------------------------------------------------------

    @app.post("/transactions/<int:tx_id>/receipts")
    def upload_receipt(tx_id: int):
        file = request.files.get("receipt")
        if not file or not file.filename:
            flash("Choose a file to upload.", "error")
            return redirect(url_for("transaction_detail", tx_id=tx_id))
        filename = secure_filename(file.filename)
        ext = Path(filename).suffix.lower()
        if ext not in ALLOWED_RECEIPT_EXT:
            flash(f"Unsupported file type: {ext}", "error")
            return redirect(url_for("transaction_detail", tx_id=tx_id))

        data = file.read()
        if not data:
            flash("Empty file.", "error")
            return redirect(url_for("transaction_detail", tx_id=tx_id))
        file_hash = hashlib.sha256(data).hexdigest()

        RECEIPTS_DIR.mkdir(parents=True, exist_ok=True)
        stored_name = f"{file_hash[:16]}_{filename}"
        stored_path = RECEIPTS_DIR / stored_name
        if not stored_path.exists():
            stored_path.write_bytes(data)

        cur = g.db.execute(
            """
            INSERT INTO receipts(transaction_id, filename, stored_path, file_hash, mime_type, size_bytes, note)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tx_id,
                filename,
                str(stored_path.relative_to(ROOT)),
                file_hash,
                file.mimetype or None,
                len(data),
                request.form.get("note") or None,
            ),
        )
        receipt_id = cur.lastrowid
        db_module.log(g.db, "receipt_upload", "receipt", receipt_id, {"transaction_id": tx_id})
        flash("Receipt uploaded.", "success")
        return redirect(url_for("transaction_detail", tx_id=tx_id))

    @app.get("/receipts/<int:receipt_id>")
    def download_receipt(receipt_id: int):
        row = g.db.execute("SELECT * FROM receipts WHERE id = ?", (receipt_id,)).fetchone()
        if row is None:
            abort(404)
        path = ROOT / row["stored_path"]
        if not path.exists():
            abort(404)
        return send_file(path, download_name=row["filename"], as_attachment=False)

    return app


if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=5050, debug=True)
