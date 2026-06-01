# Document Attachment Extraction — Phase 1

**Date:** 2026-05-18
**Scope:** NimoOS-AI agent — in-request, threadpool-isolated extraction of PDF / DOCX / XLSX / PPTX uploads to Markdown sidecars, exposed to the agent via the existing `read_attachment` tool.

## Goal

Today the agent treats anything that isn't a UTF-8 text whitelist match (or image / video / audio) as `binary`, meaning the model can only see filename + mime + size. Users routinely drop office documents into chats expecting the agent to read them.

Phase 1 adds first-class support for the four formats that cover the vast majority of office-document uploads, using lightweight pure-Python extractors. Phase 2 (out of scope here) will revisit the same surface with [docling](https://github.com/DS4SD/docling) for higher-fidelity extraction (tables, layout, OCR).

Non-goals for Phase 1:

- Legacy binary formats (`.doc` / `.xls` / `.ppt`) — fall back to `binary`.
- OCR of scanned PDFs — pypdf returns empty text for image-only pages; we surface that as `error="empty_scanned"` so the model can explain the situation to the user.
- Background / queued extraction. Phase 1 still completes extraction inside the upload request, but runs it on a worker thread with a hard timeout so it cannot block the FastAPI event loop or hang indefinitely.
- Re-extracting old uploads — applies only to new uploads after deploy.

## Architecture

A new attachment `kind`, `"document"`, joins the existing `image / text / video / audio / binary`. On upload, files with one of the four supported extensions are speculatively classified as `document`, then `handle_upload()` runs an extractor on a worker thread with a hard timeout. If extraction produces text, a Markdown sidecar is written next to the original. If extraction fails (parse error, scanned PDF, timeout, zip bomb, password-protected, sidecar write error), the row **stays `kind=document`** with `meta.extract_error` set so the model can explain the situation to the user instead of generically claiming "binary file, can't read."

The only case that downgrades all the way to `kind=binary` is `not_installed` — an extractor library wasn't packaged. That is an infra problem we should fix in the build, not something to surface to the user.

```
┌────────────────────────────────────────────────────────────────┐
│  POST /v1/sessions/{id}/attachments  (async def endpoint)      │
└────────────────┬───────────────────────────────────────────────┘
                 │ stream_to_disk → .part file
                 ▼
        ┌────────────────────┐
        │  classify(path,    │  ext ∈ {pdf,docx,xlsx,pptx,xlsm}
        │  original_name)    │     → ("application/...", "document")
        └────────┬───────────┘
                 │
                 ▼
        ┌────────────────────────────────────────────┐
        │  handle_upload (async)                     │
        │                                            │
        │  if kind == "document":                    │
        │    result = await asyncio.wait_for(        │
        │        asyncio.to_thread(                  │
        │            extract.extract_to_markdown,    │
        │            part_path, ext,                 │
        │            max_chars=max_doc_chars,        │
        │            max_uncompressed=max_uncompr),  │
        │        timeout=max_extract_seconds)        │
        │                                            │
        │    on TimeoutError → result =              │
        │       {"ok": False, "error": "timeout"}    │
        │                                            │
        │    if result.ok:                           │
        │       write sidecar .md (best-effort)      │
        │       meta = {sidecar?, extractor, pages,  │
        │               chars, truncated}            │
        │    else if "not_installed":                │
        │       kind = "binary"                      │
        │       meta = {extract_error}               │
        │    else:                                   │
        │       # keep kind = "document"             │
        │       meta = {extract_error}               │
        └────────┬───────────────────────────────────┘
                 │
                 ▼
        rename .part → final, INSERT row
```

At read time, `read_attachment` gains a `document` branch:

- If `meta.sidecar` exists and the file is on disk → return Markdown content (truncated to `MAX_CHARS_VAR`) + extractor + pages.
- If `meta.extract_error` is set → return `{kind: "document", error: "<reason>", filename, mime}` so the model can tell the user *why* (scanned PDF, password-protected, too complex, etc.).

## Components

### 1. `attachments/kind.py` — speculative classification by extension

Add a `DOCUMENT_EXT_MAP` and check it after image/video/audio but before the text whitelist branch:

```python
DOCUMENT_EXT_MAP = {
    "pdf":  "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "xlsm": "application/vnd.ms-excel.sheet.macroEnabled.12",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}
```

`classify()` returns `(DOCUMENT_EXT_MAP[ext], "document")` if the extension matches. It does **not** attempt to parse the file — that happens in `handle_upload()`. This keeps `classify()` cheap and side-effect-free, matching its current contract.

Classification at this stage is speculative. A `.pdf` extension on garbage bytes will be classified as `document`; the subsequent extraction failure is recorded as `meta.extract_error="parse_error"` and the row still ends up `kind=document` so the model can tell the user "this PDF appears corrupt." A `.pdf` that's actually 800 MB of compressed XML inside a fake header gets caught by the zip-bomb precheck (see §2).

### 2. `attachments/extract.py` — new module, format dispatch

Single public entry point:

```python
def extract_to_markdown(path: str, ext: str, *,
                       max_chars: int,
                       max_uncompressed_bytes: int) -> dict:
    """Extract a document to Markdown text. Pure / blocking / CPU bound —
    callers must run it in a worker thread.

    Returns on success:
      {"ok": True, "markdown": str, "pages": int|None,
       "chars": int, "truncated": bool, "extractor": str}
    Returns on failure:
      {"ok": False, "error": str}
    """
```

`error` values:

| value | meaning |
|---|---|
| `not_installed` | extractor library missing — packaging bug, hide from model (downgrade to binary) |
| `unsupported` | unknown `ext` reached the extractor — defensive, classify shouldn't route here |
| `zip_bomb` | uncompressed size of the ZIP-based document exceeds cap |
| `parse_error` | library raised on read (corrupt file, weird font, schema violation, etc.) |
| `encrypted` | password-protected (currently only meaningful for PDF; pypdf raises a distinct error type we map here) |
| `empty_scanned` | parser succeeded but extracted ≤ a handful of whitespace chars — typically a scanned PDF with no text layer |

The two failure modes from above this layer that also use this enum:

| value | meaning |
|---|---|
| `timeout` | extraction did not finish within `MaxDocumentExtractSeconds` (set by caller, not by `extract_to_markdown` itself) |
| `sidecar_write_failed` | extraction succeeded but writing the `.md` sidecar to disk failed (set by `handle_upload`) |

#### Per-format details

| ext | extractor | output strategy |
|-----|-----------|-----------------|
| `pdf` | `pypdf.PdfReader` | iterate pages, `page.extract_text()`, join with `\n\n---\n\n`. Track `pages = N`. |
| `docx` | `python-docx` | walk `doc.paragraphs` (preserve heading levels via `paragraph.style.name`) and `doc.tables` (pipe-table Markdown). `pages = None`. |
| `xlsx` / `xlsm` | `openpyxl.load_workbook(read_only=True, data_only=True)` | per sheet emit `## {sheet.title}` then a pipe-table of rows. `pages = sheet count`. |
| `pptx` | `python-pptx` | per slide emit `## Slide {n}` then text-frame text as bullets. `pages = slide count`. |

The extractor accumulates output incrementally and stops as soon as `len(buffer) >= max_chars` (sets `truncated=True`). Output that is empty or just whitespace after extraction yields `error="empty_scanned"`.

#### Defensive measures inside `extract_to_markdown`

These exist because pure-Python parsers on user-supplied input are a known footgun:

1. **Zip-bomb precheck (docx/xlsx/pptx only).** Before handing the file to the parser, open it with `zipfile.ZipFile(path)` and sum `zi.file_size` across `infolist()`. If the total exceeds `max_uncompressed_bytes` (default 200 MB; see §7), return `error="zip_bomb"` immediately. This catches the high-compression-ratio attack class without ever loading the XML into memory. PDFs aren't ZIPs so this step is skipped; PDF protection relies on the upload size cap, the timeout, and the broad exception handler below.

2. **Broad exception envelope.** Every per-format branch is wrapped in `try: ... except Exception as e: log.exception(...); return {"ok": False, "error": "parse_error"}`. `pypdf` in particular is known to raise unexpected exception types and even hit recursion limits on adversarial inputs — we cannot enumerate them; we have to catch them all. The matching `MaxDocumentExtractSeconds` timeout (enforced one level up in `handle_upload`) covers the pathological "infinite loop" case that no `except` catches in pure Python.

3. **pypdf encryption detection.** Before iterating pages, check `reader.is_encrypted`. If true and `reader.decrypt("")` fails, short-circuit with `error="encrypted"` instead of letting the per-page loop produce a `parse_error`.

4. **openpyxl `data_only=True` empty-cache fallback.** `data_only=True` returns cached calculation results, but workbooks generated by non-Excel tools (Python scripts, headless converters) often have no cached values — `cell.value` is `None` everywhere despite the file being non-empty. After reading a sheet, if every cell came back `None` *and* the sheet has rows according to `sheet.max_row`, we re-open that workbook with `data_only=False` and emit the formula text instead (`=SUM(A1:A10)`). It's strictly more useful than blank output for the model.

5. **ImportError guard at the dispatch level.** Each `import` of an optional library happens lazily inside its branch, wrapped in `try: import pypdf except ImportError: return {"ok": False, "error": "not_installed"}`. A missing dep degrades that one format only — service startup is unaffected.

### 3. `attachments/upload.py` — sidecar branch in `handle_upload()`

`handle_upload()` becomes `async def` so it can `await asyncio.to_thread(...)`. The endpoint in `main.py` already awaits it, so this is a local change.

After the existing image-size check and before the rename block, insert a `document` branch (parallel to the `if kind in ("video", "audio")` block):

```python
elif kind == "document":
    ext = os.path.splitext(original_name)[1].lstrip(".").lower()
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(
                extract.extract_to_markdown,
                part_path, ext,
                max_chars=max_doc_chars,
                max_uncompressed_bytes=max_doc_uncompressed,
            ),
            timeout=max_extract_seconds,
        )
    except asyncio.TimeoutError:
        result = {"ok": False, "error": "timeout"}

    if result["ok"]:
        meta = {
            "extractor": result["extractor"],
            "pages":     result["pages"],
            "chars":     result["chars"],
            "truncated": result["truncated"],
            # "sidecar" added after rename, see below
        }
    elif result["error"] == "not_installed":
        # packaging bug — hide from model, treat like binary
        kind = "binary"
        meta = {"extract_error": "not_installed"}
    else:
        # kind stays "document" so model can explain to the user
        meta = {"extract_error": result["error"]}
```

#### Ordering and failure handling

The full sequence becomes:

1. **Extract** (above). Outcome: either success-with-markdown, or error-string.
2. **Rename** `.part → final` (existing). Failure here is unchanged: 500.
3. **Write sidecar** `{final}.md`, only if step 1 produced markdown. If this write fails (disk full, EPERM), **swallow the error**: log it, set `meta = {"extract_error": "sidecar_write_failed"}`, keep `kind = "document"`, do **not** delete the final file, do **not** raise 500. The user's file is on disk; they're not penalized for our caching failure.
4. **INSERT row** (existing). If this fails (`sqlite3.Error`), we *do* delete final + sidecar and raise 500 — a half-committed DB state is worse than the user retrying.

Rationale for the asymmetry between step 3 and step 4: step 3's failure mode is recoverable per-attachment (the model still gets a useful error), step 4's failure mode breaks the entire attachment record.

`handle_upload()` gets three new keyword args, plumbed from config: `max_doc_chars`, `max_doc_uncompressed`, `max_extract_seconds`.

### 4. `skills/attachments.py` — new `document` branch in `read_attachment`

After the existing `text` branch, before the `video/audio` branch:

```python
if kind == "document":
    meta = json.loads(meta_json) if meta_json else {}

    # Failure path: tell the model why so it can tell the user.
    if "extract_error" in meta:
        return {
            "kind": "document",
            "filename": filename,
            "mime": mime,
            "error": meta["extract_error"],
            "total_bytes": size_bytes,
        }

    # Success path: read the sidecar.
    sidecar_name = meta.get("sidecar")
    if not sidecar_name:
        return {"error": "vanished"}
    sidecar_path = os.path.join(
        data_root, "sessions", session_id, "attachments", sidecar_name
    )
    if not os.path.exists(sidecar_path):
        return {"error": "vanished"}
    with open(sidecar_path, "rb") as f:
        raw = f.read(max_chars * 4 + 1)
    decoded = raw.decode("utf-8", errors="ignore")
    truncated = (len(decoded) > max_chars
                 or len(raw) > max_chars * 4
                 or meta.get("truncated", False))
    return {
        "kind": "document",
        "filename": filename,
        "mime": mime,
        "extractor": meta.get("extractor"),
        "pages": meta.get("pages"),
        "content": decoded[:max_chars],
        "truncated": truncated,
        "total_bytes": size_bytes,
    }
```

The `truncated` flag OR's the read-time truncation with the extract-time truncation, so the model gets an accurate signal either way.

The tool's docstring is updated to note: "For `kind=document` attachments, this returns the extracted Markdown, *or* an `error` field naming why extraction failed (e.g., `empty_scanned`, `encrypted`, `timeout`). When you see such an error, explain to the user in plain language what's wrong and what they can do (e.g., 'this looks like a scanned PDF; I can't OCR it yet — could you share a text PDF or paste the relevant section?')."

### 5. `agent.py` — minimal changes

`select_tools_for_run()` and `attachment_system_block()` already key on `kind != "image"`, so `document` is automatically picked up. One copy tweak to `attachment_system_block`: the existing line "Use read_attachment(id) to inspect contents" should be augmented for documents so the model knows extraction may have failed — e.g., add "For `kind=document`, the response may include an `error` field if we couldn't extract text; relay it to the user in their language."

### 6. `requirements.txt` — four new deps

```
pypdf>=4.0
python-docx>=1.1
openpyxl>=3.1
python-pptx>=0.6
```

All four are pure Python; install on amd64 / arm64 / armv7 from PyPI without native compilation. No system-level packages required.

### 7. Configuration — `config.py`

Three new knobs, all readable from the same Viper-style ini config as everything else:

| key | default | purpose |
|---|---|---|
| `MaxDocumentCharacters` | `262144` (≈ 256 KiB chars) | Cap on extracted text; protects sidecar size and downstream context budget. |
| `MaxDocumentExtractSeconds` | `8` | Hard wall-clock timeout for one extraction; on expiry returns `error="timeout"` and the thread is left to finish on its own (Python can't interrupt arbitrary C code). |
| `MaxDocumentUncompressedBytes` | `209715200` (200 MB) | Sum of `infolist().file_size` cap for ZIP-based formats. Anything above is `error="zip_bomb"`. |

`MaxAttachmentSize` (per-file upload cap) is reused unchanged — the existing cap still bounds how big a file we'll ingest in the first place.

About the "leaked" thread on timeout: `asyncio.wait_for` cancels the awaitable, but `asyncio.to_thread` runs on the default executor and the underlying thread keeps running until the Python call returns. With the upload size cap and the zip-bomb precheck both in place, the worst case (a hand-crafted pypdf-pathological 3 MB PDF) is bounded to seconds of CPU and tens of MB of RAM. We accept that bound. Phase 2's docling switch is the right time to introduce process-level isolation if we need stricter guarantees.

## Data Flow Examples

### Happy path — 42-page report

User uploads `quarterly-report.pdf` (3 MB, 42 pages of text).

1. `stream_to_disk` writes `att_ab12cd34ef56__quarterly-report.pdf.part`.
2. `classify()` sees `.pdf` → `("application/pdf", "document")`.
3. `handle_upload()` runs `extract_to_markdown` in a worker thread; it finishes in 1.8 s, returns `ok=True, truncated=True` at page 31 (chars cap), `pages=42`.
4. `.part` → final rename.
5. Sidecar `att_ab12cd34ef56__quarterly-report.pdf.md` written.
6. DB row: `kind=document`, `meta_json={"sidecar":"att_ab12cd34ef56__quarterly-report.pdf.md","extractor":"pypdf","pages":42,"chars":262144,"truncated":true}`.
7. Next agent run: system block lists the document; model calls `read_attachment("att_ab12cd34ef56")`, gets Markdown, answers the user.

### Failure path — scanned PDF

User uploads `passport-scan.pdf` (image-only).

1–4. Same as above; rename succeeds.
5. Extractor finished in 200 ms but produced 6 chars of whitespace → `ok=False, error="empty_scanned"`. No sidecar written.
6. DB row: `kind=document`, `meta_json={"extract_error":"empty_scanned"}`.
7. Model calls `read_attachment`, gets `{kind:"document", error:"empty_scanned", filename:"passport-scan.pdf"}`. Model replies (in user's language): "I see you uploaded a scanned PDF, but it doesn't contain selectable text — I can't read scanned images yet. Could you share a text-based PDF, or paste the relevant text?"

## Error Handling

| Failure | `kind` | `meta.extract_error` | Notes |
|---|---|---|---|
| Extractor library missing | `binary` | `not_installed` | Packaging bug. Model treats as binary; we fix the build. |
| Zip uncompressed > cap | `document` | `zip_bomb` | Caught before parser is invoked. |
| Extraction timeout | `document` | `timeout` | Worker thread keeps running until it finishes; we just stop waiting. |
| Encrypted PDF | `document` | `encrypted` | Detected via `pypdf.is_encrypted`. |
| Empty / whitespace-only output | `document` | `empty_scanned` | Typically scanned PDFs. |
| Library exception (corrupt file etc.) | `document` | `parse_error` | Catch-all via broad `except`. |
| Unknown extension hits extractor | `document` | `unsupported` | Defensive; should not happen given classify. |
| Sidecar write fails | `document` | `sidecar_write_failed` | Upload still 200; original file kept. |
| Sidecar missing at read time | — | — | `read_attachment` returns `{error: "vanished"}`. |
| DB INSERT fails | — | — | Delete final + sidecar, HTTP 500. |
| `.part → final` rename fails | — | — | Existing behavior unchanged: HTTP 500. |

The model is told (via the `read_attachment` docstring and the system block addendum) to translate these error codes into plain-language responses for the user.

## Testing

New / extended tests, all under `agent/tests/`:

1. **`test_attachments_kind.py`** — assert `.pdf` / `.docx` / `.xlsx` / `.xlsm` / `.pptx` → `kind="document"`. Bytes don't need to be valid; classification is by extension.

2. **`test_attachments_extract.py`** (new) — per format:
   - Happy path with a minimal valid fixture (built in-test for docx/xlsx/pptx; tiny committed fixture for pdf).
   - Corruption: garbage bytes → `ok=False, error="parse_error"`.
   - Truncation: build a fixture that yields > `max_chars=100` of text → `truncated=True`, `len(markdown) <= 100`.
   - Empty: PDF with no text layer (or mock pypdf returning empty) → `error="empty_scanned"`.
   - **Encrypted PDF** → `error="encrypted"` (small password-protected fixture).
   - **Zip-bomb precheck** for docx/xlsx/pptx: build a ZIP whose `infolist().file_size` sum exceeds `max_uncompressed_bytes=1024` → `error="zip_bomb"`. No need for an actual high-compression file; the test exercises the precheck logic via small inputs.
   - **openpyxl empty-cache fallback**: build an xlsx with a formula and no cached value → output contains the formula text (e.g., `=SUM(A1:A10)`), not blank.
   - **pypdf broad exception envelope**: monkeypatch `pypdf.PdfReader` to raise an unusual exception type (e.g., `RecursionError`) → `ok=False, error="parse_error"` (test that nothing escapes).

3. **`test_attachments_upload_document.py`** (new) — uses fake `extract_to_markdown` via monkeypatch / a fake module:
   - Success: sidecar `.md` exists on disk; DB row `kind=document`, `meta.sidecar` matches.
   - Extractor failure: DB row `kind=document` (NOT binary), `meta.extract_error` set, no sidecar file.
   - `not_installed` specifically: DB row `kind=binary`, `meta.extract_error="not_installed"`.
   - Sidecar write failure (mock `open` to raise on the sidecar path): upload still 200; DB row `kind=document`, `meta.extract_error="sidecar_write_failed"`, original file still on disk.
   - **Extraction timeout**: fake extractor `time.sleep`s past the configured timeout → DB row `kind=document`, `meta.extract_error="timeout"`; assert upload returns within ~timeout + small slack.
   - DB INSERT failure: mock cursor to raise `sqlite3.Error` → HTTP 500; final + sidecar both absent.

4. **`test_attachments_skill.py`** — `read_attachment` with `kind="document"`:
   - Sidecar present → content + extractor + pages.
   - `meta.extract_error` present → `{kind:"document", error: "<reason>", filename, mime}` (no `content` field).
   - Sidecar missing but no `extract_error` → `{error: "vanished"}`.
   - Truncation: sidecar > `max_chars` → `truncated=True`.

Existing tests are not modified beyond the new cases; the `document` kind is additive everywhere.

## Phase 2 Hook

`attachments/extract.py` exposes a single function. Phase 2 swaps its implementation for a docling-backed one (potentially via a subprocess pool or job queue, which would also give us proper hard-kill timeouts). The on-disk format (sidecar `.md`, `meta_json` shape, error enum) stays the same. `meta.extractor` already records which engine produced a given sidecar, so mixed pre/post-migration data is unambiguous.

## What This Spec Does Not Cover

- Re-extracting old `binary` rows from before the migration. If desired, a one-shot CLI script can walk the DB later.
- Frontend display of `kind="document"` (e.g., distinct icon). The current `binary`/fallback rendering handles it acceptably.
- Cross-service changes. All work is contained under `NimoOS-AI/agent/`; no Go service, no Gateway, no DB schema migration.
- Process-level isolation for extraction. The threadpool + timeout + zip-bomb + upload-cap combination is deemed sufficient given current Phase 1 risk surface; reconsider when integrating docling.
