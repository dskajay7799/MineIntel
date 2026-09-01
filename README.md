# CMPDI/CIL AI Document Intelligence Platform — Step 1 Foundation

Minimal working foundation: Browser (single-page frontend) → FastAPI → Neon PostgreSQL.

## What's included

- `index.html` — single-file frontend (HTML/CSS/JS), shows live API + DB status and a sample data table.
- `app.py` — single-file FastAPI backend, connects to Neon via `DATABASE_URL`, creates minimum tables on startup.
- `requirements.txt` — Python dependencies.
- `.env.example` — template for environment variables (copy to `.env`).
- `.gitignore`

## What's NOT included yet (by design, later steps)

OCR, ML/TFLite, Grok, RAG, report generation, speech, image AI, word cloud, analytics, advanced auth.

## Setup

### 1. Create a Neon database

1. Go to https://neon.tech and create a free project.
2. Copy the connection string from the dashboard (Connection Details). It looks like:
   ```
   postgresql://user:password@ep-xxxxx.region.aws.neon.tech/neondb?sslmode=require
   ```

### 2. Configure environment

```bash
cp .env.example .env
# edit .env and paste your Neon DATABASE_URL
```

### 3. Install dependencies

```bash
python -m venv venv
source venv/bin/activate   # on Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 4. Run the server

```bash
uvicorn app:app --reload
```

Then open **http://localhost:8000** in your browser.

## What happens on startup

- FastAPI connects to Neon using `DATABASE_URL`.
- It creates two tables if they don't already exist:
  - `mines` — minimal schema based on the approved Step 0 dataset analysis (Indian Coal Mines Dataset: state, district, mine name, production, owner, coordinates, etc.)
  - `documents` — placeholder table for tracking uploaded files (used starting Step 2, the extraction pipeline).

## Testing the full chain

1. Open the app in your browser. The **System Status** card should show both **FastAPI backend** and **Neon PostgreSQL** as green (connected).
2. Click **Seed sample rows** to insert 3 example mine records.
3. Click **Load mines** to fetch them back from Neon and render them in a table.

If the DB dot is red, check:
- `DATABASE_URL` is set correctly in `.env`.
- Your Neon project is active (not suspended/idle without auto-resume issues).
- `?sslmode=require` is present in the connection string.

## API endpoints

| Method | Path                     | Purpose                              |
|--------|--------------------------|---------------------------------------|
| GET    | `/`                      | Serves `index.html`                  |
| GET    | `/api/health`            | API + DB connectivity status         |
| GET    | `/api/mines`             | List mine records (`?limit=`)        |
| GET    | `/api/mines/count`       | Row count in `mines` table           |
| POST   | `/api/mines/seed-sample` | Insert 3 sample rows if table empty  |

## Step 2 — Document upload + extraction (added)

New file: `extractors.py` — the only new file, kept separate from `app.py` because
the extraction logic needs to be pluggable per file format (xlsx now, PDF/DOCX/
images later) without bloating the API layer.

### Pipeline

```
POST /api/upload                     -> saves file to disk, creates `documents` row (status='uploaded')
POST /api/documents/{id}/extract     -> runs the extractor, writes rows to `mines`, updates `documents`
GET  /api/documents                  -> list uploaded documents + their status
GET  /api/documents/{id}             -> single document detail
GET  /api/mines?document_id=<id>     -> mines extracted from a specific document
```

### Supported formats

| Format | Status |
|---|---|
| XLSX | ✅ Fully implemented (sheet detection, header detection, normalization) |
| PDF | 🏗️ Architecture ready, returns `status: unsupported` |
| Scanned PDF | 🏗️ Architecture ready, returns `status: unsupported` |
| DOCX | 🏗️ Architecture ready, returns `status: unsupported` |
| PNG / JPG / JPEG | 🏗️ Architecture ready, returns `status: unsupported` |

Uploading a PDF/DOCX/image still works end-to-end (file is saved, a `documents`
row is created) — calling `/extract` on it just returns a clear "not implemented
yet" status instead of failing silently. This proves the pipeline shape now
without spending time on OCR yet.

### XLSX extraction details

- **Sheet detection**: scans every sheet's first 5 rows for a header containing
  "Mine Name" (and other known headers); automatically skips non-data sheets
  like "Citation & Copyright".
- **Header detection**: maps real column headers (with their unavoidable
  inconsistencies — trailing spaces, line breaks) to canonical field names via
  an alias table, not fixed column letters.
- **Cleanup applied to every text cell**: strips `\xa0` (non-breaking space),
  collapses embedded `\n`/`\r` into a single space, collapses repeated
  whitespace, trims leading/trailing spaces.
- **Owner full name**: falls back to a small legend for known PSU abbreviations
  (NTPC, NLC Ltd, BALCO) when the source file leaves it blank; otherwise falls
  back to the short name. Every fallback is flagged in `owner_full_inferred`
  and in that row's `extraction_warnings`.
- **Ownership code** (`G`/`P`/`SG`) is expanded to a human label
  (`ownership_label`: Government / Private / State Government) while keeping
  the raw code in `ownership_type`.
- **Source** ("Google Maps: https://...") is split into `source_label` and
  `source_url`, with the original untouched string preserved in `source`.
- **Accuracy** ("Exact", "Approximate: PIN 713321", "Approximate coordinates
  of X area", "Approx") is normalized into `accuracy_type` (Exact/Approximate)
  and `accuracy_note` (the free-text remainder), handling the colon-present
  and colon-absent variants the source data actually contains.
- **Raw preservation**: every row's original cell values (keyed by their
  original header text) are stored verbatim in `mines.raw_data` (JSONB) for
  traceability back to the source document.

### Database changes (additive only — nothing from Step 1 was dropped)

`documents` gained: `file_path`, `sheet_name`, `total_rows`, `extracted_rows`,
`error_message`, `processed_at`.

`mines` gained: `document_id` (FK), `raw_data` (JSONB), `owner_full_inferred`,
`ownership_label`, `source_label`, `source_url`, `accuracy_type`,
`accuracy_note`, `extraction_warnings` (JSONB).

All migrations use `ADD COLUMN IF NOT EXISTS`, so re-running the app against
an existing database is safe.

### Testing performed

- Ran `extractors.extract_xlsx()` directly against
  `Indian_Coal_Mines_Dataset_January_2021-1.xlsx`: **459/459 rows extracted**,
  correct sheet auto-detected ("Mines Datasheet"), all 11 rows with a missing
  owner full name correctly flagged and resolved, accuracy values normalized
  into 324 "Exact" / 135 "Approximate" with notes, whitespace/newline cleanup
  verified (e.g. `"MAOHUSUDANPUR 7 PIT &\nINCLINE"` → `"MAOHUSUDANPUR 7 PIT & INCLINE"`).
- Started the FastAPI server and confirmed `/api/health`, `/`, and the upload
  endpoint's validation (rejects unsupported extensions, requires DB config)
  all behave correctly.
- **Not yet tested**: a live insert into Neon, since this sandbox cannot reach
  `neon.tech` or run a local PostgreSQL. Once you set `DATABASE_URL` in `.env`
  and run the app, test the full chain with:
  ```bash
  curl -F "file=@Indian_Coal_Mines_Dataset_January_2021-1.xlsx" http://localhost:8000/api/upload
  # note the returned "id", then:
  curl -X POST http://localhost:8000/api/documents/<id>/extract
  curl "http://localhost:8000/api/mines?document_id=<id>&limit=5"
  ```

## Step 3 — Validation + Evidence + Data Explorer (added)

New file: `validation.py` — deterministic rule engine + evidence-row builder,
kept separate from `app.py` for the same reason as `extractors.py`: it's a
distinct concern (data quality rules) that both the extract endpoint and
future re-validation endpoints will call.

### Validation

Runs automatically right after every successful extraction, on every row.
**Never rewrites a questionable source value** — every rule only flags an
issue; the original extracted/normalized value in `mines.*` is left as-is.

Rules implemented:

| Check | Severity |
|---|---|
| Missing state/district/owner/coal-type/ownership/mine-type/lat/long/production | NEEDS_REVIEW |
| Production < 0 | ERROR |
| Production == 0 | NEEDS_REVIEW |
| Latitude outside [-90, 90] | ERROR |
| Longitude outside [-180, 180] | ERROR |
| Coordinates outside India's rough bounding box | NEEDS_REVIEW |
| Coordinates are (0, 0) | WARNING |
| Coal/Lignite not in {Coal, Lignite} | WARNING |
| Ownership code not in {G, P, SG} | WARNING |
| Mine type not in {OC, UG, Mixed} | WARNING |
| Owner code missing | ERROR |
| Owner full name was inferred (not in source) | NEEDS_REVIEW |
| Duplicate mine (same name + state + district, checked across the *whole* `mines` table, not just the current upload) | WARNING |
| Mine name under 3 characters | WARNING |
| Unrecognized accuracy value | NEEDS_REVIEW |

Overall status per mine = the worst severity found (`ERROR` > `WARNING` >
`NEEDS_REVIEW` > `VERIFIED`). A numeric `confidence` (0.00–1.00) is also
computed by subtracting a weight per issue severity, stored on the row.

Verified against the real dataset: **399 VERIFIED, 57 NEEDS_REVIEW, 3
WARNING, 0 ERROR** out of 459 rows — including catching a genuine duplicate
("Urimari" appears twice in the source file under the same state/district)
and all 11 rows with an inferred owner full name.

### Evidence / traceability

New table `evidence`: one row per source column per mine — literally
**document → sheet → row → column → raw value**, plus `source_url`,
`extraction_method`, and a `confidence` score for that extraction. For XLSX
this is generated straight from the `raw_data` already captured during Step
2 extraction, so no re-parsing is needed. PDF page numbers don't apply here
since Excel has no such concept — evidence uses sheet + row + column, exactly
as it exists in a spreadsheet.

`GET /api/mines/{id}/evidence` returns all evidence rows for a mine — this
is the **VIEW EVIDENCE** interface, and the frontend's "View" button opens it
in a modal.

### Data Explorer

The old static "Sample Data" table is replaced by a full explorer over the
real `mines` table:

- **Search** (`q`): case-insensitive substring match across mine name,
  state, district, and owner (short + full name).
- **Filters**: state, ownership code, mine type, coal/lignite, validation
  status — all sent as real `WHERE` clauses, not client-side filtering.
- **Sorting**: click any sortable column header; toggles asc/desc.
- **Pagination**: page-size selector (20/50/100) + prev/next, backed by
  `LIMIT`/`OFFSET` and a real `COUNT(*)` for the total.
- Each row shows **confidence** and a **validation status badge**, links to
  the **source** URL, and has a **View** button that opens the **Evidence**
  modal for that mine (with its validation issues listed alongside).
- A validation-status summary strip (counts per status) sits above the table,
  backed by `GET /api/validation/summary`.

All of this reads directly from PostgreSQL — nothing in the explorer is
hardcoded or mocked.

### New/updated endpoints

```
GET  /api/mines?q=&state_ut=&ownership_type=&mine_type=&coal_or_lignite=
              &validation_status=&sort_by=&sort_dir=&limit=&offset=&document_id=
     -> { items: [...], total: N }               (Data Explorer backend)
