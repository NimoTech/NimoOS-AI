## Duplicate sweeper

Detect exact and perceptual-hash duplicates; move them to `/Recycled` with a 30-day TTL.

### When to use
- User asks to deduplicate, reclaim space, find redundant files

### How to run
1. Confirm the scan root with the user.
2. Run: `python /skill/duplicate-sweeper/scripts/sweep.py <folder>`
3. Show the user a candidate list BEFORE moving anything.

### Guardrails
- Never delete; only move to `/Recycled/`.
- Originals retained for 30 days.
