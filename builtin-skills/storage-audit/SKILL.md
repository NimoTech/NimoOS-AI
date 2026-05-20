## Storage audit

Surface where disk space is going on this NAS.

### When to use
- User asks "what's using space", "biggest folders", "biggest files",
  "what should I clean up?"

### How to run

**Step 1 — top-level disk usage:**
Call `list_storage()` for an inventory of disks + their used/free figures.
Call `list_merges()` if the user has merged volumes (MergerFS-style pools).

**Step 2 — per-folder breakdown (if asked to go deeper):**
Use `run_command` with `du`:
```
du -h --max-depth=1 <path> 2>/dev/null | sort -hr | head -20
```
That sorts subfolders by size, biggest first. Adjust `--max-depth` if the
user wants to drill in further.

**Step 3 — find the biggest individual files:**
```
find <path> -type f -size +100M -printf '%s\t%p\n' 2>/dev/null \
  | sort -nr | head -20 \
  | awk '{ printf "%.1f MB\t%s\n", $1/1048576, substr($0, length($1)+2) }'
```

**Step 4 — present:**
Summarize as a ranked list. For each entry include human-readable size,
path, and (where obvious) what category it is (photos / backups / Docker
volumes / etc).

### Guardrails
- **Never delete anything in an audit.** Just report.
- If the user wants to clean up afterwards, offer to hand off to
  `duplicate-sweeper` or to move specific files to `/Recycled` — but
  always confirm first.
- Skip `/proc`, `/sys`, `/dev` in any recursive `du` or `find`. The
  agent sandbox shouldn't bind these anyway, but be defensive.