GET  /api/mines/{id}/evidence                     (VIEW EVIDENCE)
GET  /api/validation/summary?document_id=         (status counts)
```

### Database changes (additive only)

`mines` gained: `row_number`, `validation_status`, `validation_issues`
(JSONB), `confidence`.

New table `evidence`: `mine_id` (FK, `ON DELETE CASCADE`), `document_id`
(FK), `sheet_name`, `row_number`, `column_name`, `raw_value`, `source_url`,
`extraction_method`, `confidence`.

### Testing performed

- Ran `validation.validate_record()` directly against all 459 real extracted
  rows (see distribution above) — confirmed no source values were altered,
  only flagged.
- Confirmed the extract endpoint's SQL wiring (insert with `RETURNING id`,
  duplicate-detection query, validation update, evidence insert) is
  syntactically correct and matches the schema.
- Booted the FastAPI server and exercised `/`, `/api/health`,
  `/api/mines`, `/api/mines/{id}/evidence`, `/api/validation/summary` —
  all return the correct `503` before a database is configured, and all
  routes are registered (`/openapi.json` confirmed).
- Syntax-checked the frontend's extracted `<script>` block with `node --check`.
- **Not yet tested**: a live end-to-end run (upload → extract → validate →
  evidence → explore) against Neon, since this sandbox cannot reach
  `neon.tech` and cannot install a local PostgreSQL for testing. Once you
  set `DATABASE_URL` and run the app, the full chain to test is:
  ```bash
  curl -F "file=@Indian_Coal_Mines_Dataset_January_2021-1.xlsx" http://localhost:8000/api/upload
  curl -X POST http://localhost:8000/api/documents/<id>/extract
  curl "http://localhost:8000/api/mines?limit=5&sort_by=confidence&sort_dir=asc"
  curl "http://localhost:8000/api/mines/<mine_id>/evidence"
  curl "http://localhost:8000/api/validation/summary"
  ```
  or just open `http://localhost:8000` and use the Upload/Extract card
  followed by the Data Explorer.

## Step 4 — Analytics + Report Generation (added)

Two new files, both dependency-isolated so nothing in Steps 1–3 was touched:

- **`analytics.py`** — every number the app shows comes from here. Pure SQL
  aggregate queries (with a small amount of Python for the JSONB issue-type
  rollup) run against the real `mines` table. **No LLM is ever involved in
  a calculation.**
- **`report_generator.py`** — turns that analytics dict into matplotlib
  charts, an optional Grok narrative, and PDF/DOCX files.

### Analytics

`GET /api/analytics/full?document_id=` returns one bundled payload:

- Totals (mine count, total/avg/max/min production, mines missing a
  production value)
- Production by state, by owner, top 15 producing mines
- Coal vs lignite, mine type distribution, government/private distribution
- Validation statistics (status counts, avg/min confidence, top issue types)

Every number is computed once per request in SQL and reused by both the
frontend Analytics card and the report generator, so the UI and the PDF/DOCX
never disagree. **No year-over-year comparison is computed or implied** —
the dataset is explicitly labeled `"dataset_period": "2019-2020"` and the
narrative is instructed never to claim a trend.

