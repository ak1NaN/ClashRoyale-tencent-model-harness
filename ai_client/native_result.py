"""Confirm an unambiguous result from the game's terminal tower state."""
import json
from pathlib import Path


def terminal_result(directory):
    directory = Path(directory)
    final = None
    log = directory / 'events.jsonl'
    if not log.exists():
        return None
    with log.open() as stream:
        for line in stream:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                # The producer may still be appending the final line.
                continue
            if row.get('event') == 'capture' and row['value'].get('battle_finished'):
                final = row
    if final is None:
        return None
    frame = final['value']
    owner = frame.get('owner')
    marker = frame.get('native_end_marker')
    if type(owner) is not int or owner not in (0, 1) or type(marker) is not int or marker <= 0:
        return None
    objects = frame.get('battlefield', {}).get('objects', ())
    towers = {}
    for item in objects:
        identity = item.get('id')
        if type(identity) is not int or not 5000000 <= identity <= 5000005:
            continue
        if identity in towers or item.get('owner') != (identity-5000000)//3:
            return None
        hp = item.get('hp')
        if type(hp) not in (int, float) or hp < 0:
            return None
        towers[identity] = hp
    if 5000000 not in towers or 5000003 not in towers:
        return None

    def crowns_against(victim):
        base = 5000000 + 3*victim
        if towers[base] == 0:
            return 3
        return sum(towers.get(base + slot, 0) == 0 for slot in (1, 2))

    own_crowns = crowns_against(1-owner)
    enemy_crowns = crowns_against(owner)
    if own_crowns == enemy_crowns:
        return None
    result = 'win' if own_crowns > enemy_crowns else 'loss'
    return dict(directory=str(directory.resolve()), result=result, source='native_terminal',
                tick=frame['tick'], native_end_marker=marker, owner=owner,
                world=frame.get('world'), generation=frame.get('generation'),
                crowns=dict(own=own_crowns, opponent=enemy_crowns),
                tower_hp={str(identity): towers.get(identity, 0) for identity in range(5000000,5000006)},
                terminal_capture_at_ms=final['at_ms'])


def save_terminal_evidence(directory, evidence):
    path = Path(directory) / 'terminal-evidence.json'
    path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + '\n')
    return path
