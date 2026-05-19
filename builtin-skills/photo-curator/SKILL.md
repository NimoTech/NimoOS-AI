## Photo curator

Cluster and rank photos in a folder by scene, location, and time.

### When to use
- The user asks to organize / dedupe / pick best photos in a folder
- A photo import folder is mentioned

### How to run
1. Confirm the target folder (default: `/Photos/_inbox`).
2. Run: `python /skill/photo-curator/scripts/cluster.py <folder>`
3. Use the existing `photos_search` tool to verify outputs land in `<folder>/Auto/`.

### Guardrails
- Never delete originals; the script only writes to `<folder>/Auto/`.
- Skip folders tagged `do-not-touch`.
- Ask before running on > 2000 files.
