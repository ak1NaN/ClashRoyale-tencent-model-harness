import copy
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
import threading

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'third_party/FirstLight_CR'))
from ai_client.config import defaults
from ai_client.control import validate_frame, action_points, AutoController, REQUIRED_COVERAGE

def frame(tick=0):
    # Synthetic unit-test data only; never used by the live reader.
    cards = [26000000, 26000001, 26000002, 26000003]
    return {'schema': 'tencent-firstlight-frame.v1', 'client_version': defaults()['expected_version'],
            'captured_at_ms': time.time()*1000, 'battle_id': 'test', 'battle_active': True,
            'coverage': list(REQUIRED_COVERAGE), 'screen_flip': False,
            'episode': {'tick_ms': 50}, 'initial_elixir': {'0': 5., '1': 5.},
            'observation': {'tier': 'fair', 'owner': 0, 'tick': tick, 'episode_id': 'test',
                'time': {}, 'towers': [], 'entities': [], 'events': [], 'terminal': {'ended': False},
                'players': [{'owner': 0, 'hand': cards, 'elixir_exact': 5., 'private_state_visible': True,
                    'metadata': {'hand_slot_by_card': {str(c): s for s,c in enumerate(cards)}}}],
                'action_mask': {'hand_slots': [True]*4, 'placement_masks': {
                    str(s): {'effective_cost': 3., 'row_major': [[True]*18 for _ in range(32)]}
                    for s in range(4)}}}}

class GateTests(unittest.TestCase):
    def setUp(self):
        self.config = defaults()

    def test_unverified_native_execution_is_blocked_before_any_input(self):
        policy = SimpleNamespace(process=SimpleNamespace(poll=lambda: None))
        device = SimpleNamespace(connected=True)
        controller = AutoController(self.config, policy, device, lambda *args: None)
        with self.assertRaisesRegex(RuntimeError, '出站回执'):
            controller.start('live')
        self.assertIsNone(controller.thread)

    def test_stale_missing_and_wrong_version_never_reach_model(self):
        for mutate in [lambda f: f.update(captured_at_ms=1),
                       lambda f: f.update(coverage=['tick']),
                       lambda f: f.update(client_version='new'),
                       lambda f: f['observation'].update(episode_id='other')]:
            f = frame()
            mutate(f)
            with self.assertRaises(ValueError):
                validate_frame(f, self.config)

    def test_card_identity_must_match_current_slot(self):
        action = {'kind':'play_card','owner':0,'hand_slot':0,'card_id':26000001,'target_grid':[2,3]}
        with self.assertRaisesRegex(ValueError, '槽位'):
            action_points(action, frame(), self.config)

    def test_insufficient_elixir_never_touches(self):
        action = {'kind':'play_card','owner':0,'hand_slot':0,'card_id':26000000,'target_grid':[2,3]}
        f = frame()
        f['observation']['players'][0]['elixir_exact'] = 2.
        with self.assertRaisesRegex(ValueError, '圣水不足'):
            action_points(action, f, self.config)

    def test_native_coordinates_rotate_once_for_top_player(self):
        action = {'kind':'play_card','owner':0,'hand_slot':0,'card_id':26000000,'target_grid':[2,3]}
        f = frame()
        start, normal = action_points(action, f, self.config)
        f['screen_flip'] = True
        start2, flipped = action_points(action, f, self.config)
        self.assertEqual(start, start2)
        left, top, right, bottom = self.config['arena']
        self.assertLessEqual(abs(normal[0]+flipped[0] - (left+right)*1440), 1)
        self.assertLessEqual(abs(normal[1]+flipped[1] - (top+bottom)*2560), 1)

    def test_masked_placement_is_rejected(self):
        action = {'kind':'play_card','owner':0,'hand_slot':0,'card_id':26000000,'target_grid':[2,3]}
        f = frame()
        f['observation']['action_mask']['placement_masks']['0']['row_major'][3][2] = False
        with self.assertRaises(ValueError):
            action_points(action, f, self.config)

class LifecycleTests(unittest.TestCase):
    def test_a_gap_ends_lstm_and_never_infers_or_touches(self):
        with tempfile.TemporaryDirectory() as directory:
            config = defaults()
            config['frame_directory'] = directory
            for index, tick in enumerate((0, 10)):
                import json
                (Path(directory)/f'frame-{index:08d}.json').write_text(json.dumps(frame(tick)))
            calls = []
            def request(value):
                calls.append(value['op'])
                return {'ok':True}
            events = []
            device = SimpleNamespace(detached=threading.Event())
            controller = AutoController(config, SimpleNamespace(request=request), device,
                                        lambda *event: events.append(event))
            controller.run()
            self.assertEqual(calls, ['start', 'end'])
            self.assertTrue(any('丢帧' in str(value) for kind,value in events if kind=='error'))

    def test_missing_bridge_is_explicit_and_does_not_touch(self):
        with tempfile.TemporaryDirectory() as directory:
            config = defaults()
            config['frame_directory'] = directory
            events = []
            device = SimpleNamespace(detached=threading.Event())
            controller = AutoController(config, None, device, lambda *event: events.append(event))
            worker = threading.Thread(target=controller.run)
            worker.start()
            time.sleep(.06)
            controller.stop()
            worker.join(1)
            self.assertFalse(worker.is_alive())
            self.assertIn('等待完整腾讯观测', events[0][1])

if __name__ == '__main__':
    unittest.main()
