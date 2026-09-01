"""
validation.py — Deterministic validation + evidence/traceability for the
CMPDI/CIL platform (Step 3).

Two responsibilities, kept in one file because they operate on the same
per-record data and are always run together right after extraction:

1. validate_record() — runs a fixed set of deterministic rules against a
   single normalized mine record and returns a status + a list of issues.
   Never silently changes a questionable source value — every rule only
   *flags*, it never rewrites `mines.*` fields.

2. build_evidence_rows() — for the XLSX pipeline, turns a record's raw
   preserved data (sheet/row/column/raw value, already captured during
   extraction) into one evidence row per field, so every value shown in the
   UI can be traced back to exactly where it came from in the source file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Reference value sets used by the category rules
# ---------------------------------------------------------------------------
VALID_COAL_OR_LIGNITE = {"Coal", "Lignite"}
VALID_OWNERSHIP_CODES = {"G", "P", "SG"}
VALID_MINE_TYPES = {"OC", "UG", "Mixed"}

# Loose bounding box for India, used only for a soft NEEDS_REVIEW nudge —
# never an ERROR, since legitimate coordinates can sit near the edges.
INDIA_LAT_RANGE = (6.0, 38.0)
INDIA_LON_RANGE = (68.0, 98.0)

SEVERITY_WEIGHT = {"ERROR": 0.40, "WARNING": 0.20, "NEEDS_REVIEW": 0.10}

# Maps canonical field name -> the column header used in the original XLSX,
# so evidence rows and validation messages talk about the field the same way
# a person looking at the spreadsheet would.
FIELD_LABELS = {
    "sl_no": "Sl No.",
    "state_ut": "State/UT Name",
    "district": "District Name",
    "mine_name": "Mine Name",
    "production_mt": "Production (MT)",
    "owner_short": "Coal Mine Owner Name",
    "owner_full": "Coal Mine Owner Full Name",
    "coal_or_lignite": "Coal/Lignite",
    "ownership_type": "Govt Owned/Private",
    "mine_type": "Type of Mine",
    "latitude": "Latitude",
    "longitude": "Longitude",
    "source": "Source",
    "accuracy": "Accuracy",
}


@dataclass
class ValidationOutcome:
    status: str  # VERIFIED | NEEDS_REVIEW | WARNING | ERROR
    confidence: float
    issues: list[dict[str, str]] = field(default_factory=list)


def _add(issues: list[dict[str, str]], field_name: str, severity: str, message: str) -> None:
    issues.append({"field": field_name, "severity": severity, "message": message})


def validate_record(record: dict[str, Any], duplicate_keys: set[tuple]) -> ValidationOutcome:
    """
    record: a dict with the normalized mine fields (same shape as what's
        stored in the `mines` table — see app.py's insert for the field list).
    duplicate_keys: set of (mine_name_lower, state_lower, district_lower)
        tuples that occur more than once across the whole `mines` table,
        precomputed once per validation run for efficiency.
    """
    issues: list[dict[str, str]] = []

    # --- missing values -----------------------------------------------
    for fld in ("state_ut", "district", "owner_short", "coal_or_lignite",
                "ownership_type", "mine_type", "latitude", "longitude"):
        if record.get(fld) in (None, ""):
            _add(issues, fld, "NEEDS_REVIEW", f"{FIELD_LABELS.get(fld, fld)} is missing.")

    if record.get("production_mt") is None:
        _add(issues, "production_mt", "NEEDS_REVIEW", "Production value is missing.")

    # --- production >= 0 -------------------------------------------------
    production = record.get("production_mt")
    if production is not None and production < 0:
        _add(issues, "production_mt", "ERROR", f"Production is negative ({production}).")
    elif production == 0:
        _add(issues, "production_mt", "NEEDS_REVIEW", "Production is exactly 0 — confirm this is correct.")

    # --- latitude / longitude range --------------------------------------
    lat, lon = record.get("latitude"), record.get("longitude")
    if lat is not None and not (-90.0 <= lat <= 90.0):
        _add(issues, "latitude", "ERROR", f"Latitude {lat} is outside the valid range [-90, 90].")
    if lon is not None and not (-180.0 <= lon <= 180.0):
        _add(issues, "longitude", "ERROR", f"Longitude {lon} is outside the valid range [-180, 180].")

    if lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180:
        if not (INDIA_LAT_RANGE[0] <= lat <= INDIA_LAT_RANGE[1] and
                 INDIA_LON_RANGE[0] <= lon <= INDIA_LON_RANGE[1]):
            _add(issues, "latitude", "NEEDS_REVIEW",
                 f"Coordinates ({lat}, {lon}) fall outside the expected India bounding box — please confirm.")
        if lat == 0 and lon == 0:
            _add(issues, "latitude", "WARNING", "Coordinates are (0, 0), which usually indicates missing data.")

    # --- coal/lignite category -------------------------------------------
    coal_type = record.get("coal_or_lignite")
    if coal_type and coal_type not in VALID_COAL_OR_LIGNITE:
        _add(issues, "coal_or_lignite", "WARNING", f"Unrecognized Coal/Lignite value '{coal_type}'.")

    # --- ownership category ------------------------------------------------
    ownership_code = record.get("ownership_type")
    if ownership_code and ownership_code.upper() not in VALID_OWNERSHIP_CODES:
        _add(issues, "ownership_type", "WARNING", f"Unrecognized ownership code '{ownership_code}'.")

    # --- mine type -----------------------------------------------------
    mine_type = record.get("mine_type")
    if mine_type and mine_type not in VALID_MINE_TYPES:
        _add(issues, "mine_type", "WARNING", f"Unrecognized mine type '{mine_type}'.")

    # --- owner code -------------------------------------------------------
    if not record.get("owner_short"):
        _add(issues, "owner_short", "ERROR", "Owner code is missing.")
    elif record.get("owner_full_inferred"):
        _add(issues, "owner_full", "NEEDS_REVIEW",
             "Owner full name was not in the source file and was inferred from the owner code.")

    # --- duplicates ---------------------------------------------------
    key = (
        (record.get("mine_name") or "").strip().lower(),
        (record.get("state_ut") or "").strip().lower(),
        (record.get("district") or "").strip().lower(),
    )
    if key in duplicate_keys:
        _add(issues, "mine_name", "WARNING",
             "Possible duplicate: another mine row shares the same name, state, and district.")

    # --- inconsistent / data-quality problems ----------------------------
    mine_name = record.get("mine_name") or ""
    if len(mine_name) < 3:
        _add(issues, "mine_name", "WARNING", "Mine name is suspiciously short.")
    accuracy_type = record.get("accuracy_type")
    if accuracy_type not in ("Exact", "Approximate", None):
        _add(issues, "accuracy", "NEEDS_REVIEW", f"Unrecognized accuracy value '{accuracy_type}'.")

    # --- resolve overall status + confidence -------------------------------
    severities = {i["severity"] for i in issues}
    if "ERROR" in severities:
        status = "ERROR"
    elif "WARNING" in severities:
        status = "WARNING"
    elif "NEEDS_REVIEW" in severities:
        status = "NEEDS_REVIEW"
    else:
        status = "VERIFIED"

    confidence = 1.0
    for issue in issues:
        confidence -= SEVERITY_WEIGHT.get(issue["severity"], 0.0)
    confidence = max(0.0, round(confidence, 2))

    return ValidationOutcome(status=status, confidence=confidence, issues=issues)


# ---------------------------------------------------------------------------
# Evidence generation (XLSX pipeline)
# ---------------------------------------------------------------------------
def build_evidence_rows(
    mine_id: int,
    document_id: int,
    sheet_name: str | None,
    row_number: int | None,
    raw_data: dict[str, Any],
    source_url: str | None,
    extraction_confidence: float,
) -> list[dict[str, Any]]:
    """
    One evidence row per raw column captured for this mine, so every value
    shown anywhere in the UI can be traced to: document -> sheet -> row ->
    column -> raw value, plus the source URL and how it was extracted.
    """
    rows = []
    for column_name, raw_value in raw_data.items():
        rows.append({
            "mine_id": mine_id,
            "document_id": document_id,
            "sheet_name": sheet_name,
            "row_number": row_number,
            "column_name": column_name,
            "raw_value": None if raw_value is None else str(raw_value),
            "source_url": source_url,
            "extraction_method": "xlsx_pandas_openpyxl",
            "confidence": extraction_confidence,
        })
    return rows
