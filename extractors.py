"""
extractors.py — Document extraction pipeline for the CMPDI/CIL platform.

Architecture:
    extract_document(file_path, file_type) -> ExtractionResult
        dispatches to a format-specific extractor. Only XLSX is fully
        implemented in Step 2. PDF / scanned PDF / DOCX / image extractors
        are stubbed with a clear NotImplemented-style result so the
        pipeline (upload -> extract -> store) already works end-to-end for
        every format; only the parsing logic inside each stub needs to be
        filled in later (OCR, PDF table extraction, etc.).

Every extractor returns a list of MineRecord-shaped dicts plus the raw
original row (for traceability) and any warnings raised during
normalization (missing values, inferred data, inconsistent labels, etc).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import pandas as pd


# ---------------------------------------------------------------------------
# Canonical field names (Step 0 approved fields) and known header variants
# ---------------------------------------------------------------------------
CANONICAL_FIELDS = [
    "sl_no",
    "state_ut",
    "district",
    "mine_name",
    "production_mt",
    "owner_short",
    "owner_full",
    "coal_or_lignite",
    "ownership_type",
    "mine_type",
    "latitude",
    "longitude",
    "source",
    "accuracy",
]

# Maps normalized header text -> canonical field name.
# Normalization = lowercase, strip, collapse whitespace, drop content in parens.
HEADER_ALIASES = {
    "sl no.": "sl_no",
    "sl no": "sl_no",
    "s.no": "sl_no",
    "state/ut name": "state_ut",
    "state / ut name": "state_ut",
    "state": "state_ut",
    "district name": "district",
    "district": "district",
    "mine name": "mine_name",
    "coal/ lignite production (mt) (2019-2020)": "production_mt",
    "coal/lignite production (mt) (2019-2020)": "production_mt",
    "production": "production_mt",
    "coal mine owner name": "owner_short",
    "owner name": "owner_short",
    "coal mine owner full name": "owner_full",
    "owner full name": "owner_full",
    "coal/lignite": "coal_or_lignite",
    "coal / lignite": "coal_or_lignite",
    "govt owned/private": "ownership_type",
    "govt owned / private": "ownership_type",
    "type of mine (oc/ug/mixed)": "mine_type",
    "type of mine": "mine_type",
    "latitude": "latitude",
    "longitude": "longitude",
    "source": "source",
    "accuracy (exact vs approximate)": "accuracy",
    "accuracy": "accuracy",
}

# Known expansions for owner short codes whose full name is missing in the
# source file. Anything not in this dict falls back to using the short
# name as the full name (flagged with a warning as inferred).
OWNER_LEGEND = {
    "NTPC": "National Thermal Power Corporation Limited",
    "NLC LTD": "NLC India Limited (formerly Neyveli Lignite Corporation)",
    "BALCO (VEDANTA GROUP)": "Bharat Aluminium Company Limited (Vedanta Group)",
}

OWNERSHIP_LABELS = {
    "G": "Government",
    "P": "Private",
    "SG": "State Government",
}


@dataclass
class ExtractionResult:
    status: str  # "success" | "unsupported" | "error"
    sheet_name: str | None = None
    total_rows: int = 0
    extracted_rows: int = 0
    records: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error_message: str | None = None


# ---------------------------------------------------------------------------
# Text normalization helpers
# ---------------------------------------------------------------------------
def clean_text(value: Any) -> str | None:
    """Normalize whitespace/newlines/non-breaking-space artifacts in a cell."""
    if value is None:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    text = str(value)
    text = text.replace("\xa0", " ")   # non-breaking space
    text = text.replace("\n", " ").replace("\r", " ")
    text = re.sub(r"\s+", " ", text)   # collapse whitespace
    text = text.strip()
    return text or None


def normalize_header(header: Any) -> str:
    if header is None:
        return ""
    text = clean_text(header) or ""
    return text.lower()


def to_number(value: Any) -> float | None:
    cleaned = clean_text(value)
    if cleaned is None:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def split_source(raw_source: str | None) -> tuple[str | None, str | None]:
    """Splits 'Google Maps: <url>' into (label, url). Falls back gracefully."""
    if not raw_source:
        return None, None
    match = re.match(r"^\s*([^:]+):\s*(https?://\S+)", raw_source)
    if match:
        return match.group(1).strip(), match.group(2).strip()
    url_match = re.search(r"https?://\S+", raw_source)
    if url_match:
        return None, url_match.group(0)
    return raw_source, None


ACCURACY_TYPE_ALIASES = {
    "exact": "Exact",
    "approximate": "Approximate",
    "approx": "Approximate",
}

_ACCURACY_PREFIX_RE = re.compile(r"^(exact|approximate|approx)\b\.?\s*:?\s*(.*)$", re.IGNORECASE)


def split_accuracy(raw_accuracy: str | None) -> tuple[str | None, str | None]:
    """
    Splits inconsistent accuracy values into (normalized_type, note).
    Handles all observed variants in the source data:
      'Exact'                                  -> ('Exact', None)
      'Approximate: PIN 713321'                -> ('Approximate', 'PIN 713321')
      'Approximate coordinates of Itapara area' -> ('Approximate', 'coordinates of Itapara area')
      'Approx'                                 -> ('Approximate', None)
      'Approx (Colliery School)'               -> ('Approximate', '(Colliery School)')
    """
    if not raw_accuracy:
        return None, None
    match = _ACCURACY_PREFIX_RE.match(raw_accuracy.strip())
    if match:
        prefix, rest = match.groups()
        acc_type = ACCURACY_TYPE_ALIASES.get(prefix.lower(), prefix.strip())
        note = rest.strip(" :") or None
        return acc_type, note
    # Unrecognized format entirely — keep raw text as the type, flagged upstream
    return raw_accuracy.strip(), None


def normalize_ownership(raw_code: str | None) -> tuple[str | None, str | None]:
    """Returns (raw_code_cleaned, human_readable_label)."""
    code = clean_text(raw_code)
    if code is None:
        return None, None
    label = OWNERSHIP_LABELS.get(code.upper())
    if label is None:
        label = code  # unknown code — keep as-is, flagged separately by caller
    return code, label


def resolve_owner_full_name(owner_short: str | None, owner_full: str | None) -> tuple[str | None, bool]:
    """
    Returns (resolved_full_name, was_inferred).
    Falls back to a known legend, then to the short name itself.
    """
    if owner_full:
        return owner_full, False
    if not owner_short:
        return None, False
    legend_hit = OWNER_LEGEND.get(owner_short.upper())
    if legend_hit:
        return legend_hit, True
    return owner_short, True


# ---------------------------------------------------------------------------
# XLSX extractor (fully implemented — priority format per Step 2 scope)
# ---------------------------------------------------------------------------
def extract_xlsx(file_path: str) -> ExtractionResult:
    try:
        all_sheets = pd.read_excel(file_path, sheet_name=None, header=None)
    except Exception as exc:  # noqa: BLE001
        return ExtractionResult(status="error", error_message=f"Could not open workbook: {exc}")

    target_sheet_name = None
    target_df = None
    header_row_idx = None
    column_map: dict[int, str] = {}

    # --- Sheet detection: find the sheet whose header row matches known
    # canonical fields (e.g. "Mine Name"). Skips sheets like a citation /
    # copyright tab that hold no tabular mine data. ---
    for sheet_name, raw_df in all_sheets.items():
        for row_idx in range(min(5, len(raw_df))):  # header is usually in the first few rows
            row_values = raw_df.iloc[row_idx].tolist()
            normalized = [normalize_header(v) for v in row_values]
            hits = {i: HEADER_ALIASES[h] for i, h in enumerate(normalized) if h in HEADER_ALIASES}
            # Require mine_name to be present to consider this a valid header row
            if "mine_name" in hits.values():
                target_sheet_name = sheet_name
                target_df = raw_df
                header_row_idx = row_idx
                column_map = hits
                break
        if target_df is not None:
            break

    if target_df is None:
        return ExtractionResult(
            status="error",
            error_message=(
                "No sheet with a recognizable header (expected a 'Mine Name' column) "
                f"was found among sheets: {list(all_sheets.keys())}"
            ),
        )

    data_rows = target_df.iloc[header_row_idx + 1:].reset_index(drop=True)
    warnings: list[str] = []
    records: list[dict[str, Any]] = []

    # Grab original (pre-normalization) header labels for raw traceability
    original_headers = {
        col_idx: clean_text(target_df.iloc[header_row_idx, col_idx]) for col_idx in column_map
    }

    for i in range(len(data_rows)):
        row = data_rows.iloc[i]

        # Skip fully blank rows
        if row.isna().all():
            continue

        raw_row: dict[str, Any] = {}
        parsed: dict[str, Any] = {}

        for col_idx, canonical in column_map.items():
            raw_value = row.iloc[col_idx]
            raw_row[original_headers.get(col_idx, canonical)] = (
                None if (isinstance(raw_value, float) and pd.isna(raw_value)) else raw_value
                if not isinstance(raw_value, (int, float)) else raw_value
            )
            parsed[canonical] = raw_value

        mine_name = clean_text(parsed.get("mine_name"))
        if not mine_name:
            # A row with no mine name is not usable — skip but note it
            warnings.append(f"Row {i + header_row_idx + 2}: skipped, no mine name.")
            continue

        row_warnings: list[str] = []
        row_number = i + header_row_idx + 2  # 1-indexed spreadsheet row, accounting for header

        sl_no_raw = to_number(parsed.get("sl_no"))
        sl_no = int(sl_no_raw) if sl_no_raw is not None else None

        production = to_number(parsed.get("production_mt"))
        if production is None and parsed.get("production_mt") is not None:
            row_warnings.append("production value could not be parsed as a number")

        owner_short = clean_text(parsed.get("owner_short"))
        owner_full_raw = clean_text(parsed.get("owner_full"))
        owner_full, owner_full_inferred = resolve_owner_full_name(owner_short, owner_full_raw)
        if owner_full_inferred:
            row_warnings.append("owner full name was missing and inferred")

        ownership_code, ownership_label = normalize_ownership(parsed.get("ownership_type"))
        if ownership_code and ownership_code.upper() not in OWNERSHIP_LABELS:
            row_warnings.append(f"unrecognized ownership code '{ownership_code}'")

        accuracy_raw = clean_text(parsed.get("accuracy"))
        accuracy_type, accuracy_note = split_accuracy(accuracy_raw)
        if accuracy_type not in ("Exact", "Approximate", None):
            row_warnings.append(f"unrecognized accuracy value '{accuracy_type}'")

        source_raw = clean_text(parsed.get("source"))
        source_label, source_url = split_source(source_raw)

        lat = to_number(parsed.get("latitude"))
        lon = to_number(parsed.get("longitude"))

        record = {
            "row_number": row_number,
            "sl_no": sl_no,
            "state_ut": clean_text(parsed.get("state_ut")),
            "district": clean_text(parsed.get("district")),
            "mine_name": mine_name,
            "production_mt": production,
            "owner_short": owner_short,
            "owner_full": owner_full,
            "owner_full_inferred": owner_full_inferred,
            "coal_or_lignite": clean_text(parsed.get("coal_or_lignite")),
            "ownership_type": ownership_code,
            "ownership_label": ownership_label,
            "mine_type": clean_text(parsed.get("mine_type")),
            "latitude": lat,
            "longitude": lon,
            "source": source_raw,
            "source_label": source_label,
            "source_url": source_url,
            "accuracy": accuracy_raw,
            "accuracy_type": accuracy_type,
            "accuracy_note": accuracy_note,
            "raw_data": {k: (None if pd.isna(v) else (v if not hasattr(v, "item") else v.item()))
                         for k, v in raw_row.items()},
            "extraction_warnings": row_warnings,
        }
        records.append(record)

    return ExtractionResult(
        status="success",
        sheet_name=target_sheet_name,
        total_rows=len(data_rows),
        extracted_rows=len(records),
        records=records,
        warnings=warnings,
    )

# ---------------------------------------------------------------------------
# Stub extractors for other formats — architecture ready, logic not yet built
# ---------------------------------------------------------------------------
def extract_pdf(file_path: str) -> ExtractionResult:
    return ExtractionResult(
        status="unsupported",
        error_message="PDF text/table extraction is not implemented yet (planned for a later step).",
    )


def extract_scanned_pdf(file_path: str) -> ExtractionResult:
    return ExtractionResult(
        status="unsupported",
        error_message="Scanned PDF (OCR) extraction is not implemented yet (planned for a later step).",
    )


def extract_docx(file_path: str) -> ExtractionResult:
    return ExtractionResult(
        status="unsupported",
        error_message="DOCX extraction is not implemented yet (planned for a later step).",
    )


def extract_image(file_path: str) -> ExtractionResult:
    return ExtractionResult(
        status="unsupported",
        error_message="Image OCR extraction is not implemented yet (planned for a later step).",
    )


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------
EXTRACTOR_DISPATCH = {
    "xlsx": extract_xlsx,
    "pdf": extract_pdf,
    "scanned_pdf": extract_scanned_pdf,
    "docx": extract_docx,
    "png": extract_image,
    "jpg": extract_image,
    "jpeg": extract_image,
}


def detect_file_type(filename: str) -> str | None:
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    if ext in ("xlsx", "xlsm"):
        return "xlsx"
    if ext == "pdf":
        return "pdf"  # scanned-vs-text PDF distinction happens inside extract_pdf later
    if ext == "docx":
        return "docx"
    if ext in ("png",):
        return "png"
    if ext in ("jpg", "jpeg"):
        return "jpg"
    return None


def extract_document(file_path: str, file_type: str) -> ExtractionResult:
    extractor = EXTRACTOR_DISPATCH.get(file_type)
    if extractor is None:
        return ExtractionResult(status="error", error_message=f"Unknown file type '{file_type}'.")
    return extractor(file_path)
