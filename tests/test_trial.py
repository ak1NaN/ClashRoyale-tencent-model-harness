from types import SimpleNamespace
import threading
import time
from ai_client.trial import NativeModelTrial

class Stream:
    def __init__(self):
        self.frames=[dict(tick=t,world='live',generation=2,captured_at_ms=time.time()*1000) for t in range(0,95,5)]
        self.receipts={}
    def begin_trial(self):pass
    def end_trial(self):pass
    def next_trial(self):return self.frames.pop(0) if self.frames else None
    def latest(self):return dict(tick=90,world='live',generation=2)

class Policy:
    process=SimpleNamespace(poll=lambda:None)
    def __init__(self,block=False):self.entered=threading.Event();self.release=threading.Event();self.block=block
    def request(self,req):
        if req['op']=='live_end':return {'ok':True}
        tick=req['snapshot']['tick']
        if self.block:
            self.entered.set();self.release.wait(1)
        return dict(ok=True,tick=tick,actions=[dict(kind='play_card',card_id=26000017)] if tick==90 else [],
            inference_ms=1,limitations=['experimental'],mapping={'towers':6,'entities':0})

class Device:
    connected=True
    def __init__(self,reject=False):self.stream=Stream();self.detached=threading.Event();self.calls=[];self.reject=reject
    def cancel_native(self):self.calls.append('cancel')
    def enable_native(self,value):self.calls.append('enable')
    def queue_action(self,action):
        self.calls.append('queue')
        self.stream.receipts['one']=dict(ok=not self.reject,stage='rejected' if self.reject else 'battle_applied',error='rejected',id='one')
        return dict(request={'id':'one'},queued={'queued':True})

def test_one_model_deployment_then_application_stops():
    d=Device();events=[];trial=NativeModelTrial(Policy(),d,lambda e,v:events.append((e,v)))
    trial.start();trial.thread.join(2)
    assert not trial.running() and d.calls.count('queue')==1
    assert any(e=='trial_success' for e,v in events)
    assert d.calls[-1]=='cancel'

def test_stop_during_inference_does_not_submit_later():
    p=Policy(block=True);d=Device();trial=NativeModelTrial(p,d,lambda *a:None)
    trial.start();assert p.entered.wait(1)
    trial.stop();p.release.set();trial.thread.join(2)
    assert not trial.running() and 'queue' not in d.calls

def test_rejection_stops_without_retry():
    d=Device(reject=True);events=[];trial=NativeModelTrial(Policy(),d,lambda e,v:events.append((e,v)))
    trial.start();trial.thread.join(2)
    assert not trial.running() and d.calls.count('queue')==1
    assert any(e=='error' and v=='rejected' for e,v in events)
    assert not any(e=='trial_success' for e,v in events)
