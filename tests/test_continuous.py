import pytest
import queue
from types import SimpleNamespace

from ai_client.continuous.geometry import marker_to_android, target_pixels
from ai_client.continuous.series import ContinuousSeries, MAX_MATCHES, RESULT_SETTLE_MS
from ai_client.gui import App
from ai_client.device import AndroidDevice


class Harness:
    def __init__(self):
        self.calls=[]
        self.tasks=[]
        self.messages=[]
        self.series=ContinuousSeries(
            arm=lambda:self.calls.append('arm'),
            tap=lambda kind,target:self.calls.append(('tap',kind,target['u'])),
            stop_match=lambda:self.calls.append('stop'),
            schedule=lambda delay,callback:self.tasks.append((delay,callback)),
            status=self.messages.append)

    def run(self, delay):
        index=next(i for i,(ms,_) in enumerate(self.tasks) if ms==delay)
        _,callback=self.tasks.pop(index)
        callback()


TARGETS={'start':{'u':.5,'v':.8},'end':{'u':.5,'v':.5}}


def test_two_matches_require_each_confirmed_result_and_end_click():
    h=Harness();h.series.begin(2,TARGETS)
    h.run(200)
    assert h.calls==['arm',('tap','start',.5)]
    h.series.on_tap_result('start')
    h.series.on_battle_started('match-a')
    h.series.on_battle_finished('match-a')
    assert h.series.completed==0 and h.calls.count('arm')==1
    h.series.on_match_result('match-a','loss')
    assert h.series.completed==1 and h.series.phase=='settling'
    assert h.calls[-1]!=('tap','end',.5)
    h.run(RESULT_SETTLE_MS)
    assert h.calls[-1]==('tap','end',.5)
    h.series.on_tap_result('end')
    h.run(2500)
    assert h.calls[-2:]==['arm',('tap','start',.5)]
    h.series.on_tap_result('start')
    h.series.on_battle_started('match-b')
    h.series.on_battle_finished('match-b')
    h.series.on_match_result('match-b','win')
    h.run(RESULT_SETTLE_MS)
    h.series.on_tap_result('end')
    assert h.series.phase=='complete' and not h.series.active
    assert h.series.completed==2 and h.calls.count('arm')==2


def test_wrong_result_or_start_timeout_stops_without_another_click():
    h=Harness();h.series.begin(2,TARGETS);h.run(200)
    h.series.on_tap_result('start')
    h.series.on_battle_started('current')
    h.series.on_battle_finished('current')
    h.series.on_match_result('other','win')
    assert h.series.phase=='failed' and h.calls[-1]=='stop'
    assert h.calls.count('arm')==1
    h=Harness();h.series.begin(1,TARGETS);h.run(200)
    h.series.on_tap_result('start');h.run(90_000)
    assert h.series.phase=='failed' and h.calls[-1]=='stop'


def test_manual_stop_invalidates_scheduled_launch():
    h=Harness();h.series.begin(1,TARGETS)
    h.series.stop();h.run(200)
    assert h.calls==[] and h.series.phase=='stopped'
    with pytest.raises(ValueError,match='1～325'):
        h.series.begin(0,TARGETS)


def test_continuous_match_limit_is_325():
    assert MAX_MATCHES==325
    h=Harness();h.series.begin(325,TARGETS)
    assert h.series.active and h.series.total==325
    h.series.stop()
    with pytest.raises(ValueError,match='1～325'):
        h.series.begin(326,TARGETS)


def test_previous_match_watchdog_cannot_stop_later_match_in_same_phase():
    h=Harness();h.series.begin(3,TARGETS);h.run(200)
    h.series.on_tap_result('start')
    old_matchmaking=next(callback for delay,callback in h.tasks if delay==90_000)
    h.series.on_battle_started('match-a')
    old_battle=next(callback for delay,callback in h.tasks if delay==8*60_000)
    h.series.on_battle_finished('match-a')
    old_result=next(callback for delay,callback in h.tasks if delay==35_000)
    h.series.on_match_result('match-a','win');h.run(RESULT_SETTLE_MS);h.series.on_tap_result('end')
    h.run(2500)
    h.series.on_tap_result('start')
    old_matchmaking()
    assert h.series.active and h.series.phase=='matchmaking'
    h.series.on_battle_started('match-b')
    old_battle()
    assert h.series.active and h.series.phase=='battle'
    h.series.on_battle_finished('match-b')
    old_result()
    assert h.series.active and h.series.phase=='result'
    current_result=[callback for delay,callback in h.tasks if delay==35_000][-1]
    current_result()
    assert h.series.phase=='failed' and h.calls[-1]=='stop'


