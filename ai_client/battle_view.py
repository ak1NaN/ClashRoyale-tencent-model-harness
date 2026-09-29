"""Presentation of the exact observation handed to FirstLight and public actions."""

from collections import deque
import json
import tkinter as tk
from tkinter import ttk


TOWER_NAMES = {'king': '国王塔', 'princess_left': '左公主塔', 'princess_right': '右公主塔'}
TOWER_TROOP_NAMES = {159000001: '加农炮手', 159000002: '飞刀女爵', 159000004: '皇家大厨'}


def summarize_observation(observation, card_names):
    owner = observation.get('owner')
    players = {p.get('owner'): p for p in observation.get('players', ())}
    ours, opponent = players.get(owner, {}), next((p for side, p in players.items() if side != owner), {})
    def card(card_id):
        return card_names.get(card_id, str(card_id))
    def elixir(player):
        value = player.get('elixir_exact')
        return f'{value:.2f}' if isinstance(value, (int, float)) else '未知'
    def towers(side):
        return '  ·  '.join(f"{TOWER_NAMES.get(t.get('tower_kind'), t.get('tower_kind', '?'))}"
            f"{'·'+TOWER_TROOP_NAMES[t['tower_troop_id']] if t.get('tower_troop_id') in TOWER_TROOP_NAMES else ''} "
            f"{t.get('hitpoints', '?')}/{t.get('max_hitpoints', '?')}"
            for t in observation.get('towers', ()) if t.get('owner') == side) or '暂无塔数据'
    mask = observation.get('action_mask') or {}
    optimistic = observation.get('metadata', {}).get('optimistic_entities') or []
    projected = sum(item.get('count', 0) for item in optimistic if item.get('state') == 'projected')
    projected_spells = sum(item.get('count', 0) for item in optimistic
                           if item.get('state') == 'projected' and item.get('kind') == 'spell')
    slots = mask.get('hand_slots') or []
    own_hand = '  ·  '.join(f'{i+1} {card(card_id)}' for i, card_id in enumerate(ours.get('hand') or [])) or '未知'
    revealed = '、'.join(card(card_id) for card_id in opponent.get('revealed_cards') or []) or '尚未公开'
    return dict(tick=observation.get('tick'), phase=observation.get('phase', '未知'),
        own_elixir=elixir(ours), opponent_elixir=elixir(opponent),
        own_towers=towers(owner), opponent_towers=towers(opponent.get('owner')),
        own_hand=own_hand, opponent_revealed=revealed,
        entities=len(observation.get('entities') or []),
        projected_entities=projected,
        projected_spells=projected_spells,
        input_events=len(observation.get('events') or []),
        pending=len(optimistic) or len(observation.get('pending_actions') or []),
        playable_slots=', '.join(str(i+1) for i, available in enumerate(slots) if available) or '无',
        source=observation.get('metadata', {}).get('source', '实时观测'))


