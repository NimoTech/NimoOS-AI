"""Every scheduled task gets its own folder, and its run may read/write there.

Why this exists
---------------
An agent-loop task has no memory across runs — each one gets a brand-new
session — so "only report what is new" is impossible for it. A daily digest
re-announced the same launch every day until the item aged out of its window.
The script version solved that with its own SQLite `seen` table; the agent loop
had no equivalent, because there was nowhere it was allowed to write.

So each task gets a directory, granted to its own run automatically. The author
does not have to configure `fs_write` for it, and a task can never write into
another task's folder: the grant names one exact path.

Where it lives is a constraint, not a preference
-----------------------------------------------
`/var/lib/nimoos` is in `driver.FS_DENY_ROOTS`, so a workspace under the agent's
own state directory would be REFUSED by the fs gate — it would grant nothing at
all, and the failure would look like a mysterious permission problem rather than
a wrong path. `/DATA` is also the only tree the user can browse in the file
manager, which is the point of letting them look at what a task stored.
Both facts are pinned by tests below, because the next person to move this
directory will not know either of them.
"""
from __future__ import annotations

import os

import pytest

from tasks import driver as task_driver
from tasks import workspace


# ── where it lives ───────────────────────────────────────────────────────────

def test_the_workspace_root_is_not_inside_a_denied_fs_root():
    """The mistake this catches: putting the root under /var/lib/nimoos.

    `fs_allowed` refuses any root that resolves into FS_DENY_ROOTS, so such a
    workspace would be created, granted, and then silently useless.
    """
    assert not task_driver.fs_root_denied(os.path.realpath(workspace.ROOT)), \
        f"{workspace.ROOT} is inside a denied fs root"


def test_the_workspace_root_is_under_the_user_visible_data_tree():
    # /DATA is what the file manager shows and what the container has mounted
    # read-write; anywhere else and the user cannot look at what a task wrote.
    assert workspace.ROOT.startswith("/DATA/")


def test_a_task_gets_a_path_named_after_its_id():
    path = workspace.path_for("1c55c5ad-02df-444c-8b9d-6bb9523db440")
    assert path == os.path.join(workspace.ROOT,
                                "1c55c5ad-02df-444c-8b9d-6bb9523db440")


def test_two_tasks_get_different_folders():
    a = workspace.path_for("aaaaaaaa-0000-0000-0000-000000000000")
    b = workspace.path_for("bbbbbbbb-0000-0000-0000-000000000000")
    assert a != b


# ── a crafted id must not escape the root ────────────────────────────────────

@pytest.mark.parametrize("bad", [
    "../../etc", "..", "a/b", "/absolute", "", "   ", None, 42,
    "x\x00y", ".", "./x", "a\\b",
])
def test_an_id_that_could_escape_the_root_yields_no_path(bad):
    # Ids are server-generated uuid4 today, so this is defence against a
    # hand-edited row rather than against a user — but a path built from
    # unvalidated text is exactly the bug that only shows up once.
    assert workspace.path_for(bad) == ""


def test_a_refused_id_creates_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace, "ROOT", str(tmp_path / "ws"))
    assert workspace.ensure("../escape") == ""
    assert not (tmp_path / "ws").exists()


# ── creation ─────────────────────────────────────────────────────────────────

def test_ensure_creates_the_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace, "ROOT", str(tmp_path / "ws"))
    path = workspace.ensure("t-1", name="daily digest")
    assert path and os.path.isdir(path)


def test_ensure_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace, "ROOT", str(tmp_path / "ws"))
    first = workspace.ensure("t-1", name="daily digest")
    second = workspace.ensure("t-1", name="daily digest")
    assert first == second and os.path.isdir(first)


def test_the_folder_says_which_task_it_belongs_to(tmp_path, monkeypatch):
    # The directory name is the task id, which is unreadable when browsing
    # /DATA in the file manager. A marker file is the cheapest way to make the
    # tree navigable by a human.
    monkeypatch.setattr(workspace, "ROOT", str(tmp_path / "ws"))
    path = workspace.ensure("t-1", name="竞品雷达")
    marker = os.path.join(path, workspace.MARKER_NAME)
    assert os.path.isfile(marker)
    assert "竞品雷达" in open(marker, encoding="utf-8").read()


def test_the_marker_does_not_overwrite_a_users_own_edits(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace, "ROOT", str(tmp_path / "ws"))
    path = workspace.ensure("t-1", name="first name")
    marker = os.path.join(path, workspace.MARKER_NAME)
    with open(marker, "w", encoding="utf-8") as fh:
        fh.write("my own notes")
    workspace.ensure("t-1", name="renamed")
    assert open(marker, encoding="utf-8").read() == "my own notes"


def test_ensure_never_raises_when_the_root_cannot_be_created(tmp_path, monkeypatch):
    # A run must not die because its scratch folder could not be made; it just
    # loses the folder. Point ROOT at a path whose parent is a FILE.
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setattr(workspace, "ROOT", str(blocker / "ws"))
    assert workspace.ensure("t-1") == ""


