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


def test_gitignore_covers_secrets_and_state() -> None:
    lines = (ROOT / ".gitignore").read_text().splitlines()
    for entry in [".env", "data/*", "logs/*"]:
        assert entry in lines, entry


def test_cli_flags() -> None:
    args = main.parse_args(["--text", "--dry-run"])
    assert args.text and args.dry_run


def test_main_refuses_to_start_without_api_key(monkeypatch, capsys) -> None:
    monkeypatch.setenv("GOOGLE_API_KEY", "")  # set-but-empty wins over .env
    assert main.main(["--text"]) == 1
    assert "GOOGLE_API_KEY is missing" in capsys.readouterr().out


def test_config_models_are_set() -> None:
    cfg = main.load_config()
    assert cfg["models"]["live"] and cfg["models"]["flash"] and cfg["voice"]["name"]
