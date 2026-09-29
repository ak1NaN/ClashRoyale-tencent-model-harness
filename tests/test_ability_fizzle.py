import io
import time
from types import SimpleNamespace

import pytest

from ai_client.autonomous import AutonomousBattle


def battle_with_pending_skill():
    events=[]
    stream=SimpleNamespace(receipts={})
    battle=AutonomousBattle({},None,SimpleNamespace(stream=stream),
                            lambda kind,value:events.append((kind,value)))
    battle.log=io.StringIO()
    battle.pending={'skill':dict(action={'kind':'activate_ability','source_entity':5000015},
                                 submitted_at=time.monotonic())}
    return battle,stream,events


def test_hero_death_before_ability_application_is_normal_fizzle():
    battle,stream,events=battle_with_pending_skill()
    stream.receipts['skill']={'id':'skill','kind':'activate_ability','ok':False,
        'stage':'source_despawned','error':'source_despawned_before_application',
        'source_entity':5000015,'ability_id':'MiniPekkHeroAbility','tick':1260}
    battle.receipts(1260)
    assert not battle.pending and battle.applied==0
    assert events[0][0]=='battle_fizzle'
    assert 'action_fizzled' in battle.log.getvalue()


def test_other_ability_failure_still_stops_controller():
    battle,stream,_=battle_with_pending_skill()
    stream.receipts['skill']={'id':'skill','kind':'activate_ability','ok':False,
        'stage':'stock_submit','error':'Native command rejected'}
    with pytest.raises(RuntimeError,match='原生命令拒绝'):
        battle.receipts(1260)
