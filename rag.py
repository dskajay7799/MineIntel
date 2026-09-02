"""
rag.py — Step 5: RAG + Grok AI Assistant.

Architecture: Browser -> FastAPI -> SQL/RAG retrieval -> evidence/results -> Grok -> answer

This module owns three things:
1. classify_question()      — deterministic keyword-based routing (structured / evidence / both)
2. retrieve_structured()    — reuses analytics.py functions; this is the numerical source of truth
3. retrieve_evidence()      — reuses the Step 3 `evidence`/`mines` tables for source/text retrieval
4. answer_question()        — sends the retrieved (already-correct) data to Grok for phrasing only,
                               with a deterministic template fallback that never fabricates anything

Grok never calculates or invents numbers, mines, states, owners, or sources — it only ever
phrases data this module already retrieved from PostgreSQL.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

import analytics as analytics_module

GROQ_MODEL = "openai/gpt-oss-120b"
DATASET_PERIOD = "2019-2020"

# Bound how much prior conversation is ever sent to Grok — old turns are
# context only and must never be able to override the current retrieval.
MAX_HISTORY_TURNS = 3


# ---------------------------------------------------------------------------
# 1. Question routing (deterministic — Grok never decides this)
# ---------------------------------------------------------------------------
STRUCTURED_PATTERNS = [
    r"\btotal production\b", r"\bhow much production\b", r"\bhow many\b",
    r"\bhighest\b", r"\btop\b", r"\blowest\b", r"\blargest\b", r"\bsmallest\b",
    r"\baverage\b", r"\bmean\b", r"\bcount\b", r"\bnumber of\b",
    r"\bverified\b", r"\bneeds?[_ ]review\b", r"\bwarning\b", r"\berror\b",
    r"\bgovernment[- ]owned\b", r"\bprivate[- ]owned\b", r"\bgovernment vs private\b",
    r"\bcoal vs lignite\b", r"\bcoal or lignite\b",
    r"\bby state\b", r"\bby owner\b", r"\bby mine\b",
    r"\bproduction of\b", r"\bproduction for\b", r"\bproduction in\b",
    r"\bvalidation\b", r"\bconfidence\b", r"\bstatistics?\b",
]

EVIDENCE_PATTERNS = [
    r"\bevidence\b", r"\bsource\b", r"\bdocument\b", r"\braw value\b",
    r"\bcitation\b", r"\bwhere does this come from\b", r"\bsupport this record\b",
    r"\bsheet\b", r"\bcolumn\b", r"\bexplain\b", r"\bwhy\b", r"\bhow was this\b",
    r"\bwhat information\b", r"\btraceab", r"\bprovenance\b",
]


def classify_question(question: str) -> str:
    """Returns 'structured', 'evidence', or 'both'. Purely rule-based — no LLM call."""
    q = question.lower()
    is_structured = any(re.search(p, q) for p in STRUCTURED_PATTERNS)
    is_evidence = any(re.search(p, q) for p in EVIDENCE_PATTERNS)
    if is_structured and is_evidence:
        return "both"
    if is_evidence and not is_structured:
        return "evidence"
    if is_structured and not is_evidence:
        return "structured"
    # Neither matched a strong keyword — default to "both" so a mine-name-only
    # question ("what about GEVRA OC?") still gets whatever facts + evidence exist.
    return "both"


# ---------------------------------------------------------------------------
# Mine-name detection — shared by both retrieval paths
# ---------------------------------------------------------------------------
_STOPWORDS = {
    "what", "is", "the", "of", "for", "in", "at", "on", "how", "much", "many",
    "does", "which", "mine", "has", "state", "have", "a", "an", "and", "to",
    "production", "total", "about", "this", "record", "tell", "me", "show",
}


def _candidate_phrases(question: str) -> list[str]:
    """Extracts plausible mine-name substrings from a question, longest first."""
    caps = re.findall(r"\b([A-Z][A-Za-z0-9&']*(?:\s+[A-Z][A-Za-z0-9&']*){0,4})\b", question)
    words = [w for w in re.findall(r"[A-Za-z0-9&']+", question) if w.lower() not in _STOPWORDS]
    stripped = " ".join(words)
    candidates = sorted(set(caps + ([stripped] if stripped else [])), key=len, reverse=True)
    return [c for c in candidates if len(c) >= 3]


def find_mine(conn: Connection, question: str, document_id: int | None = None) -> dict[str, Any] | None:
    """Best-effort deterministic mine lookup by name — never guesses; returns None if no match."""
    where_doc = " AND document_id = :document_id" if document_id is not None else ""
    for phrase in _candidate_phrases(question):
        params: dict[str, Any] = {"pattern": f"%{phrase}%"}
        if document_id is not None:
            params["document_id"] = document_id
        row = conn.execute(
            text(f"""
                SELECT id, mine_name, state_ut, district, production_mt, owner_short, owner_full,
                       coal_or_lignite, ownership_label, mine_type, validation_status, confidence,
                       source_label, source_url
                FROM mines
                WHERE mine_name ILIKE :pattern {where_doc}
                ORDER BY length(mine_name) ASC
                LIMIT 1
            """),
            params,
        ).mappings().first()
        if row:
            return dict(row)
    return None


# ---------------------------------------------------------------------------
# 2. Structured SQL/Python retrieval — reuses Step 4's analytics.py directly
# ---------------------------------------------------------------------------
def retrieve_structured(conn: Connection, question: str, document_id: int | None = None) -> dict[str, Any]:
    """
    Returns only the specific slice of analytics relevant to the question,
    computed via analytics.py (i.e. real SQL) — never via Grok.
    """
    q = question.lower()
    result: dict[str, Any] = {"dataset_period": DATASET_PERIOD}

    mine = find_mine(conn, question, document_id)
    if mine:
        result["mine"] = mine

    if re.search(r"\btotal production\b|\btotal\b.*\bproduction\b", q):
        result["total_production"] = analytics_module.total_production(conn, document_id)

    if re.search(r"\bhighest\b.*\bstate\b|\bstate\b.*\bhighest\b|\bby state\b|\btop state\b", q):
        result["by_state"] = analytics_module.production_by_state(conn, document_id, limit=10)

    if re.search(r"\bhighest\b.*\bmine\b|\bmine\b.*\bhighest\b|\btop\b.*\bmine|\btop.produc", q):
        result["top_mines"] = analytics_module.top_producing_mines(conn, document_id, limit=10)

    if re.search(r"\bby owner\b|\bowner\b.*\bhighest\b|\btop owner\b", q):
        result["by_owner"] = analytics_module.production_by_owner(conn, document_id, limit=10)

    if re.search(r"\bcoal vs lignite\b|\bcoal or lignite\b", q):
        result["coal_vs_lignite"] = analytics_module.coal_vs_lignite(conn, document_id)

    if re.search(r"\bmine type\b|\bopencast\b|\bunderground\b|\bOC\b|\bUG\b", question):
        result["mine_type_distribution"] = analytics_module.mine_type_distribution(conn, document_id)

    if re.search(r"\bgovernment\b|\bprivate\b|\bownership\b", q):
        result["ownership_distribution"] = analytics_module.ownership_distribution(conn, document_id)

    if re.search(r"\bverified\b|\bneeds?[_ ]review\b|\bwarning\b|\berror\b|\bvalidation\b|\bconfidence\b", q):
        result["validation"] = analytics_module.validation_statistics(conn, document_id)

    # Nothing specific matched but this is a structured-leaning question —
    # fall back to overall totals so there's still something concrete to answer with.
    if len(result) == 1 and not mine:
        result["total_production"] = analytics_module.total_production(conn, document_id)
        result["validation"] = analytics_module.validation_statistics(conn, document_id)
        result["_fallback"] = True

    return result


# ---------------------------------------------------------------------------
# 3. Evidence / text retrieval — reuses Step 3's evidence + mines tables
# ---------------------------------------------------------------------------
def retrieve_evidence(conn: Connection, question: str, document_id: int | None = None,
                       mine: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """
    Returns real evidence rows (document -> sheet -> row -> column -> raw value)
    relevant to the question. Never invents a source.
    """
    if mine is None:
        mine = find_mine(conn, question, document_id)

    if mine:
        rows = conn.execute(
            text("""
                SELECT e.id, e.mine_id, e.document_id, e.sheet_name, e.row_number, e.column_name,
                       e.raw_value, e.source_url, e.extraction_method, e.confidence,
                       m.mine_name
                FROM evidence e
                JOIN mines m ON m.id = e.mine_id
                WHERE e.mine_id = :mine_id
                ORDER BY e.id
                LIMIT 20
            """),
            {"mine_id": mine["id"]},
        ).mappings().all()
        return [dict(r) for r in rows]

    # No specific mine identified — do a bounded keyword search across raw evidence text.
    words = [w for w in re.findall(r"[A-Za-z0-9]+", question) if len(w) >= 4 and w.lower() not in _STOPWORDS]
    if not words:
        return []
    where_doc = " AND e.document_id = :document_id" if document_id is not None else ""
    or_clauses = " OR ".join(f"e.raw_value ILIKE :w{i}" for i in range(len(words[:5])))
    params: dict[str, Any] = {f"w{i}": f"%{w}%" for i, w in enumerate(words[:5])}
    if document_id is not None:
        params["document_id"] = document_id
    rows = conn.execute(
        text(f"""
            SELECT e.id, e.mine_id, e.document_id, e.sheet_name, e.row_number, e.column_name,
                   e.raw_value, e.source_url, e.extraction_method, e.confidence,
                   m.mine_name
            FROM evidence e
            JOIN mines m ON m.id = e.mine_id
            WHERE ({or_clauses}) {where_doc}
            ORDER BY e.id
            LIMIT 15
        """),
        params,
    ).mappings().all()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# 4. Grok answer generation — phrasing only, constrained to retrieved data
# ---------------------------------------------------------------------------
def _template_fallback_answer(question: str, retrieval_type: str,
                               structured: dict[str, Any] | None,
                               evidence: list[dict[str, Any]]) -> str:
    """Deterministic answer built directly from retrieved data — used when Grok is unavailable."""
    parts = []

    if structured:
        mine = structured.get("mine")
        if mine:
            prod = mine.get("production_mt")
            parts.append(
                f"{mine['mine_name']} ({mine.get('state_ut') or 'state not recorded'}, "
                f"{mine.get('district') or 'district not recorded'}) has a reported production of "
                f"{prod if prod is not None else 'no recorded value'} MT. "
                f"Owner: {mine.get('owner_full') or mine.get('owner_short') or 'not recorded'}. "
                f"Validation status: {mine.get('validation_status') or 'unknown'} "
                f"(confidence {mine.get('confidence')})."
            )
        if "total_production" in structured:
            t = structured["total_production"]
            parts.append(
                f"Total recorded production across {t['mine_count']} mines is "
                f"{t['total_production_mt']:,} MT ({t['mines_with_production']} mines have a reported value)."
            )
        if "by_state" in structured and structured["by_state"]:
            top = structured["by_state"][0]
            parts.append(
                f"The highest-producing state is {top['state_ut']} with "
                f"{top['total_production_mt']:,} MT across {top['mine_count']} mines."
            )
        if "top_mines" in structured and structured["top_mines"]:
            top = structured["top_mines"][0]
            parts.append(
                f"The highest-producing mine is {top['mine_name']} ({top.get('state_ut') or 'state not recorded'}) "
                f"with {top['production_mt']:,} MT."
            )
        if "by_owner" in structured and structured["by_owner"]:
            top = structured["by_owner"][0]
            parts.append(
                f"The owner with the highest total production is {top['owner']} "
                f"with {top['total_production_mt']:,} MT across {top['mine_count']} mines."
            )
        if "validation" in structured:
            v = structured["validation"]
            parts.append(
                f"Validation status across {v['total_mines']} records: "
                f"{v['by_status']['VERIFIED']} VERIFIED, {v['by_status']['NEEDS_REVIEW']} NEEDS_REVIEW, "
                f"{v['by_status']['WARNING']} WARNING, {v['by_status']['ERROR']} ERROR "
                f"(average confidence {v['avg_confidence']})."
            )
        if "coal_vs_lignite" in structured:
            parts.append("Coal vs lignite mine counts: " +
                          ", ".join(f"{r['category']}: {r['mine_count']}" for r in structured["coal_vs_lignite"]) + ".")
        if "ownership_distribution" in structured:
            parts.append("Ownership distribution: " +
                          ", ".join(f"{r['ownership_label']}: {r['mine_count']}" for r in structured["ownership_distribution"]) + ".")
        if "mine_type_distribution" in structured:
            parts.append("Mine type distribution: " +
                          ", ".join(f"{r['mine_type']}: {r['mine_count']}" for r in structured["mine_type_distribution"]) + ".")

    if evidence:
        parts.append(
            f"Found {len(evidence)} evidence record(s) tracing this back to the source spreadsheet "
            f"(sheet/row/column), listed in the sources below."
        )
    elif retrieval_type in ("evidence", "both"):
        parts.append("No matching evidence records were found in the source document for this question.")

    if not parts:
        parts.append(
            "This could not be determined from the available 2019-2020 dataset. "
            "Try asking about total production, a specific state, a specific mine, or validation statistics."
        )

    parts.append(f"(Dataset period: {DATASET_PERIOD}; no year-over-year comparison is available.)")
    return " ".join(parts)


def _build_grok_prompt(question: str, retrieval_type: str,
                        structured: dict[str, Any] | None,
                        evidence: list[dict[str, Any]],
                        history: list[dict[str, str]]) -> str:
    context = {
        "dataset_period": DATASET_PERIOD,
        "retrieval_type": retrieval_type,
        "structured_results": structured,
        "evidence_records": evidence,
    }
    history_block = ""
    if history:
        bounded = history[-MAX_HISTORY_TURNS:]
        history_block = (
            "\n\nPRIOR CONVERSATION (context only — informational, NOT authoritative; "
            "the CURRENT retrieved data above overrides anything here if they conflict):\n" +
            json.dumps(bounded, indent=2, default=str)
        )
    return (
        "You are a data assistant answering questions about a 2019-2020 Indian coal mines dataset. "
        "Below is the ONLY data you are allowed to use. Do not calculate, estimate, modify, or invent "
        "any number. Do not invent mines, states, owners, dates, sources, or evidence not present here. "
        "Do not create any year-over-year trend or comparison — this dataset covers a single period. "
        "If the retrieved data is insufficient to answer the question, say so explicitly instead of "
        "guessing. Clearly distinguish database/structured results from source/evidence information "
        "when both are present.\n\n"
        f"QUESTION: {question}\n\n"
        f"RETRIEVED DATA:\n{json.dumps(context, indent=2, default=str)}"
        f"{history_block}\n\n"
        "Reply with a concise, plain-language answer (2-5 sentences). Do not repeat this instruction "
        "text and do not add a JSON wrapper — just the answer."
    )


def answer_question(
    question: str,
    retrieval_type: str,
    structured: dict[str, Any] | None,
    evidence: list[dict[str, Any]],
    history: list[dict[str, str]] | None = None,
) -> tuple[str, str]:
    """Returns (answer_text, narrative_source)."""
    history = history or []
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        return (
            _template_fallback_answer(question, retrieval_type, structured, evidence),
            "template_fallback (GROK_API_KEY not set)",
        )

    try:
        import requests

        prompt = _build_grok_prompt(question, retrieval_type, structured, evidence, history)
        resp = requests.post(
            GROK_API_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": GROK_MODEL,
                "messages": [
                    {"role": "system", "content": "You answer factual questions using only the supplied retrieved data."},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.1,
            },
            timeout=20,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"].strip()
        if not content:
            raise ValueError("Empty response from Grok")
        return content, "grok"
    except Exception as exc:  # noqa: BLE001 — any failure falls back safely, never fabricates
        fallback = _template_fallback_answer(question, retrieval_type, structured, evidence)
        return fallback, f"template_fallback (Grok error: {exc})"


def sources_from_evidence(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Formats evidence rows into the response's `sources` list — real data only."""
    return [
        {
            "mine_id": e["mine_id"],
            "mine_name": e.get("mine_name"),
            "document_id": e["document_id"],
            "sheet_name": e.get("sheet_name"),
            "row_number": e.get("row_number"),
            "column_name": e.get("column_name"),
            "raw_value": e.get("raw_value"),
            "source_url": e.get("source_url"),
            "extraction_method": e.get("extraction_method"),
            "confidence": float(e["confidence"]) if e.get("confidence") is not None else None,
        }
        for e in evidence
    ]


