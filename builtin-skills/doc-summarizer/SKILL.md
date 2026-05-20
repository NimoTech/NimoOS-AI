## Document summarizer

Distill a folder of documents into a one-page recap plus structured fields.

### When to use
- User asks "summarize", "recap", or "one-pager" on a folder of docs
- A folder full of PDFs / `.md` / `.txt` / `.docx` is mentioned

### How to run
1. Use `list_dir(<folder>)` to enumerate the folder.
2. For each text-like file (`.md`, `.txt`, `.json`, `.csv`, source code, ...)
   call `read_file(<path>)` to get the content.
3. For each PDF / DOCX / spreadsheet, attachments must be uploaded first via
   the chat composer — those flow through `read_file_lines` /
   `read_file` (the file plumbing handles document parsing transparently).
4. Synthesize:
   - A **200-word recap** of the folder as a whole
   - **Entities** — people, orgs, amounts, dates encountered
   - **Action items** — anything that reads as a TODO with an owner

### Writing outputs (only if the user asks to save)
- `recap.md` — the 200-word overview
- `entities.json` — structured entity list
- `actions.md` — action items

Use `write_file(<path>, <content>)` to persist. Otherwise, just respond
inline.

### Guardrails
- Do not `delete_path` anything. Read-only by default.
- For folders with > 50 files, ask the user to narrow the scope (e.g.,
  "just the PDFs from Q1") before grinding through everything.
- If `read_file` returns a binary blob you can't interpret, skip and note
  it in the recap.
