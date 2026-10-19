"""Shared test setup: tests never see this PC's saved window settings."""

from __future__ import annotations

import pytest

from actions import Services

REAL_DATA = Services().data_dir.resolve()


@pytest.fixture(autouse=True)
def _default_window_prefs(monkeypatch):
    """An Assistant built in a test would read data/prefs.json (the Voice and Announce buttons as last set on
    this PC), so a test's speech could be silenced by a choice made in the real app. Inside the real data
    folder, tests get the defaults (both on); a test's own prefs file is read as usual."""
    from core import runtime

    real_load = runtime.load_prefs

    def load(config, path):
        if path is not None and path.resolve().parent == REAL_DATA:
            return {key: True for key in runtime.PREFS}
        return real_load(config, path)

    monkeypatch.setattr(runtime, "load_prefs", load)


@pytest.fixture(autouse=True)
def _no_logging_into_real_memory(monkeypatch):
    """An Assistant built in a test would keep its test conversation in this PC's data/memory.sqlite. A log
    on the real data folder never listens in tests; a test's own log (in tmp_path) works as usual."""
    from core import conversation_log

    real_attach = conversation_log.ConversationLog.attach

    def attach(self, bus):
        if self.db.path.resolve().parent == REAL_DATA:
            return
        real_attach(self, bus)

    monkeypatch.setattr(conversation_log.ConversationLog, "attach", attach)
