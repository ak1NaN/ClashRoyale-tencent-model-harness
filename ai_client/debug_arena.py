"""Live grid view of the exact battlefield observation sent to FirstLight."""

import math
import tkinter as tk
from tkinter import ttk


COLORS = {'own': '#2878c7', 'opponent': '#d64c58', 'virtual': '#f4c542'}
BRIDGE_COLUMNS = (3, 14)
RIVER_ROWS = (15, 16)
END_ROW_DEPLOY_COLUMNS = range(6, 12)


def arena_decoration_cells():
    """Troop terrain hints for the debug view; spells and model masks are untouched."""
    blocked_ends = {(col, row) for row in (0, 31) for col in range(18)
                    if col not in END_ROW_DEPLOY_COLUMNS}
    bridges = {(col, row) for col in BRIDGE_COLUMNS for row in RIVER_ROWS}
    return blocked_ends, bridges


def oriented_position(x, y, owner):
    """Keep the local player's king tower at the bottom of the grid."""
    return (18000-x, 32000-y) if owner == 0 else (x, y)


def scene_from_observation(observation):
    """Classify model-visible objects without mixing in the raw native frame."""
    owner = observation.get('owner')
    projected_ids = {
        entity_id
        for state in observation.get('metadata', {}).get('optimistic_entities', ())
        if state.get('state') == 'projected'
        for entity_id in state.get('entity_ids', ())
    }
    scene = []
    for source, tower in ((observation.get('towers') or (), True),
                          (observation.get('entities') or (), False)):
        for item in source:
            if tower and (item.get('active') is False or
                          isinstance(item.get('hitpoints'), (int, float)) and item['hitpoints'] <= 0):
                continue
            position = item.get('position')
            if not isinstance(position, (list, tuple)) or len(position) != 2:
                continue
            x, y = position
            if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in (x, y)):
                continue
            entity_id = item.get('entity_id')
            causal = item.get('causal_group') or {}
            is_virtual = entity_id in projected_ids or str(causal.get('handle', '')).startswith('optimistic')
            if not is_virtual and item.get('owner') not in (0, 1):
                continue
            side = 'virtual' if is_virtual else 'own' if item['owner'] == owner else 'opponent'
            scene.append(dict(side=side, tower=tower, entity_id=entity_id,
                card_id=item.get('card_id'), x=float(x), y=float(y),
                hp=item.get('hitpoints'), kind=item.get('tower_kind') if tower else item.get('entity_kind')))
    # Draw yellow predictions above real entities at the same location.
    scene.sort(key=lambda item: (item['side'] == 'virtual', not item['tower']))
    return scene


