"""Appearance lock: candidate split, row stripping, C1 bypass and solo-only row picks.

Runs in the existing tools/test_*.py CI discovery on Windows and Linux.
"""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import appearance_lock  # noqa: E402
from core.headless_image_module_param_service import HeadlessImageModuleParamService  # noqa: E402


class _Context:
    def __init__(self, mode="NAI"):
        self.mode = mode
        self.repo_root = str(ROOT)

    def get_api_mode(self):
        return self.mode

    @staticmethod
    def _coerce_bool(value):
        return value is True or str(value).lower() in {"1", "true", "yes", "on"}


class _SearchResults:
    def __init__(self):
        self.calls = []

    def pop_random_row(self, ratings):
        self.calls.append(("any", ratings, ()))
        return "row"

    def pop_random_row_with_tag(self, ratings, tag):
        self.calls.append(("tag", ratings, tag))
        return "solo-row"


SAMPLE_ROW = [
    "1girl", "angel wings", "long hair", "pink eyes", "pink hair", "white ribbon",
    "swimsuit", "white bikini", "smile", "crossed arms", "pool", "night sky", "solo",
]


class LockStateTest(unittest.TestCase):
    def test_set_update_clear(self):
        context = _Context()
        state = appearance_lock.set_lock(context, prompt="girl, pink hair, ", include_clothes=True)
        self.assertTrue(state["active"])
        self.assertEqual(state["prompt"], "girl, pink hair")
        self.assertIn("remove_clothes", appearance_lock.forced_options(appearance_lock.active_lock(context)))
        with self.assertRaises(ValueError):
            appearance_lock.update_lock(context, prompt="  ")
        state = appearance_lock.update_lock(context, prompt="girl, blue hair")
        self.assertEqual(state["prompt"], "girl, blue hair")
        state = appearance_lock.clear_lock(context)
        self.assertFalse(state["active"])
        self.assertEqual(state["prompt"], "")
        self.assertGreater(state["seq"], 0)

    def test_inactive_outside_nai(self):
        context = _Context(mode="WEBUI")
        appearance_lock.set_lock(context, prompt="girl, pink hair")
        self.assertIsNone(appearance_lock.active_lock(context))
        self.assertEqual(appearance_lock.generation_params(context), {})
        self.assertFalse(appearance_lock.public_state(context)["active"])


class TagSplitTest(unittest.TestCase):
    def test_prompt_parsing_drops_weights_artists_and_annotations(self):
        prompt = (
            "1girl, 1.0::artist:wanke ::, artist:foo, \n#특징:\n, pink hair, long hair, "
            "{{{masterpiece, best quality}}}, -3::loli, child::, solo"
        )
        tags = appearance_lock.tags_from_prompt(prompt)
        self.assertEqual(tags, ["1girl", "pink hair", "long hair", "solo"])

    def test_candidates_group_features_and_clothes(self):
        groups = appearance_lock.candidates(SAMPLE_ROW, ["sample (series)"], None, ROOT)
        self.assertEqual(groups["person"], ["girl"])
        self.assertEqual(groups["names"], ["sample (series)"])
        for tag in ("pink hair", "pink eyes", "long hair"):
            self.assertIn(tag, groups["features"])
        self.assertIn("white bikini", groups["clothes"])
        self.assertNotIn("smile", groups["features"])

    def test_strip_row_tags_keeps_person_and_scene(self):
        main = list(SAMPLE_ROW)
        removed = []
        dropped = appearance_lock.strip_row_tags(main, removed, {"include_clothes": False}, None, ROOT)
        self.assertIn("pink hair", dropped)
        self.assertIn("1girl", main)
        self.assertIn("crossed arms", main)
        self.assertIn("white bikini", main)
        self.assertEqual(dropped, removed)

        main = list(SAMPLE_ROW)
        appearance_lock.strip_row_tags(main, [], {"include_clothes": True}, None, ROOT)
        self.assertNotIn("white bikini", main)


class RowPickTest(unittest.TestCase):
    def test_solo_only_while_locked(self):
        context = _Context()
        rows = _SearchResults()
        self.assertEqual(appearance_lock.pop_random_row(context, rows, {"g"}), "row")
        appearance_lock.set_lock(context, prompt="girl, pink hair")
        self.assertEqual(appearance_lock.pop_random_row(context, rows, {"g"}), "solo-row")
        self.assertEqual(rows.calls[-1], ("tag", {"g"}, "solo"))


