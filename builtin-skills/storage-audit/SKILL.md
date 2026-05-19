## Storage audit

Surface where disk space is going on this NAS.

### When to use
- User asks "what's using space", "biggest folders", "biggest files"

### How to run
1. For per-disk numbers use the `get_disks` tool.
2. For per-folder usage:
   `du -sh /<path>/* | sort -hr | head -20`
3. Surface a ranked list with sizes.

### Guardrails
- Never delete anything in an audit. Just report.
