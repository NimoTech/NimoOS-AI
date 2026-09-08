import inspect

import agent as agent_module


def test_append_text_handles_str_and_blocks():
    assert agent_module._append_text("q", "EV") == "q\n\nEV"
    assert agent_module._append_text("q", "") == "q"
    blocks = [{"type": "input_text", "text": "q"}, {"type": "input_image", "image_url": "x"}]
    out = agent_module._append_text(blocks, "EV")
    assert out[-1] == {"type": "input_text", "text": "EV"} and len(out) == 3
    assert blocks[-1]["type"] == "input_image"          # input not mutated


def test_run_wires_ask_pipeline_by_source():
    src = inspect.getsource(agent_module.AgentRunner.run)
    assert 'profile.pre_run == "ask"' in src
    assert "ask_pipeline.run_guarded(" in src
    assert "_append_text(user_content" in src
    assert "profile.max_turns" in src
    # _summarize_fn must exist before the pipeline runs (rewrite uses .complete)
    assert src.index("_summarize_fn = _make_summarize_fn(") < src.index("ask_pipeline.run_guarded(")
    assert src.count("_summarize_fn = _make_summarize_fn(") == 1
