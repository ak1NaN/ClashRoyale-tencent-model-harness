import copy
import time
import unittest
from ai_client.bridge import NativeStream, deployment_request

def snapshot():
    return dict(captured_at_ms=time.time()*1000, owner=1,generation=2,world='test-world',tick=100,
                mana_raw=60000,hand=[dict(hand_slot=0,card_id=26000018,cost=4,form=2)])
def action():
    return dict(kind='play_card',owner=1,hand_slot=0,card_id=26000018,target_grid=[8,26],
                metadata={'policy_effective_form_code':2},execute_offset_ticks=2)

class BridgeTests(unittest.TestCase):
    def test_hero_ability_is_bound_to_current_native_controller(self):
        state=snapshot()
        state['abilities']=[dict(source_entity=5000018,controller_slot=1,card_id=26000069,
            button_state=2,charges_raw=1,remaining_cooldown_ms=0)]
        skill=dict(kind='activate_ability',owner=1,source_entity=5000018,
            ability_id='SkeletonKing',verified_ability_cost=2,execute_offset_ticks=1)
        req=deployment_request(skill,state,request_id='skill')
        self.assertEqual((req['kind'],req['source_entity'],req['controller_slot'],req['due_tick']),
                         ('activate_ability',5000018,1,101))
        for change in (dict(source_entity=5000019),dict(verified_ability_cost=11),dict(owner=0)):
            altered=dict(skill,**change)
            with self.assertRaises(ValueError):deployment_request(altered,state)
        state['abilities'][0]['charges_raw']=0
        with self.assertRaises(ValueError):deployment_request(skill,state)

    def test_upstream_form_and_native_coordinates(self):
        req=deployment_request(action(),snapshot(),request_id='once')
        self.assertEqual((req['x'],req['y'],req['due_tick'],req['form_code']),(8500,26500,102,2))
        self.assertEqual((req['world'],req['generation'],req['id']),('test-world',2,'once'))

    def test_stale_identity_form_mana_and_delay(self):
        for field,value in [('owner',0),('card_id',123),('execute_offset_ticks',11),('metadata',{})]:
            a=action();a[field]=value
            with self.assertRaises(ValueError):deployment_request(a,snapshot())
        for field,value in [('captured_at_ms',1),('mana_raw',10000)]:
            f=snapshot();f[field]=value
            with self.assertRaises(ValueError):deployment_request(action(),f)

    def test_native_submission_age_matches_live_frame_guard(self):
        now_ms=1_000_000
        recent=snapshot();recent['captured_at_ms']=now_ms-600
        self.assertEqual(deployment_request(action(),recent,now_ms=now_ms)['observed_tick'],100)
        guest_ahead=snapshot();guest_ahead['captured_at_ms']=now_ms+30
        self.assertEqual(deployment_request(action(),guest_ahead,now_ms=now_ms)['observed_tick'],100)
        implausible_future=snapshot();implausible_future['captured_at_ms']=now_ms+101
        with self.assertRaisesRegex(ValueError,'原生状态过期'):
            deployment_request(action(),implausible_future,now_ms=now_ms)
        stale=snapshot();stale['captured_at_ms']=now_ms-751
        with self.assertRaisesRegex(ValueError,'原生状态过期'):
            deployment_request(action(),stale,now_ms=now_ms)

    def test_partial_does_not_enter_complete_stream(self):
        stream=NativeStream();f=snapshot()
        stream.ingest({'event':'native_partial_frame','frame':f})
        self.assertEqual(stream.latest()['guest_captured_at_ms'],f['captured_at_ms'])
        self.assertEqual(stream.latest()['captured_at_ms'],f['captured_at_ms'])
        self.assertIsNone(stream.next())

    def test_guest_clock_offset_is_applied_to_live_freshness(self):
        stream=NativeStream();stream.clock_offset_ms=490
        stream.begin_trial()
        f=snapshot();f['captured_at_ms']-=490
        guest_time=f['captured_at_ms']
        stream.ingest({'event':'native_partial_frame','frame':f})
        normalized=stream.next_trial()
        self.assertEqual(normalized['captured_at_ms'],guest_time+490)
        self.assertEqual(normalized['guest_captured_at_ms'],guest_time)
        self.assertEqual(stream.latest(),normalized)
        self.assertEqual(deployment_request(action(),normalized)['observed_tick'],100)

    def test_complete_queue_never_silently_drops_lstm_frame(self):
        stream=NativeStream(capacity=1)
        for tick in [0,5]:stream.ingest({'event':'native_complete_frame','frame':{'tick':tick}})
        with self.assertRaisesRegex(RuntimeError,'溢出'):stream.next()

    def test_receipt_is_submission_stage_not_fabricated_ack(self):
        stream=NativeStream();r={'event':'native_deploy_receipt','id':'a','stage':'stock_submit','ok':True}
        stream.ingest(r)
        self.assertEqual(stream.receipts['a'],r)
        self.assertNotIn('server_ack',stream.receipts['a'])
