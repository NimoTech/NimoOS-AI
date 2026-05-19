## Document summarizer

Distill a folder of documents into one page + structured fields.

### When to use
- User asks "summarize", "recap", "one-pager" on a folder of docs

### How to run
1. Use `list_dir` to enumerate the folder.
2. For each file use `read_document` to extract content.
3. Synthesize a 200-word recap + entity list + action items.

### Output format
- `recap.md` — 200-word overview
- `entities.json` — people, orgs, amounts, dates
- `actions.md` — action items with owners