# ---------------------------------------------------------------------------
# Step 6 — Image-grounded questions
#
# Image -> OCR (image_processing.py) -> extracted text -> this function
# tries to match that text to a REAL mine record. If it matches, the answer
# is fully database-verified (same structured/evidence pipeline as text
# questions). If it doesn't match anything real, the answer is clearly
# labeled as coming from the image only, unverified — never presented as
# database-confirmed.
# ---------------------------------------------------------------------------
def answer_image_question(
    conn: Connection,
    question: str,
    ocr_text: str,
    document_id: int | None = None,
    history: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """
    Returns a dict with: answer, retrieval_type, structured_results, sources,
    narrative_source, warnings, image_extracted_text, image_verified.
    """
    warnings: list[str] = []
    question = question.strip()

    # Try to match the image's OCR text (and any accompanying question) to a
    # real mine record — this is the only thing that can turn "image says X"
    # into "database confirms X".
    combined_for_matching = f"{question}\n{ocr_text}".strip()
    mine = find_mine(conn, combined_for_matching, document_id) if combined_for_matching else None

    if mine:
        # Verified path — behaves exactly like a normal text question from here,
        # so it gets the same structured + evidence backing as any other answer.
        structured = retrieve_structured(conn, question or mine["mine_name"], document_id)
        structured.setdefault("mine", mine)
        structured["image_extracted_text"] = ocr_text or None
        structured["image_verified_against_database"] = True

        evidence = retrieve_evidence(conn, question or mine["mine_name"], document_id, mine=mine)
        effective_question = question or f"What does the database say about {mine['mine_name']}?"
        answer, narrative_source = answer_question(effective_question, "both", structured, evidence, history)
        answer = (
            f"[Matched to a verified database record: {mine['mine_name']}] " + answer
        )
        return {
            "answer": answer,
            "retrieval_type": "both",
            "structured_results": structured,
            "sources": sources_from_evidence(evidence),
            "narrative_source": narrative_source,
            "warnings": warnings,
            "image_extracted_text": ocr_text or None,
            "image_verified": True,
        }

    # Unverified path — nothing in the image text matched a real record.
    # Never call Grok to "interpret" this as fact; state plainly what was
    # read from the image and that it is unverified.
    if not ocr_text:
        warnings.append("No text could be read from the image.")
        answer = (
            "I couldn't read any text from the uploaded image, and no question text was "
            "matched to a database record either." if not question else
            "I couldn't read any text from the uploaded image. "
            f"Regarding your question: this could not be matched to a specific record in the "
            f"{DATASET_PERIOD} database."
        )
    else:
        warnings.append("Image text did not match any record in the database — shown as unverified.")
        snippet = ocr_text[:400]
        answer = (
            f'Text extracted from the uploaded image (NOT verified against the database): "{snippet}". '
            "This did not match any mine record in the project's database, so it is shown here only "
            "as what the image appears to contain, not as a confirmed data point."
        )

    return {
        "answer": answer,
        "retrieval_type": "evidence",
        "structured_results": {
            "dataset_period": DATASET_PERIOD,
            "image_extracted_text": ocr_text or None,
            "image_verified_against_database": False,
        },
        "sources": [],
        "narrative_source": "template_fallback (image not matched to database)",
        "warnings": warnings,
        "image_extracted_text": ocr_text or None,
        "image_verified": False,
    }