Verified against the real dataset (calculated in Python as a stand-in for
the SQL, since Neon isn't reachable — see Testing below): 459 mines, 773.47
MT total production, Chhattisgarh the top state (157.24 MT / 52 mines),
GEVRA OC the top mine (45.0 MT), South Eastern Coalfields Limited the top
owner (150.54 MT / 73 mines), 96%/4% coal/lignite split, and the Step 3
validation split (399 VERIFIED / 57 NEEDS_REVIEW / 3 WARNING / 0 ERROR)
carried straight through.

### Report generation

`POST /api/documents/{id}/reports` runs the full pipeline:

```
PostgreSQL (analytics.py)
   -> Python calculations (same module)
   -> matplotlib charts (report_generator.generate_charts)
   -> Grok narrative, constrained to those exact numbers (generate_narrative)
   -> PDF (reportlab) + DOCX (python-docx) assembly
   -> saved to /reports/{report_id}/ and recorded in the `reports` table
```

Each report contains all 10 required sections: Executive Summary, Dataset
Overview, Production Analysis, Mine/Owner Analysis, Key Findings, Data
Quality, Important Observations, Tables, Charts, Sources — with real charts
and tables embedded in both the PDF and the DOCX.

**Grok narrative rules, enforced in code, not just prompt text:**
- The prompt sent to Grok contains *only* the pre-calculated JSON (totals,
  top-5 breakdowns, validation stats) — nothing else.
- The prompt explicitly forbids calculating, estimating, inventing, or
  modifying any number, inventing mines/owners/sources, or claiming a
  trend across periods.
- If `GROK_API_KEY` (or `XAI_API_KEY`) isn't set, or the API call fails or
  returns something malformed, generation **falls back automatically** to
  `_template_fallback_narrative()` — a deterministic Python function that
  builds the same six narrative sections directly from the same numbers
  with plain string formatting. No number in either path is invented.
- Every report records which path was used (`narrative_source`: `"grok"`
  or `"template_fallback (<reason>)"`), and this is shown in both the
  in-app "View Report" screen and printed at the bottom of the PDF/DOCX.

**Sources** are pulled from the real `source_url`/`source_label` values
already captured in `mines` during Step 2/3 extraction — nothing is
fabricated. If a document has no source URLs, the report says so plainly
instead of inventing one.

### New/updated endpoints

```
GET  /api/analytics/full?document_id=              (all analytics, source of truth)
POST /api/documents/{id}/reports                    (generate a report end-to-end)
GET  /api/reports?document_id=                      (list generated reports)
GET  /api/reports/{id}                               (VIEW REPORT — narrative + analytics JSON)
GET  /api/reports/{id}/pdf                           (download PDF)
GET  /api/reports/{id}/docx                          (download DOCX)
```

### Frontend additions

- **Analytics card**: document-ID filter, "Load Analytics" button, stat
  tiles, and bar-chart visualizations (state, top mines, owner, coal/lignite,
  mine type, ownership, validation status/issues) — all rendered from
  `/api/analytics/full`, nothing hardcoded. Auto-loads after a successful
  extraction.
- **Reports card**: "Generate Report" (posts to `/api/documents/{id}/reports`),
  a list of past reports with status badges and narrative-source labels,
  "View" (opens the narrative + key stats in a modal), and PDF/DOCX download
  buttons.
- The Data Explorer, Evidence modal, and Upload/Extract flow from Steps 2–3
  are unchanged.

### Database changes (additive only)

New table `reports`: `document_id` (FK), `title`, `dataset_period`, `status`
(`generating` / `generated` / `error`), `analytics_snapshot` (JSONB, the
exact numbers used), `narrative_snapshot` (JSONB), `narrative_source`,
`pdf_path`, `docx_path`, `error_message`. Nothing in `mines`, `documents`,
or `evidence` was altered.

### Dependencies added

`matplotlib`, `reportlab`, `python-docx`, `requests` (for the optional Grok
HTTP call). All installed and confirmed working in this environment.

### Testing performed

**Fully tested, with real data, in this sandbox:**
- Ran the exact aggregation logic `analytics.py`'s SQL performs, in plain
  Python, over all 459 real extracted-and-validated records (numbers
  reported above) — confirms the *logic* is correct; the SQL statements
  themselves were reviewed but not executed (see below).
- `report_generator.generate_charts()` — all 7 charts rendered successfully
  as PNGs from that real data.
- `report_generator.generate_narrative()` — template fallback path (no
  `GROK_API_KEY` set) tested and produces accurate, non-fabricated prose
  built only from the real numbers.
- `report_generator.render_pdf()` and `render_docx()` — both generated
  successfully; **visually verified** by rasterizing every page to an image
  (`pdftoppm` for the PDF, LibreOffice `--convert-to pdf` + `pdftoppm` for
  the DOCX) and inspecting them — correct layout, correct real numbers,
  correct charts, all 10 sections present, sources and narrative-source
  disclosed.
- Full backend boot test after wiring everything into `app.py`: server
  starts cleanly, all 16 routes register (`/openapi.json` confirmed), every
  new endpoint (`/api/analytics/full`, `/api/documents/{id}/reports`,
  `/api/reports`, `/api/reports/{id}/pdf`, `/api/reports/{id}/docx`)
  returns the correct `503` without a database configured, and **every
  existing Step 1–3 endpoint (`/api/mines`, `/api/mines/{id}/evidence`,
  `/api/validation/summary`, `/api/documents`, `/api/upload`) still
  responds correctly — nothing was broken.**
- Frontend `<script>` block syntax-checked with `node --check` after the
  Analytics/Reports UI additions.

**Not tested — dependent on external access this sandbox doesn't have:**
- **Live Neon/PostgreSQL**: `/api/analytics/full` and
  `/api/documents/{id}/reports` have not been run against a real database
  connection — `neon.tech` is not reachable here. The SQL in `analytics.py`
  was written and reviewed carefully but not executed against Postgres.
- **Live Grok call**: `api.x.ai` is not reachable here either, so the
  actual HTTP call in `generate_narrative()` — its request/response
  handling, JSON parsing, and error paths — has not been exercised, only
  the fallback branch. If you have a `GROK_API_KEY`, set it in `.env` and
  test with a real document; if the call fails for any reason the report
  still generates via the template fallback, so this is a quality
  enhancement, not a blocker.

Once you have a live `DATABASE_URL` (and optionally `GROK_API_KEY`), the
end-to-end test is:
```bash
curl -F "file=@Indian_Coal_Mines_Dataset_January_2021-1.xlsx" http://localhost:8000/api/upload
curl -X POST http://localhost:8000/api/documents/<id>/extract
curl -X POST http://localhost:8000/api/documents/<id>/reports
curl "http://localhost:8000/api/reports?document_id=<id>"
curl "http://localhost:8000/api/reports/<report_id>" | python3 -m json.tool
curl -o report.pdf "http://localhost:8000/api/reports/<report_id>/pdf"
curl -o report.docx "http://localhost:8000/api/reports/<report_id>/docx"
```
or just use the Analytics and Reports cards in the UI.

## Step 5 — RAG + Grok AI Assistant (added)

One new file: **`rag.py`** — deterministic question routing, structured SQL
retrieval (reusing `analytics.py` directly, no duplicated calculation logic),
evidence/text retrieval (reusing Step 3's `evidence`/`mines` tables), and
Grok answer generation with a safe template fallback. `app.py` gained one
new endpoint; nothing in Steps 1–4 was modified.

### Architecture

```
Browser -> FastAPI (/api/assistant/ask) -> SQL/RAG retrieval -> evidence/results -> Grok -> answer
```

1. **Routing** (`rag.classify_question`) — pure regex/keyword matching, no
   LLM call, returns `"structured"`, `"evidence"`, or `"both"`. Numerical
   phrasing ("total", "highest", "how many", "verified", "government
   owned", etc.) routes to structured; source-ish phrasing ("evidence",
   "source", "document", "sheet", "column", "explain") routes to evidence;
   both sets of keywords → both paths run and their results are combined.
2. **Structured retrieval** (`rag.retrieve_structured`) — calls the *exact
   same* Step 4 functions (`analytics.total_production`,
   `production_by_state`, `top_producing_mines`, `production_by_owner`,
   `coal_vs_lignite`, `ownership_distribution`, `validation_statistics`),
   selecting only the ones relevant to the question, plus a deterministic
   mine-name lookup (`rag.find_mine`) for "how much does GEVRA OC produce"
   style questions. **This is the numerical source of truth — Grok never
   sees this step, only its output.**
3. **Evidence retrieval** (`rag.retrieve_evidence`) — if a mine was
   identified, pulls all its real `evidence` rows (sheet/row/column/raw
   value, exactly as built in Step 3); otherwise does a bounded ILIKE
   keyword search across `evidence.raw_value`. Never invents a source — if
   nothing matches, the response says so.
4. **Answer generation** (`rag.answer_question`) — sends Grok *only* the
   already-retrieved JSON (structured results + evidence rows) plus a
   strict instruction set (no calculating, no inventing mines/states/
   owners/sources, no year-over-year trends, say so if data is
   insufficient). If `GROK_API_KEY`/`XAI_API_KEY` isn't set, or the call
   fails, times out, or returns something unusable, it **falls back
   automatically** to `_template_fallback_answer()` — plain Python string
   formatting over the same retrieved data, so every number in the answer
   is traceable and nothing is ever fabricated on the fallback path either.

### API

```
POST /api/assistant/ask
  body: { "question": "...", "document_id": <optional int>, "history": [{"question","answer"}, ...] }
  ->  { "question", "answer", "retrieval_type", "structured_results",
        "sources": [...], "data_period": "2019-2020", "narrative_source", "warnings": [...] }
```

- Empty question → `400`. Question over 1000 chars → `400`. Missing field
  → FastAPI's standard `422`. No database configured → `503` (checked
  before any retrieval is attempted). Retrieval or Grok failures are caught
  per-stage and surfaced as `warnings` in the response rather than crashing
  the request — the assistant always returns *something*, honestly.

### Conversation memory

The frontend keeps the full visible chat in a JS array. Only the **last 3
completed turns** are ever sent to the backend, and the backend re-bounds
that to `rag.MAX_HISTORY_TURNS = 3` regardless of what's sent. History is
injected into the Grok prompt in a clearly labeled block — *"context only —
informational, NOT authoritative; the CURRENT retrieved data above
overrides anything here if they conflict"* — so an earlier (possibly
outdated) answer can never override this turn's fresh retrieval.

