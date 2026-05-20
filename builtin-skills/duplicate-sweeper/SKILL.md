## Duplicate sweeper

Find exact-duplicate files in a folder via SHA-256 hashing. Reports
candidates. Moving to `/Recycled` is a SEPARATE explicit step after the
user reviews the list.

### When to use
- User asks to deduplicate / reclaim space / find redundant files in a
  specific folder (e.g. `/Downloads`, `/Photos/2024`).

### How to run

**Step 1 — scan:**
Use the `run_command` tool to invoke the bundled script:
```
bash /skill/duplicate-sweeper/scripts/find_dupes.sh <folder> <min_size_bytes>
```
- `<folder>` — directory to scan recursively
- `<min_size_bytes>` — minimum file size to consider (default 1048576 = 1 MiB; raise this if scanning a folder with many tiny files)

The output is groups of duplicate paths separated by blank lines. First
path in each group is the "keeper"; the rest are exact duplicates.

**Step 2 — present:**
Summarize the groups for the user. For each group, show:
- Number of duplicates
- Total reclaimable bytes (size × (n-1))
- First 2-3 paths as examples

**Step 3 — confirm + move:**
Ask the user which groups to clean up. On confirmation:
1. `mkdir -p /Recycled` (via `run_command`)
2. For each duplicate to remove: `mv <path> /Recycled/<original-folder>/<filename>`. Preserve directory structure under `/Recycled` so restoration is straightforward.
3. If `/Recycled/...` already has a same-name file, append a timestamp suffix (e.g. `name.20260519T142301.ext`).

### Guardrails
- **Never `rm`.** Always move to `/Recycled/`.
- The script already skips `/Recycled/*` and hidden files.
- Exact-match only (SHA-256). Perceptual / near-duplicate detection is
  out of scope — if the user asks for "similar" or "near" duplicates,
  say so and recommend they revisit later.
- For huge trees (>100k files) the scan may take minutes — warn the user
  before kicking off.