# ── the gate actually honours it ─────────────────────────────────────────────

def test_a_file_in_the_workspace_is_writable_once_granted(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace, "ROOT", str(tmp_path / "ws"))
    path = workspace.ensure("t-1")
    assert task_driver.fs_allowed(os.path.join(path, "seen.json"), [path])


def test_one_tasks_grant_does_not_reach_another_tasks_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace, "ROOT", str(tmp_path / "ws"))
    mine = workspace.ensure("t-1")
    theirs = workspace.ensure("t-2")
    assert not task_driver.fs_allowed(os.path.join(theirs, "seen.json"), [mine])


def test_the_grant_does_not_reach_the_shared_root(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace, "ROOT", str(tmp_path / "ws"))
    mine = workspace.ensure("t-1")
    assert not task_driver.fs_allowed(
        os.path.join(str(tmp_path / "ws"), "elsewhere.json"), [mine])


# ── the model has to be told the folder exists ────────────────────────────────

def test_the_briefing_names_the_working_folder():
    """Without this the folder is invisible and therefore useless.

    The whole point is cross-run state, and nothing else in the run path tells
    the model where it may write: `format_preauth_note` is after-the-fact
    provenance appended to the summary, which the model never sees.
    """
    from tasks import runner
    text = runner.format_run_briefing({"fs_write": ["/DATA/AppData/nimoos-tasks/t-1"],
                                       "workspace": "/DATA/AppData/nimoos-tasks/t-1"})
    assert "/DATA/AppData/nimoos-tasks/t-1" in text


def test_the_briefing_says_the_folder_survives_between_runs():
    # A model that does not know the folder persists has no reason to use it for
    # dedupe state — which is the only reason it exists.
    from tasks import runner
    text = runner.format_run_briefing({"workspace": "/DATA/AppData/nimoos-tasks/t-1"})
    assert any(word in text for word in ("between runs", "previous run", "persists"))


def test_a_workspace_alone_still_produces_a_briefing():
    # It used to return "" unless the task had a `scripts` grant.
    from tasks import runner
    assert runner.format_run_briefing({"workspace": "/DATA/x", "scripts": []})


def test_a_task_with_neither_gets_no_briefing():
    from tasks import runner
    assert runner.format_run_briefing({"scripts": [], "fs_write": []}) == ""
    assert runner.format_run_briefing({}) == ""


def test_scripts_and_workspace_are_both_briefed():
    from tasks import runner
    text = runner.format_run_briefing({"scripts": ["/DATA/AppData/radar/radar.py"],
                                       "workspace": "/DATA/AppData/nimoos-tasks/t-1"})
    assert "python3 /DATA/AppData/radar/radar.py" in text
    assert "/DATA/AppData/nimoos-tasks/t-1" in text


def test_the_briefing_stays_bounded_with_both(monkeypatch):
    from tasks import runner
    text = runner.format_run_briefing(
        {"scripts": [f"/DATA/s{i}.py" for i in range(200)],
         "workspace": "/DATA/AppData/nimoos-tasks/" + "x" * 300})
    assert len(text) <= runner.BRIEFING_MAX_CHARS


# ── granting a folder must also hand over the tool to use it ──────────────────

@pytest.mark.asyncio
async def test_a_run_with_a_workspace_starts_with_the_files_tools_unlocked():
    """The failure this fixes, observed on the live box.

    `read_file` and `list_dir` are in CORE_TOOL_NAMES, so a run could READ its
    folder — and it did, correctly reporting "first run, no history". But
    `write_file` lives in the gated `files` category, so the tool to write the
    state back was not even visible to the model, and the run finished having
    saved nothing. `denied_actions` was empty: nothing was refused, because
    nothing was attempted.

    Granting a folder and briefing it while withholding the only tool that can
    write to it is a half-built feature. Discovering `expand_tools` is friction
    that produced exactly this miss at the end of a long run.
    """
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    import db as dbmod
    from tasks import runner as task_runner
    from skills import tool_registry

    assert "write_file" not in tool_registry.CORE_TOOL_NAMES, \
        "if write_file became core this fix is obsolete — delete it"
    assert any(getattr(t, "name", "") == "write_file"
               for t in tool_registry.CATEGORY_TOOLS["files"])

    unlocked = task_runner.unlocked_for_workspace([])
    assert "files" in unlocked


def test_the_categories_a_task_already_had_are_kept():
    from tasks import runner as task_runner
    got = task_runner.unlocked_for_workspace(["web", "notes"])
    assert set(got) >= {"web", "notes", "files"}


def test_no_workspace_means_no_extra_unlocking(tmp_path, monkeypatch):
    # A task without a folder must keep the gate exactly as it was; this feature
    # is not an excuse to widen the default tool surface for every task.
    from tasks import runner as task_runner
    assert task_runner.unlocked_for_workspace(["web"], workspace="") == ["web"]