### `GROK_API_KEY` security

- Read once, server-side, via `os.getenv("GROK_API_KEY")` in `rag.py` (same
  pattern as Step 4's `report_generator.py`).
- **Never** sent to, embedded in, or read from `index.html` — confirmed by
  grepping the frontend for any key/token reference (none found).
- Never included in any API response body — confirmed by inspecting
  `/openapi.json` and every response schema; the only place the *string*//
  `"GROK_API_KEY"` appears is inside a diagnostic label like
  `"template_fallback (GROK_API_KEY not set)"`, which names the environment
  variable for debugging purposes and never contains its value.

### Frontend

New **AI Assistant** card (added after Reports, nothing else moved/changed):
chat window with user/assistant bubbles, a bounded document-ID filter, a
text input + Send button (Enter also sends), a disabled-while-pending send
button with a "Thinking…" indicator to prevent duplicate submissions,
source references shown in a collapsible `<details>` block under any answer
that used evidence, a warnings line when retrieval found nothing, a "Clear
Chat" button, and 7 suggested-question chips drawn only from questions this
dataset can actually answer (matches the Step 5 spec's example list).

### Testing performed

**Fully tested, with real data:**
- `classify_question()` against all of Step 5's example questions (12
  cases) — every one routed as expected (structured / evidence).
- `_candidate_phrases()` / mine-name detection logic — correctly isolates
  "GEVRA OC", "KUSMUNDA", "Ningah Colliery" etc. from natural-language
  questions.
- `answer_question()` template-fallback path exercised against **real
  Step 4 analytics output** for all 6 of Step 5's required structured test
  questions — every answer matched the actual computed numbers exactly:
  - "What is the total production?" → 773.47 MT across 459 mines ✓.
  - "Which state has the highest production?" → Chhattisgarh, 157.241 MT ✓.
  - "Which mine has the highest production?" → GEVRA OC, 45.0 MT ✓.
  - "How much production does GEVRA OC have?" → 45.0 MT, SECL, VERIFIED ✓.
  - "How many records are verified?" → 399/57/3/0 split, matches Step 3 ✓.
  - "How many mines are government owned?" → Government 366 / State Govt 75 / Private 18 ✓.
- Evidence retrieval formatting (`sources_from_evidence`) tested against
  **real evidence built from GEVRA OC's actual raw extracted data** — 14
  real sheet/row/column/raw-value entries correctly formatted, real source
  URL preserved. The "no evidence found" case was also tested and
  responds honestly rather than fabricating a source.
- Full backend syntax check (`ast.parse` on all 6 Python files) and a
  clean server boot with `rag.py` wired in.
- **Full regression**: every Step 1–4 endpoint
  (`/api/mines`, `/api/mines/{id}/evidence`, `/api/validation/summary`,
  `/api/documents`, `/api/analytics/full`, `/api/reports`) still responds
  correctly after adding Step 5 — confirmed via a live request loop
  against the running server, all returning the expected `503` without a
  database. All 18 routes (17 from Steps 1–4 + 1 new) confirmed registered
  via `/openapi.json`.
- `/api/assistant/ask` error handling tested live: empty question → `400`
  with a clear message (checked before touching the database); missing
  `question` field → `422`; no database configured → `503`.
- **Security check**: confirmed no API key or token string appears
  anywhere in `index.html`, and the only appearance of the string
  `"GROK_API_KEY"` in any API response is a diagnostic label, never a value.
- Frontend `<script>` block (28KB, includes the new chat UI) syntax-checked
  with `node --check` — no errors.

**Not tested — dependent on external access this sandbox doesn't have:**
- **Live Neon/PostgreSQL**: `/api/assistant/ask`'s actual SQL (the ILIKE
  mine-name lookup, the evidence JOIN query, and `analytics.py`'s
  aggregate queries it calls) has not been executed against a real
  Postgres connection — `neon.tech` is unreachable here. The SQL was
  written and reviewed carefully, and its equivalent logic was verified
  against real data in Python (see above), but the actual database round
  trip is unverified.
- **Live Grok call**: `api.x.ai` is unreachable here, so `answer_question()`'s
  actual HTTP request/response handling has not been exercised — only the
  fallback branch (triggered by `GROK_API_KEY` being unset) was tested. If
  the live call fails for any reason once you have real credentials, the
  assistant still answers via the template fallback rather than fabricating
  or crashing — but the success path itself (parsing a real Grok response)
  is unverified.

Once you have a live `DATABASE_URL` (and optionally `GROK_API_KEY`), the
end-to-end test is:
```bash
curl -X POST http://localhost:8000/api/assistant/ask \
  -H "Content-Type: application/json" \
  -d '{"question": "Which mine has the highest production?"}'
```
or just use the AI Assistant card in the UI after uploading and extracting
a document.

### Status: **COMPLETE WITH LIVE-INTEGRATION LIMITATIONS**

All Step 5 code is implemented, wired in, and tested as far as this
sandbox allows (logic, routing, fallback answer generation, evidence
formatting, security, and full regression against Steps 1–4 all verified
with real data). The live Postgres and live Grok round trips remain
unverified pending actual `DATABASE_URL` / `GROK_API_KEY` access.

