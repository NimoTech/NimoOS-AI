## Wiki keeper

Read from and append to the user's personal wiki via the existing
`wiki_*` tools.

### When to use
- User wants to look up something they previously noted ("what did I write about X?")
- User wants to add a new entry under a path
- User mentions "wiki", "my notes", or "knowledge base"

### How to run

**To look something up:**
1. Call `wiki_list_full_tree()` to see what's there (cheap, returns
   the tree skeleton).
2. Pick the most likely path and call `wiki_get_node(<path>)`.
3. If nothing matches, suggest related paths.

**To add a new note:**
1. Confirm the target path with the user. If the path doesn't exist
   yet, ask whether to create it (`wiki_register_root(<path>, level)` —
   `level` is `project` or `topic`).
2. Use `wiki_append_user_notes(<path>, <text>)` to append.

**To replace existing notes (less common, destructive):**
- Use `wiki_replace_user_notes(<path>, <text>)`. ASK FIRST.

**To see what changed recently:**
- `wiki_recent_changes(<path>, since_days=7)` lists edits.

### Guardrails
- **Always confirm before writing.** `append` is forgiving (additive),
  but `replace` wipes the section.
- Don't fabricate wiki paths the user didn't mention; ask if unsure.
- If the user's request is ambiguous (e.g., "save this to my wiki"),
  ask where: "Under which path? (try `wiki_list_full_tree` first to see options)"
