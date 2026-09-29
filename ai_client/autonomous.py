"""Continuous model decisions with nonblocking native receipt monitoring.

The model receives every real five-tick snapshot. Submitted cards and their
elixir are reserved until actual application; no invented battle frames.
"""
import json
from datetime import datetime
from pathlib import Path
import threading
import time
import uuid
from .config import ROOT
from .bridge import NativeQueueStateChanged
from .match_files import link_labeled_events

class AutonomousBattle:
    TRANSIENT_NATIVE_STATE = frozenset({
        '原生状态过期', '手牌已变化', '当前圣水不足',
        '英雄技能已失效或圣水不足', '动作与原生卡牌形态不一致',
    })

    def __init__(self, config, policy, device, emit):
        self.config,self.policy,self.device,self.emit=config,policy,device,emit
        self.stop_event=threading.Event();self.thread=None;self.directory=None
        self.decisions=0;self.applied=0;self.pending={};self.optimistic={};self.last_result=None

    def running(self):return self.thread is not None and self.thread.is_alive()
    def start(self, dry=False):
        if self.running():raise RuntimeError('自动对战已经启动')
        gap=int(self.config.get('min_card_gap_ms',1000))
        if not 0<=gap<=5000:raise ValueError('最小下牌间隔须为 0–5000 ms')
        if not self.device.connected or not self.policy.process or self.policy.process.poll() is not None:
            raise RuntimeError('请先连接设备并加载模型')
        self.last_result=None
        self.directory=None
        self.dry=dry;self.stop_event.clear();self.device.stream.begin_trial()
        self.thread=threading.Thread(target=self.run,daemon=True);self.thread.start()
    def stop(self):
        self.stop_event.set()
        urgent=getattr(self.device.stream,'urgent_event',None)
        if urgent is not None:urgent.set()
    def write(self,event,value):
        self.log.write(json.dumps(dict(at_ms=int(time.time()*1000),event=event,value=value),ensure_ascii=False)+'\n')
        self.log.flush()
    def skip_action(self,action,decision_tick,latest_tick,reason):
        detail=dict(action=action,decision_tick=decision_tick,
            latest_tick=latest_tick,reason=reason)
        self.write('expired_action',detail)
        self.emit('expired_action',detail)
    def receipts(self,frame_tick=None):
        for token,record in list(self.pending.items()):
            receipt=self.device.stream.receipts.get(token)
            if receipt and receipt.get('stage')=='battle_applied':
                if not record.get('applied'):
                    self.applied+=1;self.write('application',receipt);record['applied']=True
                    self.emit('battle_application',dict(receipt=receipt,applied=self.applied))
                if frame_tick is not None and frame_tick>=receipt['tick']:del self.pending[token]
            elif (receipt and record['action'].get('kind')=='activate_ability' and
                  receipt.get('stage')=='source_despawned' and
                  receipt.get('error')=='source_despawned_before_application' and
                  receipt.get('source_entity')==record['action'].get('source_entity')):
                self.write('action_fizzled',receipt)
                self.emit('battle_fizzle',dict(receipt=receipt,reason='英雄阵亡，技能未生效'))
                del self.pending[token]
            elif receipt and receipt.get('stage')=='state_changed' and not receipt.get('ok'):
                self.optimistic.pop(token,None)
                self.write('native_state_changed', receipt)
                native_error=receipt.get('error', '')
                reason=('原生命令等待期间观测已过期' if 'stale battle or tick' in native_error else
                        '执行前英雄技能状态或圣水变化' if 'Hero ability state' in native_error else
                        '执行前原生手牌或圣水变化')
                self.skip_action(record['action'],record['decision_tick'],
                    receipt.get('tick'), reason)
                del self.pending[token]
            elif receipt and receipt.get('stage')=='battle_ended' and not receipt.get('ok'):
                self.optimistic.pop(token,None)
                self.write('action_closed_at_terminal',receipt)
                del self.pending[token]
            elif receipt and not receipt.get('ok'):
                raise RuntimeError('原生命令拒绝：'+receipt.get('error','unknown'))
            elif time.monotonic()-record['submitted_at']>4:
                raise RuntimeError('原生应用回执超时；停止且不重复提交')

    def run(self):
        started=False;identity=None;last_at=None;error=None;pace_seconds=.25
        card_gap_ms=int(self.config.get('min_card_gap_ms',1000))
        last_card_queued_at=None
        self.pending={};self.optimistic={};self.decisions=0;self.applied=0
        started_at=datetime.now()
        match_label='经典1v1'
        checkpoint=self.config['checkpoint']
        base=Path(self.config.get('match_directory') or ROOT/'records/matches').expanduser()
        self.directory=base/(started_at.strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:6])
        self.directory.mkdir(parents=True)
        self.log=(self.directory/'events.jsonl').open('w')
        self.write('armed',dict(checkpoint=checkpoint,match_label=match_label,dry=self.dry,mode='1v1',mapping='experimental',extended_public_mapping=self.config.get('extended_public_mapping',False),min_card_gap_ms=card_gap_ms,optimistic_entities=self.config.get('optimistic_entities',True)))
        self.emit('control_status','连续 FL 已待命；进入新 1v1，从真实 tick 0 接入')
        try:
            self.device.cancel_native()
            while not self.stop_event.wait(.005):
                if self.device.detached.is_set():raise RuntimeError('设备断开，执行停止')
                frame=self.device.stream.next_trial()
                if frame is None:
                    if started and time.monotonic()-last_at>3:
                        self.write('stream_stopped',dict(applied=self.applied,decisions=self.decisions))
                        self.emit('battle_finished',dict(directory=str(self.directory),applied=self.applied,
                            decisions=self.decisions,result='unconfirmed',reason='状态流结束，等待结果画面确认'))
                        break
                    if started and time.monotonic()-last_at<.75:self.receipts()
                    continue
                if not started and frame['tick']!=0:continue
                if started:
                    frame,skipped=self.device.stream.fresh_trial_frame(frame)
                    if skipped:
                        public=[]
                        for old in skipped:
                            if (old['world'],old['generation'])!=identity:raise RuntimeError('过期观测身份变更')
                            self.write('capture_skipped',old)
                            public.extend(old.get('public_deployments',()))
                        frame=dict(frame,public_deployments=public+list(frame.get('public_deployments',())))
                        self.write('latency_recovered',dict(skipped_ticks=[f['tick'] for f in skipped],resumed_tick=frame['tick']))
                if started and frame.get('battle_finished') and (frame['world'],frame['generation'])==identity:
                    self.write('capture',frame)
                    self.write('native_end',dict(tick=frame['tick'],native_end_marker=frame['native_end_marker'],pending=list(self.pending)))
                    self.emit('battle_finished',dict(directory=str(self.directory),reason='原生终局标志；未应用命令不计下牌',result='unconfirmed'))
                    break
                self.receipts(frame['tick'])
                if frame.get('mapping_error'):raise RuntimeError(frame['mapping_error'])
                if time.time()*1000-frame['captured_at_ms']>750:raise RuntimeError('实时输入积压，停止执行')
                new_identity=(frame['world'],frame['generation'])
                if started and identity!=new_identity:raise RuntimeError('对局身份变更，等待重新启动')
                last_at=time.monotonic()
                self.write('capture',frame)
                if frame.get('urgent_opponent_deploy'):
                    self.write('opponent_fast_path',dict(tick=frame['tick'],
                        event_ticks=[event['tick'] for event in frame.get('public_deployments',())
                                     if event['owner']!=frame['owner']]))
                reserved=[r['action'] for r in self.pending.values()]
                # The GUI can disable projections during a match. Drop queued
                # ghosts before constructing the very next model observation.
                if not self.config.get('optimistic_entities',True):
                    self.optimistic.clear()
                for token,record in tuple(self.optimistic.items()):
                    observed_tick=record['request'].get('observed_tick')
                    if not isinstance(observed_tick,int) or frame['tick']-observed_tick>80:
                        self.write('optimistic_expired',dict(request_id=token,tick=frame['tick'],
                            card_id=record['request'].get('card_id')))
                        del self.optimistic[token]
                pending_deployments=list(self.optimistic.values())
                inference_started=time.monotonic()
                block_play_cards=(last_card_queued_at is not None and
                    (inference_started-last_card_queued_at)*1000<card_gap_ms)
                result=self.policy.request(dict(op='live_step' if started else 'live_start',snapshot=frame,
                    reserved_actions=reserved,pending_deployments=pending_deployments,
                    allow_forms=True,allow_abilities=True,
                    block_play_cards=block_play_cards,
                    extended_public_mapping=self.config.get('extended_public_mapping',False)))
                for state in result.get('observation',{}).get('metadata',{}).get('optimistic_entities',()):
                    if state.get('state') in ('real_observed','spell_effect_elapsed','unsupported_card',
                                              'unsupported_form','unmapped_archetype'):
                        self.optimistic.pop(state['request_id'],None)
                started=True;identity=new_identity
                self.write('decision',result)
                if self.stop_event.is_set():break
                self.emit('trial_mapping',{k:result[k] for k in ('tick','mapping','limitations')})
                if result.get('terminal'):
                    self.emit('battle_finished',dict(directory=str(self.directory),reason='原生国王塔终局',result='unconfirmed'))
                    break
                if frame['tick']<90:continue
                self.decisions+=1
                # Share the same observation given to the policy with the GUI.
                # The journal still records only a compact action summary.
                self.emit('battle_decision',result)
                for action in result['actions']:
                    if action['kind'] not in ('play_card','activate_ability') or self.dry:continue
                    if action['kind']=='play_card' and last_card_queued_at is not None:
                        remaining_ms=card_gap_ms-(time.monotonic()-last_card_queued_at)*1000
                        if remaining_ms>0:
                            detail=dict(action=action,decision_tick=frame['tick'],
                                remaining_ms=int(remaining_ms+0.999),min_card_gap_ms=card_gap_ms)
                            self.write('card_gap_blocked',detail)
                            self.emit('card_gap_blocked',detail)
                            continue
                    if action['kind']=='activate_ability':
                        states=next((p.get('ability_runtime_states',()) for p in result['observation']['players']
                            if p['owner']==action['owner']),())
                        state=next((s for s in states if s['source_entity']==action.get('source_entity') and
                            s['ability_id']==action.get('ability_id') and s['available']),None)
                        if state is None:raise RuntimeError('模型技能来源不在当前有效观测中')
                        if any(r['action']['kind']=='activate_ability' and
                            r['action']['source_entity']==action['source_entity'] for r in self.pending.values()):
                            raise RuntimeError('模型对已保留的英雄重复释放技能')
                        action=dict(action,verified_ability_cost=int(state['elixir_cost']))
                    elif any(r['action']['kind']=='play_card' and r['action']['hand_slot']==action['hand_slot']
                             for r in self.pending.values()):
                        raise RuntimeError('模型对已保留的槽位输出重复部署')
                    latest=self.device.stream.latest()
                    if (latest['world'],latest['generation'])!=identity:raise RuntimeError('动作对局身份变更')
                    if latest['tick']-frame['tick']>10:
                        self.skip_action(action,frame['tick'],latest['tick'],'模型观测已过期')
                        continue
                    if self.stop_event.is_set():break
                    self.device.enable_native(True)
                    try:
                        queued=self.device.queue_action(action)
                    except ValueError as exc:
                        if not isinstance(exc,NativeQueueStateChanged) and str(exc) not in self.TRANSIENT_NATIVE_STATE:raise
                        self.skip_action(action,frame['tick'],latest['tick'],str(exc))
                        continue
                    self.pending[queued['request']['id']]=dict(action=action,
                        request=queued['request'],decision_tick=frame['tick'],submitted_at=time.monotonic())
                    if action['kind']=='play_card' and self.config.get('optimistic_entities',True):
                        self.optimistic[queued['request']['id']]=dict(request=queued['request'],
                            action=action,decision_tick=frame['tick'])
                    if action['kind']=='play_card':last_card_queued_at=time.monotonic()
                    self.write('native_request',queued);self.emit('native_request',queued)
                self.emit('battle_progress',dict(decisions=self.decisions,applied=self.applied,pending=len(self.pending),
                    directory=str(self.directory)))
                # Slow CPU inference cannot sustain a 250 ms decision cadence.
                # Yield between decisions instead of keeping the host saturated;
                # the next iteration drains directly to the newest real frame.
                new_pace=.5 if result.get('inference_ms',0)>250 else .25
                if new_pace!=pace_seconds:
                    pace_seconds=new_pace
                    self.write('decision_pacing',dict(interval_ms=int(pace_seconds*1000),
                        inference_ms=result.get('inference_ms'),tick=frame['tick']))
                    self.emit('decision_pacing',dict(interval_ms=int(pace_seconds*1000),
                        inference_ms=result.get('inference_ms'),tick=frame['tick']))
                delay=max(0,pace_seconds-(time.monotonic()-inference_started))
                urgent=getattr(self.device.stream,'urgent_event',None)
                if urgent is None:self.stop_event.wait(delay)
                else:urgent.wait(delay)
        except Exception as exc:
            error=str(exc);self.write('error',error);self.emit('error',error)
        finally:
            self.device.stream.end_trial()
            try:self.device.cancel_native()
            except Exception:pass
            if started:
                try:self.policy.request(dict(op='live_end'))
                except Exception:pass
            self.log.close()
            labeled_path=None;labeled_error=None
            try:
                labeled_path=link_labeled_events(self.directory,started_at=started_at,
                    label=match_label,checkpoint=checkpoint)
            except OSError as exc:
                labeled_error=str(exc)
                self.emit('error','对局 JSONL 标签文件创建失败：'+labeled_error)
            self.last_result=dict(directory=str(self.directory),decisions=self.decisions,applied=self.applied,error=error,
                result='unconfirmed',checkpoint=checkpoint,match_label=match_label,
                labeled_events_file=str(labeled_path) if labeled_path else None,
                labeled_events_error=labeled_error,
                extended_public_mapping=self.config.get('extended_public_mapping',False))
            (self.directory/'summary.json').write_text(json.dumps(self.last_result,ensure_ascii=False,indent=2)+'\n')
            if labeled_path:
                self.emit('match_file',str(labeled_path))
            self.stop_event.set()
            self.emit('control_status','连续 FL 已停止；结果需由真实比赛画面确认')
