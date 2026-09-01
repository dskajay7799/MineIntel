"""
CMPDI/CIL AI Document Intelligence Platform — Step 1 Foundation
FastAPI backend + Neon PostgreSQL.

Run locally:
    pip install -r requirements.txt
    cp .env.example .env   # then fill in DATABASE_URL
    uvicorn app:app --reload
"""

import json
import os
import uuid
from datetime import datetime
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Depends, Request, Response, Cookie
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

from extractors import extract_document, detect_file_type
from validation import validate_record, build_evidence_rows
import analytics as analytics_module
import report_generator
import rag
import image_processing
import auth

load_dotenv()

UPLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

REPORTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")
os.makedirs(REPORTS_DIR, exist_ok=True)

DATABASE_URL = os.getenv("DATABASE_URL")

engine = None
db_error_message = None

if DATABASE_URL:
    try:
        # Neon requires sslmode=require; pool_pre_ping keeps connections healthy
        engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    except Exception as exc:  # noqa: BLE001
        db_error_message = str(exc)
else:
    db_error_message = "DATABASE_URL environment variable is not set."


# ---------------------------------------------------------------------------
# Minimal schema based on the approved Step 0 dataset analysis
# (Indian Coal Mines Dataset — Mines Datasheet)
# ---------------------------------------------------------------------------
CREATE_MINES_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS mines (
    id SERIAL PRIMARY KEY,
    sl_no INTEGER,
    state_ut TEXT,
    district TEXT,
    mine_name TEXT NOT NULL,
    production_mt NUMERIC,
    owner_short TEXT,
    owner_full TEXT,
    coal_or_lignite TEXT,
    ownership_type TEXT,       -- Govt (G) / Private (P)
    mine_type TEXT,            -- OC / UG / Mixed
    latitude DOUBLE PRECISION,
    longitude DOUBLE PRECISION,
    source TEXT,
    accuracy TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);
"""

# Table to track uploaded documents (used by later steps: extract/validate/report)
CREATE_DOCUMENTS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS documents (
    id SERIAL PRIMARY KEY,
    filename TEXT NOT NULL,
    file_type TEXT,
    status TEXT DEFAULT 'uploaded',
    uploaded_at TIMESTAMP DEFAULT NOW()
);
"""

# Additive columns for Step 2 (document ingestion pipeline). Kept as
# separate ALTER statements (idempotent via IF NOT EXISTS) so Step 1's
# tables/data are never dropped or rewritten.
ALTER_DOCUMENTS_SQL = [
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS file_path TEXT;",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS sheet_name TEXT;",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS total_rows INTEGER;",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS extracted_rows INTEGER;",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS error_message TEXT;",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS processed_at TIMESTAMP;",
]

ALTER_MINES_SQL = [
    "ALTER TABLE mines ADD COLUMN IF NOT EXISTS document_id INTEGER REFERENCES documents(id);",
    "ALTER TABLE mines ADD COLUMN IF NOT EXISTS raw_data JSONB;",
    "ALTER TABLE mines ADD COLUMN IF NOT EXISTS owner_full_inferred BOOLEAN DEFAULT FALSE;",
    "ALTER TABLE mines ADD COLUMN IF NOT EXISTS ownership_label TEXT;",
    "ALTER TABLE mines ADD COLUMN IF NOT EXISTS source_label TEXT;",
    "ALTER TABLE mines ADD COLUMN IF NOT EXISTS source_url TEXT;",
    "ALTER TABLE mines ADD COLUMN IF NOT EXISTS accuracy_type TEXT;",
    "ALTER TABLE mines ADD COLUMN IF NOT EXISTS accuracy_note TEXT;",
    "ALTER TABLE mines ADD COLUMN IF NOT EXISTS extraction_warnings JSONB;",
    # Step 3 — validation
    "ALTER TABLE mines ADD COLUMN IF NOT EXISTS row_number INTEGER;",
    "ALTER TABLE mines ADD COLUMN IF NOT EXISTS validation_status TEXT DEFAULT 'NEEDS_REVIEW';",
    "ALTER TABLE mines ADD COLUMN IF NOT EXISTS validation_issues JSONB;",
    "ALTER TABLE mines ADD COLUMN IF NOT EXISTS confidence NUMERIC(4,2);",
]

# Step 3 — one evidence row per source column per mine, for full
# document -> sheet -> row -> column -> raw value traceability.
CREATE_EVIDENCE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS evidence (
    id SERIAL PRIMARY KEY,
    mine_id INTEGER REFERENCES mines(id) ON DELETE CASCADE,
    document_id INTEGER REFERENCES documents(id) ON DELETE CASCADE,
    sheet_name TEXT,
    row_number INTEGER,
    column_name TEXT,
    raw_value TEXT,
    source_url TEXT,
    extraction_method TEXT,
    confidence NUMERIC(4,2),
    created_at TIMESTAMP DEFAULT NOW()
);
"""

# Step 4 — generated analytics reports (PDF/DOCX), keyed to a source document
CREATE_REPORTS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS reports (
    id SERIAL PRIMARY KEY,
    document_id INTEGER REFERENCES documents(id) ON DELETE CASCADE,
    title TEXT,
    dataset_period TEXT,
    status TEXT DEFAULT 'generated',
    analytics_snapshot JSONB,
    narrative_snapshot JSONB,
    narrative_source TEXT,
    pdf_path TEXT,
    docx_path TEXT,
    error_message TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);
"""

# Step 8 — authentication. Passwords are never stored as plaintext — only
# a bcrypt hash (see auth.py). Sessions are opaque server-side tokens with
# a real expiry, deleted on logout, not stateless/self-trusting JWTs.
CREATE_USERS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT NOW()
);
"""

CREATE_SESSIONS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    token TEXT UNIQUE NOT NULL,
    created_at TIMESTAMP DEFAULT NOW(),
    expires_at TIMESTAMP NOT NULL
);
"""