class DebugArena:
    def __init__(self, parent, card_names):
        self.frame = ttk.Frame(parent, padding=8)
        self.card_names = card_names
        self.last_observation = None
        self.stale = False
        self.summary = tk.StringVar(value='等待模型对局观测')
        ttk.Label(self.frame, textvariable=self.summary, style='Step.TLabel').pack(anchor='w', pady=(0, 5))
        legend = ttk.Frame(self.frame)
        legend.pack(fill='x', pady=(0, 6))
        for color, label in ((COLORS['own'], '● 我方'), (COLORS['opponent'], '● 对手'),
                             (COLORS['virtual'], '● 虚拟实体')):
            ttk.Label(legend, text=label, foreground=color).pack(side='left', padx=(0, 16))
        ttk.Label(legend, text='灰格/河道仅提示军队地形；法术不受此限制',
                  foreground='#536778').pack(side='right')
        self.canvas = tk.Canvas(self.frame, bg='#e9f0e8', highlightthickness=0,
                                width=410, height=570)
        self.canvas.pack(fill='both', expand=True)
        self.canvas.bind('<Configure>', lambda _event: self.render())
        self.canvas.bind('<Button-1>', self.inspect)
        self.detail = tk.StringVar(value='点击场上标记查看实体、坐标和血量')
        ttk.Label(self.frame, textvariable=self.detail, wraplength=690).pack(anchor='w', pady=(6, 0))
        self.hit_items = {}

    def reset(self):
        self.last_observation = None
        self.stale = False
        self.summary.set('等待模型对局观测')
        self.detail.set('点击场上标记查看实体、坐标和血量')
        self.canvas.delete('all')
        self.hit_items.clear()

    def observation(self, value):
        self.last_observation = value
        self.stale = False
        if self.frame.winfo_ismapped():
            self.render()

    def mark_stale(self):
        if self.last_observation is not None:
            self.stale = True
            if self.frame.winfo_ismapped():
                self.render()

    def inspect(self, event):
        for item_id in reversed(self.canvas.find_overlapping(event.x-7, event.y-7, event.x+7, event.y+7)):
            item = self.hit_items.get(item_id)
            if item is None:
                continue
            name = self.card_names.get(item['card_id'], item['kind'] or item['entity_id'])
            side = {'own': '我方', 'opponent': '对手', 'virtual': '虚拟实体'}[item['side']]
            self.detail.set(f"{side} · {name} · ID {item['entity_id']} · "
                            f"原生坐标 ({item['x']:.0f}, {item['y']:.0f}) · 血量 {item['hp'] if item['hp'] is not None else '—'}")
            break

    def render(self):
        if not self.frame.winfo_ismapped():
            return
        canvas = self.canvas
        width, height = max(canvas.winfo_width(), 1), max(canvas.winfo_height(), 1)
        scale = max(1, min((width-24)/18, (height-16)/32))
        left, top = (width-18*scale)/2, (height-32*scale)/2
        right, bottom = left+18*scale, top+32*scale
        canvas.delete('all')
        self.hit_items.clear()
        canvas.create_rectangle(left, top, right, bottom, fill='#dcebdc', outline='#67866e', width=2)
        blocked_ends, bridges = arena_decoration_cells()
        for col, row in blocked_ends:
            canvas.create_rectangle(left+col*scale, top+row*scale,
                                    left+(col+1)*scale, top+(row+1)*scale,
                                    fill='#b7c5b8', outline='')
        canvas.create_rectangle(left, top+15*scale, right, top+17*scale,
                                fill='#9bcbdc', outline='')
        for col, row in bridges:
            canvas.create_rectangle(left+col*scale, top+row*scale,
                                    left+(col+1)*scale, top+(row+1)*scale,
                                    fill='#c9b697', outline='')
        for col in range(1, 18):
            x = left+col*scale
            canvas.create_line(x, top, x, bottom, fill='#bfd3bf')
        for row in range(1, 32):
            y = top+row*scale
            canvas.create_line(left, y, right, y, fill='#bfd3bf')
        canvas.create_rectangle(left, top, right, bottom, fill='', outline='#67866e', width=2)
        if not self.last_observation:
            canvas.create_text(width/2, height/2, text='等待对局数据', fill='#536778')
            return
        obs = self.last_observation
        scene = scene_from_observation(obs)
        counts = {side: sum(item['side'] == side for item in scene) for side in COLORS}
        fast = '  ·  对手下牌快速帧' if obs.get('metadata', {}).get('urgent_opponent_deploy') else ''
        prefix = '已停止 · 最后一帧 · ' if self.stale else ''
        self.summary.set(f"{prefix}tick {obs.get('tick', '—')}  ·  蓝色我方 {counts['own']}  ·  "
                         f"红色对手 {counts['opponent']}  ·  黄色虚拟 {counts['virtual']}{fast}")
        owner = obs.get('owner')
        for item in scene:
            x, y = oriented_position(item['x'], item['y'], owner)
            px = left+max(0, min(18000, x))*scale/1000
            py = top+max(0, min(32000, y))*scale/1000
            radius = max(4, min(10, scale*.48)) if item['tower'] else max(3, min(7, scale*.32))
            color = COLORS[item['side']]
            if item['tower']:
                marker = canvas.create_rectangle(px-radius, py-radius, px+radius, py+radius,
                                                  fill=color, outline='#243c59', width=1)
            else:
                marker = canvas.create_oval(px-radius, py-radius, px+radius, py+radius,
                                            fill=color, outline='#243c59', width=1)
            self.hit_items[marker] = item
