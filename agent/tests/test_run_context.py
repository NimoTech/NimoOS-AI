import contextvars
import run_context as rc


def test_defaults_and_current():
    assert rc.current() is None
    ctx = rc.RunCtx(session_id="s", user_id="u", model_name="m", provider_type="other", window=1000)
    tok = rc.RUN_CTX_VAR.set(ctx)
    try:
        assert rc.current() is ctx
        assert ctx.last_input_tokens == 0 and ctx.summary == "" and ctx.fold_idx == 0
        assert ctx.compaction_enabled is True
        assert ctx.peak_input_tokens == 0
    finally:
        rc.RUN_CTX_VAR.reset(tok)
    assert rc.current() is None


def test_isolated_per_context():
    ctx = rc.RunCtx(session_id="s", user_id="u", model_name="m", provider_type="other", window=1)
    rc.RUN_CTX_VAR.set(ctx)
    fresh = contextvars.Context()
    assert fresh.run(rc.current) is None
    rc.RUN_CTX_VAR.set(None)
