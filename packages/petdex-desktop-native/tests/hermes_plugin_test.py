#!/usr/bin/env python3
"""Behavioral oracles for the embedded Hermes lifecycle plugin.

A hook event that cannot be attributed to a real conversation must not open a
card. A card with no title is indistinguishable from internal machinery and
surfaces as a phantom: bubble text (whatever tool ran last) with no title row,
persisting until the runtime prunes it. The plugin therefore requires a title,
or the prompt that is about to seed one, before it forwards an event.
"""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import sqlite3
import tempfile
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "src" / "assets" / "hermes-petdex-plugin" / "__init__.py"


def load_plugin():
    spec = importlib.util.spec_from_file_location("petdex_hermes_plugin_test", PLUGIN)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class HermesPluginCardGuardTests(unittest.TestCase):
    """The plugin forwards an event only when it can attribute it to a card."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="petdex-hermes-plugin-")
        home = Path(self._tmp.name)
        # The plugin resolves its state DB from HERMES_HOME, and its runner
        # from Path.home(). Pin both so the oracle never touches the real home.
        (home / ".petdex" / "bin").mkdir(parents=True)
        (home / ".petdex" / "bin" / "petdex-hook").write_text("", encoding="utf-8")

        db = home / "state.db"
        with sqlite3.connect(db) as con:
            con.executescript(
                """
                CREATE TABLE sessions (
                    id TEXT PRIMARY KEY, title TEXT, display_name TEXT,
                    source TEXT, model_config TEXT, parent_session_id TEXT,
                    session_key TEXT
                );
                INSERT INTO sessions VALUES ('titled','Fix the tail','','cli','','','');
                INSERT INTO sessions VALUES ('untitled','',NULL,'cli','','','');
                INSERT INTO sessions VALUES ('worker','',NULL,'subagent','','titled','');
                """
            )

        self._prev_home = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = str(home)
        self._real_path_home = pathlib.Path.home
        pathlib.Path.home = staticmethod(lambda: home)

    def tearDown(self) -> None:
        pathlib.Path.home = self._real_path_home
        if self._prev_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = self._prev_home
        self._tmp.cleanup()

    def emit(self, phase: str, payload: dict) -> list[dict]:
        module = load_plugin()
        sent: list[bytes] = []

        class FakeSubprocess:
            DEVNULL = -3

            def run(self, *_args, **kwargs):  # noqa: ANN002, ANN003
                sent.append(kwargs.get("input"))
                return types.SimpleNamespace(returncode=0)

        module.subprocess = FakeSubprocess()
        module._callback(phase)(**payload)
        return [json.loads(item.decode("utf-8")) for item in sent]

    def test_a_tool_event_for_a_titled_session_opens_a_card(self) -> None:
        events = self.emit("post", {"session_id": "titled", "tool_name": "process_manage"})
        self.assertEqual(len(events), 1, "a titled session must render its card")
        self.assertEqual(events[0]["petdex_session_title"], "Fix the tail")

    def test_a_tool_event_without_a_title_is_deferred(self) -> None:
        # The phantom shape: a row exists but carries no title, so the card
        # would render as bare tool text with no title row.
        self.assertEqual(
            self.emit("post", {"session_id": "untitled", "tool_name": "process_manage"}),
            [],
        )

    def test_an_unresolvable_session_is_deferred(self) -> None:
        self.assertEqual(
            self.emit("post", {"session_id": "missing", "tool_name": "process_manage"}),
            [],
        )

    def test_an_event_without_any_session_is_deferred(self) -> None:
        self.assertEqual(self.emit("post", {"tool_name": "process_manage"}), [])

    def test_the_first_prompt_opens_a_card_without_a_stored_title(self) -> None:
        # A real conversation seeds its title from the prompt, so deferring on
        # the titleless tool event costs it nothing: the prompt opens the card.
        events = self.emit("user-prompt", {"session_id": "untitled", "user_message": "hello"})
        self.assertEqual(len(events), 1, "the first prompt must open the card")

    def test_a_prompt_for_an_unreadable_session_opens_a_card(self) -> None:
        # No readable row, so no stored title and no session id to label the
        # card with: the prompt is its only title. Both policies forward this
        # event, and dropping it would leave a real conversation silent.
        events = self.emit("user-prompt", {"session_id": "missing", "user_message": "hello"})
        self.assertEqual(len(events), 1, "the prompt must open the card")
        self.assertNotIn("petdex_session_title", events[0])

    def test_a_worker_is_still_suppressed(self) -> None:
        self.assertEqual(
            self.emit("post", {"session_id": "worker", "tool_name": "terminal"}),
            [],
        )


if __name__ == "__main__":
    unittest.main()