def init_db():
    """Create minimum tables if they do not exist yet, then apply additive migrations."""
    global db_error_message
    if engine is None:
        return
    try:
        with engine.begin() as conn:
            conn.execute(text(CREATE_MINES_TABLE_SQL))
            conn.execute(text(CREATE_DOCUMENTS_TABLE_SQL))
            for stmt in ALTER_DOCUMENTS_SQL:
                conn.execute(text(stmt))
            for stmt in ALTER_MINES_SQL:
                conn.execute(text(stmt))
            conn.execute(text(CREATE_EVIDENCE_TABLE_SQL))
            conn.execute(text(CREATE_REPORTS_TABLE_SQL))
            conn.execute(text(CREATE_USERS_TABLE_SQL))
            conn.execute(text(CREATE_SESSIONS_TABLE_SQL))
        db_error_message = None
    except OperationalError as exc:
        db_error_message = str(exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="CMPDI/CIL AI Document Intelligence Platform", lifespan=lifespan)

# Security / deployment: this app serves its own frontend from the same
# origin by default (see serve_index() below), which is how every prior
# step's testing was actually done — same-origin requests don't need CORS
# at all, and a wildcard "*" policy would needlessly expose public
# endpoints like /api/auth/login to credential-stuffing from any page.
#
# For an optional split deployment (e.g. frontend on Netlify, backend on
# Render), set CORS_ALLOWED_ORIGINS to a comma-separated list of exact
# trusted origins (e.g. "https://myapp.netlify.app") and CORS will be
# enabled for exactly those origins, with credentials allowed so the
# session cookie can be sent. IMPORTANT — this is a partial mitigation
# only, not a full cross-origin auth redesign: the session cookie is
# still set with SameSite=Lax (see auth.py / _set_session_cookie), and
# modern browsers will not attach a Lax cookie to a genuinely cross-site
# fetch. Cross-origin login has not been implemented or tested; the
# supported, tested path is same-origin (Render serving this app's own
# index.html), documented in README's deployment section.
_cors_origins = [o.strip() for o in os.getenv("CORS_ALLOWED_ORIGINS", "").split(",") if o.strip()]
if _cors_origins:
    from fastapi.middleware.cors import CORSMiddleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


# ---------------------------------------------------------------------------
# Step 8 — Authentication schemas + session dependency
# ---------------------------------------------------------------------------
class SignupRequest(BaseModel):
    email: str
    password: str


class LoginRequest(BaseModel):
    email: str
    password: str


class UserOut(BaseModel):
    id: int
    email: str
    created_at: datetime | None = None


def _get_session_user(conn, token: str | None) -> dict | None:
    """Looks up the user for a session token, deleting it first if expired."""
    if not token:
        return None
    row = conn.execute(
        text(
            """
            SELECT s.id AS session_id, s.expires_at, u.id AS user_id, u.email, u.created_at
            FROM sessions s
            JOIN users u ON u.id = s.user_id
            WHERE s.token = :token
            """
        ),
        {"token": token},
    ).mappings().first()
    if row is None:
        return None
    if auth.is_expired(row["expires_at"]):
        conn.execute(text("DELETE FROM sessions WHERE id = :id"), {"id": row["session_id"]})
        return None
    return {"id": row["user_id"], "email": row["email"], "created_at": row["created_at"]}


def get_current_user(session_token: str | None = Cookie(default=None)) -> dict | None:
    """Optional-auth dependency — returns the user dict or None. Never raises."""
    if engine is None or not session_token:
        return None
    try:
        with engine.connect() as conn:
            return _get_session_user(conn, session_token)
    except OperationalError:
        return None


def require_auth(session_token: str | None = Cookie(default=None)) -> dict:
    """
    Required-auth dependency for protected endpoints. Raises 401 for any
    missing/invalid/expired session — callers get a uniform, generic error
    with no hint about which part of the credential was wrong.
    """
    if engine is None:
        raise HTTPException(status_code=503, detail="Database not configured.")
    try:
        with engine.connect() as conn:
            user = _get_session_user(conn, session_token)
    except OperationalError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated. Please log in.")
    return user


def _set_session_cookie(response: Response, request: Request, token: str) -> None:
    response.set_cookie(
        key=auth.SESSION_COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        secure=(request.url.scheme == "https"),
        max_age=auth.SESSION_DURATION_HOURS * 3600,
        path="/",
    )


@app.post("/api/auth/signup", response_model=UserOut)
def signup(payload: SignupRequest, request: Request, response: Response):
    if engine is None:
        raise HTTPException(status_code=503, detail="Database not configured.")
    try:
        email = auth.validate_email(payload.email)
        password = auth.validate_password(payload.password)
    except auth.AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    password_hash = auth.hash_password(password)
    try:
        with engine.begin() as conn:
            existing = conn.execute(
                text("SELECT id FROM users WHERE email = :email"), {"email": email}
            ).first()
            if existing:
                raise HTTPException(status_code=409, detail="An account with this email already exists.")
            user_row = conn.execute(
                text(
                    "INSERT INTO users (email, password_hash) VALUES (:email, :hash) "
                    "RETURNING id, email, created_at"
                ),
                {"email": email, "hash": password_hash},
            ).mappings().first()
            token = auth.generate_session_token()
            conn.execute(
                text("INSERT INTO sessions (user_id, token, expires_at) VALUES (:uid, :token, :exp)"),
                {"uid": user_row["id"], "token": token, "exp": auth.session_expiry()},
            )
        _set_session_cookie(response, request, token)
        return dict(user_row)
    except HTTPException:
        raise
    except OperationalError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/api/auth/login", response_model=UserOut)
def login(payload: LoginRequest, request: Request, response: Response):
    if engine is None:
        raise HTTPException(status_code=503, detail="Database not configured.")
    email = (payload.email or "").strip().lower()
    generic_error = HTTPException(status_code=401, detail="Invalid email or password.")
    if not email or not payload.password:
        raise generic_error

    try:
        with engine.begin() as conn:
            user_row = conn.execute(
                text("SELECT id, email, password_hash, created_at FROM users WHERE email = :email"),
                {"email": email},
            ).mappings().first()
            if user_row is None or not auth.verify_password(payload.password, user_row["password_hash"]):
                raise generic_error
            token = auth.generate_session_token()
            conn.execute(
                text("INSERT INTO sessions (user_id, token, expires_at) VALUES (:uid, :token, :exp)"),
                {"uid": user_row["id"], "token": token, "exp": auth.session_expiry()},
            )
        _set_session_cookie(response, request, token)
        return {"id": user_row["id"], "email": user_row["email"], "created_at": user_row["created_at"]}
    except HTTPException:
        raise
    except OperationalError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/api/auth/logout")