class BattleView:
    def __init__(self, parent, card_names):
        self.frame = ttk.Frame(parent, padding=12)
        self.card_names = card_names
        self.seen_deployments = set()
        self.rows = deque(maxlen=300)
        self.last_observation = None
        self.last_output = None
        self.requests = {}
        self.projection_states = {}
        body = ttk.Frame(self.frame)
        body.pack(fill='both', expand=True)
        ttk.Label(body, text='模型输入 · 真实观测与待应用预测对象', style='Step.TLabel').pack(anchor='w')
        self.metrics = tk.StringVar(value='等待对局观测')
        ttk.Label(body, textvariable=self.metrics).pack(anchor='w', pady=(6, 9))
        self.record_file = tk.StringVar(value='本局 JSONL：对局结束后生成日期与模式文件')
        ttk.Label(body,textvariable=self.record_file,wraplength=850,foreground='#536778').pack(anchor='w',pady=(0,6))
        self.elixir = tk.StringVar(value='我方圣水 —    对手圣水 未知')
        ttk.Label(body, textvariable=self.elixir, font=('Helvetica', 13, 'bold')).pack(anchor='w')
        self.own_towers = tk.StringVar(value='我方塔血量：等待观测')
        self.opponent_towers = tk.StringVar(value='对手塔血量：等待观测')
        ttk.Label(body, textvariable=self.own_towers, wraplength=850).pack(anchor='w', pady=(8, 2))
        ttk.Label(body, textvariable=self.opponent_towers, wraplength=850).pack(anchor='w')
        self.cards = tk.StringVar(value='手牌与已公开卡牌：等待观测')
        ttk.Label(body, textvariable=self.cards, wraplength=850).pack(anchor='w', pady=(8, 2))
        self.input_counts = tk.StringVar(value='单位 —  · 输入事件 —  · 可下槽位 —  · 待应用 —')
        ttk.Label(body, textvariable=self.input_counts).pack(anchor='w', pady=(0, 10))
        ttk.Label(body, text='模型输出 / 客户端执行', style='Step.TLabel').pack(anchor='w')
        self.output = tk.StringVar(value='尚无模型决策')
        ttk.Label(body, textvariable=self.output, wraplength=850, justify='left').pack(anchor='w', pady=(6, 10))
        self.own_placement = tk.StringVar(value='我方落点：尚无')
        self.opponent_placement = tk.StringVar(value='对手落点：尚无')
        ttk.Label(body, textvariable=self.own_placement, wraplength=850, justify='left').pack(anchor='w', pady=(0, 3))
        ttk.Label(body, textvariable=self.opponent_placement, wraplength=850, justify='left').pack(anchor='w', pady=(0, 9))
        ttk.Label(body, text='双方操作与下牌链路', style='Step.TLabel').pack(anchor='w')
        columns = ('tick', 'side', 'stage', 'card', 'position', 'detail')
        self.events = ttk.Treeview(body, columns=columns, show='headings', height=12)
        for name, title, width in [('tick','tick',65), ('side','归属',62), ('stage','阶段',100),
                                    ('card','卡牌',130), ('position','落点',180), ('detail','详情',270)]:
            self.events.heading(name, text=title)
            self.events.column(name, width=width, stretch=name == 'detail')
        scroll = ttk.Scrollbar(body, orient='vertical', command=self.events.yview)
        self.events.configure(yscrollcommand=scroll.set)
        self.events.pack(side='left', fill='both', expand=True)
        scroll.pack(side='right', fill='y')

    def reset(self):
        self.seen_deployments.clear()
        self.rows.clear()
        for row in self.events.get_children():
            self.events.delete(row)
        self.last_observation = None
        self.last_output = None
        self.requests.clear()
        self.projection_states.clear()
        self.metrics.set('等待新对局观测')
        self.record_file.set('本局 JSONL：对局结束后生成日期与模式文件')
        self.output.set('尚无模型决策')
        self.own_placement.set('我方落点：尚无')
        self.opponent_placement.set('对手落点：尚无')

    def observation(self, value, inference_ms=None):
        self.last_observation = value
        summary = summarize_observation(value, self.card_names)
        latency = '—' if inference_ms is None else f'{inference_ms:.1f} ms'
        self.metrics.set(f"tick {summary['tick']}  ·  阶段 {summary['phase']}  ·  推理 {latency}")
        self.elixir.set(f"我方圣水 {summary['own_elixir']}    对手圣水 {summary['opponent_elixir']}")
        self.own_towers.set('我方塔血量：' + summary['own_towers'])
        self.opponent_towers.set('对手塔血量：' + summary['opponent_towers'])
        self.cards.set(f"我方手牌：{summary['own_hand']}\n对手已公开：{summary['opponent_revealed']}")
        self.input_counts.set(f"对象 {summary['entities']}（预测 {summary['projected_entities']}，其中法术 {summary['projected_spells']}）  ·  输入事件 {summary['input_events']}  ·  "
                              f"可下槽位 {summary['playable_slots']}  ·  待应用 {summary['pending']}")
        for state in value.get('metadata', {}).get('optimistic_entities') or []:
            if state.get('kind') != 'spell':
                continue
            request_id, phase = state.get('request_id'), state.get('state')
            if self.projection_states.get(request_id) == phase:
                continue
            self.projection_states[request_id] = phase
            stage = {'projected': '法术投影', 'real_observed': '真实弹体接管',
                     'spell_effect_elapsed': '法术投影结束'}.get(phase, '法术投影状态')
            self.add_event(value.get('tick'), '我方', stage, state.get('card_id'),
                f"原生 ({state.get('x')}, {state.get('y')})", f'请求 {request_id} · {phase}')
        return summary

    def decision(self, value):
        self.last_output = value
        actions = value.get('actions') or []
        selected = [a for a in actions if a.get('kind') in ('play_card','activate_ability')]
        if not selected:
            self.output.set(f"tick {value.get('tick', '—')}：等待  ·  推理 {value.get('inference_ms', 0):.1f} ms")
        else:
            parts = []
            for action in selected:
                if action.get('kind')=='activate_ability':
                    parts.append(f"技能 {action.get('ability_id')} · 实体 {action.get('source_entity')}")
                    continue
                target = action.get('target_grid')
                parts.append(f"{self.card_names.get(action.get('card_id'), action.get('card_id'))} "
                    f"· 槽位 {action.get('hand_slot', 0)+1} · 网格落点 {target}")
            self.output.set(f"tick {value.get('tick', '—')}：" + '；'.join(parts) +
                f"  ·  推理 {value.get('inference_ms', 0):.1f} ms")
        for action in selected:
            if action.get('kind')=='activate_ability':
                self.add_event(value.get('tick'),'我方','模型选择技能',None,
                    f"实体 {action.get('source_entity')}",str(action.get('ability_id')))
                continue
            card = self.card_names.get(action.get('card_id'), action.get('card_id'))
            self.own_placement.set(f"我方落点：模型选择 {card} · 网格 {action.get('target_grid')} · 等待客户端提交")
            self.add_event(value.get('tick'), '我方', '模型选择', action.get('card_id'),
                f"网格 {action.get('target_grid')}", '等待提交')

    def submitted(self, request):
        self.requests[request['id']] = request
        if request.get('kind')=='activate_ability':
            self.add_event(request.get('observed_tick'),'我方','技能已提交',request.get('card_id'),
                f"实体 {request['source_entity']}",f"{request['ability_id']} · 请求 {request['id']}")
            return
        card = self.card_names.get(request.get('card_id'), request.get('card_id'))
        position = f"原生 ({request['x']}, {request['y']})"
        self.own_placement.set(f"我方落点：{card} · {position} · 已提交，等待应用")
        self.add_event(request.get('observed_tick'),'我方','已提交',request.get('card_id'),
            position,f"请求 {request['id']} · 预计 tick {request['due_tick']}")

    def applied(self, receipt, count):
        request = self.requests.get(receipt.get('id'))
        if receipt.get('kind')=='activate_ability':
            self.add_event(receipt.get('tick'),'我方','技能已释放',receipt.get('card_id'),
                f"实体 {receipt['source_entity']}",
                f"{receipt['ability_id']} · 次数 {receipt.get('charges_before')} → {receipt.get('charges_after')}")
            return
        position = f"原生 ({request['x']}, {request['y']})" if request else '原生坐标未收到'
        card = self.card_names.get(receipt.get('card_id'), receipt.get('card_id'))
        self.own_placement.set(f"我方落点：{card} · {position} · tick {receipt.get('tick')} 已应用")
        self.add_event(receipt.get('tick'),'我方','已应用',receipt.get('card_id'),
            position,f"请求 {receipt.get('id')} · 本局第 {count} 次")

    def fizzled(self, receipt):
        self.requests.pop(receipt.get('id'),None)
        self.add_event(receipt.get('tick'),'我方','技能落空',receipt.get('card_id'),
            f"实体 {receipt.get('source_entity')}",
            f"{receipt.get('ability_id')} · 英雄阵亡，未计入成功释放")

    def skipped(self, detail):
        action=detail['action']
        self.add_event(detail['decision_tick'],'我方','未提交',action.get('card_id'),
            action.get('target_grid'),detail['reason'])

    def add_event(self, tick, side, stage, card_id, position=None, detail=''):
        card = self.card_names.get(card_id, str(card_id)) if card_id is not None else '—'
        if isinstance(position, (list, tuple)):
            position = ', '.join(str(x) for x in position)
        row = (tick if tick is not None else '—', side, stage, card, position or '—', detail)
        self.rows.append(row)
        self.events.insert('', 0, values=row)
        children = self.events.get_children()
        if len(children) > 300:
            self.events.delete(children[-1])

    def public_deployments(self, frame):
        new = []
        for deploy in frame.get('public_deployments') or []:
            identity = deploy.get('action_id') or (deploy.get('tick'), deploy.get('owner'),
                deploy.get('card_id'), deploy.get('x'), deploy.get('y'))
            if identity in self.seen_deployments:
                continue
            self.seen_deployments.add(identity)
            side = '我方' if deploy.get('owner') == frame.get('owner') else '对手'
            self.add_event(deploy.get('tick'), side, '公开下牌', deploy.get('card_id'),
                f"原生 ({deploy.get('x')}, {deploy.get('y')})",
                f"耗费 {deploy.get('effective_cost', '?')} · {deploy.get('source', '')}")
            if side == '对手':
                card = self.card_names.get(deploy.get('card_id'), deploy.get('card_id'))
                self.opponent_placement.set(f"对手落点：{card} · 原生 ({deploy.get('x')}, {deploy.get('y')}) · tick {deploy.get('tick')}")
            new.append(dict(side=side, **deploy))
        return new

    def raw_input(self):
        return json.dumps(self.last_observation, ensure_ascii=False, indent=2) if self.last_observation else ''
