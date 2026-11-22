"""Rnd + Gen 단추의 서버 판정: `generate_once` 표가 있는 사람의 Random 에만 한 장을 붙인다.

Runs in the existing tools/test_*.py CI discovery on Windows and Linux.
"""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.backend.server.generation_commands import (  # noqa: E402
    _should_auto_generate_after_random,
    _should_generate_once_after_random,
)


class _Storyteller:
    def __init__(self, running):
        self.running = running

    def is_running(self, *_args):
        return self.running


class _Context:
    def __init__(self, *, generate_on_random=False, auto_generate=False, story_running=False):
        self.options = {"generate_on_random": generate_on_random, "auto_generate": auto_generate}
        self.story = _Storyteller(story_running)

    def get_options(self):
        return dict(self.options)

    def _storyteller_service(self):
        return self.story

    @staticmethod
    def _coerce_bool(value):
        return value is True or str(value).lower() in {"1", "true", "yes", "on"}


class RandomGenerateOnce(unittest.TestCase):
    def once(self, command, **ctx):
        return _should_generate_once_after_random(_Context(**ctx), command, {})

    def test_plain_random_does_not_generate(self):
        self.assertFalse(self.once({"pressed": True}))

    def test_generate_once_flag_generates_without_option(self):
        self.assertTrue(self.once({"pressed": True, "generate_once": True}))

    def test_option_still_works(self):
        self.assertTrue(self.once({"pressed": True}, generate_on_random=True))

    def test_flag_needs_pressed(self):
        self.assertFalse(self.once({"generate_once": True}))

    def test_flag_respects_skip_guards(self):
        self.assertFalse(self.once({"pressed": True, "generate_once": True, "force_naia_skip_generate": True}))
        self.assertFalse(self.once({"pressed": True, "generate_once": True, "respect_naia_autogen": False}))

    def test_flag_blocked_while_storyteller_runs(self):
        self.assertFalse(self.once({"pressed": True, "generate_once": True}, story_running=True))

    def test_flag_does_not_turn_on_continuous(self):
        command = {"pressed": True, "generate_once": True}
        self.assertFalse(_should_auto_generate_after_random(_Context(), command, {}))


if __name__ == "__main__":
    unittest.main()
