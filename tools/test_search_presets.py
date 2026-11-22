"""Search presets: normalizing, saving, overwriting, deleting and the stale-pool key.

Runs in the existing tools/test_*.py CI discovery on Windows and Linux.
"""
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import search_presets  # noqa: E402


class _Context:
    def __init__(self, directory):
        self.directory = Path(directory)

    def _save_path(self, name):
        return str(self.directory / name)


class NormalizeTest(unittest.TestCase):
    def test_name_required_and_collapsed(self):
        self.assertIsNone(search_presets.normalize({"name": "   "}))
        self.assertIsNone(search_presets.normalize("not a dict"))
        preset = search_presets.normalize({"name": "  NSFW   poses "})
        self.assertEqual(preset["name"], "NSFW poses")

    def test_ratings_order_and_empty_fallback(self):
        self.assertEqual(search_presets.normalize({"name": "a", "ratings": ["e", "Q"]})["ratings"], ["q", "e"])
        self.assertEqual(search_presets.normalize({"name": "a", "ratings": "es"})["ratings"], ["s", "e"])
        self.assertEqual(search_presets.normalize({"name": "a", "ratings": []})["ratings"], list("gsqe"))

    def test_bucket_range_swapped_and_invalid_dropped(self):
        preset = search_presets.normalize({"name": "a", "bucket_start": 9, "bucket_end": 3})
        self.assertEqual((preset["bucket_start"], preset["bucket_end"]), (3, 9))
        preset = search_presets.normalize({"name": "a", "bucket_start": True, "bucket_end": -1})
        self.assertEqual((preset["bucket_start"], preset["bucket_end"]), (None, None))


class StorageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.context = _Context(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_save_overwrite_find_delete(self):
        self.assertEqual(search_presets.load_presets(self.context), [])
        search_presets.save_preset(self.context, {"name": "SFW", "query": "1girl", "exclude": "1boy",
                                                  "exclude_permanent": "ignored"})
        items = search_presets.save_preset(self.context, {"name": "nsfw", "query": "1girl, 1boy", "ratings": ["e"]})
        self.assertEqual([item["name"] for item in items], ["nsfw", "SFW"])
        self.assertTrue(items[0]["saved_at"])

        # Same name in another case overwrites instead of adding.
        items = search_presets.save_preset(self.context, {"name": "NSFW", "query": "solo", "ratings": ["e"]})
        self.assertEqual(len(items), 2)
        found = search_presets.find_preset(self.context, "nsfw")
        self.assertEqual((found["name"], found["query"]), ("NSFW", "solo"))
        sfw = search_presets.find_preset(self.context, "sfw")
        self.assertEqual(sfw["exclude"], "1boy")
        self.assertNotIn("exclude_permanent", sfw)     # the permanent exclude is shared, not per preset

        items = search_presets.delete_preset(self.context, "sfw")
        self.assertEqual([item["name"] for item in items], ["NSFW"])
        self.assertIsNone(search_presets.find_preset(self.context, "SFW"))

    def test_broken_file_reads_as_empty(self):
        search_presets.presets_path(self.context).write_text("{not json", encoding="utf-8")
        self.assertEqual(search_presets.load_presets(self.context), [])

    def test_excludes_tag_follows_search_rules(self):
        excludes = search_presets.excludes_tag
        self.assertTrue(excludes({}, "1boy", permanent="animal, 1boy"))
        self.assertTrue(excludes({"exclude": "boy"}, "1boy"))              # plain term = substring
        self.assertTrue(excludes({"exclude": "{2girls|*1boy}"}, "1boy"))
        self.assertFalse(excludes({"exclude": "{2girls|~1boy}"}, "1boy"))     # `~` is literal inside {a|b}
        self.assertFalse(excludes({"exclude": "~boy"}, "1boy"))            # ~ = whole tag
        self.assertFalse(excludes({"exclude": "1boys"}, "1boy", permanent="multiple boys"))

    def test_condition_key_ignores_name_and_time(self):
        first = search_presets.normalize({"name": "a", "query": "x", "saved_at": "1"})
        second = search_presets.normalize({"name": "b", "query": "x", "saved_at": "2"})
        self.assertEqual(search_presets.condition_key(first), search_presets.condition_key(second))
        # A changed shared permanent exclude makes a built pool stale too.
        self.assertNotEqual(search_presets.condition_key(first), search_presets.condition_key(first, "1boy"))


class CommandTest(unittest.TestCase):
    def test_save_broadcasts_and_get_replies_to_requester(self):
        import asyncio
        import json

        from app.backend.server.search_commands import handle_search_command

        class _Ws:
            def __init__(self):
                self.sent = []

            async def send_text(self, text):
                self.sent.append(json.loads(text))

        async def run_in_thread(func, *args, **kwargs):
            return func(*args, **kwargs)

        broadcasts = []

        async def broadcast_json(clients, payload):
            broadcasts.append(payload)

        with tempfile.TemporaryDirectory() as directory:
            context = _Context(directory)
            ws = _Ws()

            async def scenario():
                handled = await handle_search_command(
                    ws, context, {"type": "save_search_preset", "preset": {"name": "NSFW", "ratings": ["e"]}},
                    run_in_thread=run_in_thread, clients=set(), broadcast_json=broadcast_json,
                )
                self.assertTrue(handled)
                await handle_search_command(ws, context, {"type": "get_search_presets"}, run_in_thread=run_in_thread)
                await handle_search_command(
                    ws, context, {"type": "delete_search_preset", "name": "nsfw"}, run_in_thread=run_in_thread,
                )

            asyncio.run(scenario())

        self.assertEqual(broadcasts[0]["type"], "search_presets")
        self.assertEqual([item["name"] for item in broadcasts[0]["items"]], ["NSFW"])
        # Without a client registry the reply goes to the requester only.
        self.assertEqual([item["name"] for item in ws.sent[0]["items"]], ["NSFW"])
        self.assertEqual(ws.sent[1]["items"], [])


if __name__ == "__main__":
    unittest.main()
