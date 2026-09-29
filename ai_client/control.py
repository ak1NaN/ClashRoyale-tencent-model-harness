"""Episode lifecycle, complete-observation gate and cancellable touch execution."""
import json
import math
import threading
import time
from pathlib import Path

REQUIRED_COVERAGE = {'battle_lifecycle', 'tick', 'own_hand', 'own_elixir', 'towers',
                     'entities', 'public_events', 'placement', 'forms', 'abilities'}

def validate_frame(frame, config, now_ms=None):
    now_ms = time.time() * 1000 if now_ms is None else now_ms
    if frame.get('schema') != 'tencent-firstlight-frame.v1':
        raise ValueError('完整观测协议不匹配')
    if frame.get('client_version') != config['expected_version']:
        raise ValueError('完整观测的客户端版本不匹配')
    age = now_ms - float(frame['captured_at_ms'])
    if not math.isfinite(age) or not 0 <= age <= config['max_age_ms']:
        raise ValueError('观测过期或时间戳无效')
    if not frame.get('battle_id') or frame.get('battle_active') is not True:
        raise ValueError('当前不是有效对局')
    missing = REQUIRED_COVERAGE - set(frame.get('coverage', []))
    if missing:
        raise ValueError('缺少真实观测: ' + ', '.join(sorted(missing)))
    observation = frame['observation']
    if observation.get('tier') != 'fair' or observation.get('owner') not in (0, 1):
        raise ValueError('需要 FAIR 玩家视角观测')
    if observation.get('episode_id') != frame['battle_id']:
        raise ValueError('对局标识与观测不一致')
    if type(observation.get('tick')) is not int:
        raise ValueError('缺少原生 tick')
    for key in ('time', 'players', 'towers', 'entities', 'events', 'action_mask', 'terminal'):
        if key not in observation:
            raise ValueError('完整观测缺少 ' + key)
    player = next(p for p in observation['players'] if p['owner'] == observation['owner'])
    if player.get('elixir_exact') is None or not player.get('private_state_visible'):
        raise ValueError('缺少我方实时圣水')
    if not player.get('hand') or not player.get('metadata', {}).get('hand_slot_by_card'):
        raise ValueError('缺少我方实时手牌及槽位映射')
    if 'episode' not in frame or set(frame.get('initial_elixir', {})) != {'0', '1'}:
        raise ValueError('缺少本局初始配置或初始圣水')
    if type(frame.get('screen_flip')) is not bool:
        raise ValueError('缺少已验证的屏幕朝向')
    return observation

def action_points(action, frame, config):
    """FirstLight decoded actions are already in native arena coordinates."""
    if action['kind'] != 'play_card':
        raise ValueError('触摸后端尚不支持该动作: ' + action['kind'])
    observation = frame['observation']
    if action['owner'] != observation['owner']:
        raise ValueError('动作不属于本机玩家')
    slot = action['hand_slot']
    if type(slot) is not int or not 0 <= slot < 4:
        raise ValueError('无效手牌槽位')
    player = next(p for p in observation['players'] if p['owner'] == action['owner'])
    mapping = player['metadata']['hand_slot_by_card']
    card = next((int(card) for card, index in mapping.items() if int(index) == slot), None)
    if card is None or card != action.get('card_id'):
        raise ValueError('动作卡牌已不在原手牌槽位')
    mask = observation['action_mask']
    if not mask['hand_slots'][slot]:
        raise ValueError('当前手牌不可部署')
    placement = mask['placement_masks'][str(slot)]
    if float(placement['effective_cost']) > float(player['elixir_exact']) + 0.0001:
        raise ValueError('当前圣水不足')
    x, y = action['target_grid']
    if type(x) is not int or type(y) is not int or not (0 <= x < 18 and 0 <= y < 32):
        raise ValueError('落点超出竞技场')
    # Use the upstream exact legality helper, preserving its real placement ABI.
    from native_runner.training.v4.decoding import _native_placement_allows
    from native_runner.training.v4.config import ModelConfigV4
    if 'row_major' not in placement:
        raise ValueError('缺少原生合法落点矩阵')
    _native_placement_allows(placement, native_grid=(x, y), config=ModelConfigV4())
    offset = action.get('subcell_offset') or [0, 0]
    x, y = x + 0.5 + float(offset[0]), y + 0.5 + float(offset[1])
    if frame['screen_flip']:
        x, y = 18 - x, 32 - y
    left, top, right, bottom = config['arena']
    w, h = config['screen_width'], config['screen_height']
    start = tuple(round(n * scale) for n, scale in zip(config['hand'][slot], (w, h)))
    end = (round((left + (right-left)*x/18)*w), round((top + (bottom-top)*y/32)*h))
    return start, end

class FrameDirectory:
    """Atomic per-frame files from a future Tencent native adapter; never replay files."""
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.seen = set()

    def next(self):
        for path in sorted(self.directory.glob('frame-*.json')):
            if path.name not in self.seen:
                self.seen.add(path.name)
                return json.loads(path.read_text())
        return None

