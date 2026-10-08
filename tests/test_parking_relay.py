import threading
import unittest
from types import SimpleNamespace as NS
from urllib.parse import parse_qs, urlsplit

from parking_relay import ParkingRelay


class RelayTests(unittest.TestCase):
    def relay(self):
        self.messages = []
        hub = NS(publish_messages=lambda messages: self.messages.extend(messages),
                 stream_snapshot=lambda: [{"camera_id": "parking"}])
        relay = ParkingRelay('http://backend.example', hub, threading.Event())
        self.snapshot = {'event_cursor': '0', 'cameras': [{'camera_id': 'parking',
            'spaces': [{'space_id': 's', 'occupancy': 'occupied'}],
            'summary': {'total': 1, 'occupied': 1, 'empty': 0, 'unknown': 0}}]}
        self.events = []
        relay.fetch = lambda path: self.snapshot if path == '/api/parking' else {'events': [
            e for e in self.events if int(e['event_id']) > int(parse_qs(urlsplit(path).query)['after'][0])][:100]}
        return relay

    def change(self, cursor, state):
        self.snapshot['event_cursor'] = str(cursor)
        self.snapshot['cameras'][0]['spaces'][0]['occupancy'] = state
        self.snapshot['cameras'][0]['summary'] = {'total': 1, **{
            key: int(key == state) for key in ('occupied', 'empty', 'unknown')}}

    def test_current_and_events_separate_deduped_and_outage_unknown(self):
        relay = self.relay()
        relay.poll_once()
        self.assertEqual(self.messages[0][1], 'parking_status')
        relay.poll_once()
        self.assertEqual(len(self.messages), 1)
        self.events = [{'event_id': '1', 'camera_id': 'parking', 'occupancy': 'empty'}]
        self.change(1, 'empty')
        relay.poll_once()
        self.assertEqual(self.messages[-2][1], 'event')
        self.assertEqual(self.messages[-2][2]['kind'], 'parking_occupancy_changed')
        self.assertEqual(self.messages[-1][2]['spaces'][0]['occupancy'], 'empty')
        self.assertEqual(relay.cursor, 1)
        relay.unavailable()
        self.assertEqual(self.messages[-1][2]['spaces'][0]['occupancy'], 'unknown')
        self.assertEqual(self.messages[-1][2]['summary']['unknown'], 1)
        self.assertEqual(self.snapshot['cameras'][0]['spaces'][0]['occupancy'], 'empty')
        self.events = []
        relay.poll_once()
        self.assertEqual(self.messages[-1][2]['spaces'][0]['occupancy'], 'empty')

    def test_deleting_last_space_emits_empty_current_snapshot(self):
        relay = self.relay()
        relay.poll_once()
        self.snapshot['cameras'] = []
        relay.poll_once()
        self.assertEqual(self.messages[-1][2]['spaces'], [])
        self.assertEqual(self.messages[-1][2]['summary']['total'], 0)

    def test_disabled_camera_event_does_not_block_live_cameras(self):
        relay = self.relay()
        relay.poll_once()
        self.events = [{'event_id': '1', 'camera_id': 'disabled'},
                       {'event_id': '2', 'camera_id': 'parking', 'occupancy': 'empty'}]
        self.change(2, 'empty')
        relay.poll_once()
        self.assertEqual(relay.cursor, 2)
        self.assertEqual(self.messages[-2][2]['event_id'], '2')
        self.assertFalse(any(message[0] == 'disabled' for message in self.messages))

    def test_commit_after_snapshot_waits_for_next_snapshot(self):
        relay = self.relay()
        relay.poll_once()
        self.messages.clear()
        self.change(1, 'empty')
        self.events = [{'event_id': str(i), 'camera_id': 'parking', 'occupancy': state}
                       for i, state in ((1, 'empty'), (2, 'occupied'))]
        relay.poll_once()
        self.assertEqual([m[1] for m in self.messages], ['event', 'parking_status'])
        self.assertEqual(self.messages[0][2]['event_id'], '1')
        self.assertEqual(self.messages[1][2]['spaces'][0]['occupancy'], 'empty')
        self.assertEqual(relay.cursor, 1)
        self.change(2, 'occupied')
        relay.poll_once()
        self.assertEqual(self.messages[-2][2]['event_id'], '2')
        self.assertEqual(self.messages[-1][2]['spaces'][0]['occupancy'], 'occupied')

    def test_backlog_is_drained_before_snapshot_without_skipping_events(self):
        relay = self.relay()
        relay.poll_once()
        self.messages.clear()
        self.change(450, 'empty')
        self.events = [{'event_id': str(i), 'camera_id': 'parking', 'occupancy': 'empty'}
                       for i in range(1, 452)]
        relay.poll_once()
        self.assertEqual(self.messages, [])
        self.assertEqual(relay.cursor, 0)
        self.assertEqual(len(relay.pending['events']), 400)
        relay.poll_once()
        self.assertEqual([m[2]['event_id'] for m in self.messages[:-1]],
                         [str(i) for i in range(1, 451)])
        self.assertEqual(self.messages[-1][1], 'parking_status')
        self.assertEqual(relay.cursor, 450)

    def test_failed_batch_does_not_advance_cursor_and_outage_discards_pending(self):
        relay = self.relay()
        relay.poll_once()
        self.change(1, 'empty')
        self.events = [{'event_id': '1', 'camera_id': 'parking', 'occupancy': 'empty'}]
        publish = relay.hub.publish_messages
        relay.hub.publish_messages = lambda messages: (_ for _ in ()).throw(ValueError('fixture'))
        with self.assertRaises(ValueError):
            relay.poll_once()
        self.assertEqual(relay.cursor, 0)
        relay.hub.publish_messages = publish
        relay.unavailable()
        self.assertIsNone(relay.pending)
        self.change(2, 'occupied')
        self.events.append({'event_id': '2', 'camera_id': 'parking', 'occupancy': 'occupied'})
        relay.poll_once()
        self.assertEqual(relay.cursor, 2)
        self.assertEqual(self.messages[-1][2]['spaces'][0]['occupancy'], 'occupied')

    def test_expired_event_history_does_not_reset_cursor(self):
        relay = self.relay()
        self.change(5, 'occupied')
        relay.poll_once()
        self.change(0, 'unknown')
        relay.poll_once()
        self.assertEqual(relay.cursor, 5)
        self.assertEqual(self.messages[-1][2]['spaces'][0]['occupancy'], 'unknown')

    def test_events_always_end_in_current_snapshot_even_when_space_was_deleted(self):
        relay = self.relay()
        self.snapshot['cameras'] = []
        relay.poll_once()
        self.snapshot['event_cursor'] = '1'
        self.events = [{'event_id': '1', 'camera_id': 'parking', 'occupancy': 'occupied'}]
        relay.poll_once()
        self.assertEqual([m[1] for m in self.messages], ['event', 'parking_status'])
        self.assertEqual(self.messages[-1][2]['spaces'], [])
        self.assertEqual(self.messages[-1][2]['summary']['total'], 0)

    def test_roundtrip_events_still_end_in_unchanged_snapshot(self):
        relay = self.relay()
        relay.poll_once()
        self.messages.clear()
        self.change(2, 'occupied')
        self.events = [{'event_id': str(i), 'camera_id': 'parking', 'occupancy': state}
                       for i, state in ((1, 'empty'), (2, 'occupied'))]
        relay.poll_once()
        self.assertEqual([m[1] for m in self.messages], ['event', 'event', 'parking_status'])
        self.assertEqual(self.messages[-1][2]['spaces'][0]['occupancy'], 'occupied')


if __name__ == '__main__':
    unittest.main()