def logout(response: Response, session_token: str | None = Cookie(default=None)):
    if engine is not None and session_token:
        try:
            with engine.begin() as conn:
                conn.execute(text("DELETE FROM sessions WHERE token = :token"), {"token": session_token})
        except OperationalError:
            pass  # logout should never fail the client-side experience
    response.delete_cookie(key=auth.SESSION_COOKIE_NAME, path="/")
    return {"status": "logged_out"}


@app.get("/api/auth/me", response_model=UserOut | None)
def me(user: dict | None = Depends(get_current_user)):
    return user


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class MineOut(BaseModel):
    id: int
    document_id: int | None = None
    sl_no: int | None = None
    state_ut: str | None = None
    district: str | None = None
    mine_name: str
    production_mt: float | None = None
    owner_short: str | None = None
    owner_full: str | None = None
    owner_full_inferred: bool | None = None
    coal_or_lignite: str | None = None
    ownership_type: str | None = None
    ownership_label: str | None = None
    mine_type: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    source_label: str | None = None
    source_url: str | None = None
    accuracy_type: str | None = None
    accuracy_note: str | None = None
    extraction_warnings: list[str] | None = None
    row_number: int | None = None
    validation_status: str | None = None
    validation_issues: list[dict] | None = None
    confidence: float | None = None


class MinesPage(BaseModel):
    items: list[MineOut]
    total: int


class EvidenceOut(BaseModel):
    id: int
    mine_id: int
    document_id: int
    sheet_name: str | None = None
    row_number: int | None = None
    column_name: str | None = None
    raw_value: str | None = None
    source_url: str | None = None
    extraction_method: str | None = None
    confidence: float | None = None


class ValidationSummaryOut(BaseModel):
    status: str
    count: int


class ReportOut(BaseModel):
    id: int
    document_id: int | None = None
    title: str | None = None
    dataset_period: str | None = None
    status: str
    narrative_source: str | None = None
    error_message: str | None = None
    created_at: datetime | None = None
    has_pdf: bool = False
    has_docx: bool = False


class AssistantHistoryTurn(BaseModel):
    question: str
    answer: str


class AssistantAskRequest(BaseModel):
    question: str
    document_id: int | None = None
    history: list[AssistantHistoryTurn] | None = None


class AssistantSourceOut(BaseModel):
    mine_id: int
    mine_name: str | None = None
    document_id: int
    sheet_name: str | None = None
    row_number: int | None = None
    column_name: str | None = None
    raw_value: str | None = None
    source_url: str | None = None
    extraction_method: str | None = None
    confidence: float | None = None


class AssistantAskResponse(BaseModel):
    question: str
    answer: str
    retrieval_type: str
    structured_results: dict | None = None
    sources: list[AssistantSourceOut] = []
    data_period: str = "2019-2020"
    narrative_source: str
    warnings: list[str] = []
    # Step 6: makes explicit whether this answer has a real evidence/source
    # reference behind it, so the frontend can render "no source available"
    # honestly instead of implying every answer is document-backed.
    source_availability: str = "not_applicable"  # "available" | "unavailable" | "not_applicable"


class AssistantImageAskResponse(AssistantAskResponse):
    image_extracted_text: str | None = None
    image_verified: bool = False
    image_format: str | None = None


class DocumentOut(BaseModel):
    id: int
    filename: str
    file_type: str | None = None
    status: str
    sheet_name: str | None = None
    total_rows: int | None = None
    extracted_rows: int | None = None
    error_message: str | None = None
    uploaded_at: datetime | None = None
    processed_at: datetime | None = None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/api/health")
def health():
    """Reports FastAPI status and live Neon PostgreSQL connectivity."""
    result = {
        "api": "ok",
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "database": "unknown",
        "database_error": None,
    }

    if engine is None:
        result["database"] = "not_configured"
        result["database_error"] = db_error_message
        return JSONResponse(result, status_code=200)

    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        result["database"] = "connected"
    except OperationalError as exc:
        result["database"] = "error"
        result["database_error"] = str(exc)

    return JSONResponse(result, status_code=200)


@app.get("/api/mines/count")
def mines_count(user: dict = Depends(require_auth)):
    if engine is None:
        raise HTTPException(status_code=503, detail="Database not configured.")
    try:
        with engine.connect() as conn:
            count = conn.execute(text("SELECT COUNT(*) FROM mines")).scalar()
        return {"count": count}
    except OperationalError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


SORTABLE_COLUMNS = {
    "id", "mine_name", "state_ut", "district", "production_mt",
    "owner_short", "coal_or_lignite", "mine_type", "validation_status",
    "confidence", "sl_no",
}