class SearchResultTagPickTest(unittest.TestCase):
    def test_pop_random_row_with_tag_only_returns_whole_tag_matches(self):
        import pandas as pd
        from core.search_result_model import SearchResultModel

        frame = pd.DataFrame([
            {"id": i, "general": "1girl, solo, smile" if i % 3 == 0 else "2girls, solo focus, hug",
             "character": None, "copyright": None, "artist": None, "meta": None, "rating": "g"}
            for i in range(60)
        ])
        model = SearchResultModel(frame)
        picked = [model.pop_random_row_with_tag({"g"}, "solo") for _ in range(20)]
        self.assertTrue(all(row is not None and "1girl" in row["general"] for row in picked))
        self.assertIsNone(model.pop_random_row_with_tag({"g"}, "solo"))
        self.assertEqual(model.get_count(), 40)


class CandidateSourceTest(unittest.TestCase):
    """Which tags describe the drawn character, per kind of image."""

    class _Item:
        def __init__(self, params=None, row=None):
            self.generation_params = params or {}
            self.source_row = row

    def test_image_drawn_under_lock_uses_the_locked_c1_not_the_row(self):
        from app.backend.server.appearance_lock_routes import _candidate_tags

        item = self._Item(
            params={appearance_lock.APPEARANCE_LOCK_MARKER: True,
                    "_executed_characters": ["girl, aqua hair, purple eyes"]},
            row={"character": "aoi sena", "general": "1girl, purple hair, school uniform, solo"},
        )
        self.assertEqual(_candidate_tags(item, {}), ("lock", [], [], "girl, aqua hair, purple eyes"))

    def test_in_memory_row_comes_with_the_sent_c1(self):
        from app.backend.server.appearance_lock_routes import _candidate_tags

        item = self._Item(params={"_executed_characters": ["girl, red ribbon"]},
                          row={"character": "x (y)", "general": "1girl, pink hair, solo"})
        self.assertEqual(_candidate_tags(item, {}),
                         ("row", ["x (y)"], ["1girl", "pink hair", "solo"], "girl, red ribbon"))

    def test_imported_file_uses_saved_prompt_and_c1(self):
        from app.backend.server.appearance_lock_routes import _candidate_tags

        source, _names, general, c1 = _candidate_tags(None, {
            "prompt": "1girl, 1.0::artist:x ::, smile, solo",
            "characters": ["girl, 3::large breasts::, aqua hair"],
        })
        self.assertEqual((source, general, c1), ("prompt", ["1girl", "smile", "solo"], "girl, 3::large breasts::, aqua hair"))

    def test_c1_units_are_kept_whole_with_weights(self):
        groups = appearance_lock.candidates(
            ["1girl", "pink hair", "white bikini", "solo"], [], None, ROOT,
            character_prompt="girl, 3::large breasts::, fair skin, white bikini",
        )
        self.assertEqual(groups["character"], ["girl", "3::large breasts::", "fair skin"])
        self.assertIn("white bikini", groups["clothes"])
        self.assertEqual(groups["person"], [])
        self.assertIn("pink hair", groups["features"])


class GenerationBypassTest(unittest.TestCase):
    def _service(self):
        context = _Context()
        appearance_lock.set_lock(context, prompt="girl, pink hair", uc="extra arms")
        return HeadlessImageModuleParamService(context)

    def test_plain_request_gets_locked_c1(self):
        params = {"input": "1girl, solo, smile"}
        self._service()._apply_appearance_lock(params, "NAI")
        self.assertEqual(params["sketchbook_character_prompts"], [{"prompt": "girl, pink hair", "uc": "extra arms"}])
        self.assertTrue(params[appearance_lock.APPEARANCE_LOCK_MARKER])

    def test_requests_that_own_characters_are_untouched(self):
        service = self._service()
        for params in (
            {"sketchbook_character_prompts": [{"prompt": "other", "uc": ""}]},
            {"_reference_inset_pin": True},
            {"type": "inpaint"},
            {"_reference_inset_skip_characters": True},
            {"characters": ["girl, red hair"], "_skip_character_late_binding": True},
        ):
            before = dict(params)
            service._apply_appearance_lock(params, "NAI")
            self.assertEqual(params, before)

    def test_auto_generation_keeps_marker_for_live_refetch(self):
        from app.backend.server.generation_runner import _auto_generation_overrides

        overrides = _auto_generation_overrides({
            "sketchbook_character_prompts": [{"prompt": "girl", "uc": ""}],
            appearance_lock.APPEARANCE_LOCK_MARKER: True,
        })
        self.assertTrue(overrides.get(appearance_lock.APPEARANCE_LOCK_MARKER))


if __name__ == "__main__":
    unittest.main()
