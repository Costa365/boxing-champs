import asyncio
import json
import unittest
from unittest.mock import patch

from starlette.datastructures import State

from app import main


BOXER_URL = "https://en.wikipedia.org/wiki/New_Champion"
FLAG = {"country": "mx", "flagUrl": "https://flagcdn.com/w40/mx.png"}


def champion_data(url=BOXER_URL):
    champion = {"name": "New Champion", "wikiUrl": url}
    return [{"name": "Heavyweight", "weight": "", "wba": [champion],
             "wbc": [champion.copy()]}]


class ObservedEvent(asyncio.Event):
    """Let tests know when the worker reaches its wait between passes."""

    def __init__(self):
        super().__init__()
        self.waiting = asyncio.Event()

    async def wait(self):
        self.waiting.set()
        return await super().wait()


class NationalityRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.refresh = ObservedEvent()
        self.saved = asyncio.Event()
        self.enterContext(patch.object(main.app, "state", State({
            "champions_data": [],
            "nationalities": {},
            "nationalities_refresh": self.refresh,
        })))
        self.lookup = self.enterContext(patch.object(
            main, "fetch_nationality", return_value=FLAG
        ))
        self.categories = self.enterContext(patch.object(
            main, "fetch_nationality_from_wiki_categories", return_value=None
        ))
        self.ring = self.enterContext(patch.object(
            main, "fetch_nationality_from_ring", return_value=None
        ))
        self.save = self.enterContext(patch.object(
            main, "_save_nationalities", side_effect=lambda cache: self.saved.set()
        ))
        self.enterContext(patch.object(main, "NATIONALITY_REQUEST_DELAY", 0))
        # All blocking work is mocked; run it inline for deterministic scheduling.
        async def run_inline(func, *args):
            return func(*args)
        self.to_thread = self.enterContext(patch.object(
            main.asyncio, "to_thread", side_effect=run_inline
        ))

    def start_task(self, coroutine):
        task = asyncio.create_task(coroutine)

        async def stop():
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.addAsyncCleanup(stop)
        return task

    async def wait_for(self, event):
        await asyncio.wait_for(event.wait(), timeout=2)

    async def test_scan_wakes_idle_worker_and_updates_api_and_page(self):
        self.start_task(main._enrich_nationalities_loop(poll_seconds=3600))
        await self.wait_for(self.refresh.waiting)
        data = champion_data()
        # Existing flags should be reused without an external lookup.
        data[0]["ibf"] = [{"name": "Known Champion", "wikiUrl": "known"}]
        main.app.state.nationalities["known"] = FLAG.copy()
        self.enterContext(patch.object(main, "fetch_page", return_value="html"))
        parse = self.enterContext(patch.object(main, "parse_champions", return_value=data))

        self.start_task(main._refresh_loop(interval_seconds=3600))
        await self.wait_for(self.saved)

        parse.assert_called_once_with("html")
        self.lookup.assert_called_once_with("New Champion", BOXER_URL)
        self.categories.assert_not_called()
        response = await main.champions()
        for org in ("wba", "wbc", "ibf"):
            self.assertEqual(json.loads(response.body)[0][org][0]["flagUrl"], FLAG["flagUrl"])
        page = main.templates.get_template("index.html").render(divisions=data)
        self.assertEqual(page.count(f'src="{FLAG["flagUrl"]}"'), 3)
        self.assertIn('title="Mexico"', page)

    async def test_refresh_during_lookup_updates_live_snapshot_and_queues_pass(self):
        old_data = champion_data()
        main.app.state.champions_data = old_data
        lookup_started = asyncio.Event()
        finish_lookup = asyncio.Event()
        second_pass_saved = asyncio.Event()

        def on_save(cache):
            if self.save.call_count == 2:
                second_pass_saved.set()
        self.save.side_effect = on_save

        async def run_with_blocked_lookup(func, *args):
            if func is self.lookup and args[1] == BOXER_URL:
                lookup_started.set()
                await finish_lookup.wait()
            return func(*args)
        self.to_thread.side_effect = run_with_blocked_lookup
        self.start_task(main._enrich_nationalities_loop(poll_seconds=3600))
        await self.wait_for(lookup_started)

        live_data = champion_data()
        second_url = "https://en.wikipedia.org/wiki/Another_Champion"
        live_data[0]["ibf"] = [{"name": "Another Champion", "wikiUrl": second_url}]
        main.app.state.champions_data = live_data
        self.refresh.set()
        finish_lookup.set()
        await self.wait_for(second_pass_saved)

        self.assertIs(main.app.state.champions_data, live_data)
        for org in ("wba", "wbc", "ibf"):
            self.assertEqual(live_data[0][org][0]["country"], "mx")
            self.assertEqual(live_data[0][org][0]["flagUrl"], FLAG["flagUrl"])
        self.assertEqual(self.lookup.call_count, 2)
        self.lookup.assert_any_call("Another Champion", second_url)

    async def test_periodic_retry_resolves_cached_miss(self):
        data = champion_data()
        del data[0]["wbc"]
        main.app.state.champions_data = data
        self.lookup.side_effect = [None, FLAG]
        self.start_task(main._enrich_nationalities_loop(poll_seconds=0.01))
        await self.wait_for(self.refresh.waiting)
        self.assertIsNone(main.app.state.nationalities[BOXER_URL]["flagUrl"])
        self.saved.clear()
        await self.wait_for(self.saved)

        self.assertEqual(data[0]["wba"][0]["flagUrl"], FLAG["flagUrl"])
        self.assertEqual(self.lookup.call_count, 2)
        self.categories.assert_called_once_with(BOXER_URL)

    async def test_manual_ring_override_is_preserved(self):
        main.app.state.champions_data = champion_data()
        ring_url = "https://www.ringmagazine.com/fighters/new-champion"
        main.app.state.nationalities[BOXER_URL] = {"ringUrl": ring_url, "flagUrl": None}
        self.ring.return_value = FLAG
        self.start_task(main._enrich_nationalities_loop())
        await self.wait_for(self.saved)

        self.ring.assert_called_once_with(ring_url, None)
        self.lookup.assert_not_called()
        self.assertEqual(main.app.state.nationalities[BOXER_URL]["ringUrl"], ring_url)
        self.assertEqual(main.app.state.champions_data[0]["wbc"][0]["flagUrl"], FLAG["flagUrl"])


if __name__ == "__main__":
    unittest.main()
