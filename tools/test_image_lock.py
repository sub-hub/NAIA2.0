"""Image lock: tag categories, per-category row shaping, C1 bypass, row filter and lock pools.

Runs in the existing tools/test_*.py CI discovery on Windows and Linux.
"""
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import image_lock, image_lock_pool  # noqa: E402
from core.headless_image_module_param_service import HeadlessImageModuleParamService  # noqa: E402


_FILTER_MANAGER = None
# User data folders of the test contexts (prefs, search presets); removed after the module.
_DATA_ROOT = tempfile.TemporaryDirectory()


def tearDownModule():
    _DATA_ROOT.cleanup()


def _filter_manager():
    """The real tag dictionaries; without them expression, pose and location cannot be told apart."""
    global _FILTER_MANAGER
    if _FILTER_MANAGER is None:
        import contextlib
        import io

        from core.filter_data_manager import FilterDataManager

        with contextlib.redirect_stdout(io.StringIO()):
            _FILTER_MANAGER = FilterDataManager(str(ROOT / "data"))
    return _FILTER_MANAGER


class _Context:
    def __init__(self, mode="NAI", directory=None):
        self.mode = mode
        self.repo_root = str(ROOT)
        if directory is None:
            directory = tempfile.mkdtemp(dir=_DATA_ROOT.name)
        self._directory = Path(directory)
        self.filter_data_manager = _filter_manager()

    def get_api_mode(self):
        return self.mode

    def _save_path(self, name):
        return str(self._directory / name)

    @staticmethod
    def _coerce_bool(value):
        return value is True or str(value).lower() in {"1", "true", "yes", "on"}


SAMPLE_ROW = [
    "1girl", "angel wings", "long hair", "pink eyes", "pink hair", "white ribbon",
    "swimsuit", "white bikini", "smile", "crossed arms", "cowboy shot", "pool", "night sky", "solo",
]


def _groups():
    return image_lock.candidates(SAMPLE_ROW, names=["sample (series)"], copyright_tags=["series"],
                                 filter_manager=_filter_manager(), repo_root=ROOT)


def _lock(context, preset="look", **overrides):
    groups = _groups()
    modes = dict(image_lock.PRESETS[preset])
    modes.update(overrides.pop("modes", {}))
    return image_lock.set_lock(context, groups=groups, kept=groups, modes=modes, **overrides)


class ClassifyTest(unittest.TestCase):
    def test_categories(self):
        groups = _groups()
        self.assertEqual(groups["person"], ["1girl", "solo"])
        self.assertEqual(groups["names"], ["sample (series)"])
        self.assertEqual(groups["copyright"], ["series"])
        for tag in ("pink hair", "pink eyes", "long hair", "angel wings"):
            self.assertIn(tag, groups["features"])
        self.assertIn("white bikini", groups["clothes"])
        self.assertIn("smile", groups["expression"])
        self.assertIn("cowboy shot", groups["composition"])
        self.assertNotIn("cowboy shot", groups["pose"])

    def test_prompt_parsing_drops_weights_artists_and_annotations(self):
        prompt = (
            "1girl, 1.0::artist:wanke ::, artist:foo, \n#특징:\n, pink hair, long hair, "
            "{{{masterpiece, best quality}}}, -3::loli, child::, solo, nahida \\(genshin impact\\)"
        )
        tags = image_lock.tags_from_prompt(prompt)
        self.assertEqual(tags, ["1girl", "pink hair", "long hair", "solo", "nahida (genshin impact)"])
        self.assertIn("nahida (genshin impact)", image_lock.classify_tags(tags, _filter_manager(), ROOT)["names"])

    def test_general_tags_with_a_qualifier_are_not_names(self):
        tags = ["sword (weapon)", "cross (symbol)", "jacket (mecha)", "nahida (genshin impact)"]
        self.assertEqual(image_lock.classify_tags(tags, _filter_manager(), ROOT)["names"], ["nahida (genshin impact)"])
        groups = image_lock.candidates([], character_prompt="girl, alternate breast size (larger), bow (weapon)",
                                       filter_manager=_filter_manager(), repo_root=ROOT)
        self.assertEqual(groups["names"], [])

    def test_c1_units_are_kept_whole_with_weights(self):
        groups = image_lock.candidates(
            ["1girl", "pink hair", "white bikini", "solo"], filter_manager=_filter_manager(), repo_root=ROOT,
            character_prompt="girl, 3::large breasts::, fair skin, white bikini",
        )
        self.assertEqual(groups["features"][:2], ["3::large breasts::", "fair skin"])
        self.assertIn("pink hair", groups["features"])
        self.assertIn("white bikini", groups["clothes"])
        self.assertIn("1girl", groups["person"])


