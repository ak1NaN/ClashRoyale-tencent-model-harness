import threading,time
from types import SimpleNamespace
import pytest
import ai_client.autonomous as module
from ai_client.bridge import NativeQueueStateChanged

class Stream:
    # Controller tests supply frames only as the loop consumes them. The
    # transport's catch-up behavior is covered separately in test_live_mapping.
    def fresh_trial_frame(self,frame):return frame,[]
    def __init__(self):
        self.frames=[dict(tick=t,world='live',generation=2,captured_at_ms=time.time()*1000,
            battle_finished=t==95,native_end_marker=4001 if t==95 else 0) for t in (0,90,95)]
        self.receipts={}
    def begin_trial(self):pass
    def end_trial(self):pass
    def next_trial(self):return self.frames.pop(0) if self.frames else None
    def latest(self):return dict(tick=90,world='live',generation=2)
class Policy:
    process=SimpleNamespace(poll=lambda:None)
    def request(self,r):
        if r['op']=='live_end':return {'ok':True}
        tick=r['snapshot']['tick']
        return dict(ok=True,tick=tick,actions=[dict(kind='play_card',hand_slot=0,card_id=26000017)] if tick==90 else [],
            inference_ms=1,limitations=['test-fixture'],mapping={'towers':6,'entities':0})
class Device:
    connected=True
    def __init__(self):self.stream=Stream();self.detached=threading.Event();self.calls=[]
    def cancel_native(self):self.calls.append('cancel')
    def enable_native(self,v):self.calls.append('enable')
    def queue_action(self,a):self.calls.append('queue');return dict(request={'id':'pending'},queued={'queued':True})

def test_native_terminal_cancels_without_inventing_pending_application(tmp_path,monkeypatch):
    monkeypatch.setattr(module,'ROOT',tmp_path);events=[];d=Device()
    battle=module.AutonomousBattle({'checkpoint':'test-fixture'},Policy(),d,lambda e,v:events.append((e,v)))
    battle.start();battle.thread.join(2)
    assert not battle.running() and d.calls.count('queue')==1
    assert battle.last_result['error'] is None and battle.last_result['applied']==0
    assert any(e=='battle_finished' for e,v in events)
    assert d.calls[-1]=='cancel'


def test_min_card_gap_blocks_second_card_and_masks_next_decision(tmp_path,monkeypatch):
    monkeypatch.setattr(module,'ROOT',tmp_path)
    d=Device();events=[]
    d.stream.frames=[dict(tick=t,world='live',generation=2,captured_at_ms=time.time()*1000,
        battle_finished=t==100,native_end_marker=4001 if t==100 else 0)
        for t in (0,90,95,100)]
    class TwoCards(Policy):
        def __init__(self):self.requests=[]
        def request(self,r):
            if r['op']=='live_end':return {'ok':True}
            self.requests.append(r)
            result=super().request(r)
            if r['snapshot']['tick']==90:
                result['actions'].append(dict(kind='play_card',hand_slot=1,card_id=26000018))
            return result
    policy=TwoCards()
    battle=module.AutonomousBattle({'checkpoint':'test-fixture','min_card_gap_ms':1000},
        policy,d,lambda e,v:events.append((e,v)))
    battle.start();battle.thread.join(3)
    assert not battle.running() and battle.last_result['error'] is None
    assert d.calls.count('queue')==1
    assert any(e=='card_gap_blocked' and v['action']['hand_slot']==1 for e,v in events)
    assert next(r for r in policy.requests if r['snapshot']['tick']==95)['block_play_cards']


def test_expired_action_is_logged_without_submit_or_process_exit(tmp_path,monkeypatch):
    monkeypatch.setattr(module,'ROOT',tmp_path);events=[];d=Device()
    d.stream.latest=lambda:dict(tick=110,world='live',generation=2)
    battle=module.AutonomousBattle({'checkpoint':'test-fixture'},Policy(),d,lambda e,v:events.append((e,v)))
    battle.start();battle.thread.join(2)
    assert 'queue' not in d.calls and battle.last_result['error'] is None
    assert 'expired_action' in (battle.directory/'events.jsonl').read_text()


@pytest.mark.parametrize('enabled',[True,False])
def test_next_model_frame_receives_queued_deployment_for_optimistic_entity(tmp_path,monkeypatch,enabled):
    monkeypatch.setattr(module,'ROOT',tmp_path);d=Device()
    d.stream.frames=[dict(tick=t,world='live',generation=2,captured_at_ms=time.time()*1000,
        battle_finished=t==100,native_end_marker=4001 if t==100 else 0)
        for t in (0,90,95,100)]
    class CapturingPolicy(Policy):
        def __init__(self):self.requests=[]
        def request(self,r):
            self.requests.append(r)
            return super().request(r)
    policy=CapturingPolicy()
    def queued(action):
        d.calls.append('queue')
        return dict(request=dict(id='pending',generation=2,world='live',observed_tick=90,
            card_id=26000017,form_code=0,x=8000,y=25000),queued={'queued':True})
    d.queue_action=queued
    battle=module.AutonomousBattle({'checkpoint':'test-fixture','optimistic_entities':enabled},
        policy,d,lambda *args:None)
    battle.start();battle.thread.join(3)
    assert battle.last_result['error'] is None
    next_frame=next(r for r in policy.requests if r.get('snapshot',{}).get('tick')==95)
    assert [x['request']['id'] for x in next_frame['pending_deployments']]==(['pending'] if enabled else [])
    assert next_frame['reserved_actions'][0]['kind']=='play_card'