def test_marker_position_maps_to_android_content_not_mumu_toolbar():
    window={'x':968,'y':37,'width':459,'height':854}
    target=marker_to_android((1197.5,483),window,(1440,2560))
    assert target_pixels(target,(1440,2560))==(720,1280)
    with pytest.raises(ValueError,match='游戏画面内部'):
        marker_to_android((1197,45),window,(1440,2560))


def test_click_uses_adb_android_coordinates_without_desktop_mouse():
    class Device:
        connected=True
        calls=[]
        def foreground(self):return True
        def adb(self,*args):self.calls.append(args)
    app=App.__new__(App)
    app.device=Device()
    app.config={'screen_width':1440,'screen_height':2560}
    app.events=queue.Queue()
    app.perform_series_tap('start',{'u':.5,'v':.5})
    event,result=app.events.get(timeout=2)
    assert event=='continuous_tap_result' and result['error'] is None
    assert app.device.calls==[('shell','input','tap',720,1280)]


def test_foreground_reads_window_summary_focus_fields():
    device=AndroidDevice({'package':'com.tencent.tmgp.supercell.clashroyale'})
    calls=[]
    def adb(*args):
        calls.append(args)
        return 'mCurrentFocus=Window{123 u0 com.tencent.tmgp.supercell.clashroyale/GameAppTencent}'
    device.adb=adb
    assert device.foreground()
    assert calls==[('shell','dumpsys','window')]


class GuiValue:
    def __init__(self, value=None):self.value=value
    def get(self):return self.value
    def set(self, value):self.value=value


class GuiButton:
    def __init__(self):self.state=None
    def configure(self, **options):self.state=options['state']


def _mode_app():
    app=App.__new__(App)
    app.launch_mode=None
    app.events=queue.Queue()
    app.autonomous=SimpleNamespace(running=lambda:False)
    app.trial=SimpleNamespace(running=lambda:False)
    app.single_button=GuiButton()
    app.continuous_button=GuiButton()
    app.series_count_spinbox=GuiButton()
    app.series_status=GuiValue('未运行')
    app.dry=GuiValue(False)
    app.busy=False
    app.series_count=GuiValue('2')
    app.device=SimpleNamespace(connected=True)
    app.policy=SimpleNamespace(process=SimpleNamespace(poll=lambda:None))
    app.config={'screen_width':1440,'screen_height':2560,'continuous_targets':TARGETS}
    app.hide_targets=lambda:None
    return app


def test_single_battle_locks_continuous_entry_until_match_finishes():
    app=_mode_app()
    app.series=SimpleNamespace(active=False)
    arms=[]
    app._arm_match=arms.append
    app.refresh_battle_controls()
    assert app.single_button.state==app.continuous_button.state=='normal'
    app.start()
    assert arms==[False] and app.launch_mode=='single'
    assert app.single_button.state==app.continuous_button.state=='disabled'
    app.start_continuous()
    assert app.events.get_nowait()==('error','已有对战在运行，请先停止或等待本局结束')
    assert app.launch_mode=='single'


def test_continuous_battle_locks_single_entry_until_series_stops(monkeypatch):
    app=_mode_app()
    arms=[]
    app._arm_match=arms.append
    app.series=ContinuousSeries(arm=lambda:arms.append(False),tap=lambda *_:None,
        stop_match=lambda:None,schedule=lambda *_:None,status=app.set_series_status)
    monkeypatch.setattr('ai_client.gui.save',lambda _:None)
    app.start_continuous()
    assert app.launch_mode=='continuous' and app.series.active
    assert app.single_button.state==app.continuous_button.state=='disabled'
    assert app.series_count_spinbox.state=='disabled'
    app.start()
    assert arms==[]
    assert app.events.get_nowait()[0]=='continuous_status'
    assert app.events.get_nowait()==('error','已有对战在运行，请先停止或等待本局结束')
    app.series.stop()
    assert app.launch_mode is None
    assert app.single_button.state==app.continuous_button.state=='normal'
    assert app.series_count_spinbox.state=='normal'
