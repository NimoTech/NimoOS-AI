## Photo finder

Find photos in the on-device photo library by natural-language query.

### When to use
- The user asks to find, look up, or search for photos
- A description, person, place, or year is mentioned alongside "photo" / "picture" / "image"

### How to run
Call the `search_photos` function tool with:
- `query` (required) — natural-language description, e.g. `"beach at sunset"`, `"cat"`, `"weekend trip"`
- `year` (optional, default 0 = any) — filter by year
- `limit` (optional, default 20) — max results

Example: user says "show me beach pictures from 2024" →
`search_photos(query="beach", year=2024, limit=20)`

### Output format
The tool returns a list of photo paths with date, location (if known), and
caption. Surface the top results as a numbered list and offer to open the
folder. If results are empty, suggest broader queries (drop the year filter,
try synonyms).

### Guardrails
- This skill is **read-only**. Never call `delete_path`, `rename`, or move
  files from inside this skill.
- If the user asks to organize, delete, or move photos, hand off explicitly
  ("I'll need to use a different tool for moving — is that OK?") rather than
  silently doing it.
