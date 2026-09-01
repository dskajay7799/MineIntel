"""
analytics.py — Descriptive analytics over the `mines` table.

Every number here comes from a PostgreSQL aggregate query (or a small
Python aggregation over the query results) — never from an LLM. This
module is the "source of truth" the report generator and the frontend
charts both read from.

The current dataset represents 2019–2020 only. Nothing here computes or
implies year-over-year trends; there is only one period in the data.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection


def _where(document_id: int | None) -> tuple[str, dict]:
    if document_id is not None:
        return "WHERE document_id = :document_id", {"document_id": document_id}
    return "", {}


def total_production(conn: Connection, document_id: int | None = None) -> dict[str, Any]:
    where_sql, params = _where(document_id)
    row = conn.execute(
        text(f"""
            SELECT
                COUNT(*) AS mine_count,
                COUNT(production_mt) AS mines_with_production,
                COALESCE(SUM(production_mt), 0) AS total_production_mt,
                COALESCE(AVG(production_mt), 0) AS avg_production_mt,
                COALESCE(MAX(production_mt), 0) AS max_production_mt,
                COALESCE(MIN(production_mt) FILTER (WHERE production_mt IS NOT NULL), 0) AS min_production_mt
            FROM mines
            {where_sql}
        """),
        params,
    ).mappings().first()
    return {
        "mine_count": row["mine_count"],
        "mines_with_production": row["mines_with_production"],
        "mines_missing_production": row["mine_count"] - row["mines_with_production"],
        "total_production_mt": round(float(row["total_production_mt"]), 3),
        "avg_production_mt": round(float(row["avg_production_mt"]), 3),
        "max_production_mt": round(float(row["max_production_mt"]), 3),
        "min_production_mt": round(float(row["min_production_mt"]), 3),
    }


def production_by_state(conn: Connection, document_id: int | None = None, limit: int = 100) -> list[dict[str, Any]]:
    where_sql, params = _where(document_id)
    params["limit"] = limit
    rows = conn.execute(
        text(f"""
            SELECT
                COALESCE(state_ut, 'Unknown') AS state_ut,
                COUNT(*) AS mine_count,
                COALESCE(SUM(production_mt), 0) AS total_production_mt
            FROM mines
            {where_sql}
            GROUP BY 1
            ORDER BY total_production_mt DESC
            LIMIT :limit
        """),
        params,
    ).mappings().all()
    return [
        {"state_ut": r["state_ut"], "mine_count": r["mine_count"],
         "total_production_mt": round(float(r["total_production_mt"]), 3)}
        for r in rows
    ]


def production_by_owner(conn: Connection, document_id: int | None = None, limit: int = 20) -> list[dict[str, Any]]:
    where_sql, params = _where(document_id)
    params["limit"] = limit
    rows = conn.execute(
        text(f"""
            SELECT
                COALESCE(owner_full, owner_short, 'Unknown') AS owner,
                COUNT(*) AS mine_count,
                COALESCE(SUM(production_mt), 0) AS total_production_mt
            FROM mines
            {where_sql}
            GROUP BY 1
            ORDER BY total_production_mt DESC
            LIMIT :limit
        """),
        params,
    ).mappings().all()
    return [
        {"owner": r["owner"], "mine_count": r["mine_count"],
         "total_production_mt": round(float(r["total_production_mt"]), 3)}
        for r in rows
    ]


def top_producing_mines(conn: Connection, document_id: int | None = None, limit: int = 15) -> list[dict[str, Any]]:
    where_sql, params = _where(document_id)
    params["limit"] = limit
    extra = "AND production_mt IS NOT NULL" if where_sql else "WHERE production_mt IS NOT NULL"
    rows = conn.execute(
        text(f"""
            SELECT mine_name, state_ut, district, owner_short, production_mt
            FROM mines
            {where_sql} {extra}
            ORDER BY production_mt DESC
            LIMIT :limit
        """),
        params,
    ).mappings().all()
    return [
        {"mine_name": r["mine_name"], "state_ut": r["state_ut"], "district": r["district"],
         "owner_short": r["owner_short"], "production_mt": round(float(r["production_mt"]), 3)}
        for r in rows
    ]


def coal_vs_lignite(conn: Connection, document_id: int | None = None) -> list[dict[str, Any]]:
    where_sql, params = _where(document_id)
    rows = conn.execute(
        text(f"""
            SELECT
                COALESCE(coal_or_lignite, 'Unknown') AS category,
                COUNT(*) AS mine_count,
                COALESCE(SUM(production_mt), 0) AS total_production_mt
            FROM mines
            {where_sql}
            GROUP BY 1
            ORDER BY total_production_mt DESC
        """),
        params,
    ).mappings().all()
    return [
        {"category": r["category"], "mine_count": r["mine_count"],
         "total_production_mt": round(float(r["total_production_mt"]), 3)}
        for r in rows
    ]


def mine_type_distribution(conn: Connection, document_id: int | None = None) -> list[dict[str, Any]]:
    where_sql, params = _where(document_id)
    rows = conn.execute(
        text(f"""
            SELECT COALESCE(mine_type, 'Unknown') AS mine_type, COUNT(*) AS mine_count
            FROM mines
            {where_sql}
            GROUP BY 1
            ORDER BY mine_count DESC
        """),
        params,
    ).mappings().all()
    return [{"mine_type": r["mine_type"], "mine_count": r["mine_count"]} for r in rows]


def ownership_distribution(conn: Connection, document_id: int | None = None) -> list[dict[str, Any]]:
    where_sql, params = _where(document_id)
    rows = conn.execute(
        text(f"""
            SELECT
                COALESCE(ownership_label, ownership_type, 'Unknown') AS ownership_label,
                COUNT(*) AS mine_count,
                COALESCE(SUM(production_mt), 0) AS total_production_mt
            FROM mines
            {where_sql}
            GROUP BY 1
            ORDER BY mine_count DESC
        """),
        params,
    ).mappings().all()
    return [
        {"ownership_label": r["ownership_label"], "mine_count": r["mine_count"],
         "total_production_mt": round(float(r["total_production_mt"]), 3)}
        for r in rows
    ]


def validation_statistics(conn: Connection, document_id: int | None = None) -> dict[str, Any]:
    where_sql, params = _where(document_id)
    rows = conn.execute(
        text(f"""
            SELECT COALESCE(validation_status, 'NEEDS_REVIEW') AS status, COUNT(*) AS count
            FROM mines
            {where_sql}
            GROUP BY 1
        """),
        params,
    ).mappings().all()
    by_status = {r["status"]: r["count"] for r in rows}
    total = sum(by_status.values())

    conf_row = conn.execute(
        text(f"""
            SELECT COALESCE(AVG(confidence), 0) AS avg_confidence,
                   COALESCE(MIN(confidence), 0) AS min_confidence
            FROM mines
            {where_sql}
        """),
        params,
    ).mappings().first()

    # Most common issue types (from the validation_issues JSONB array), for a
    # quick "what's actually wrong" breakdown. Done in Python since JSONB
    # array aggregation in raw SQL is unwieldy for a small dataset like this.
    issue_rows = conn.execute(
        text(f"SELECT validation_issues FROM mines {where_sql} WHERE validation_issues IS NOT NULL"),
        params,
    ).all()
    issue_counts: dict[str, int] = {}
    for (issues,) in issue_rows:
        for issue in (issues or []):
            key = f"{issue.get('severity')}: {issue.get('field')}"
            issue_counts[key] = issue_counts.get(key, 0) + 1
    top_issues = sorted(issue_counts.items(), key=lambda kv: kv[1], reverse=True)[:10]

    return {
        "total_mines": total,
        "by_status": {
            "VERIFIED": by_status.get("VERIFIED", 0),
            "NEEDS_REVIEW": by_status.get("NEEDS_REVIEW", 0),
            "WARNING": by_status.get("WARNING", 0),
            "ERROR": by_status.get("ERROR", 0),
        },
        "avg_confidence": round(float(conf_row["avg_confidence"]), 3),
        "min_confidence": round(float(conf_row["min_confidence"]), 3),
        "top_issue_types": [{"issue": k, "count": v} for k, v in top_issues],
    }


def full_analytics(conn: Connection, document_id: int | None = None) -> dict[str, Any]:
    """
    Single call the report generator and the frontend both use — bundles
    every metric above into one payload so numbers are computed exactly
    once per request and stay consistent across charts/tables/narrative.
    """
    return {
        "document_id": document_id,
        "dataset_period": "2019-2020",
        "totals": total_production(conn, document_id),
        "by_state": production_by_state(conn, document_id),
        "by_owner": production_by_owner(conn, document_id, limit=15),
        "top_mines": top_producing_mines(conn, document_id, limit=15),
        "coal_vs_lignite": coal_vs_lignite(conn, document_id),
        "mine_type_distribution": mine_type_distribution(conn, document_id),
        "ownership_distribution": ownership_distribution(conn, document_id),
        "validation": validation_statistics(conn, document_id),
    }
