"""Events carry the time they happened (the web UI shows it on hover)."""
import time
import unittest

from harness.controller import transcript_events
from harness.events import ChatEvent, EventBus, StatusEvent, event_to_json


class EventTimes(unittest.TestCase):
    def test_put_stamps_the_time_once(self):
        bus = EventBus()
        sub = bus.subscribe(replay=False)
        before = time.time()
        ev = ChatEvent("assistant", "hi")
        bus.put(ev)
        stamped = event_to_json(sub.get_nowait())["ts"]
        self.assertGreaterEqual(stamped, before)
        bus.put(ev)                                   # a second put keeps the first time
        self.assertEqual(event_to_json(sub.get_nowait())["ts"], stamped)

    def test_replay_keeps_the_original_time(self):
        bus = EventBus()
        bus.put(ChatEvent("assistant", "earlier"))
        first = bus.subscribe().get_nowait()
        time.sleep(0.01)
        replayed = bus.subscribe().get_nowait()
        self.assertEqual(event_to_json(replayed)["ts"], event_to_json(first)["ts"])

    def test_a_loaded_session_has_no_times(self):
        bus = EventBus()
        sub = bus.subscribe(replay=False)
        for ev in transcript_events([{"role": "user", "content": "q"},
                                     {"role": "assistant", "content": "a"}]):
            bus.put(ev)
        self.assertEqual([event_to_json(sub.get_nowait())["ts"] for _ in range(2)], [None, None])

    def test_status_events_serialise_too(self):
        self.assertIn("ts", event_to_json(StatusEvent("coding", "m", "/w", 0, "normal")))


if __name__ == "__main__":
    unittest.main()