class AutoController:
    def __init__(self, config, policy, device, emit):
        self.config, self.policy, self.device, self.emit = config, policy, device, emit
        self.stop_event = threading.Event()
        self.mode = 'dry'
        self.thread = None
        self.episode_id = None
        self.last_tick = None
        self.last_frame_at = time.monotonic()
        self.latest_frame = None
        self.awaiting_card = None
        self.ack = threading.Event()

    def start(self, mode='dry'):
        if self.thread and self.thread.is_alive():
            raise RuntimeError('托管已经运行')
        if not self.device.connected:
            raise RuntimeError('请先连接设备')
        if not self.policy.process or self.policy.process.poll() is not None:
            raise RuntimeError('请先加载模型')
        if mode == 'live' and self.config.get('execution_backend', 'native') == 'native':
            raise RuntimeError('训练营原生下牌已验证；模型自动执行与出站回执闭环尚未接入，只推理模式可用')
        if mode == 'live' and not self.config['calibrated']:
            raise RuntimeError('请先在对战画面校准竞技场和四个手牌槽位')
        self.mode = mode
        self.stop_event.clear()
        self.episode_id = self.last_tick = None
        self.last_frame_at = time.monotonic()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()

    def overlay_event(self, event):
        if (event.get('event') == 'queue_deploy' and event.get('side') == 'self'
                and event.get('card_id') == self.awaiting_card):
            self.ack.set()

    def run(self):
        source = getattr(self.device, 'stream', None) or FrameDirectory(self.config['frame_directory'])
        self.emit('control_status', '等待完整腾讯观测；overlay 下牌事件不足以驱动模型')
        try:
            while not self.stop_event.wait(0.02):
                if self.device.detached.is_set():
                    raise RuntimeError('设备或 Frida 已断开')
                frame = source.next()
                if frame is None:
                    if self.episode_id and time.monotonic() - self.last_frame_at > self.config['max_age_ms']/1000:
                        raise RuntimeError('完整观测流中断，自动操作已停止')
                    continue
                if frame.get('battle_active') is False:
                    if self.episode_id:
                        self.policy.request({'op': 'end'})
                        self.episode_id = self.last_tick = None
                    self.emit('control_status', '等待下一局')
                    continue
                observation = validate_frame(frame, self.config)
                self.latest_frame = frame
                self.last_frame_at = time.monotonic()
                if observation['terminal'].get('ended') or observation.get('truncated'):
                    self.policy.request({'op': 'end'})
                    self.episode_id = self.last_tick = None
                    self.emit('control_status', '对局结束')
                    continue
                tick = observation['tick']
                if self.episode_id != frame['battle_id']:
                    # Require prelude coverage instead of initializing an LSTM mid-match.
                    if tick != 0:
                        raise ValueError('新对局必须从 tick 0 开始读取，不能中途伪造模型记忆')
                    self.policy.request({'op': 'start', 'episode': frame['episode'],
                        'observation': observation, 'initial_elixir': frame['initial_elixir']})
                    self.episode_id, self.last_tick = frame['battle_id'], tick
                    self.emit('control_status', '对局已绑定，模型记忆已重置')
                    continue
                if tick <= self.last_tick:
                    raise ValueError('观测 tick 重复或倒退')
                if tick != self.last_tick + 5:
                    raise ValueError('观测丢帧：需要连续五 tick 的模型输入')
                self.last_tick = tick
                result = self.policy.request({'op': 'observe' if tick < 90 else 'act', 'observation': observation})
                self.emit('decision', result)
                if self.mode == 'live':
                    if self.config.get('execution_backend', 'native') == 'native':
                        raise RuntimeError('模型到原生执行的自动托管尚未接入，已停止')
                    for action in sorted(result.get('actions', []), key=lambda a: a.get('execute_offset_ticks', 0)):
                        if action['kind'] in ('wait', 'wait_until_affordable'):
                            continue
                        delay = action.get('execute_offset_ticks', 0)*frame['episode'].get('tick_ms', 50)/1000
                        deadline = frame['captured_at_ms']/1000 + delay
                        if self.stop_event.wait(max(0, deadline-time.time())):
                            break
                        validate_frame(frame, self.config)
                        if not self.device.foreground():
                            raise RuntimeError('腾讯客户端不在前台，停止操作')
                        if self.stop_event.is_set():
                            break
                        start, end = action_points(action, frame, self.config)
                        self.awaiting_card = action['card_id']
                        self.ack.clear()
                        self.device.drag(start, end)
                        self.emit('touch', {'card': self.awaiting_card, 'start': start, 'end': end})
                        # A pending action blocks duplicate deployment until overlay confirms it.
                        deadline = time.monotonic() + 2
                        while not self.ack.is_set() and not self.stop_event.wait(0.02):
                            if time.monotonic() > deadline:
                                raise RuntimeError('未收到我方下牌回执，已停止以避免重复操作')
                        self.awaiting_card = None
        except Exception as error:
            self.emit('error', str(error))
        finally:
            self.stop_event.set()
            self.awaiting_card = None
            if self.episode_id:
                try:
                    self.policy.request({'op': 'end'})
                except Exception:
                    pass
            self.episode_id = self.last_tick = None
            self.emit('control_status', '托管已停止')
