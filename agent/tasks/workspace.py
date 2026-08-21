"""Per-task working folders — one directory per scheduled task.

An agent-loop task has no memory across runs: every run gets a brand-new
session, so "only report what is new" is impossible for it. A daily digest
re-announced the same product launch every day until the item aged out of its
window. The script-based version solved that with its own SQLite `seen` table;
the agent loop had nowhere it was allowed to write.

So each task gets a folder, and its run is granted write access to that one
folder automatically — the author never configures `fs_write` for it, and a task
can never write into another task's folder because the grant names one exact
path.

Where this lives is a CONSTRAINT, not a preference
--------------------------------------------------
`/var/lib/nimoos` is in `tasks/driver.py::FS_DENY_ROOTS`. A workspace under the
agent's own state directory would be created, granted, and then silently refused
by `fs_allowed` — the failure would surface as an inexplicable permission error
rather than a wrong path. `/DATA` is also the only tree the file manager shows,
which is the whole point of letting a user look at what their task stored.
Both facts are pinned by tests in `tests/test_tasks_workspace.py`.

Nothing here raises. A run that cannot get its folder loses the folder, not the
run.
"""
from __future__ import annotations

import logging
import os
import re

logger = logging.getLogger("nimoos-agent.tasks")

# Overridable for tests and for an install that keeps /DATA elsewhere.
ROOT = (os.environ.get("NIMOOS_TASK_WORKSPACE_ROOT", "").strip()
        or "/DATA/AppData/nimoos-tasks")

# Written on creation so the tree is navigable by a human: the directory name is
# a task id, which tells someone browsing /DATA nothing at all.
MARKER_NAME = "_task.txt"

# A task id is a server-generated uuid4 today, so this is defence against a
# hand-edited row rather than against a user. It is still worth having: a path
# built by joining unvalidated text is the kind of bug that shows up exactly
# once, and `os.path.join(root, "../../etc")` escapes the root without any
# cleverness at all.
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def path_for(task_id) -> str:
    """The folder for `task_id`, or `""` when the id is not usable as one.

    Rejects anything that is not a plain single path component — separators,
    `..`, a leading dot, embedded NULs, non-strings. `""` means "no workspace",
    and every caller must treat it as such rather than falling back to ROOT
    itself (which would hand one task the whole shared tree).
    """
    if not isinstance(task_id, str):
        return ""
    ident = task_id.strip()
    if not ident or ident in (".", ".."):
        return ""
    if not _SAFE_ID.match(ident):
        return ""
    return os.path.join(ROOT, ident)


def ensure(task_id, name: str = "") -> str:
    """Create the folder for `task_id` if needed; return its path or `""`."""
    path = path_for(task_id)
    if not path:
        logger.warning("tasks workspace: refusing unusable task id %r", task_id)
        return ""
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as exc:
        logger.warning("tasks workspace: cannot create %s: %s", path, exc)
        return ""
    _write_marker(path, task_id, name)
    return path


def _write_marker(path: str, task_id: str, name: str) -> None:
    """Name the task inside its folder, once.

    Deliberately never overwritten: the file is inside a directory the user is
    invited to browse and edit, and clobbering their notes on every run would
    make the folder feel unsafe to touch. A rename therefore leaves the old name
    in place — the id below it is the authoritative link.
    """
    marker = os.path.join(path, MARKER_NAME)
    if os.path.exists(marker):
        return
    try:
        with open(marker, "w", encoding="utf-8") as fh:
            fh.write(f"{name or '(unnamed task)'}\n"
                     f"task id: {task_id}\n\n"
                     "This folder belongs to a NimoOS scheduled task. The task "
                     "may read and write files here — it is where it keeps "
                     "state between runs.\n\n"
                     "You can browse, edit and clear it from the NimoOS file "
                     "manager. Note that the agent runs as root, so files here "
                     "are root-owned: over SSH as another user you can read "
                     "them but not change them.\n\n"
                     "This file is written once and never overwritten.\n")
    except OSError as exc:
        # A missing marker costs readability, nothing else.
        logger.warning("tasks workspace: cannot write marker in %s: %s",
                       path, exc)