class StateTest(unittest.TestCase):
    def test_set_update_clear(self):
        context = _Context()
        state = _lock(context)
        self.assertTrue(state["active"])
        self.assertTrue(state["c1_mode"])
        self.assertTrue(state["prompt"].startswith("girl, sample (series), "))
        self.assertNotIn("white bikini", state["prompt"])      # clothes roll in "look"
        state = image_lock.update_lock(context, prompt="girl, sample (series), blue hair")
        self.assertEqual(state["kept"]["features"], ["blue hair"])
        self.assertEqual(state["kept"]["names"], ["sample (series)"])
        with self.assertRaises(ValueError):
            image_lock.update_lock(context, prompt="girl")
        self.assertFalse(image_lock.clear_lock(context)["active"])

    def test_keep_on_an_empty_category_becomes_none(self):
        context = _Context()
        state = _lock(context, modes={"object": "keep"})
        self.assertEqual(state["modes"]["object"], "none")

    def test_inactive_outside_nai(self):
        context = _Context(mode="WEBUI")
        _lock(context)
        self.assertIsNone(image_lock.active_lock(context))
        self.assertEqual(image_lock.generation_params(context), {})

    def test_nsfw_switch_applies_and_restores_clothes(self):
        with tempfile.TemporaryDirectory() as directory:
            context = _Context(directory=directory)
            image_lock.save_prefs(context, {"nsfw_clothes_mode": "random"})
            _lock(context, preset="character")
            state = image_lock.update_lock(context, nsfw=True)
            self.assertEqual(state["modes"]["clothes"], "random")
            state = image_lock.update_lock(context, nsfw=False)
            self.assertEqual(state["modes"]["clothes"], "keep")
            # The dialog sends the switch with its own modes: those win over the default.
            modes = dict(state["modes"], clothes="keep")
            state = image_lock.update_lock(context, nsfw=True, modes=modes, sfw_clothes_mode="keep")
            self.assertEqual(state["modes"]["clothes"], "keep")
            self.assertEqual(state["sfw_clothes_mode"], "keep")


class RowShapeTest(unittest.TestCase):
    def test_look_keeps_scene_from_row_and_drops_row_look(self):
        context = _Context()
        _lock(context)
        lock = image_lock.active_lock(context)
        row = ["1girl", "solo", "blonde hair", "maid", "lying", "bedroom", "from above", "highres"]
        removed = []
        dropped = image_lock.apply_to_row(row, removed, lock, _filter_manager(), ROOT)
        self.assertIn("blonde hair", dropped)
        self.assertEqual(dropped, removed)
        for tag in ("1girl", "solo", "maid", "lying", "bedroom", "from above", "highres"):
            self.assertIn(tag, row)

    def test_scene_keeps_original_scene_and_takes_only_pose(self):
        context = _Context()
        _lock(context, preset="scene")
        lock = image_lock.active_lock(context)
        row = ["1girl", "solo", "blonde hair", "maid", "lying", "bedroom", "from above", "blush"]
        image_lock.apply_to_row(row, [], lock, _filter_manager(), ROOT)
        self.assertIn("lying", row)
        self.assertIn("blush", row)
        for tag in ("blonde hair", "maid", "bedroom", "from above"):
            self.assertNotIn(tag, row)
        for tag in ("pool", "night sky", "cowboy shot"):
            self.assertIn(tag, row)          # kept tags go to the main prompt
        self.assertNotIn("pink hair", row)   # the look goes to C1

    def test_new_character_sends_no_c1_and_keeps_look_from_row(self):
        context = _Context()
        _lock(context, preset="scene_new_character")
        lock = image_lock.active_lock(context)
        self.assertFalse(image_lock.c1_mode(lock))
        self.assertEqual(image_lock.generation_params(context),
                         {image_lock.IMAGE_LOCK_NO_CHARACTERS: True, image_lock.IMAGE_LOCK_MARKER: True})
        row = ["1girl", "solo", "blonde hair", "lying", "bedroom"]
        image_lock.apply_to_row(row, [], lock, _filter_manager(), ROOT)
        self.assertIn("blonde hair", row)
        self.assertNotIn("lying", row)
        self.assertIn("white bikini", row)   # kept clothes go to main without C1

    def test_forced_options_follow_modes(self):
        context = _Context()
        _lock(context, preset="scene")
        forced = image_lock.forced_options(image_lock.active_lock(context))
        self.assertTrue(forced["remove_character_features"])
        self.assertTrue(forced["remove_location_and_background_color"])
        self.assertFalse(forced["remove_pose_action"])       # pose rolls
        self.assertFalse(forced["remove_expression"])