## Step 7 — Comprehensive Testing (added)

No new features. One real bug found and fixed: `requirements.txt` was
missing `Pillow` and `pytesseract`, which `image_processing.py` (Step 6)
depends on — a fresh `pip install -r requirements.txt` would have failed
to import them. Both are now declared.

A full test pass was run across source-backed AI answers, image input, the
speech-to-text implementation, and regression across every prior step.
Frontend JavaScript was genuinely executed (not just read) against a
`jsdom`-simulated DOM to verify rendering logic, and the Web Speech API was
exercised via a mocked `SpeechRecognition` constructor. Full detailed
results are in the test report delivered alongside this update; summary:

- **Source-backed AI**: 7/7 PASS — numerical answers matched real Step 4/5
  data exactly, evidence retrieval traced to real sheet/row/column values,
  and the "no source available" case was confirmed to state that plainly
  while still returning the verified database answer, with the frontend
  correctly rendering all three states (available / unavailable /
  not-applicable) without ever fabricating a source.
- **Image**: 10/10 PASS — PNG, JPG, valid image w/ readable text, corrupted
  file, wrong-extension-but-invalid-bytes, unsupported format (BMP), empty
  upload, oversized upload, no-readable-text, and a simulated OCR crash all
  behaved correctly (400s with clear messages for every invalid case; OCR
  failures degrade gracefully without fabricating text).
- **Speech**: 6/6 PASS **at the JS-logic level** (unsupported browser,
  successful transcription landing in the editable input without
  auto-submitting, editing before send, permission denied, recognition
  error, manual stop) — all simulated via a mocked `SpeechRecognition`
  constructor in `jsdom`, since this sandbox has no real browser or
  microphone. **Actual browser/microphone behavior remains UNVERIFIED.**
- **Regression**: 13/13 PASS — every Step 1–6 endpoint still responds
  correctly, extraction/validation reproduced the exact same 459-row,
  399/57/3/0 split as the original Step 3 baseline (zero drift), and
  report generation (charts, narrative, PDF, DOCX) regenerated correctly.

**Still blocked by environment** (as in every prior step): live
Neon/PostgreSQL and live Grok/`api.x.ai` access are unavailable in this
sandbox, so the real database round-trip and the real Grok HTTP call
remain unverified — only their fallback/error paths could be tested.

See the delivered test report for the full test-by-test breakdown (Sections
A–H) and exact pass/fail/unverified status for every case.

## Step 8 — Authentication, Themes, UI Polish (added)

One new file: **`auth.py`** — password hashing/verification, email/password
validation, session token generation and expiry logic. `app.py` gained a
`users`/`sessions` schema (additive), 4 auth endpoints, and a
`require_auth` dependency applied to every data/application endpoint.
`index.html` gained a navbar, a login/signup gate in front of the whole
app, and a dark/light theme system. Nothing from Steps 1–7's actual logic
was rewritten.

### Authentication

- **Passwords**: hashed with `bcrypt` (random per-password salt built in).
  Plaintext passwords are never stored, logged, or returned in any
  response — `UserOut` only ever exposes `id`/`email`/`created_at`.
- **Sessions**: opaque random tokens (`secrets.token_urlsafe(32)`) stored
  server-side in a `sessions` table with a real `expires_at` (24h). This is
  deliberately *not* a JWT — logout and expiration are enforced by deleting
  the actual database row, not by trusting an unexpired signed token.
- **Cookie**: `HttpOnly`, `SameSite=Lax`, and `Secure` set dynamically from
  the request's actual scheme (`True` over https, `False` over local http)
  — never hardcoded either way. The frontend never reads, stores, or
  transmits the token itself; it only ever relies on the browser sending
  the cookie automatically.
- **Endpoints**: `POST /api/auth/signup`, `POST /api/auth/login` (generic
  "Invalid email or password" on any failure — never reveals whether the
  email exists), `POST /api/auth/logout` (deletes the session row),
  `GET /api/auth/me` (returns the current user or `null`, never errors,
  used by the frontend on load to decide whether to show the app or the
  login gate).

### Authorization

Every data/application endpoint now requires `Depends(require_auth)`:
`/api/upload`, `/api/documents*`, `/api/mines*`, `/api/validation/summary`,
`/api/analytics/full`, `/api/reports*`, `/api/assistant/*`. An
unauthenticated request gets a real `401` — there is no way to reach mine
data, evidence, analytics, reports, or the AI assistant by calling the API
directly without a valid session. `/`, `/api/health`, and the four
`/api/auth/*` endpoints remain public (a health check and the login/signup
flow have to be reachable before a session exists).

### Session handling / CSRF

`SameSite=Lax` is the primary CSRF mitigation: the browser will not attach
the session cookie to a cross-site POST, so a request forged from another
origin fails before it ever reaches a protected endpoint. No separate CSRF
token was added on top of this, since every mutating request in this
single-page app is a same-origin `fetch()` call, not a cross-site form
post — adding a second layer would be complexity without a matching threat
model here. `/api/auth/me` never raises on a missing/invalid session; it
simply returns `null`, so probing it can't be used to distinguish
"session expired" from "never logged in."

### Dark / light mode

All colors are CSS custom properties on `:root`/`[data-theme]`, switched by
setting `data-theme="light"|"dark"` on `<html>`. An inline script at the
very top of `<head>` (before the stylesheet paints) reads
`localStorage.getItem('cmpdi-theme')` and applies it immediately, so there
is no flash of the wrong theme on refresh. Every existing surface — navbar,
cards, tables, forms, the Data Explorer, Evidence modal, Analytics bar
charts, Reports, the AI Assistant chat, upload/validation UI, empty states,
and error states — was already built on top of `var(--bg)`/`var(--panel)`/
`var(--text)`/`var(--muted)`/`var(--border)`/`var(--accent)` etc. rather
than hardcoded hex colors; the remaining hardcoded colors found during this
step (`#0a1017`, `#06231a`, used in a dozen places) were swept to
`var(--input-bg)`/`var(--accent-contrast)` so they also respond to the
theme. Two status colors (`WARNING`'s orange, a button's white text)
were deliberately left fixed since they're semantic/contrast colors that
shouldn't shift with the theme.

### UI polish

Added a navbar (app mark, title, theme toggle, user email, logout), a
centered login/signup card with inline validation errors, consistent
button/input focus states (`:focus-visible` ring using the theme's accent
color), subtle hover/disabled states on buttons, and a `.skeleton` loading
style available for future loading placeholders. The existing card-based
layout, Data Explorer, chat UI, and chart visualizations were kept as-is
per the "no unnecessary rewrite" constraint — this was a styling and
structural-wrapper pass, not a redesign.

### Security review performed

- ✅ Passwords never stored in plaintext (bcrypt hash only).
- ✅ No secret (password, hash, `GROK_API_KEY`, `DATABASE_URL`) appears in
  any API response — confirmed by inspecting `/openapi.json` and grepping
  every response model.
- ✅ `GROK_API_KEY` and `DATABASE_URL` remain backend-only — confirmed no
  reference to either exists in `index.html` (the one string match for
  `DATABASE_URL` is a pre-existing user-facing setup hint, not a value).
- ✅ Every protected endpoint enforces authentication via `require_auth` —
  confirmed live for all of `/api/mines`, `/api/mines/count`,
  `/api/mines/{id}/evidence`, `/api/validation/summary`, `/api/documents*`,
  `/api/analytics/full`, `/api/reports*`, `/api/upload`,
  `/api/documents/{id}/extract`, `/api/assistant/*`.