@app.get("/api/mines", response_model=MinesPage)
def list_mines(
    limit: int = 20,
    offset: int = 0,
    document_id: int | None = None,
    q: str | None = None,
    state_ut: str | None = None,
    ownership_type: str | None = None,
    mine_type: str | None = None,
    coal_or_lignite: str | None = None,
    validation_status: str | None = None,
    sort_by: str = "id",
    sort_dir: str = "asc",
    user: dict = Depends(require_auth),
):
    """
    Data Explorer backend: search + filters + sorting + pagination over the
    real `mines` table. `q` does a case-insensitive substring match across
    mine name, state, district, and owner name.
    """
    if engine is None:
        raise HTTPException(status_code=503, detail="Database not configured.")

    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    sort_by = sort_by if sort_by in SORTABLE_COLUMNS else "id"
    sort_dir = "DESC" if sort_dir.lower() == "desc" else "ASC"

    where_clauses = []
    params: dict = {"limit": limit, "offset": offset}

    if document_id is not None:
        where_clauses.append("document_id = :document_id")
        params["document_id"] = document_id
    if q:
        where_clauses.append(
            "(mine_name ILIKE :q OR state_ut ILIKE :q OR district ILIKE :q "
            "OR owner_short ILIKE :q OR owner_full ILIKE :q)"
        )
        params["q"] = f"%{q}%"
    if state_ut:
        where_clauses.append("state_ut = :state_ut")
        params["state_ut"] = state_ut
    if ownership_type:
        where_clauses.append("ownership_type = :ownership_type")
        params["ownership_type"] = ownership_type
    if mine_type:
        where_clauses.append("mine_type = :mine_type")
        params["mine_type"] = mine_type
    if coal_or_lignite:
        where_clauses.append("coal_or_lignite = :coal_or_lignite")
        params["coal_or_lignite"] = coal_or_lignite
    if validation_status:
        where_clauses.append("validation_status = :validation_status")
        params["validation_status"] = validation_status

    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

    select_sql = f"""
        SELECT id, document_id, sl_no, state_ut, district, mine_name, production_mt,
               owner_short, owner_full, owner_full_inferred, coal_or_lignite,
               ownership_type, ownership_label, mine_type, latitude, longitude,
               source_label, source_url, accuracy_type, accuracy_note,
               extraction_warnings, row_number, validation_status,
               validation_issues, confidence
        FROM mines
        {where_sql}
        ORDER BY {sort_by} {sort_dir} NULLS LAST
        LIMIT :limit OFFSET :offset
    """
    count_sql = f"SELECT COUNT(*) FROM mines {where_sql}"

    try:
        with engine.connect() as conn:
            total = conn.execute(text(count_sql), params).scalar()
            rows = conn.execute(text(select_sql), params).mappings().all()
        return {"items": [dict(r) for r in rows], "total": total}
    except OperationalError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/mines/{mine_id}/evidence", response_model=list[EvidenceOut])
def get_mine_evidence(mine_id: int, user: dict = Depends(require_auth)):
    """VIEW EVIDENCE: document -> sheet -> row -> column -> raw value, per field."""
    if engine is None:
        raise HTTPException(status_code=503, detail="Database not configured.")
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT id, mine_id, document_id, sheet_name, row_number, column_name,
                           raw_value, source_url, extraction_method, confidence
                    FROM evidence
                    WHERE mine_id = :mine_id
                    ORDER BY id
                    """
                ),
                {"mine_id": mine_id},
            ).mappings().all()
        return [dict(r) for r in rows]
    except OperationalError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/validation/summary", response_model=list[ValidationSummaryOut])
def validation_summary(document_id: int | None = None, user: dict = Depends(require_auth)):
    """Counts of mines grouped by validation_status — powers the Data Explorer's status filter."""
    if engine is None:
        raise HTTPException(status_code=503, detail="Database not configured.")
    where_sql = "WHERE document_id = :document_id" if document_id is not None else ""
    params = {"document_id": document_id} if document_id is not None else {}
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(f"""
                    SELECT COALESCE(validation_status, 'NEEDS_REVIEW') AS status, COUNT(*) AS count
                    FROM mines
                    {where_sql}
                    GROUP BY 1
                    ORDER BY 1
                """),
                params,
            ).mappings().all()
        return [dict(r) for r in rows]
    except OperationalError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Document upload + extraction pipeline (Step 2)
# ---------------------------------------------------------------------------
MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MB — generous for xlsx/pdf/docx source documents


@app.post("/api/upload", response_model=DocumentOut)
async def upload_document(file: UploadFile = File(...), user: dict = Depends(require_auth)):
    """
    Accepts a document (xlsx, pdf, docx, png, jpg/jpeg), saves it to disk,
    and creates a `documents` row with status 'uploaded'. Extraction is a
    separate step — call POST /api/documents/{id}/extract next.
    """
    if engine is None:
        raise HTTPException(status_code=503, detail="Database not configured.")

    # Security: never trust the client-supplied filename as a path component.
    # `os.path.basename` strips any directory traversal segments (e.g.
    # "../../etc/passwd") so the file can only ever land inside UPLOAD_DIR.
    original_filename = os.path.basename((file.filename or "").replace("\\", "/"))
    if not original_filename:
        raise HTTPException(status_code=400, detail="Missing filename.")

    file_type = detect_file_type(original_filename)
    if file_type is None:
        raise HTTPException(
            status_code=400,
            detail="Unsupported file extension. Supported: xlsx, pdf, docx, png, jpg, jpeg.",
        )

    safe_name = f"{uuid.uuid4().hex}_{original_filename}"
    dest_path = os.path.join(UPLOAD_DIR, safe_name)
    # Defense in depth: even after basename-stripping, confirm the resolved
    # path still lands inside UPLOAD_DIR before writing anything to disk.
    if os.path.commonpath([os.path.abspath(dest_path), os.path.abspath(UPLOAD_DIR)]) != os.path.abspath(UPLOAD_DIR):
        raise HTTPException(status_code=400, detail="Invalid filename.")

    # Security: enforce a size cap while streaming to disk, rather than
    # loading the whole upload into memory or trusting Content-Length
    # (which a client can lie about). Aborts and cleans up on overflow.
    bytes_written = 0
    try:
        with open(dest_path, "wb") as out_file:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                bytes_written += len(chunk)
                if bytes_written > MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=400,
                        detail=f"File is too large. Maximum allowed is {MAX_UPLOAD_BYTES // (1024*1024)} MB.",
                    )
                out_file.write(chunk)
    except HTTPException:
        if os.path.exists(dest_path):
            os.remove(dest_path)
        raise
    finally:
        await file.close()

    try:
        with engine.begin() as conn:
            row = conn.execute(
                text(
                    """
                    INSERT INTO documents (filename, file_type, status, file_path)
                    VALUES (:filename, :file_type, 'uploaded', :file_path)
                    RETURNING id, filename, file_type, status, sheet_name, total_rows,
                              extracted_rows, error_message, uploaded_at, processed_at
                    """
                ),
                {"filename": original_filename, "file_type": file_type, "file_path": dest_path},
            ).mappings().first()
        return dict(row)
    except OperationalError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/api/documents/{document_id}/extract", response_model=DocumentOut)