class RowFilterTest(unittest.TestCase):
    def test_solo_and_partner(self):
        context = _Context()
        _lock(context)
        accept = image_lock.row_filter(image_lock.active_lock(context))
        self.assertTrue(accept({"1girl", "solo"}))
        self.assertFalse(accept({"1girl", "1boy", "hetero"}))
        self.assertFalse(accept({"1boy", "solo"}))
        image_lock.update_lock(context, partner=True)
        # The partner toggle only applies in NSFW.
        self.assertFalse(image_lock.row_filter(image_lock.active_lock(context))({"1girl", "1boy", "hetero"}))
        image_lock.update_lock(context, nsfw=True)
        accept = image_lock.row_filter(image_lock.active_lock(context))
        self.assertTrue(accept({"1girl", "1boy", "hetero"}))
        self.assertTrue(accept({"1girl", "2boys"}))
        self.assertFalse(accept({"2girls", "1girl"}))
        self.assertFalse(accept({"1girl", "multiple girls"}))

    def test_pop_uses_main_pool_without_preset(self):
        class _Rows:
            def __init__(self):
                self.calls = []

            def pop_random_row(self, ratings):
                self.calls.append(("any", ratings))
                return "row"

            def pop_random_row_where(self, ratings, accept):
                self.calls.append(("where", ratings, accept({"1girl", "solo"})))
                return "solo-row"

        context = _Context()
        rows = _Rows()
        self.assertEqual(image_lock.pop_random_row(context, rows, {"g"}), "row")
        _lock(context)
        self.assertEqual(image_lock.pop_random_row(context, rows, {"g"}), "solo-row")
        self.assertEqual(rows.calls[-1], ("where", {"g"}, True))


class SearchResultWhereTest(unittest.TestCase):
    def test_pop_random_row_where_only_returns_accepted_rows(self):
        import pandas as pd
        from core.search_result_model import SearchResultModel

        frame = pd.DataFrame([
            {"id": i, "general": "1girl, solo, smile" if i % 3 == 0 else "2girls, solo focus, hug",
             "character": None, "copyright": None, "artist": None, "meta": None, "rating": "g"}
            for i in range(60)
        ])
        model = SearchResultModel(frame)
        accept = lambda tags: "solo" in tags  # noqa: E731
        picked = [model.pop_random_row_where({"g"}, accept) for _ in range(20)]
        self.assertTrue(all(row is not None and "1girl" in row["general"] for row in picked))
        self.assertIsNone(model.pop_random_row_where({"g"}, accept))
        self.assertEqual(model.get_count(), 40)


