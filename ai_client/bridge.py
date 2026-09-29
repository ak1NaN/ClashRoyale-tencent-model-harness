"""In-memory native transport. Partial telemetry never masquerades as FAIR input."""
import math
import queue
import threading
import time
import uuid

class NativeQueueStateChanged(ValueError):
    """The guest rejected an action before it entered the native queue."""

class NativeStream:
    def __init__(self, capacity=64):
        self.frames = queue.Queue(maxsize=capacity)
        self.lock = threading.Lock()
        self.partial = None
        self.receipts = {}
        self.failure = None
        self.received = 0
        self.last_latency_ms = None
        self.clock_offset_ms = 0.0
        self.trial_frames = queue.Queue(maxsize=64)
        self.trial_enabled = False
        self.trial_failure = None
        self.urgent_event = threading.Event()
        self.urgent_pending = 0

    def ingest(self, event):
        kind = event.get('event')
        with self.lock:
            if kind == 'native_partial_frame':
                # Frida timestamps use the Android clock. Normalize them to
                # the host clock before any freshness guard or model input.
                self.partial = event['frame']
                self.partial['guest_captured_at_ms'] = self.partial['captured_at_ms']
                self.partial['captured_at_ms'] += self.clock_offset_ms
                self.received += 1
                self.last_latency_ms = time.time()*1000 - self.partial['captured_at_ms']
                if self.trial_enabled:
                    try:
                        self.trial_frames.put_nowait(self.partial)
                        if self.partial.get('urgent_opponent_deploy'):
                            self.urgent_pending += 1
                            self.urgent_event.set()
                    except queue.Full:self.trial_failure = 'FL 实战试验输入队列溢出'
            elif kind == 'native_deploy_receipt':
                self.receipts[event['id']] = dict(event)
                if len(self.receipts) > 100:
                    del self.receipts[next(iter(self.receipts))]
            elif kind == 'native_complete_frame':
                try:
                    self.frames.put_nowait(event['frame'])
                except queue.Full:
                    self.failure = '完整观测队列溢出，不能跳过 LSTM 输入'
            elif kind in ('native_runtime_error', 'probe_error'):
                self.failure = event.get('message', kind)

    def next(self):
        with self.lock:
            if self.failure:
                raise RuntimeError(self.failure)
        try:
            return self.frames.get_nowait()
        except queue.Empty:
            return None

    def latest(self):
        with self.lock:
            return self.partial

    def begin_trial(self):
        with self.lock:
            self.trial_frames = queue.Queue(maxsize=64)
            self.trial_failure = None
            self.trial_enabled = True
            self.urgent_pending = 0
            self.urgent_event.clear()

    def end_trial(self):
        with self.lock:
            self.trial_enabled = False
            self.urgent_pending = 0
            self.urgent_event.clear()

    def next_trial(self):
        with self.lock:
            if self.trial_failure:raise RuntimeError(self.trial_failure)
            try:frame=self.trial_frames.get_nowait()
            except queue.Empty:return None
            if frame.get('urgent_opponent_deploy'):
                self.urgent_pending -= 1
                if self.urgent_pending == 0:self.urgent_event.clear()
            return frame

    def fresh_trial_frame(self, frame, *, max_age_ms=750, now_ms=None):
        skipped=[]
        # Policy inference may take longer than the 250 ms capture cadence.
        # Always consume the newest real snapshot, even before the age limit,
        # so an apparently fresh queued frame cannot trail the current hand.
        while True:
            newer=self.next_trial()
            if newer is None:break
            skipped.append(frame);frame=newer
        return frame,skipped


def deployment_request(action, snapshot, *, now_ms=None, request_id=None):
    """Convert upstream decoded native-grid action to Tencent mailbox coordinates."""
    now_ms = time.time()*1000 if now_ms is None else now_ms
    age = now_ms - snapshot['captured_at_ms']
    # Native telemetry arrives roughly 300 ms after capture; one 250 ms frame
    # interval plus model inference can exceed 500 ms without losing identity.
    # Keep this bound aligned with the live controller's 750 ms frame guard.
    # The Android guest and macOS host clocks can differ by a few tens of
    # milliseconds. A small negative age is clock skew, not a future frame.
    if not math.isfinite(age) or not -100 <= age <= 750:
        raise ValueError('原生状态过期')
    if action.get('owner') != snapshot['owner']:
        raise ValueError('动作玩家与本机身份不符')
    if action.get('kind') == 'activate_ability':
        source=action.get('source_entity')
        ability_id=action.get('ability_id')
        cost=action.get('verified_ability_cost')
        if type(source) is not int or type(ability_id) is not str or not ability_id or type(cost) is not int or not 0<=cost<=10:
            raise ValueError('技能动作缺少已验证的来源、ID 或耗费')
        native=next((a for a in snapshot.get('abilities',()) if a['source_entity']==source),None)
        if (native is None or native['controller_slot'] not in (1,2) or native['button_state'] not in (2,4) or
            native['remaining_cooldown_ms']!=0 or native['charges_raw']<=0 or cost*10000>snapshot['mana_raw']):
            raise ValueError('英雄技能已失效或圣水不足')
        delay=action.get('execute_offset_ticks',0)
        if type(delay) is not int or not 0<=delay<=10:
            raise ValueError('技能延迟超出当前实时窗口')
        return {'id':request_id or str(uuid.uuid4()),'kind':'activate_ability',
                'generation':snapshot['generation'],'world':snapshot['world'],
                'observed_tick':snapshot['tick'],'due_tick':snapshot['tick']+delay,
                'source_entity':source,'controller_slot':native['controller_slot'],
                'card_id':native['card_id'],'ability_id':ability_id,'ability_cost':cost}
    if action.get('kind') != 'play_card':
        raise ValueError('未知模型动作')
    slot = action.get('hand_slot')
    if type(slot) is not int or not 0 <= slot < 4:
        raise ValueError('无效手牌槽位')
    card = next((c for c in snapshot['hand'] if c['hand_slot'] == slot), None)
    if card is None or card['card_id'] != action.get('card_id'):
        raise ValueError('手牌已变化')
    if card['form'] != action.get('metadata', {}).get('policy_effective_form_code', action.get('form_code', 0)):
        raise ValueError('动作与原生卡牌形态不一致')
    if card['cost']*10000 > snapshot['mana_raw']:
        raise ValueError('当前圣水不足')
    grid = action.get('target_grid', [])
    offset = action.get('subcell_offset') or [0, 0]
    if len(grid) != 2 or len(offset) != 2 or any(type(v) is not int for v in grid):
        raise ValueError('无效落点')
    if not (0 <= grid[0] < 18 and 0 <= grid[1] < 32):
        raise ValueError('落点超出竞技场')
    if any(not isinstance(v, (float, int)) or not math.isfinite(v) or abs(v) > .5 for v in offset):
        raise ValueError('无效格内偏移')
    x, y = (round((grid[i]+.5+offset[i])*1000) for i in range(2))
    delay = action.get('execute_offset_ticks', 0)
    if type(delay) is not int or not 0 <= delay <= 10:
        raise ValueError('动作延迟超出当前实时窗口')
    # Upstream decoded grid has already been transformed to native coordinates.
    # Screen orientation is deliberately absent from this conversion.
    return {'id': request_id or str(uuid.uuid4()), 'generation': snapshot['generation'],
            'world': snapshot['world'], 'observed_tick': snapshot['tick'],
            'due_tick': snapshot['tick'] + delay, 'hand_slot': slot,
            'card_id': card['card_id'], 'form_code': card['form'], 'x': x, 'y': y}