- ✅ User input validated: email format, password length (8–200 chars),
  malformed/missing JSON fields rejected with `422` by FastAPI/Pydantic
  before reaching any handler.
- ✅ Authentication failures don't leak details: login always returns the
  same generic message regardless of whether the email exists or the
  password was wrong; a malformed/expired session hash never surfaces
  driver-level or database error text to the client.

### Bugs found and fixed during this step

**`auth.is_expired()` crashed on a string timestamp.** Discovered while
testing session expiry against a real SQLAlchemy engine (SQLite, since
Neon is unreachable here — see Testing below): `datetime.fromisoformat()`
handling was missing, so a driver that returns `expires_at` as a string
instead of a native `datetime` would crash session validation entirely.
Fixed by making `is_expired()` accept either a `datetime` or an
ISO-format string. This is defensive — psycopg2/Postgres normally returns
native `datetime` objects — but it's a real robustness gap that testing
caught and is now closed.

### Testing performed

- `auth.py` unit-tested directly: password hashing/verification (correct
  password accepted, wrong password rejected, hash never equals plaintext,
  two hashes of the same password differ), email validation (valid/invalid/
  empty/whitespace-and-case-normalized), password length validation
  (too short / too long / valid), session token uniqueness, expiry logic.
- **Real end-to-end auth flow tested against an actual SQLAlchemy engine**
  (SQLite in-memory, since live Postgres is unreachable here): signup
  (duplicate-email detection, real `INSERT ... RETURNING`), login (correct/
  wrong password against a real stored hash), and — critically — `app.py`'s
  *actual* `_get_session_user()` function called directly against real
  session rows: valid session resolves to the right user, invalid token
  resolves to `None`, an expired session is rejected **and its row is
  deleted**, and a session is correctly invalidated after logout. This is
  the same level of rigor used for Step 2–5's testing (real logic, real
  data, substitute-engine where the live database is unavailable) and is
  what caught the bug above.
- Frontend auth/theme logic **genuinely executed** (not just read) via
  `jsdom`: theme toggle flips `data-theme`, updates the icon, and persists
  to `localStorage`; auth-mode switching updates all the login/signup
  labels; empty-field submission is blocked client-side with no network
  call; a full `onAuthenticated()` → `logout()` cycle correctly
  shows/hides the auth gate vs. app content and sets/clears the navbar.
- Full backend syntax check across all 8 Python files; clean server boot.
- **Full regression, live against the running server**: every endpoint
  from Steps 1–7 (`/`, `/api/health`, `/api/mines*`, `/api/validation/
  summary`, `/api/documents*`, `/api/analytics/full`, `/api/reports*`,
  `/api/upload`, `/api/documents/{id}/extract`, `/api/assistant/ask`,
  `/api/assistant/ask-image`) still responds with its expected status —
  no regressions. All 23 routes (19 previous + 4 new auth routes)
  confirmed via `/openapi.json`.
- Confirmed `/api/health` remains public (no auth required) and
  `/api/auth/me` never errors on a missing session (returns `null`, `200`).

### Not tested — dependent on external access this sandbox doesn't have

- **Live Neon/PostgreSQL**: the actual `users`/`sessions` DDL and the
  live signup/login/logout HTTP round trip have not been executed against
  real Postgres — confirmed instead against a real (if substitute) SQL
  engine, as described above. Every DB-dependent endpoint correctly
  returns `503` without a configured `DATABASE_URL`.
- **Cookie behavior in a real browser** (actual `Secure`/`SameSite`
  enforcement, `HttpOnly` inaccessibility from JS) — reviewed against the
  FastAPI/Starlette cookie API and standard browser behavior, but not
  observed in an actual browser session, since this sandbox has none.
- **Theme/UI visuals in a real browser** — CSS was written correctly and
  the underlying JS logic was verified via `jsdom`, but actual rendered
  appearance (contrast, spacing, responsive breakpoints) has not been
  visually confirmed in a real browser window.

## Step 9 — Security, Full Testing & Accuracy Review

A complete security and accuracy pass over Steps 1–8. Two real vulnerabilities were found and fixed (not just reviewed); everything else was inspected and either confirmed safe or already covered by prior steps' testing.

### Security findings & fixes

| # | Finding | Severity | Fix |
|---|---|---|---|
| 1 | **Path traversal in `/api/upload`** — `file.filename` was used unsanitized as a path component (`f"{uuid}_{file.filename}"`). A filename like `../../../../tmp/evil.xlsx` resolves outside `UPLOAD_DIR` (**demonstrated concretely**: `os.path.normpath` confirmed the escape before the fix). | **High** | `os.path.basename()` strips any directory component, plus a `os.path.commonpath` check confirms the resolved path is still inside `UPLOAD_DIR` before any file is written. **Re-verified**: 4 real attack payloads (`../`, `..\`, `/etc/passwd`, `....//`) all now correctly contained; 2 legitimate filenames (including one matching the real dataset's actual name) still work. |
| 2 | **No upload size limit** — `/api/upload` streamed the entire file to disk with no cap, unlike the Step 6 image endpoint's 8MB limit. | **Medium** | Added a 25MB cap enforced while streaming (1MB chunks), with the partial file deleted on overflow. **Verified**: a 10MB upload completes normally; a 30MB upload is rejected mid-stream and the partial file is cleaned up. |
| 3 | Wildcard CORS (`allow_origins=["*"]`) served no purpose for a same-origin single-page app and needlessly exposed `/api/auth/login` to cross-origin credential-stuffing attempts from any page. | Low | CORS is now **off by default** (same-origin only, matching how the app has actually been built and tested) and only enabled via an explicit `CORS_ALLOWED_ORIGINS` allow-list env var for an optional split deployment — see Step 10. |

**Reviewed and confirmed safe (no fix needed):**
- **SQL injection**: audited every `text(f"...")` construction in `app.py` and `rag.py`. All user-supplied *values* are bound parameters (`:param`); the only f-string interpolation is either a hardcoded clause template, a whitelisted `sort_by`/`sort_dir` (checked against `SORTABLE_COLUMNS` / normalized to `ASC`/`DESC` before use), or generated placeholder *names* (`:w0`, `:w1`, ...) — never raw user text reaching the SQL string itself.
- **Passwords**: bcrypt-hashed only, confirmed never returned in any response (Step 8).
- **`GROK_API_KEY`/`DATABASE_URL`**: confirmed backend-only, no frontend reference beyond a pre-existing user-facing setup hint string (Step 8).
- **Image upload validation**: real bytes validated (not extension-trusted), size-capped, format-checked (Step 6/7).
- **Authorization**: every data/application endpoint requires `Depends(require_auth)`, confirmed live (Step 8).

### Numerical accuracy — cross-consistency

Total production, top state, top mine, owner totals, and validation counts are computed **exactly once**, in `analytics.py`, and that same dict is reused by the Analytics API, the frontend dashboard, the report generator (PDF/DOCX), and the AI Assistant's structured retrieval (`rag.py` calls `analytics.py`'s functions directly rather than recalculating). There is no second, independent calculation path that could drift — confirmed by inspecting every call site. Grok is never given a number to compute; it only ever phrases numbers already in this shared dict, with the fallback template answer path using the identical values.

### Ground-truth accuracy sample

