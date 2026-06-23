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
1. If the user gave a path (or you found one with list_dir/nimoos_search),
   call `read_document(path="/abs/path")` — this reads any file by path,
   including files NOT yet indexed (e.g. just uploaded). For scanned/image
   PDFs add `ocr=true`.
2. Otherwise, if you have the file's `file_id` from nimoos_search, call
   `read_document(file_id=...)` — faster, and supports `offset` paging with
   `[Page N]` markers for long indexed documents.
3. Answer from the returned text. Cite the file name (and `[Page N]` when
   present).

You may only read paths within your authorized scope (same as read_file). If
read_document reports it is not authorized, ask the user to grant access to
that folder.

### How to answer a question ACROSS documents
1. `nimoos_search(query, sources="semantic")` to find the most relevant files.
2. For the top hits, call `read_document(file_id)` to read their content.
3. Synthesize an answer grounded in what you read; cite each source file
   (and page) you used.

### When the text isn't enough (scanned pages, tables, figures, layout)
Sometimes `read_document` returns little or garbled text (scanned/image PDFs),
or the question is about a figure, a complex table, or how a page looks.
- If your model is vision-capable: call
  `view_document_page(path="/abs/file.pdf", page=N, question="...")` — it
  renders that PDF page to an image and looks at it. Use the page number from
  the `[Page N]` markers in `read_document` output.
- If your model is NOT vision-capable: call
  `read_document(path="/abs/file.pdf", ocr=true)` to OCR the scanned text
  instead. (view_document_page will tell you to do this if vision is off.)

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