def test_disabling_projection_during_match_clears_it_before_next_inference(tmp_path,monkeypatch):
    monkeypatch.setattr(module,'ROOT',tmp_path);d=Device()
    d.stream.frames=[dict(tick=t,world='live',generation=2,captured_at_ms=time.time()*1000,
        battle_finished=t==100,native_end_marker=4001 if t==100 else 0)
        for t in (0,90,95,100)]
    class CapturingPolicy(Policy):
        def __init__(self):self.requests=[]
        def request(self,r):
            self.requests.append(r)
            return super().request(r)
    policy=CapturingPolicy()
    d.queue_action=lambda action:dict(request=dict(id='pending',generation=2,world='live',
        observed_tick=90,card_id=26000017,form_code=0,x=8000,y=25000),queued={'queued':True})
    config={'checkpoint':'test-fixture','optimistic_entities':True}
    def emit(event,_value):
        if event=='native_request':config['optimistic_entities']=False
    battle=module.AutonomousBattle(config,policy,d,emit)
    battle.start();battle.thread.join(3)
    next_frame=next(r for r in policy.requests if r.get('snapshot',{}).get('tick')==95)
    assert next_frame['pending_deployments']==[]
    assert battle.last_result['error'] is None


def test_confirmed_opponent_event_interrupts_decision_pace(tmp_path,monkeypatch):
    monkeypatch.setattr(module,'ROOT',tmp_path);d=Device()
    d.stream.frames=[dict(tick=t,world='live',generation=2,captured_at_ms=time.time()*1000,
        battle_finished=t==100,native_end_marker=4001 if t==100 else 0)
        for t in (0,90,95,100)]
    d.stream.urgent_event=threading.Event()
    class TimedPolicy(Policy):
        def __init__(self):self.started={}
        def request(self,r):
            if 'snapshot' in r:self.started[r['snapshot']['tick']]=time.monotonic()
            return super().request(r)
    policy=TimedPolicy()
    def emit(event,value):
        if event=='battle_decision' and value['tick']==90:
            d.stream.urgent_event.set()
    battle=module.AutonomousBattle({'checkpoint':'test-fixture'},policy,d,emit)
    battle.start();battle.thread.join(2)
    assert battle.last_result['error'] is None
    assert policy.started[95]-policy.started[90]<.2


def test_optimistic_entity_survives_hand_rotation_until_real_entity_is_seen(tmp_path,monkeypatch):
    monkeypatch.setattr(module,'ROOT',tmp_path);d=Device()
    battle=module.AutonomousBattle({'checkpoint':'test-fixture'},Policy(),d,lambda *args:None)
    battle.log=(tmp_path/'events.jsonl').open('w')
    request=dict(id='queued',observed_tick=90,card_id=26000018,x=8000,y=25000)
    battle.pending={'queued':dict(action=dict(kind='play_card',hand_slot=0,card_id=26000018),
        request=request,decision_tick=90,submitted_at=time.monotonic())}
    battle.optimistic={'queued':dict(request=request,
        action=dict(kind='play_card',owner=1,card_id=26000018),decision_tick=90)}
    d.stream.receipts['queued']=dict(id='queued',ok=True,stage='battle_applied',tick=110,
        card_id=26000018)
    try:battle.receipts(110)
    finally:battle.log.close()
    assert not battle.pending
    assert 'queued' in battle.optimistic


def test_changed_identity_still_stops_before_action(tmp_path,monkeypatch):
    monkeypatch.setattr(module,'ROOT',tmp_path);d=Device()
    d.stream.latest=lambda:dict(tick=90,world='different',generation=2)
    battle=module.AutonomousBattle({'checkpoint':'test-fixture'},Policy(),d,lambda *args:None)
    battle.start();battle.thread.join(2)
    assert 'queue' not in d.calls and '身份变更' in battle.last_result['error']


def test_manual_hand_change_skips_model_action_and_continues(tmp_path,monkeypatch):
    monkeypatch.setattr(module,'ROOT',tmp_path);events=[];d=Device()
    def changed(action):
        d.calls.append('queue')
        raise ValueError('手牌已变化')
    d.queue_action=changed
    battle=module.AutonomousBattle({'checkpoint':'test-fixture'},Policy(),d,
                                   lambda e,v:events.append((e,v)))
    battle.start();battle.thread.join(2)
    assert battle.last_result['error'] is None
    assert battle.last_result['applied']==0
    assert any(e=='expired_action' and v['reason']=='手牌已变化' for e,v in events)