**Methodology**: 14 rows were read directly from the source `.xlsx` with `openpyxl` (bypassing the extraction pipeline entirely) to serve as an independent ground truth. The sample was deliberately **stratified**, not random: the first 10 data rows, plus 4 known edge cases from prior testing (3 rows sharing the "Urimari" duplicate name, and the row where `NTPC`'s owner-full-name cell contains a literal `#N/A` — a broken formula result in the source spreadsheet, not a Claude-introduced error). Each ground-truth row was then compared field-by-field against `extract_document()`'s actual output for that same row.

- **Direct-value fields** (`sl_no`, `state`, `district`, `mine_name`, `production`, `coal/lignite`, `ownership`, `mine_type`, `latitude`, `longitude`) — considered a match if the extracted value equals the raw value after the *documented* whitespace/newline cleanup (never a value change). **140/140 fields matched (100%).**
- **Transformation fields** (`owner_full`, `accuracy`) — considered correct if: a valid raw owner name is preserved verbatim, OR a missing/`#N/A` raw owner name is correctly flagged `owner_full_inferred=True` with a non-empty fallback; and if `accuracy_type`/`accuracy_note` correctly reflect the raw "Exact"/"Approximate: ..." text, including the no-colon variants. **28/28 checks correct (100%)**, including the `#N/A` case resolving correctly.
- **Combined: 168/168 field-level checks matched across 14 rows (100% on this sample).**

**Limitations, stated plainly**: 14 rows is roughly 3% of the 459-row dataset — enough to catch and confirm several distinct edge cases (multi-line names, both accuracy-note formats, a broken-formula owner cell, a duplicate), but **not** a statistically powered sample of the full dataset, and this number is **not** extrapolated into a system-wide accuracy claim. It's reported as exactly what it is: 100% match on this specific 14-row/168-check sample, with the sample composition documented above so the result is falsifiable and reproducible.

### Fabrication check

Audited every place a number, source, or citation reaches the user:
- **AI numerical answers**: sourced from `analytics.py`/`rag.py`'s structured retrieval, never Grok-calculated (Step 5/6 testing).
- **Source references** (sheet/row/column/URL): sourced from the real `evidence` table, built from `raw_data` captured at extraction time — never fabricated (Step 3/5/7 testing). The app never claims a PDF-style "page number" for an XLSX source, since Excel has no such concept.
- **Report/dashboard/chart numbers**: all trace back to the same single `analytics.py` dict (see "cross-consistency" above) — no separate hardcoded figures found anywhere in `app.py`, `rag.py`, or `report_generator.py`.
- **"Topics" and "Word Cloud"**: **these features do not exist in this project.** They appear in Step 9's test checklist but were never part of any of Steps 1–8's actual scope or implementation. There is nothing to audit for fabrication because there is no such feature — flagging this explicitly rather than inventing a result for a feature that isn't there.
- **"Dashboard"**: there is no separate page called "Dashboard" — the Analytics card *is* the dashboard in this single-page app, and its numbers were already covered above.

### Regression

Full live-server regression re-run after the two security fixes: all 23 routes still registered, every endpoint's expected status code (`200`/`401`/`503` as appropriate) confirmed unchanged, zero pyflakes warnings across all 8 Python files (two real unused-import/lint issues found and cleaned up: `StaticFiles` and `shutil` were no longer used after the upload rewrite; a stray f-string in `report_generator.py` had no placeholders).

### LOCAL TESTED vs LIVE ENVIRONMENT NOT TESTED

**LOCAL TESTED** (real code, real data, real logic — this sandbox): SQL injection audit, path-traversal fix (4 real attack payloads), size-limit fix (real chunked-write simulation), ground-truth accuracy sample (real file, real extraction), fabrication audit, full regression, syntax/lint cleanup.

**LIVE ENVIRONMENT NOT TESTED / BLOCKED** (unchanged from every prior step): live Neon/PostgreSQL round-trip, live Grok API call, live browser session (cookie/theme/speech behavior). See Step 10 below for exactly what remains unverified going into deployment.

---

## Step 10 — Final Production Readiness (GitHub / Neon / Render / Netlify)

This is the final step. No new features were added — this is deployment configuration, documentation, and a last stability/security pass.

### Final stability check performed

- Full syntax check (`ast.parse`) on all 8 Python files — clean.
- `pyflakes` run on all 8 files — clean (after removing 2 now-unused imports and 1 lint warning found during this pass).
- Frontend `<script>` block syntax-checked with `node --check` — clean (carried over from Step 8, unchanged since).
- Server boots cleanly; all 23 routes confirmed via `/openapi.json`.
- Confirmed the frontend already uses `window.location.origin` for its API base URL (set once, in Step 1) — **no hardcoded `localhost` anywhere**, so the same `index.html` works unmodified in local dev and in production behind Render.

### Final architecture note — same-origin by design

This project's actual architecture is **one FastAPI app that serves its own `index.html`** (`GET /` → `FileResponse("index.html")`), not a separately-built frontend. Every step's testing — including all of Steps 1–9 — was done against this same-origin setup. That makes the **simplest, tested, recommended deployment a single Render service** (FastAPI serves both the API and the page). A separate Netlify deployment is documented below as an *optional* path since Step 10 asks for it, but it introduces a real, disclosed limitation: the Step 8 session cookie is `SameSite=Lax`, which modern browsers will not attach to a genuinely cross-site request. `CORS_ALLOWED_ORIGINS` (new in this step) lets you allow-list a Netlify origin so *unauthenticated* endpoints and preflight requests behave correctly, but **authenticated cross-origin login has not been implemented or tested** — this is stated plainly rather than silently shipping a broken cross-origin login or quietly rewriting the cookie/session design (which Step 10 explicitly says not to do). If a fully split deployment is required later, the follow-up work is switching the cookie to `SameSite=None; Secure` and re-testing the auth flow cross-origin — out of scope for this step.

### Environment variables

| Variable | Required | Read by | Purpose |
|---|---|---|---|
| `DATABASE_URL` | Yes | `app.py` (`create_engine`) | Neon Postgres connection string |
| `GROK_API_KEY` (or `XAI_API_KEY`) | No | `rag.py`, `report_generator.py` | Enables live Grok phrasing; falls back to a deterministic template if unset — the app never fails without it |
| `CORS_ALLOWED_ORIGINS` | No | `app.py` | Comma-separated allow-list for an optional split frontend deployment (see above); same-origin deployment needs nothing here |
| `PORT` | No (Render sets it) | `app.py`'s `if __name__` block | Local dev only; Render's start command reads `$PORT` from the shell directly |

`.env.example` now lists all four with placeholder-only values (no real credentials), and `.env` remains in `.gitignore`.

### GitHub checklist

```bash
git init
git add .
git commit -m "CMPDI/CIL AI Document Intelligence Platform"
git branch -M main
git remote add origin https://github.com/<your-username>/<your-repo>.git
git push -u origin main
```

- ✅ `.gitignore` covers `.env`, `.venv/`/`venv/`/`env/`, `__pycache__/`, `*.pyc`, and the app's own runtime-generated directories (`uploads/`, `outputs/`, `reports/`) and logs — updated this step to add `.venv/`, `env/`, `reports/`, and log files, which were missing before.
- ✅ Repository-wide secret scan performed (`grep` for API-key-shaped strings, Postgres connection strings with real-looking credentials, AWS-style keys) across every `.py`/`.html`/`.md`/`.txt` file — **no real secrets found**. The only connection-string-shaped text is the obviously-placeholder `user:password@ep-xxxxx...` example, which has since been replaced with a bare `DATABASE_URL=` in `.env.example` per Step 10's explicit instruction not to use even a placeholder-formatted example.

### Neon checklist

1. Create a Neon project at neon.tech, create a database.
2. Copy the connection string from **Connection Details** (must include `?sslmode=require`).
3. Set it as `DATABASE_URL` in Render's environment variables (see below) — never commit it.
4. **Schema initialization is automatic**: `app.py`'s `init_db()` runs on every app startup (`lifespan` hook) and issues `CREATE TABLE IF NOT EXISTS` / `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` for every table (`mines`, `documents`, `evidence`, `reports`, `users`, `sessions`) — there is no separate migration step or CLI command to run; simply setting `DATABASE_URL` and starting the app is sufficient, and re-deploys are safe to run against an existing database (every statement is idempotent).
5. The deployed backend connects via SQLAlchemy + `psycopg2-binary`, exactly as tested locally — no code difference between environments.

### Render checklist

1. Create a new **Web Service**, connect the GitHub repo.
2. **Runtime**: Python 3.
3. **Build command**: `pip install -r requirements.txt`
4. **Start command** (matches this project's actual entrypoint, `app.py` → `app = FastAPI(...)`):
   ```
   uvicorn app:app --host 0.0.0.0 --port $PORT
   ```
5. **Root directory**: the repository root — `app.py` and `index.html` must be in the same working directory Render runs the start command from, since `serve_index()` opens `index.html` via a relative path.
6. **Environment variables**: set `DATABASE_URL` (required) and `GROK_API_KEY` (optional) in Render's dashboard, never in code.
7. Once deployed, Render's HTTPS termination means the session cookie's `Secure` flag (set dynamically from `request.url.scheme`) will correctly be `True` in production without any code change.

### Netlify checklist (optional — see the architecture note above)

1. If deploying the frontend separately: create a new Netlify site, connect the repo (or drag-and-drop `index.html`), with `index.html` as the publish directory root — there is no build step, it's a single static file.
2. Set `CORS_ALLOWED_ORIGINS` on the Render backend to the exact Netlify URL (e.g. `https://your-app.netlify.app`).
3. The frontend needs no configuration change to point at Render — it already calls `window.location.origin`, which on Netlify would resolve to the Netlify domain, **not** the Render backend. For a genuine split deployment, `BASE_URL` in `index.html` would need to become a configured Render URL instead of `window.location.origin` — **this change has not been made**, since the currently-tested and recommended path is the same-origin Render-only deployment described above. Making this change is a small, mechanical follow-up if a true split deployment is required.
4. No secret is ever placed in the Netlify-hosted frontend — it only ever talks to the public Render URL.

### CORS in production

No wildcard `*` CORS is used. Default is same-origin only (no CORS middleware at all) for the recommended Render-only deployment. `CORS_ALLOWED_ORIGINS` provides an explicit allow-list for the optional split-deployment case, with `allow_credentials=True` so a cookie *could* be sent — subject to the `SameSite=Lax` limitation disclosed above.

### Production security checklist

| Item | Status |
|---|---|
| No secrets in Git | ✅ Verified — repo-wide scan, `.env` gitignored |
| No secrets in frontend | ✅ Verified — grepped `index.html`, only a setup-hint string mentions `DATABASE_URL` by name |
| `GROK_API_KEY` backend-only | ✅ Verified — `os.getenv()` only in `rag.py`/`report_generator.py`, never in a response |
| `DATABASE_URL` backend-only | ✅ Verified — same |
| `.env` ignored | ✅ Verified |
| Passwords securely hashed | ✅ bcrypt, verified Step 8 |
| Authentication functioning | ✅ Verified against a real SQL engine (Step 8); live Postgres round-trip **not tested** (blocked) |
| Protected endpoints require auth | ✅ Verified live for every data/application endpoint |
| SQL parameterized | ✅ Audited this step, confirmed clean |
| Upload validation present | ✅ Extension + real-byte validation (images), extension check (documents) |
| File-size limits present | ✅ **Added this step** — 25MB documents, 8MB images (Step 6) |
| Path traversal protection | ✅ **Added this step** — verified against 4 real attack payloads |
| CORS appropriately configured | ✅ Same-origin by default, explicit allow-list only when needed |
| Production error handling | ✅ Generic login error message; no stack traces or driver internals returned to the client |
| No debug credentials / hardcoded secrets | ✅ Verified |

### Final project structure

```
cmpdi-mvp/
├── app.py                  # FastAPI entrypoint — all API routes, DB schema/init
├── auth.py                 # Password hashing, session tokens, validation
├── extractors.py           # XLSX (+ stub PDF/DOCX/image) extraction pipeline
├── validation.py           # Deterministic data-quality rule engine
├── analytics.py            # All numerical calculations (SQL) — source of truth
├── report_generator.py     # Charts, Grok narrative, PDF/DOCX assembly
├── rag.py                  # AI Assistant: routing, retrieval, Grok answer generation
├── image_processing.py     # Image validation + OCR for the AI Assistant
├── index.html              # Entire frontend — single file, no build step
├── requirements.txt        # Python dependencies
├── .env.example            # Placeholder-only environment variable template
├── .gitignore
├── README.md                # This file
└── STEP7_TEST_REPORT.md    # Detailed Step 7 test report
```

*(`uploads/`, `outputs/`, `reports/` are created at runtime and gitignored — not part of the committed structure.)*

### Full demo checklist

```
LOGIN → DASHBOARD (Analytics card) → UPLOAD → EXTRACTION → VALIDATION →
EVIDENCE → DATA EXPLORER → ANALYTICS → GENERATE REPORT → AI ASSISTANT →
SOURCE-BACKED ANSWER → IMAGE QUESTION → VOICE QUESTION
```

Every stage above **except** live Neon/Grok connectivity and real-browser speech has been tested with real logic and real data across Steps 1–9, as documented in each step's section of this README and in `STEP7_TEST_REPORT.md`. **"Topics" and "Word Cloud"**, listed in Step 9/10's suggested demo flow, are **not implemented** in this project — omitted from the checklist above rather than falsely claimed.

### Known limitations

- Live Neon/PostgreSQL and live Grok API calls have never been executed in this development sandbox (no network access to either) — only their fallback/error paths are tested. This is the single biggest thing to verify first after deployment.
- Speech-to-text and real browser theme/cookie behavior are verified only via simulated/logic-level testing (`jsdom` + mocked Web Speech API), never a real browser.
- Cross-origin (Netlify + Render split) authenticated login is not implemented — same-origin deployment is the tested, recommended path.
- PDF, scanned-PDF, DOCX, and image extraction (as *document sources*, distinct from the AI Assistant's image-question feature) remain architecture-only stubs, unchanged since Step 2 — only XLSX extraction is fully implemented.
- No automated CI/test suite is wired up; all testing in this project was done manually/interactively during development.
- No rate limiting on `/api/auth/login` — a determinate attacker could still attempt many password guesses per unit time; CORS tightening in this step reduces but doesn't eliminate that surface.

### Future improvements

- Implement PDF/DOCX/scanned-image extraction to complete the Step 2 architecture.
- Add rate limiting / login throttling on `/api/auth/*`.
- If a true split Netlify/Render deployment is required, migrate the session cookie to `SameSite=None; Secure` and make `BASE_URL` in `index.html` configurable rather than derived from `window.location.origin`.
- Wire up an automated test suite (the manual tests performed throughout this project's development would translate directly into `pytest` cases).
- Expand the ground-truth accuracy sample beyond 14 rows for a statistically stronger accuracy measurement.
