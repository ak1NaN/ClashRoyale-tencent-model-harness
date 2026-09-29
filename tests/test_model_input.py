import time
import unittest
from ai_client.model_input import inspect_live_input

class ReadinessTests(unittest.TestCase):
    def snapshot(self):
        return {'tick':5,'owner':1,'captured_at_ms':time.time()*1000,
                'coverage':['tick','own_hand','own_elixir','forms'],'complete':False,
                'hand':[{'hand_slot':s,'card_id':26000000+s,'cost':3,'form':0} for s in range(4)]}

    def test_live_partial_lists_missing_battlefield_and_disallows_execution(self):
        report=inspect_live_input(self.snapshot())
        self.assertFalse(report['ready'])
        self.assertFalse(report['execution_allowed'])
        self.assertIn('towers',report['missing'])
        self.assertIn('placement',report['missing'])
        self.assertEqual(report['source'],'live-tencent-native')

    def test_old_records_cannot_pass_as_current_live_state(self):
        snapshot=self.snapshot();snapshot['captured_at_ms']=1
        with self.assertRaisesRegex(ValueError,'过期'):inspect_live_input(snapshot)

    def test_duplicate_slots_are_rejected(self):
        snapshot=self.snapshot();snapshot['hand'][3]['hand_slot']=0
        with self.assertRaisesRegex(ValueError,'槽位'):inspect_live_input(snapshot)

    def test_missing_capture_does_not_construct_demo_state(self):
        with self.assertRaisesRegex(ValueError,'尚未收到'):inspect_live_input(None)
