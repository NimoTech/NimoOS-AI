"""Regression tests: unexpanded `$VAR` / `${VAR}` tokens are treated as
harmless literals by the first-pass classifier.

`parse.segments()` is fail-CLOSED for command substitution — `$(`, backticks,
`<(`, `>(` all return None → GRAY (parse.py:112) — and special-cases
`${IFS}`/`$IFS`. Every other expansion is carried through verbatim as an
ordinary token, with no marker that its content is unknown, while `rules.py`
goes on to judge that token as if it were the final argv. Variable expansion is
therefore fail-OPEN, the opposite of command substitution.

`extract_paths()` (parse.py:141) recognises a path by `startswith("/") or "/"
in tok`, so `${SECRET_FILE}` is not a path at all: it is dropped, the protected
prefix/suffix checks see nothing, and a read via a SAFE command name lands on
`safe` — executed with no judge, no confirmation, no audit of the real target.

Fix is deferred; these are marked xfail(strict=True) so they turn into hard
failures — forcing the markers to be removed — the moment the fix lands.
"""
import pytest

import db as dbmod
from shell_guard import allowlist as AL
from shell_guard import classify
from shell_guard.parse import segments

_TODO = "shell_guard: unexpanded ${VAR} tokens are classified as harmless literals"

# The sibling defect, tracked separately because the fix is different in kind:
# brace expansion, globs, ANSI-C quoting and `~` are resolved by bash purely
# from the command text — no runtime value involved — so the classifier COULD
# resolve them and simply does not. It hands the raw blob to os.path.realpath,
# which does not understand any of that syntax, and the protected-prefix match
# then misses a path that was fully determined all along.
_TODO_STATIC = "shell_guard: statically resolvable expansions are never expanded"


class TestPlaceholderPathIsNotSafe:
    """A SAFE command name must not stay SAFE when its target is unknowable."""

    @pytest.mark.xfail(strict=True, reason=_TODO)
    def test_cat_of_placeholder_path(self):
        # SECRET_FILE could be /etc/shadow, ~/.ssh/id_rsa or agent.db
        assert classify("cat ${SECRET_FILE}").level != "safe"

    @pytest.mark.xfail(strict=True, reason=_TODO)
    def test_bare_dollar_placeholder_path(self):
        assert classify('cat "$FILE"').level != "safe"

    @pytest.mark.xfail(strict=True, reason=_TODO)
    def test_ls_of_placeholder_dir(self):
        assert classify("ls ${DIR}").level != "safe"

    @pytest.mark.xfail(strict=True, reason=_TODO)
    def test_grep_of_placeholder_dir(self):
        assert classify("grep -r secret ${PROJ}").level != "safe"

    @pytest.mark.xfail(strict=True, reason=_TODO)
    def test_partially_resolved_path_can_still_escape_data(self):
        """`/DATA/${X}/a.txt` looks scoped to /DATA, but X=`../etc` reads
        /etc/a.txt — the prefix proves nothing while X is unknown."""
        assert classify("cat /DATA/${X}/a.txt").level != "safe"


class TestOtherUnexpandedPlaceholderForms:
    """`${VAR}` is not the only spelling of "value known only at runtime"."""

    @pytest.mark.xfail(strict=True, reason=_TODO)
    def test_positional_parameters(self):
        assert classify('cat "$@"').level != "safe"

    @pytest.mark.xfail(strict=True, reason=_TODO)
    def test_array_subscript(self):
        assert classify("ls ${a[0]}").level != "safe"

    @pytest.mark.xfail(strict=True, reason=_TODO)
    def test_indirect_expansion(self):
        assert classify("cat ${!ref}").level != "safe"

    def test_placeholder_redirect_target_is_already_not_safe(self):
        """Currently GRAY (unclassified verb + redirect) — pinned so a future
        change cannot quietly promote a write to an unknown path to SAFE."""
        assert classify("echo x > ${OUT}").level != "safe"
        assert classify("bash < ${SCRIPT}").level != "safe"


