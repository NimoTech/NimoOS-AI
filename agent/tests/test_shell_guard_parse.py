from shell_guard.parse import segments, extract_paths, Segment


def test_simple_command():
    segs = segments("ls -la /DATA")
    assert segs is not None
    assert len(segs) == 1
    assert segs[0].argv == ["ls", "-la", "/DATA"]


def test_pipeline_and_logical_split():
    segs = segments("cat a | grep x && rm -rf /tmp/y")
    assert [s.argv[0] for s in segs] == ["cat", "grep", "rm"]


def test_redirect_target_captured():
    segs = segments("echo hi > /DATA/out.txt")
    assert segs[0].argv[0] == "echo"
    assert "/DATA/out.txt" in segs[0].redirect_targets


def test_append_redirect_captured():
    segs = segments("cat a >> /var/log/x")
    assert "/var/log/x" in segs[0].redirect_targets


def test_ampersand_redirect_target_captured():
    segs = segments("rm -rf /DATA/foo &> /tmp/log")
    assert "/tmp/log" in segs[0].redirect_targets


def test_read_redirect_target_captured():
    segs = segments("cat < /etc/shadow")
    assert "/etc/shadow" in segs[0].read_targets


def test_unbalanced_quotes_unparseable():
    assert segments('echo "unterminated') is None


def test_extract_paths():
    segs = segments("rm -rf /DATA/foo ./bar plainarg")
    paths = extract_paths(segs[0])
    assert "/DATA/foo" in paths
    assert "./bar" in paths
    assert "plainarg" not in paths  # no slash → not a path arg


def test_subshell_is_unparseable_or_segmented():
    # $(...) content must not be silently dropped; treat whole thing conservatively.
    segs = segments("echo $(rm -rf /DATA)")
    # Either None (unparseable) or a segment where the inner command surfaces.
    assert segs is None or any("rm" in tok for s in segs for tok in s.argv)