def test_ability_despawn_before_native_queue_does_not_stop_next_card(tmp_path,monkeypatch):
    monkeypatch.setattr(module,'ROOT',tmp_path);events=[];d=Device()
    d.stream.frames=[dict(tick=t,world='live',generation=2,captured_at_ms=time.time()*1000,
        battle_finished=t==100,native_end_marker=4001 if t==100 else 0)
        for t in (0,90,95,100)]
    class SkillThenCard(Policy):
        def request(self,r):
            if r['op']=='live_end':return {'ok':True}
            tick=r['snapshot']['tick']
            action=(dict(kind='activate_ability',owner=1,source_entity=5000086,
                         ability_id='MiniPekkHeroAbility') if tick==90 else
                    dict(kind='play_card',owner=1,hand_slot=0,card_id=26000018))
            return dict(ok=True,tick=tick,actions=[action] if tick in (90,95) else [],
                inference_ms=1,limitations=[],mapping={},
                observation={'players':[{'owner':1,'ability_runtime_states':[
                    {'source_entity':5000086,'ability_id':'MiniPekkHeroAbility',
                     'available':True,'elixir_cost':1}]}]})
    def queue(action):
        d.calls.append(('queue',action['kind']))
        if action['kind']=='activate_ability':
            raise NativeQueueStateChanged('Ability state changed before queue: source missing at tick 91')
        return dict(request={'id':'card'},queued={'queued':True})
    d.queue_action=queue
    battle=module.AutonomousBattle({'checkpoint':'test-fixture','min_card_gap_ms':0},
        SkillThenCard(),d,lambda e,v:events.append((e,v)))
    battle.start();battle.thread.join(3)
    assert battle.last_result['error'] is None
    assert [c for c in d.calls if isinstance(c,tuple)]==[
        ('queue','activate_ability'),('queue','play_card')]
    assert any(e=='expired_action' and 'source missing' in v['reason'] for e,v in events)


def test_native_manual_change_receipt_releases_pending(tmp_path,monkeypatch):
    monkeypatch.setattr(module,'ROOT',tmp_path);d=Device();events=[]
    battle=module.AutonomousBattle({'checkpoint':'test-fixture'},Policy(),d,
                                   lambda e,v:events.append((e,v)))
    battle.directory=tmp_path
    battle.log=(tmp_path/'events.jsonl').open('w')
    battle.pending={'manual':dict(action={'kind':'play_card','card_id':26000018},
        decision_tick=100,submitted_at=time.monotonic())}
    d.stream.receipts['manual']=dict(id='manual',ok=False,stage='state_changed',tick=103,
                                    error='Hand or elixir changed')
    try:battle.receipts(103)
    finally:battle.log.close()
    assert battle.pending=={} and battle.applied==0
    assert any(e=='expired_action' for e,v in events)


def test_stale_native_request_keeps_original_error_and_does_not_timeout(tmp_path,monkeypatch):
    monkeypatch.setattr(module,'ROOT',tmp_path);d=Device();events=[]
    battle=module.AutonomousBattle({'checkpoint':'test-fixture'},Policy(),d,
                                   lambda e,v:events.append((e,v)))
    battle.log=(tmp_path/'events.jsonl').open('w')
    battle.pending={'stale':dict(action={'kind':'play_card','card_id':28000011},
        decision_tick=665,submitted_at=time.monotonic()-5)}
    d.stream.receipts['stale']=dict(id='stale',ok=False,stage='state_changed',tick=700,
        error='Deployment request belongs to a stale battle or tick')
    try:battle.receipts(700)
    finally:battle.log.close()
    assert battle.pending=={} and battle.applied==0
    assert any(e=='expired_action' and v['reason']=='原生命令等待期间观测已过期'
               for e,v in events)
    assert 'Deployment request belongs to a stale battle or tick' in (tmp_path/'events.jsonl').read_text()


def test_terminal_receipt_closes_unapplied_request_without_error(tmp_path,monkeypatch):
    monkeypatch.setattr(module,'ROOT',tmp_path);d=Device()
    battle=module.AutonomousBattle({'checkpoint':'test-fixture'},Policy(),d,lambda *args:None)
    battle.log=(tmp_path/'events.jsonl').open('w')
    battle.pending={'old':dict(action={'kind':'play_card','card_id':26000018},
        decision_tick=3595,submitted_at=time.monotonic())}
    d.stream.receipts['old']=dict(id='old',ok=False,stage='battle_ended',tick=3600,
        error='Battle ended before application')
    try:battle.receipts(3600)
    finally:battle.log.close()
    assert battle.pending=={} and battle.applied==0
    assert 'action_closed_at_terminal' in (tmp_path/'events.jsonl').read_text()


def test_custom_match_directory_gets_labeled_jsonl(tmp_path,monkeypatch):
    monkeypatch.setattr(module,'ROOT',tmp_path)
    target=tmp_path/'chosen-jsonl-folder'
    battle=module.AutonomousBattle({'checkpoint':'/models/hog26_2.pt',
        'match_directory':str(target)},Policy(),Device(),lambda *args:None)
    battle.start();battle.thread.join(2)
    path=__import__('pathlib').Path(battle.last_result['labeled_events_file'])
    assert path.parent.parent == target
    assert '经典1v1_hog26_2_' in path.name
    assert path.samefile(path.parent/'events.jsonl')
