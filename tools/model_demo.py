"""An explicitly synthetic model demonstration, isolated from device execution."""
from dataclasses import replace
from native_runner.contracts import (ActionMaskV1, EpisodeConfigV1, ObservationV1,
                                     PlayerStateV1, TowerStateV1, TimeStateV1)
from native_runner.match_factory import STANDARD_DECK
from native_runner.training.v4.factory import production_semantic_bundle

def run_demo(service):
    specs = production_semantic_bundle().card_specs
    deck = STANDARD_DECK
    placements = {str(slot): {'card_id': card, 'effective_cost': specs[card].elixir_cost,
        'form_code': 0, 'shape': [32, 18], 'row_major': [[True]*18 for _ in range(32)]}
        for slot, card in enumerate(deck[:4])}
    # These six towers and masks are demonstration fixtures, not live state.
    towers = tuple(TowerStateV1(entity_id=i, owner=owner, tower_kind=kind, position=(x,y),
        hitpoints=3000., max_hitpoints=3000., tower_troop_id=None if kind=='king' else 159000000)
        for i,owner,kind,x,y in ((1,0,'king',9000,3000),(2,0,'princess_left',3500,6500),
        (3,0,'princess_right',14500,6500),(4,1,'king',9000,29000),
        (5,1,'princess_left',3500,25500),(6,1,'princess_right',14500,25500)))
    initial = ObservationV1(tier='fair', owner=0, tick=0, episode_id='synthetic-gui-demo',
        phase='regulation', players=(PlayerStateV1(owner=0, elixir_exact=10., elixir_visible=10.,
        hand=deck[:4], deck=deck, cycle=deck[4:], private_state_visible=True,
        metadata={'hand_slot_by_card': {str(c):s for s,c in enumerate(deck[:4])},
                  'hand_runtime_by_slot':{str(s):{'form_code':0} for s in range(4)}}),
        PlayerStateV1(owner=1)), towers=towers, entities=(),
        action_mask=ActionMaskV1(kinds={'wait':True,'play_card':True,'activate_ability':False},
            hand_slots=(True,)*4, placement_masks=placements,
            reasons={'effective_elixir':10.,'reserved_elixir':0.}))
    episode=EpisodeConfigV1(ruleset_id='synthetic-gui-demo-only',deck0=deck,deck1=deck,seed=1)
    try:
        service.handle({'op':'start','episode':episode.to_dict(),'observation':initial.to_dict(),
                        'initial_elixir':{'0':10.,'1':10.}})
        for tick in range(5,90,5):
            obs=replace(initial,tick=tick,time=TimeStateV1(elapsed_ms=tick*50))
            service.handle({'op':'observe','observation':obs.to_dict()})
        decisions=[]
        for tick in (90,95,100):
            obs=replace(initial,tick=tick,time=TimeStateV1(elapsed_ms=tick*50))
            decisions.append(service.handle({'op':'act','observation':obs.to_dict()}))
        return {'ok':True,'source':'synthetic-gui-demo','execution_allowed':False,
                'decisions':decisions,'checkpoint_id':service.loaded.checkpoint_id}
    finally:
        service.end()

def check_live_cards(snapshot):
    from ai_client.model_input import inspect_live_input
    report=inspect_live_input(snapshot)
    bundle=production_semantic_bundle()
    report['card_mapping']=[]
    for card in report['hand']:
        card_id=card['card_id']
        try:
            vocab_id=bundle.card_catalog.require_vocab_id(card_id)
            if vocab_id <= 0:raise ValueError('unknown card')
            report['card_mapping'].append({'hand_slot':card['hand_slot'],'card_id':card_id,
                'vocab_id':vocab_id,'form':card['form'],'cost':card['cost'],'known':True})
        except (ValueError,KeyError):
            report['card_mapping'].append({'hand_slot':card['hand_slot'],'card_id':card_id,'known':False})
    report['unsupported_cards']=[c['card_id'] for c in report['card_mapping'] if not c['known']]
    report['ready']=report['ready'] and not report['unsupported_cards']
    return {'ok':True,'report':report}
