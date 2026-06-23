## File reader

Read and answer questions about documents on the NAS — PDF, Word, PowerPoint,
Excel, HTML, Markdown, and plain text. Works for a single document's full text
and for questions that span many documents.

### When to use
- User asks what a specific document says, or to summarize / read one file
- User asks a question that should be answered from their documents
  ("what do my notes say about X")

### Tools
- `nimoos_search(query, sources, top_k)` — find documents. Use
  `sources="filenames"` to locate a file by name/path, `sources="semantic"`
  to find passages by meaning. Each hit carries a `file_id`.
- `read_document(file_id, offset, max_chars)` — the full extracted text of one
  document, reconstructed from the index, with `[Page N]` markers. Returns
  `truncated` and `next_offset` for long documents.

### How to read ONE specific document
1. If you don't already have its `file_id`, call `nimoos_search` with
   `sources="filenames"` and the file name to get it.
2. Call `read_document(file_id)`.
3. Answer from the returned text. Cite the file name and `[Page N]` when useful.

### How to answer a question ACROSS documents
1. `nimoos_search(query, sources="semantic")` to find the most relevant files.
2. For the top hits, call `read_document(file_id)` to read their content.
3. Synthesize an answer grounded in what you read; cite each source file
   (and page) you used.

### Long documents
- If `read_document` returns `"truncated": true`, the document is long. Do NOT
  loop dozens of times through `next_offset` to read it all — you will lose the
  thread and burn context.
  - To find a specific fact: use `nimoos_search` to jump to the relevant
    passage instead of reading the whole file.
  - To summarize: summarize the portion you have and say it is based on the
    first part of the document.

### Guardrails
- Read-only. Do not modify or delete files.
- If a document isn't found or returns no text, say so plainly — don't guess at
  its contents.