def extract_document_endpoint(document_id: int, user: dict = Depends(require_auth)):
    """
    Runs the extraction pipeline on a previously uploaded document.
    XLSX is fully implemented. Other formats return a clear 'unsupported'
    status without failing the request, so the architecture stays testable
    end-to-end while OCR/PDF/DOCX parsing is built out later.
    """
    if engine is None:
        raise HTTPException(status_code=503, detail="Database not configured.")

    try:
        with engine.connect() as conn:
            doc = conn.execute(
                text("SELECT * FROM documents WHERE id = :id"), {"id": document_id}
            ).mappings().first()
    except OperationalError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found.")

    result = extract_document(doc["file_path"], doc["file_type"])

    try:
        with engine.begin() as conn:
            if result.status == "success":
                # Replace any previous extraction for this document (idempotent re-run).
                # ON DELETE CASCADE on evidence.mine_id cleans up evidence automatically.
                conn.execute(text("DELETE FROM mines WHERE document_id = :id"), {"id": document_id})
                inserted_records = []
                for rec in result.records:
                    inserted_id = conn.execute(
                        text(
                            """
                            INSERT INTO mines
                            (document_id, row_number, sl_no, state_ut, district, mine_name, production_mt,
                             owner_short, owner_full, owner_full_inferred, coal_or_lignite,
                             ownership_type, ownership_label, mine_type, latitude, longitude,
                             source, source_label, source_url, accuracy, accuracy_type,
                             accuracy_note, raw_data, extraction_warnings)
                            VALUES
                            (:document_id, :row_number, :sl_no, :state_ut, :district, :mine_name, :production_mt,
                             :owner_short, :owner_full, :owner_full_inferred, :coal_or_lignite,
                             :ownership_type, :ownership_label, :mine_type, :latitude, :longitude,
                             :source, :source_label, :source_url, :accuracy, :accuracy_type,
                             :accuracy_note, CAST(:raw_data AS JSONB), CAST(:extraction_warnings AS JSONB))
                            RETURNING id
                            """
                        ),
                        {
                            "document_id": document_id,
                            "row_number": rec["row_number"],
                            "sl_no": rec["sl_no"],
                            "state_ut": rec["state_ut"],
                            "district": rec["district"],
                            "mine_name": rec["mine_name"],
                            "production_mt": rec["production_mt"],
                            "owner_short": rec["owner_short"],
                            "owner_full": rec["owner_full"],
                            "owner_full_inferred": rec["owner_full_inferred"],
                            "coal_or_lignite": rec["coal_or_lignite"],
                            "ownership_type": rec["ownership_type"],
                            "ownership_label": rec["ownership_label"],
                            "mine_type": rec["mine_type"],
                            "latitude": rec["latitude"],
                            "longitude": rec["longitude"],
                            "source": rec["source"],
                            "source_label": rec["source_label"],
                            "source_url": rec["source_url"],
                            "accuracy": rec["accuracy"],
                            "accuracy_type": rec["accuracy_type"],
                            "accuracy_note": rec["accuracy_note"],
                            "raw_data": json.dumps(rec["raw_data"]),
                            "extraction_warnings": json.dumps(rec["extraction_warnings"]),
                        },
                    ).scalar()
                    rec_with_id = dict(rec)
                    rec_with_id["id"] = inserted_id
                    inserted_records.append(rec_with_id)

                conn.execute(
                    text(
                        """
                        UPDATE documents
                        SET status = 'extracted', sheet_name = :sheet_name,
                            total_rows = :total_rows, extracted_rows = :extracted_rows,
                            error_message = NULL, processed_at = NOW()
                        WHERE id = :id
                        """
                    ),
                    {
                        "sheet_name": result.sheet_name,
                        "total_rows": result.total_rows,
                        "extracted_rows": result.extracted_rows,
                        "id": document_id,
                    },
                )

                # --- Step 3: validation + evidence -----------------------
                # Build the duplicate-detection index across the WHOLE mines
                # table (not just this document), so cross-document
                # duplicates are caught too.
                dup_rows = conn.execute(
                    text(
                        """
                        SELECT lower(trim(mine_name)) AS key_name,
                               lower(trim(coalesce(state_ut, ''))) AS key_state,
                               lower(trim(coalesce(district, ''))) AS key_district
                        FROM mines
                        GROUP BY 1, 2, 3
                        HAVING COUNT(*) > 1
                        """
                    )
                ).all()
                duplicate_keys = {(r[0], r[1], r[2]) for r in dup_rows}

                for rec in inserted_records:
                    outcome = validate_record(rec, duplicate_keys)

                    conn.execute(
                        text(
                            """
                            UPDATE mines
                            SET validation_status = :status,
                                validation_issues = CAST(:issues AS JSONB),
                                confidence = :confidence
                            WHERE id = :id
                            """
                        ),
                        {
                            "status": outcome.status,
                            "issues": json.dumps(outcome.issues),
                            "confidence": outcome.confidence,
                            "id": rec["id"],
                        },
                    )

                    # Extraction confidence for evidence: xlsx cells are read
                    # deterministically by coordinate, so confidence is high
                    # unless the field came back empty.
                    evidence_rows = build_evidence_rows(
                        mine_id=rec["id"],
                        document_id=document_id,
                        sheet_name=result.sheet_name,
                        row_number=rec["row_number"],
                        raw_data=rec["raw_data"],
                        source_url=rec.get("source_url"),
                        extraction_confidence=0.98,
                    )
                    for ev in evidence_rows:
                        conn.execute(
                            text(
                                """
                                INSERT INTO evidence
                                (mine_id, document_id, sheet_name, row_number, column_name,
                                 raw_value, source_url, extraction_method, confidence)
                                VALUES
                                (:mine_id, :document_id, :sheet_name, :row_number, :column_name,
                                 :raw_value, :source_url, :extraction_method, :confidence)
                                """
                            ),
                            ev,
                        )
            else:
                # 'unsupported' (format not built yet) or 'error' (bad file) — record and move on
                status_value = "unsupported" if result.status == "unsupported" else "error"
                conn.execute(
                    text(
                        """
                        UPDATE documents
                        SET status = :status, error_message = :error_message, processed_at = NOW()
                        WHERE id = :id
                        """
                    ),
                    {"status": status_value, "error_message": result.error_message, "id": document_id},
                )

            updated = conn.execute(
                text(
                    """
                    SELECT id, filename, file_type, status, sheet_name, total_rows,
                           extracted_rows, error_message, uploaded_at, processed_at
                    FROM documents WHERE id = :id
                    """
                ),
                {"id": document_id},
            ).mappings().first()
        return dict(updated)
    except OperationalError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/documents", response_model=list[DocumentOut])