class TestStaticExpansionsAreNotResolved:
    """Unlike `${VAR}`, every case here is fully determined by the command text
    alone — bash needs no runtime state to reach the protected file, and
    neither would the classifier if it expanded these forms."""

    @pytest.mark.xfail(strict=True, reason=_TODO_STATIC)
    def test_brace_expansion_reaches_a_protected_path(self):
        # bash: {/etc,/tmp}/passwd -> /etc/passwd /tmp/passwd
        assert classify("cat {/etc,/tmp}/passwd").level != "safe"

    @pytest.mark.xfail(strict=True, reason=_TODO_STATIC)
    def test_glob_reaches_a_protected_path(self):
        # bash: /*/shadow -> /etc/shadow
        assert classify("cat /*/shadow").level != "safe"

    @pytest.mark.xfail(strict=True, reason=_TODO_STATIC)
    def test_single_char_glob_reaches_a_protected_path(self):
        # bash: /et?/shadow -> /etc/shadow
        assert classify("cat /et?/shadow").level != "safe"

    @pytest.mark.xfail(strict=True, reason=_TODO_STATIC)
    def test_ansi_c_quoting_hides_the_path(self):
        # bash: $'/etc/shadow' -> /etc/shadow. shlex drops the quotes but keeps
        # the '$', producing the token '$/etc/shadow', which realpath treats as
        # an ordinary relative name.
        assert classify("cat $'/etc/shadow'").level != "safe"

    @pytest.mark.xfail(strict=True, reason=_TODO_STATIC)
    def test_tilde_traversal_reaches_a_protected_path(self):
        # bash: ~/../../etc/shadow -> /home/<user>/../../etc/shadow -> /etc/shadow
        assert classify("cat ~/../../etc/shadow").level != "safe"

    def test_named_tilde_ssh_key_is_currently_caught(self):
        """Passes today, but only incidentally: `~root` is never expanded — the
        raw token just happens to contain the '/.ssh/' substring from
        _PROTECTED_SUBSTR. Pinned so the tilde fix keeps it protected for the
        right reason instead of dropping it."""
        assert classify("cat ~root/.ssh/id_rsa").level == "protected"


class TestProtectedPathHiddenInsideExpansion:
    """The sensitive path is right there in the command text, but `realpath`
    normalises the whole `${...}` blob and the prefix match misses it."""

    @pytest.mark.xfail(strict=True, reason=_TODO)
    def test_default_value_expansion(self):
        assert classify("cat ${f:-/etc/shadow}").level != "safe"

    @pytest.mark.xfail(strict=True, reason=_TODO)
    def test_pattern_substitution_expansion(self):
        assert classify("cat ${p//x//etc/shadow}").level != "safe"


class TestDestructivePlaceholderTarget:
    @pytest.mark.xfail(strict=True, reason=_TODO)
    def test_unresolved_target_reaches_the_backstop(self):
        """`rm -rf ${TARGET}` is already DANGEROUS via the rm/-rf rule, so the
        user is still asked — but `decision.paths` is empty, so
        skills/shell.py:324 calls `prepare_backstop([])`, no snapshot or trash
        copy is made, and the confirm card tells the user the delete is
        irreversible even when the real target was recoverable. The unresolved
        token has to reach the caller in some form."""
        d = classify('rm -rf "${TARGET}"')
        assert d.paths, "unresolved destructive target must be reported in decision.paths"

    @pytest.mark.xfail(strict=True, reason=_TODO)
    def test_assignment_prefixed_unresolved_target_reaches_the_backstop(self):
        d = classify("X=${Y} rm -rf ${Z}")
        assert d.paths, "unresolved destructive target must be reported in decision.paths"


class TestAllowlistDoesNotVouchForPlaceholders:
    @pytest.mark.xfail(strict=True, reason=_TODO)
    def test_prefix_entry_does_not_waive_a_placeholder_target(self):
        """`_entry_matches` refuses a loose prefix/regex entry whose command
        touches a protected path (allowlist.py:47), but that check runs on
        `extract_paths`, which returns [] for a placeholder. An allowlisted
        `cat ` prefix then auto-runs `cat ${SECRET}` — unattended, no confirm."""
        conn = dbmod.init_db(":memory:")
        AL.add(conn, "prefix", "cat ", "user")
        assert AL.match(conn, "cat ${SECRET}") is False


class TestFailClosedBehaviourStillHolds:
    """Currently correct — pinned so a future "expand variables" change does
    not accidentally start statically resolving substitutions instead."""

    def test_command_substitution_stays_unparseable(self):
        assert segments("ls $(pwd)") is None
        assert classify("ls $(pwd)").level == "gray"

    def test_arithmetic_expansion_stays_unparseable(self):
        assert segments("echo $((1+1))") is None

    def test_placeholder_command_name_is_not_safe(self):
        assert classify("${CMD} -rf /DATA").level != "safe"
