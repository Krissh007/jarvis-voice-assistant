"""Phase 0 smoke tests: project layout, config shape, entry point."""

from pathlib import Path

import main

ROOT = Path(main.__file__).resolve().parent


def test_structure_exists() -> None:
    for rel in ["core", "actions", "tests/fakes/fake_live.py", "config.yaml", ".env.example", ".gitignore"]:
        assert (ROOT / rel).exists(), rel


def test_config_has_required_sections() -> None:
    cfg = main.load_config()
    for key in ["user", "voice", "listening", "wake_word", "audio", "hotkeys", "confirm",
                "apps", "sites", "music", "paths", "limits", "features"]:
        assert key in cfg, key
    assert cfg["listening"]["mode"] in {"always_on", "push_to_talk", "wake_word"}
    assert cfg["hotkeys"]["confirm"] == "ctrl+alt+y"
    assert cfg["hotkeys"]["cancel"] == "ctrl+alt+n"
    assert cfg["confirm"]["timeout_s"] == 20
    assert cfg["features"]["wake_word"] is False


def test_gitignore_covers_secrets_and_state() -> None:
    lines = (ROOT / ".gitignore").read_text().splitlines()
    for entry in [".env", "data/*", "logs/*"]:
        assert entry in lines, entry


def test_main_runs(capsys) -> None:
    assert main.main(["--text", "--dry-run"]) == 0
    assert "mode=text" in capsys.readouterr().out