def list_documents(limit: int = 50, user: dict = Depends(require_auth)):
    if engine is None:
        raise HTTPException(status_code=503, detail="Database not configured.")
    limit = max(1, min(limit, 200))
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT id, filename, file_type, status, sheet_name, total_rows,
                           extracted_rows, error_message, uploaded_at, processed_at
                    FROM documents
                    ORDER BY id DESC
                    LIMIT :limit
                    """
                ),
                {"limit": limit},
            ).mappings().all()
        return [dict(r) for r in rows]
    except OperationalError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/documents/{document_id}", response_model=DocumentOut)
def get_document(document_id: int, user: dict = Depends(require_auth)):
    if engine is None:
        raise HTTPException(status_code=503, detail="Database not configured.")
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT id, filename, file_type, status, sheet_name, total_rows,
                           extracted_rows, error_message, uploaded_at, processed_at
                    FROM documents WHERE id = :id
                    """
                ),
                {"id": document_id},
            ).mappings().first()
        if row is None:
            raise HTTPException(status_code=404, detail="Document not found.")
        return dict(row)
    except OperationalError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


class SeedResponse(BaseModel):
    inserted: int


@app.post("/api/mines/seed-sample", response_model=SeedResponse)
def seed_sample(user: dict = Depends(require_auth)):
    """
    Inserts a handful of sample mine rows so the frontend has something to
    display before the real ingestion pipeline (Step 2+) is built.
    Safe to call multiple times — it only seeds if the table is empty.
    """
    if engine is None:
        raise HTTPException(status_code=503, detail="Database not configured.")

    sample_rows = [
        (1, "West Bengal", "Paschim Bardhaman", "Ningah Colliery", 0.01, "ECL",
         "Eastern Coalfields Limited", "Coal", "G", "UG", 23.6743, 87.0333),
        (2, "West Bengal", "Paschim Bardhaman", "Jhanjhara Project Colly", 3.5, "ECL",
         "Eastern Coalfields Limited", "Coal", "G", "UG", 23.668, 87.2963),
        (3, "West Bengal", "Paschim Bardhaman", "Maohusudanpur 7 Pit & Incline", 0.04, "ECL",
         "Eastern Coalfields Limited", "Coal", "G", "UG", 23.6338, 87.2037),
    ]

    try:
        with engine.begin() as conn:
            existing = conn.execute(text("SELECT COUNT(*) FROM mines")).scalar()
            if existing and existing > 0:
                return {"inserted": 0}
            for row in sample_rows:
                conn.execute(
                    text(
                        """
                        INSERT INTO mines
                        (sl_no, state_ut, district, mine_name, production_mt, owner_short,
                         owner_full, coal_or_lignite, ownership_type, mine_type, latitude, longitude)
                        VALUES
                        (:sl_no, :state_ut, :district, :mine_name, :production_mt, :owner_short,
                         :owner_full, :coal_or_lignite, :ownership_type, :mine_type, :latitude, :longitude)
                        """
                    ),
                    dict(zip(
                        ["sl_no", "state_ut", "district", "mine_name", "production_mt",
                         "owner_short", "owner_full", "coal_or_lignite", "ownership_type",
                         "mine_type", "latitude", "longitude"],
                        row,
                    )),
                )
        return {"inserted": len(sample_rows)}
    except OperationalError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Step 4 — Analytics (all numbers computed via SQL/Python, never by an LLM)
# ---------------------------------------------------------------------------
@app.get("/api/analytics/full")
def get_full_analytics(document_id: int | None = None, user: dict = Depends(require_auth)):
    """
    Bundles every analytics metric into one payload: totals, production by
    state/mine/owner, coal vs lignite, mine type distribution, ownership
    distribution, and validation statistics. Backs both the frontend charts
    and the report generator, so numbers are computed once and stay
    consistent everywhere they're shown.
    """
    if engine is None:
        raise HTTPException(status_code=503, detail="Database not configured.")
    try:
        with engine.connect() as conn:
            return analytics_module.full_analytics(conn, document_id)
    except OperationalError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Step 4 — Report generation
# PostgreSQL -> Python calculations (analytics.py) -> charts (report_generator.py)
# -> Grok narrative (constrained to those calculations) -> PDF/DOCX
# ---------------------------------------------------------------------------
def _get_document_row(conn, document_id: int):
    doc = conn.execute(
        text("SELECT * FROM documents WHERE id = :id"), {"id": document_id}
    ).mappings().first()
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found.")
    return dict(doc)


def _get_sources(conn, document_id: int) -> list[dict]:
    """Real source URLs already captured in Step 3's evidence/mines data — never fabricated."""
    rows = conn.execute(
        text(
            """
            SELECT DISTINCT source_label, source_url
            FROM mines
            WHERE document_id = :document_id AND source_url IS NOT NULL
            ORDER BY source_label
            """
        ),
        {"document_id": document_id},
    ).mappings().all()
    return [dict(r) for r in rows]


