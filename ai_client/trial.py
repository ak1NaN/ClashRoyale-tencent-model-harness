"""One actual model-selected deployment, then stop after native application."""
import threading
import time

class NativeModelTrial:
    def __init__(self,policy,device,emit):
        self.policy,self.device,self.emit=policy,device,emit
        self.stop_event=threading.Event();self.thread=None
    def running(self):return self.thread is not None and self.thread.is_alive()
    def start(self):
        if self.running():raise RuntimeError('FL 实战试验正在运行')
        if not self.device.connected or not self.policy.process or self.policy.process.poll() is not None:
            raise RuntimeError('请先连接设备并加载模型')
        self.device.stream.begin_trial();self.stop_event.clear()
        self.thread=threading.Thread(target=self.run,daemon=True);self.thread.start()
    def stop(self):self.stop_event.set()
    def run(self):
        started=False;last_at=None;world=None
        self.emit('control_status','FL 实战试验已待命；请进入新训练营，从 tick 0 接入')
        try:
            self.device.cancel_native()
            while not self.stop_event.wait(.01):
                if self.device.detached.is_set():raise RuntimeError('设备已断开')
                frame=self.device.stream.next_trial()
                if frame is None:
                    if started and time.monotonic()-last_at>1.5:raise RuntimeError('试验状态流中断，已停止')
                    continue
                if not started and frame['tick']!=0:continue
                if frame.get('mapping_error'):raise RuntimeError(frame['mapping_error'])
                if time.time()*1000-frame['captured_at_ms']>750:raise RuntimeError('试验输入积压，已停止')
                identity=(frame['world'],frame['generation'])
                if started and identity!=world:raise RuntimeError('原生对局身份已变化')
                result=self.policy.request({'op':'live_step' if started else 'live_start','snapshot':frame})
                started=True;world=identity;last_at=time.monotonic()
                if self.stop_event.is_set():break
                self.emit('trial_mapping',{k:result[k] for k in ('tick','mapping','limitations')})
                if frame['tick']<90:continue
                self.emit('trial_decision',{k:v for k,v in result.items() if k!='observation'})
                actions=[a for a in result['actions'] if a['kind']=='play_card']
                if not actions:continue
                action=actions[0]
                latest=self.device.stream.latest()
                if (latest['world'],latest['generation'])!=world or latest['tick']-frame['tick']>10:
                    raise RuntimeError('推理动作已过期')
                if self.stop_event.is_set():break
                self.device.enable_native(True)
                queued=self.device.queue_action(action)
                self.emit('native_request',queued)
                self.emit('trial_evidence',{'decision':result,'request':queued['request']})
                token=queued['request']['id'];deadline=time.monotonic()+3
                while not self.stop_event.wait(.02):
                    receipt=self.device.stream.receipts.get(token)
                    if receipt and receipt.get('stage')=='battle_applied':
                        self.emit('trial_success',{'action':action,'receipt':receipt,'source':'FL General real live input'})
                        return
                    if receipt and not receipt.get('ok'):raise RuntimeError(receipt.get('error','原生部署未通过'))
                    if time.monotonic()>deadline:raise RuntimeError('未收到实际应用回执；没有重试下牌')
        except Exception as error:self.emit('error',str(error))
        finally:
            self.device.stream.end_trial()
            try:self.device.cancel_native()
            except Exception:pass
            if started:
                try:self.policy.request({'op':'live_end'})
                except Exception:pass
            self.stop_event.set();self.emit('control_status','FL 单次实战试验已停止')