class LockPoolTest(unittest.TestCase):
    def test_pool_is_built_once_and_rebuilt_after_preset_change(self):
        import pandas as pd
        from core import search_presets

        with tempfile.TemporaryDirectory() as directory:
            context = _Context(directory=directory)
            built = []

            def builder(preset, permanent):
                built.append((preset["query"], permanent))
                return pd.DataFrame([{"id": 1, "general": "1girl, solo", "rating": "e", "character": None,
                                      "copyright": None, "artist": None, "meta": None}])

            context.image_lock_pool_builder = builder
            self.assertIsNone(image_lock_pool.pool_for(context, ""))
            search_presets.save_preset(context, {"name": "NSFW", "query": "1girl", "ratings": ["e"]})
            first = image_lock_pool.pool_for(context, "nsfw")
            self.assertIs(image_lock_pool.pool_for(context, "NSFW"), first)
            search_presets.save_preset(context, {"name": "NSFW", "query": "1girl, 1boy", "ratings": ["e"]})
            self.assertIsNot(image_lock_pool.pool_for(context, "NSFW"), first)
            self.assertEqual(built, [("1girl", ""), ("1girl, 1boy", "")])
            # The shared permanent exclude is part of the pool's condition.
            context.search_filter_state = {"exclude_permanent": "futanari"}
            image_lock_pool.pool_for(context, "NSFW")
            self.assertEqual(built[-1], ("1girl, 1boy", "futanari"))

            image_lock.save_prefs(context, {"nsfw_preset": "NSFW"})
            _lock(context, nsfw=True)
            row = image_lock.pop_random_row(context, None, {"g"})
            self.assertEqual(row["id"], 1)
            # Exhausted: searched again.
            row = image_lock.pop_random_row(context, None, {"g"})
            self.assertEqual(row["id"], 1)
            self.assertEqual(len(built), 4)


class CandidateSourceTest(unittest.TestCase):
    """Which tags the dialog offers, per kind of image."""

    class _Item:
        def __init__(self, params=None, row=None):
            self.generation_params = params or {}
            self.source_row = row

    def test_request_body_wins_over_row(self):
        from app.backend.server.image_lock_routes import _candidate_source

        item = self._Item(
            params={"_prompt_body_tags": ["1girl", "smile", "solo"],
                    "_prompt_source_names": {"character": "aoi sena", "copyright": "blue archive"},
                    "_executed_characters": ["girl, aqua hair"]},
            row={"character": "x", "general": "1girl, purple hair, solo"},
        )
        source, kwargs = _candidate_source(item, {})
        self.assertEqual(source, "body")
        self.assertEqual(kwargs["body_tags_in"], ["1girl", "smile", "solo"])
        self.assertEqual(kwargs["names"], ["aoi sena"])
        self.assertEqual(kwargs["character_prompt"], "girl, aqua hair")

    def test_row_of_an_image_drawn_under_a_lock_is_not_used(self):
        from app.backend.server.image_lock_routes import _candidate_source

        item = self._Item(params={image_lock.IMAGE_LOCK_MARKER: True, "_executed_characters": ["girl, aqua hair"]},
                          row={"character": "x", "general": "1girl, purple hair, solo"})
        source, kwargs = _candidate_source(item, {"prompt": "1girl, smile, solo"})
        self.assertEqual(source, "prompt")
        self.assertEqual(kwargs["character_prompt"], "girl, aqua hair")

    def test_imported_file_uses_saved_prompt_and_c1(self):
        from app.backend.server.image_lock_routes import _candidate_source

        source, kwargs = _candidate_source(None, {
            "prompt": "1girl, 1.0::artist:x ::, smile, solo",
            "characters": ["girl, 3::large breasts::, aqua hair"],
        })
        self.assertEqual(source, "prompt")
        self.assertEqual(kwargs["body_tags_in"], ["1girl", "smile", "solo"])


class GenerationBypassTest(unittest.TestCase):
    def _service(self):
        context = _Context()
        _lock(context, uc="extra arms")
        return HeadlessImageModuleParamService(context)

    def test_plain_request_gets_locked_c1(self):
        params = {"input": "1girl, solo, smile"}
        self._service()._apply_image_lock(params, "NAI")
        self.assertEqual(params["sketchbook_character_prompts"][0]["uc"], "extra arms")
        self.assertIn("pink hair", params["sketchbook_character_prompts"][0]["prompt"])
        self.assertTrue(params[image_lock.IMAGE_LOCK_MARKER])

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
            service._apply_image_lock(params, "NAI")
            self.assertEqual(params, before)

    def test_auto_generation_keeps_marker_for_live_refetch(self):
        from app.backend.server.generation_runner import _auto_generation_overrides

        overrides = _auto_generation_overrides({
            "sketchbook_character_prompts": [{"prompt": "girl", "uc": ""}],
            image_lock.IMAGE_LOCK_MARKER: True,
        })
        self.assertTrue(overrides.get(image_lock.IMAGE_LOCK_MARKER))


if __name__ == "__main__":
    unittest.main()