@app.post("/api/documents/{document_id}/reports", response_model=ReportOut)
def generate_report(document_id: int, user: dict = Depends(require_auth)):
    """
    Runs the full pipeline for one document: pulls analytics from
    PostgreSQL, renders charts, generates a narrative (Grok if
    GROK_API_KEY is set, otherwise a deterministic template built from the
    same numbers), then assembles PDF + DOCX files.
    """
    if engine is None:
        raise HTTPException(status_code=503, detail="Database not configured.")

    with engine.connect() as conn:
        document_info = _get_document_row(conn, document_id)
        if document_info["status"] != "extracted":
            raise HTTPException(
                status_code=400,
                detail=f"Document status is '{document_info['status']}' — extract it successfully before generating a report.",
            )
        analytics = analytics_module.full_analytics(conn, document_id)
        sources = _get_sources(conn, document_id)

    if analytics["totals"]["mine_count"] == 0:
        raise HTTPException(status_code=400, detail="No mine records found for this document.")

    try:
        with engine.begin() as conn:
            report_id = conn.execute(
                text(
                    """
                    INSERT INTO reports (document_id, title, dataset_period, status)
                    VALUES (:document_id, :title, :dataset_period, 'generating')
                    RETURNING id
                    """
                ),
                {
                    "document_id": document_id,
                    "title": f"Indian Coal Mines Report — {document_info['filename']}",
                    "dataset_period": analytics["dataset_period"],
                },
            ).scalar()
    except OperationalError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    report_dir = os.path.join(REPORTS_DIR, str(report_id))
    charts_dir = os.path.join(report_dir, "charts")

    try:
        charts = report_generator.generate_charts(analytics, charts_dir)
        narrative, narrative_source = report_generator.generate_narrative(analytics, document_info)

        pdf_path = os.path.join(report_dir, "report.pdf")
        docx_path = os.path.join(report_dir, "report.docx")
        report_generator.render_pdf(pdf_path, document_info, analytics, narrative, narrative_source, charts, sources)
        report_generator.render_docx(docx_path, document_info, analytics, narrative, narrative_source, charts, sources)

        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE reports
                    SET status = 'generated',
                        analytics_snapshot = CAST(:analytics AS JSONB),
                        narrative_snapshot = CAST(:narrative AS JSONB),
                        narrative_source = :narrative_source,
                        pdf_path = :pdf_path,
                        docx_path = :docx_path,
                        error_message = NULL
                    WHERE id = :id
                    """
                ),
                {
                    "analytics": json.dumps(analytics),
                    "narrative": json.dumps(narrative),
                    "narrative_source": narrative_source,
                    "pdf_path": pdf_path,
                    "docx_path": docx_path,
                    "id": report_id,
                },
            )
            row = conn.execute(
                text("SELECT * FROM reports WHERE id = :id"), {"id": report_id}
            ).mappings().first()
        result = dict(row)
        result["has_pdf"] = bool(result.get("pdf_path"))
        result["has_docx"] = bool(result.get("docx_path"))
        return result
    except Exception as exc:  # noqa: BLE001 — record the failure, don't leave the row stuck in 'generating'
        try:
            with engine.begin() as conn:
                conn.execute(
                    text("UPDATE reports SET status = 'error', error_message = :err WHERE id = :id"),
                    {"err": str(exc), "id": report_id},
                )
        except OperationalError:
            pass
        raise HTTPException(status_code=500, detail=f"Report generation failed: {exc}") from exc


@app.get("/api/reports", response_model=list[ReportOut])
def list_reports(document_id: int | None = None, user: dict = Depends(require_auth)):
    if engine is None:
        raise HTTPException(status_code=503, detail="Database not configured.")
    where_sql = "WHERE document_id = :document_id" if document_id is not None else ""
    params = {"document_id": document_id} if document_id is not None else {}
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(f"SELECT * FROM reports {where_sql} ORDER BY id DESC"), params
            ).mappings().all()
        results = []
        for r in rows:
            d = dict(r)
            d["has_pdf"] = bool(d.get("pdf_path"))
            d["has_docx"] = bool(d.get("docx_path"))
            results.append(d)
        return results
    except OperationalError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/reports/{report_id}")
def get_report(report_id: int, user: dict = Depends(require_auth)):
    """VIEW REPORT: returns the full assembled report content (analytics + narrative) for in-app display."""
    if engine is None:
        raise HTTPException(status_code=503, detail="Database not configured.")
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT * FROM reports WHERE id = :id"), {"id": report_id}
            ).mappings().first()
        if row is None:
            raise HTTPException(status_code=404, detail="Report not found.")
        d = dict(row)
        return {
            "id": d["id"],
            "document_id": d["document_id"],
            "title": d["title"],
            "dataset_period": d["dataset_period"],
            "status": d["status"],
            "narrative_source": d["narrative_source"],
            "created_at": d["created_at"],
            "analytics": d.get("analytics_snapshot"),
            "narrative": d.get("narrative_snapshot"),
            "has_pdf": bool(d.get("pdf_path")),
            "has_docx": bool(d.get("docx_path")),
        }
    except OperationalError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/reports/{report_id}/pdf")
def download_report_pdf(report_id: int, user: dict = Depends(require_auth)):
    if engine is None:
        raise HTTPException(status_code=503, detail="Database not configured.")
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT pdf_path, title FROM reports WHERE id = :id"), {"id": report_id}
            ).mappings().first()
    except OperationalError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if row is None or not row["pdf_path"] or not os.path.exists(row["pdf_path"]):
        raise HTTPException(status_code=404, detail="PDF not found for this report.")
    return FileResponse(row["pdf_path"], media_type="application/pdf", filename=f"report_{report_id}.pdf")


@app.get("/api/reports/{report_id}/docx")
def download_report_docx(report_id: int, user: dict = Depends(require_auth)):
    if engine is None:
        raise HTTPException(status_code=503, detail="Database not configured.")
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT docx_path, title FROM reports WHERE id = :id"), {"id": report_id}
            ).mappings().first()
    except OperationalError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if row is None or not row["docx_path"] or not os.path.exists(row["docx_path"]):
        raise HTTPException(status_code=404, detail="DOCX not found for this report.")
    return FileResponse(
        row["docx_path"],
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=f"report_{report_id}.docx",
    )


# ---------------------------------------------------------------------------
# Step 5 — RAG + Grok AI Assistant
# Browser -> FastAPI -> SQL/RAG retrieval -> evidence/results -> Grok -> answer
# GROK_API_KEY is read from the environment server-side only; it is never
# sent to, or accepted from, the browser.
# ---------------------------------------------------------------------------
@app.post("/api/assistant/ask", response_model=AssistantAskResponse)
def assistant_ask(payload: AssistantAskRequest, user: dict = Depends(require_auth)):
    question = (payload.question or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="Question cannot be empty.")
    if len(question) > 1000:
        raise HTTPException(status_code=400, detail="Question is too long (max 1000 characters).")

    if engine is None:
        raise HTTPException(status_code=503, detail="Database not configured.")

    warnings: list[str] = []
    retrieval_type = rag.classify_question(question)

    # Bound conversation history before it goes anywhere near Grok — the
    # frontend may keep the full visible chat, but only the last few turns
    # are ever sent, and only as non-authoritative context (see rag.py).
    history = None
    if payload.history:
        history = [h.model_dump() for h in payload.history[-rag.MAX_HISTORY_TURNS:]]

    try:
        with engine.connect() as conn:
            structured = None
            evidence: list[dict] = []

            if retrieval_type in ("structured", "both"):
                try:
                    structured = rag.retrieve_structured(conn, question, payload.document_id)
                except OperationalError as exc:
                    warnings.append(f"Structured retrieval failed: {exc}")

            if retrieval_type in ("evidence", "both"):
                try:
                    mine = structured.get("mine") if structured else None
                    evidence = rag.retrieve_evidence(conn, question, payload.document_id, mine=mine)
                    if not evidence:
                        warnings.append("No matching evidence records were found for this question.")
                except OperationalError as exc:
                    warnings.append(f"Evidence retrieval failed: {exc}")
            elif structured and structured.get("mine"):
                # Step 6: even for a purely "structured" question, if it resolved to a
                # specific mine, attach that mine's real evidence so the numeric answer
                # is still source-backed — never fabricated, just the actual evidence rows.
                try:
                    evidence = rag.retrieve_evidence(conn, question, payload.document_id, mine=structured["mine"])
                except OperationalError as exc:
                    warnings.append(f"Evidence retrieval failed: {exc}")
            elif structured:
                # Aggregate/statistical answer (e.g. total production, top state) has no
                # single source row — say so explicitly rather than leaving it ambiguous.
                warnings.append(
                    "This is an aggregate calculation across the dataset; no single source "
                    "row applies. The database is the source of truth for this number."
                )
    except OperationalError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    answer, narrative_source = rag.answer_question(question, retrieval_type, structured, evidence, history)

    if evidence:
        source_availability = "available"
    elif retrieval_type in ("evidence", "both") or (structured and structured.get("mine")):
        source_availability = "unavailable"
    else:
        source_availability = "not_applicable"

    return {
        "question": question,
        "answer": answer,
        "retrieval_type": retrieval_type,
        "structured_results": structured,
        "sources": rag.sources_from_evidence(evidence),
        "data_period": rag.DATASET_PERIOD,
        "narrative_source": narrative_source,
        "warnings": warnings,
        "source_availability": source_availability,
    }


# ---------------------------------------------------------------------------
# Step 6 — Image input for the AI Assistant
# Image -> validate (image_processing.py) -> OCR -> extracted text ->
# same SQL/RAG/evidence pipeline as text questions -> Grok -> answer.
# Images are processed in memory only; nothing is written to disk.
# ---------------------------------------------------------------------------
@app.post("/api/assistant/ask-image", response_model=AssistantImageAskResponse)
async def assistant_ask_image(
    image: UploadFile = File(...),
    question: str = Form(""),
    document_id: int | None = Form(None),
    history: str = Form("[]"),
    user: dict = Depends(require_auth),
):
    question = (question or "").strip()
    if len(question) > 1000:
        raise HTTPException(status_code=400, detail="Question is too long (max 1000 characters).")

    try:
        history_list = json.loads(history) if history else []
        if not isinstance(history_list, list):
            history_list = []
    except (json.JSONDecodeError, TypeError):
        history_list = []
    bounded_history = [
        {"question": h.get("question", ""), "answer": h.get("answer", "")}
        for h in history_list[-rag.MAX_HISTORY_TURNS:] if isinstance(h, dict)
    ] or None

    try:
        image_bytes = await image.read()
    finally:
        await image.close()

    # Image validation is pure, local, DB-independent work — do it before
    # touching the database, so a bad upload fails fast with a clear 400
    # instead of being masked behind a 503 if the DB happens to be down.
    try:
        pil_image = image_processing.validate_and_load_image(image_bytes, image.filename or "")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        ocr_text = image_processing.extract_text(pil_image)
    except RuntimeError as exc:
        # OCR failure is not fatal — answer from the question/database alone,
        # and say plainly that image text could not be read. Never fabricate OCR output.
        ocr_text = ""
        ocr_warning = str(exc)
    else:
        ocr_warning = None

    if not question and not ocr_text:
        raise HTTPException(
            status_code=400,
            detail="No question was provided and no text could be read from the image.",
        )

    if engine is None:
        raise HTTPException(status_code=503, detail="Database not configured.")

    try:
        with engine.connect() as conn:
            result = rag.answer_image_question(conn, question, ocr_text, document_id, bounded_history)
    except OperationalError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    warnings = list(result["warnings"])
    if ocr_warning:
        warnings.append(ocr_warning)

    source_availability = "available" if result["sources"] else (
        "unavailable" if result["image_verified"] or result.get("structured_results", {}).get("mine") else "not_applicable"
    )

    return {
        "question": question or "(image only — no question text provided)",
        "answer": result["answer"],
        "retrieval_type": result["retrieval_type"],
        "structured_results": result["structured_results"],
        "sources": result["sources"],
        "data_period": rag.DATASET_PERIOD,
        "narrative_source": result["narrative_source"],
        "warnings": warnings,
        "source_availability": source_availability,
        "image_extracted_text": ocr_text or None,
        "image_verified": result["image_verified"],
        "image_format": pil_image.format,
    }


# ---------------------------------------------------------------------------
# Serve the single-file frontend
# ---------------------------------------------------------------------------
@app.get("/")
def serve_index():
    return FileResponse("index.html")


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", 8000))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=True)
