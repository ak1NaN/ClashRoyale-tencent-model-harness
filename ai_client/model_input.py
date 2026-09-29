"""Describe live input readiness without filling missing battlefield fields."""
import math
import time

INPUT_LABELS = {'battle_lifecycle': '开局/结束', 'tick': '原生 tick', 'own_hand': '我方手牌',
                'own_elixir': '我方圣水', 'towers': '塔状态', 'entities': '战场单位',
                'public_events': '公开事件', 'placement': '合法落点',
                'forms': '卡牌形态', 'abilities': '技能状态'}

def inspect_live_input(snapshot, *, now_ms=None):
    if not isinstance(snapshot, dict):
        raise ValueError('尚未收到实时对局状态')
    now_ms = time.time()*1000 if now_ms is None else now_ms
    age = now_ms - float(snapshot['captured_at_ms'])
    if not math.isfinite(age) or not 0 <= age <= 750:
        raise ValueError('对局状态已过期，请在对局进行时检查')
    if snapshot.get('owner') not in (0, 1) or type(snapshot.get('tick')) is not int:
        raise ValueError('缺少本机玩家身份或原生 tick')
    hand = snapshot.get('hand', [])
    if len(hand) != 4 or sorted(c.get('hand_slot', -1) for c in hand) != list(range(4)):
        raise ValueError('手牌槽位映射不完整')
    coverage = set(snapshot.get('coverage', []))
    missing = sorted(set(INPUT_LABELS)-coverage)
    return {'schema': 'tencent-fl-readiness.v1', 'tick': snapshot['tick'], 'owner': snapshot['owner'],
            'age_ms': round(age, 1), 'available': sorted(coverage), 'missing': missing,
            'missing_labels': [INPUT_LABELS[key] for key in missing],
            'hand': [dict(card) for card in hand],
            'ready': snapshot.get('complete') is True and not missing,
            'source': 'live-tencent-native', 'execution_allowed': False}
