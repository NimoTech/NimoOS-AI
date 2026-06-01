import importlib


def test_defaults(monkeypatch):
    for k in ("NIMOOS_MAX_ATTACHMENT_SIZE",
              "NIMOOS_MAX_IMAGE_ATTACHMENT_SIZE",
              "NIMOOS_MAX_ATTACHMENTS_PER_SESSION",
              "NIMOOS_MAX_ATTACHMENT_TEXT_CHARS",
              "NIMOOS_FFPROBE_TIMEOUT",
              "NIMOOS_ATTACHMENT_GC_AGE"):
        monkeypatch.delenv(k, raising=False)
    import main as main_module
    importlib.reload(main_module)
    assert main_module.MAX_ATTACHMENT_SIZE == 524_288_000
    assert main_module.MAX_IMAGE_ATTACHMENT_SIZE == 20_971_520
    assert main_module.MAX_ATTACHMENTS_PER_SESSION == 50
    assert main_module.MAX_ATTACHMENT_TEXT_CHARS == 32_768
    assert main_module.FFPROBE_TIMEOUT == 5
    assert main_module.ATTACHMENT_GC_AGE == 86_400


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("NIMOOS_MAX_ATTACHMENT_SIZE", "12345")
    monkeypatch.setenv("NIMOOS_MAX_IMAGE_ATTACHMENT_SIZE", "100")
    import main as main_module
    importlib.reload(main_module)
    assert main_module.MAX_ATTACHMENT_SIZE == 12345
    assert main_module.MAX_IMAGE_ATTACHMENT_SIZE == 100
