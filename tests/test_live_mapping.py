import copy
import sys
import time
from pathlib import Path
import pytest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tools'));sys.path.insert(0,str(ROOT/'third_party/FirstLight_CR'))
from live_mapping import LiveMapping,hero_source_card
from ai_client.bridge import NativeStream

def frame(tick=0):
    # Only a test fixture, never part of the live capture or execution path.
    deck=[26000017,26000018,28000008,26000003,26000027,28000012,28000016,26000033]
    objects=[]
    for owner in (0,1):
        for kind,x,y,gid,hp in [('king',9000,3000,35000000,7032),('left',3500,6500,35000001,4424),('right',14500,6500,35000001,4424)]:
            objects.append(dict(id=len(objects)+5000000,owner=owner,x=x,y=y if owner==0 else 32000-y,
                card_id=-1,data_id_40=gid,data_name='KingTower' if kind=='king' else 'PrincessTower',hp=hp,max_hp=hp))
    return dict(tick=tick,owner=1,generation=1,world='test',captured_at_ms=int(time.time()*1000),mana_raw=60000,
        hand=[dict(hand_slot=s,card_id=c,cost=cost,form=0) for s,(c,cost) in enumerate(zip(deck[:4],[5,4,4,5]))],
        battlefield=dict(deck=deck,cycle=deck[4:],objects=objects))

def test_actual_hp_and_hidden_opponent_preserved():
    o=LiveMapping().observation(frame())
    assert len(o.towers)==6 and o.towers[0].hitpoints==7032
    enemy=next(p for p in o.players if p.owner==0)
    assert enemy.hand==() and enemy.deck==() and enemy.elixir_exact is None
    assert o.metadata['complete'] is False
    mask=o.action_mask.placement_masks['0']['row_major']
    assert not mask[5][8] and mask[26][8]

def test_wrong_tower_identity_and_midgame_rejected():
    f=frame();f['battlefield']['objects'][1]['data_name']='DaggerDuchess'
    with pytest.raises(ValueError,match='未知塔兵身份'):LiveMapping().observation(f)
    with pytest.raises(ValueError,match='tick 0'):LiveMapping().observation(frame(100))


def test_non_tower_projectile_with_negative_card_id_is_not_treated_as_tower():
    f=frame()
    f['battlefield']['objects'].append(dict(id=5000010,owner=0,x=10000,y=10000,
        target_x=11000,target_y=11000,card_id=-1,data_id_40=10000003,
        data_name='TowerPrincessProjectile',hp=1,max_hp=1))
    o=LiveMapping().observation(f)
    assert len(o.towers)==6
    assert any(e.entity_id==5000010 for e in o.entities)


def test_dagger_duchess_towers_keep_exact_troop_id():
    f=frame()
    for obj in f['battlefield']['objects']:
        if obj['owner']==0 and obj['data_name']=='PrincessTower':
            obj.update(data_id_40=2545895475,data_name='DaggerDuchess',hp=3571,max_hp=3571)
    o=LiveMapping().observation(f)
    enemy_towers=[t for t in o.towers if t.owner==0]
    assert len(enemy_towers)==3
    assert [t.tower_troop_id for t in enemy_towers]==[None,159000002,159000002]
    assert 'tower_troop_runtime_unknown' in o.metadata['limitations']


def test_royal_chef_king_and_side_towers_have_distinct_ids():
    f=frame()
    for obj in f['battlefield']['objects']:
        if obj['owner']!=0:continue
        if obj['data_name']=='KingTower':
            obj.update(data_id_40=1471932914,data_name='ChefTowerKing',hp=6408,max_hp=6408)
        else:
            obj.update(data_id_40=3191249505,data_name='ChefTower',hp=3571,max_hp=3571)
    o=LiveMapping().observation(f)
    enemy_towers=[t for t in o.towers if t.owner==0]
    assert [t.tower_troop_id for t in enemy_towers]==[159000004,159000000,159000000]
    assert all(t.hitpoints==t.max_hitpoints for t in enemy_towers)
    assert 'tower_troop_runtime_unknown' in o.metadata['limitations']

def test_revealed_entity_resolves_catalog_and_events():
    m=LiveMapping();m.observation(frame())
    f=frame(5);f['battlefield']['objects'].append(dict(id=5000010,owner=0,x=4000,y=9000,target_x=4000,target_y=9500,
        card_id=26000003,data_id_40=34000003,data_name='Giant',hp=4000,max_hp=5766))
    o=m.observation(f)
    assert o.entities[0].native_data_global_id==34000003
    assert o.events[0].event_type=='spawn'
    assert next(p for p in o.players if p.owner==0).revealed_cards==(26000003,)
    assert not o.metadata['unknown_archetypes']

def test_corrupt_cycle_and_form_rejected():
    for mutate in (lambda f:f['battlefield'].update(cycle=[26000017]*4),lambda f:f['hand'][0].update(form=9)):
        f=frame();mutate(f)
        with pytest.raises(ValueError):LiveMapping().observation(f)

def test_special_form_is_observed_but_not_executable():
    f=frame();f['hand'][0]['form']=2
    o=LiveMapping().observation(f)
    assert not o.action_mask.hand_slots[0]
    assert o.players[1].metadata['hand_runtime_by_slot']['0']['form_code']==2

def test_min_card_gap_masks_all_card_placements():
    o=LiveMapping().observation(frame(),block_play_cards=True)
    assert not o.action_mask.kinds['play_card']
    assert not any(o.action_mask.hand_slots)
    assert all(not any(any(row) for row in mask['row_major'])
               for mask in o.action_mask.placement_masks.values())
    assert o.action_mask.reasons['min_card_gap_active'] is True


def test_queued_defender_is_visible_to_model_until_real_spawn():
    m=LiveMapping();m.observation(frame())
    request=dict(id='queued-mini-pekka',generation=1,world='test',observed_tick=0,
                 card_id=26000018,form_code=0,x=9000,y=25000)
    pending=[dict(request=request,action=dict(kind='play_card',owner=1,card_id=26000018),decision_tick=0)]
    unseen=m.observation(frame(5),pending_deployments=pending,
        reserved_actions=[dict(kind='play_card',card_id=26000018,hand_slot=1)])
    ghost=next(e for e in unseen.entities if e.card_id==26000018)
    assert ghost.owner==1 and ghost.position==(9000,25000)
    assert ghost.hitpoints==ghost.max_hitpoints and ghost.native_data_global_id==34000016
    assert unseen.metadata['optimistic_entities'][0]['state']=='projected'
    assert not unseen.action_mask.hand_slots[1]
    assert not any(e.event_type=='spawn' for e in unseen.events)
    public_only=frame(8)
    public_only['public_deployments']=[dict(world='test',tick=8,owner=1,
        card_id=26000018,x=9000,y=25000,action_id=1,created_entity_ids=[],
        effective_cost=4,form_code=0,source='native')]
    still_pending=m.observation(public_only,pending_deployments=pending)
    assert still_pending.metadata['optimistic_entities'][0]['state']=='projected'
    assert len(still_pending.entities)==1
    real=frame(10)
    real['battlefield']['objects'].append(dict(id=5000010,owner=1,x=9050,y=24900,
        target_x=9050,target_y=24900,card_id=26000018,data_id_40=34000016,
        data_name='MiniPekka',hp=2019,max_hp=2019))
    resolved=m.observation(real,pending_deployments=pending)
    assert [e.entity_id for e in resolved.entities]==[5000010]
    assert resolved.metadata['optimistic_entities'][0]['state']=='real_observed'
    assert resolved.events[0].event_type=='spawn'


def test_optimistic_defender_reaches_production_firstlight_tensor():
    from live_tracker import LiveTensorizer, PublicLiveTracker
    m=LiveMapping();b=m.bundle;deck=frame()['battlefield']['deck']
    tensorizer=LiveTensorizer(actor_owner=1,card_catalog=b.card_catalog,
        ability_catalog=b.ability_catalog,entity_archetype_catalog=b.entity_archetype_catalog,
        effect_catalog=b.effect_catalog,card_specs=b.card_specs,deck=deck,
        deck_roles={card:(False,False) for card in deck},tracker=PublicLiveTracker(1))
    tensorizer.start_episode(m.observation(frame(),extended_public_mapping=True),initial_elixir={})
    pending=[dict(request=dict(id='queued-mini-pekka',generation=1,world='test',
        observed_tick=0,card_id=26000018,form_code=0,x=9000,y=25000),
        action=dict(kind='play_card',owner=1,card_id=26000018),decision_tick=0)]
    observation=m.observation(frame(5),pending_deployments=pending,extended_public_mapping=True)
    batch=tensorizer.tensorize(observation)
    assert int(batch.groups.mask.sum())==1
    assert int(batch.groups.child_mask.sum())==1


def test_queued_multi_unit_card_projects_group_and_removes_on_receipt():
    initial=frame();initial['battlefield']['deck'][0]=26000005
    initial['hand'][0]['card_id']=26000005
    m=LiveMapping();m.observation(initial)
    request=dict(id='queued-minions',generation=1,world='test',observed_tick=0,
                 card_id=26000005,form_code=0,x=9000,y=24000)
    pending=[dict(request=request,action=dict(kind='play_card',owner=1,card_id=26000005),decision_tick=0)]
    next_frame=frame(5);next_frame['battlefield']['deck'][0]=26000005
    next_frame['hand'][0]['card_id']=26000005
    projected=m.observation(next_frame,pending_deployments=pending)
    assert len(projected.entities)==3
    assert len({e.causal_group.handle for e in projected.entities})==1
    assert projected.metadata['optimistic_entities'][0]['count']==3
    after_frame=frame(10);after_frame['battlefield']['deck'][0]=26000005
    after_frame['hand'][0]['card_id']=26000005
    after_receipt=m.observation(after_frame,pending_deployments=[])
    assert not after_receipt.entities
    assert not after_receipt.metadata['optimistic_entities']


def test_spell_and_wrong_battle_never_create_optimistic_entity():
    m=LiveMapping();m.observation(frame())
    pending=[dict(request=dict(id='spell',generation=1,world='test',observed_tick=0,
        card_id=28000008,form_code=0,x=9000,y=25000),
        action=dict(kind='play_card',owner=1,card_id=28000008),decision_tick=0),
        dict(request=dict(id='other-battle',generation=1,world='other',observed_tick=0,
        card_id=26000018,form_code=0,x=9000,y=25000),
        action=dict(kind='play_card',owner=1,card_id=26000018),decision_tick=0)]
    o=m.observation(frame(5),pending_deployments=pending)
    assert not o.entities
    assert o.metadata['optimistic_entities'][0]['state']=='unsupported_card'


def test_log_projection_reaches_model_and_yields_to_real_projectile():
    from live_tracker import LiveTensorizer, PublicLiveTracker
    def log_frame(tick):
        f=frame(tick)
        f['battlefield']['deck'][2]=28000011
        f['hand'][2]['card_id']=28000011
        return f
    m=LiveMapping();initial=m.observation(log_frame(0))
    request=dict(id='queued-log',generation=1,world='test',observed_tick=0,
                 card_id=28000011,form_code=0,x=9000,y=25000)
    pending=[dict(request=request,action=dict(kind='play_card',owner=1,card_id=28000011))]
    projected=m.observation(log_frame(5),pending_deployments=pending)
    ghost=next(e for e in projected.entities if e.card_id==28000011)
    assert (ghost.entity_kind,ghost.native_data_global_id,ghost.position)==(
        'projectile',10000032,(9000,25000))
    assert ghost.movement_target==(9000,14900)
    assert projected.metadata['optimistic_entities'][0]['state']=='projected'
    assert not projected.events
    b=m.bundle;deck=log_frame(0)['battlefield']['deck']
    tensorizer=LiveTensorizer(actor_owner=1,card_catalog=b.card_catalog,
        ability_catalog=b.ability_catalog,entity_archetype_catalog=b.entity_archetype_catalog,
        effect_catalog=b.effect_catalog,card_specs=b.card_specs,deck=deck,
        deck_roles={card:(False,False) for card in deck},tracker=PublicLiveTracker(1))
    tensorizer.start_episode(initial,initial_elixir={})
    batch=tensorizer.tensorize(projected)
    assert int(batch.groups.mask.sum())==1
    assert int(batch.groups.child_mask.sum())==1
    real=log_frame(10)
    real['battlefield']['objects'].append(dict(id=5000010,owner=1,x=9000,y=24500,
        target_x=9000,target_y=24000,card_id=28000011,data_id_40=10000031,
        data_name='LogProjectile'))
    resolved=m.observation(real,pending_deployments=pending)
    assert [e.entity_id for e in resolved.entities]==[5000010]
    assert resolved.metadata['optimistic_entities'][0]['state']=='real_observed'


def test_fireball_projection_expires_after_measured_application_if_projectile_missed():
    from live_tracker import LiveTensorizer, PublicLiveTracker
    def fireball_frame(tick):
        f=frame(tick)
        f['battlefield']['deck'][2]=28000000
        f['hand'][2]['card_id']=28000000
        return f
    m=LiveMapping();initial=m.observation(fireball_frame(0))
    request=dict(id='queued-fireball',generation=1,world='test',observed_tick=0,
                 card_id=28000000,form_code=0,x=12000,y=18000)
    pending=[dict(request=request,action=dict(kind='play_card',owner=1,card_id=28000000))]
    projected=m.observation(fireball_frame(5),pending_deployments=pending)
    ghost=projected.entities[0]
    assert (ghost.position,ghost.movement_target,ghost.native_data_global_id)==(
        (9000,29000),(12000,18000),10000000)
    b=m.bundle;deck=fireball_frame(0)['battlefield']['deck']
    tensorizer=LiveTensorizer(actor_owner=1,card_catalog=b.card_catalog,
        ability_catalog=b.ability_catalog,entity_archetype_catalog=b.entity_archetype_catalog,
        effect_catalog=b.effect_catalog,card_specs=b.card_specs,deck=deck,
        deck_roles={card:(False,False) for card in deck},tracker=PublicLiveTracker(1))
    tensorizer.start_episode(initial,initial_elixir={})
    assert int(tensorizer.tensorize(projected).groups.child_mask.sum())==1
    public=fireball_frame(10)
    public['public_deployments']=[dict(world='test',tick=9,owner=1,card_id=28000000,
        x=12000,y=18000,action_id='fireball',effective_cost=4,form_code=0,source='native')]
    assert m.observation(public,pending_deployments=pending).metadata[
        'optimistic_entities'][0]['state']=='projected'
    elapsed=m.observation(fireball_frame(25),pending_deployments=pending)
    assert not elapsed.entities
    assert elapsed.metadata['optimistic_entities'][0]['state']=='spell_effect_elapsed'


def test_optimistic_entity_must_match_the_models_queued_card():
    m=LiveMapping();m.observation(frame())
    request=dict(id='wrong-card',generation=1,world='test',observed_tick=0,
        card_id=26000018,form_code=0,x=9000,y=25000)
    pending=[dict(request=request,
        action=dict(kind='play_card',owner=1,card_id=26000017),decision_tick=0)]
    observation=m.observation(frame(5),pending_deployments=pending)
    assert not observation.entities
    assert not observation.metadata['optimistic_entities']

def test_trial_stream_does_not_promote_partial_or_drop_overflow():
    stream=NativeStream();stream.begin_trial();stream.trial_frames=__import__('queue').Queue(maxsize=1)
    stream.ingest(dict(event='native_partial_frame',frame=frame()))
    stream.ingest(dict(event='native_partial_frame',frame=frame(5)))
    assert stream.next() is None
    with pytest.raises(RuntimeError,match='溢出'):stream.next_trial()


def test_hero_ability_requires_exact_runtime_source_and_reserves_cost():
    f=frame()
    f['battlefield']['deck'][0]=26000069
    f['hand'][0]['card_id']=26000069
    f['battlefield']['objects'].append(dict(id=5000010,owner=1,x=9000,y=25000,
        target_x=9000,target_y=25000,card_id=26000069,data_id_40=None,
        data_name='SkeletonKing',hp=1000,max_hp=1000))
    f['abilities']=[dict(source_entity=5000010,card_id=26000069,controller_slot=1,
        button_state=2,charges_raw=1,remaining_cooldown_ms=0,configured_cooldown_ms=0)]
    disabled=LiveMapping().observation(f,allow_abilities=False)
    assert not disabled.action_mask.ability_sources
    enabled=LiveMapping().observation(f,allow_abilities=True)
    assert enabled.action_mask.ability_sources==(5000010,)
    gated=LiveMapping().observation(f,allow_abilities=True,block_play_cards=True)
    assert gated.action_mask.ability_sources==(5000010,)
    assert gated.action_mask.kinds['activate_ability']
    state=enabled.players[1].ability_runtime_states[0]
    assert state.ability_id=='SkeletonKing' and state.available and state.elixir_cost==2
    reserved=LiveMapping().observation(f,allow_abilities=True,reserved_actions=[
        dict(kind='activate_ability',source_entity=5000010,ability_id='SkeletonKing')])
    assert not reserved.action_mask.ability_sources
    assert reserved.action_mask.reasons['reserved_elixir']==2


def test_tencent_hero_form_maps_mini_pekka_source_and_ability():
    f=frame()
    f['hand'][1]['form']=2
    f['battlefield']['deck_runtime']=[dict(card_id=c,form=2 if c==26000018 else 0)
        for c in f['battlefield']['deck']]
    f['battlefield']['objects'].append(dict(id=5000017,owner=1,x=9000,y=25000,
        target_x=9000,target_y=25000,card_id=306000018,data_id_40=3126564425,
        data_name='MiniPekkaHero',hp=2019,max_hp=2019))
    f['abilities']=[dict(source_entity=5000017,card_id=306000018,controller_slot=2,
        button_state=2,charges_raw=1,remaining_cooldown_ms=0,configured_cooldown_ms=0)]
    o=LiveMapping().observation(f,allow_abilities=True,extended_public_mapping=True)
    assert o.action_mask.ability_sources==(5000017,)
    assert next(e for e in o.entities if e.entity_id==5000017).card_id==26000018
    state=o.players[1].ability_runtime_states[0]
    assert state.ability_id=='MiniPekkHeroAbility' and state.elixir_cost==1


def test_hero_form_ids_use_upstream_binding_instead_of_low_digit_guess():
    assert hero_source_card(306000018)==26000018
    assert hero_source_card(306000076)==26000102  # Berserker
    assert hero_source_card(306000088)==27000009  # Tombstone
    assert hero_source_card(306000107)==28000015  # Barbarian Barrel
    assert hero_source_card(306000023) is None    # Ice Wizard absent upstream


def test_loading_warmup_gap_uses_actual_tick_but_live_gap_stops():
    m=LiveMapping();m.observation(frame())
    o=m.observation(frame(30),extended_public_mapping=True)
    assert o.tick==30 and o.time.elapsed_ms==1500
    assert o.metadata['observed_tick_gap']==30
    m.observation(frame(90),extended_public_mapping=True)
    with pytest.raises(ValueError,match='间隔'):m.observation(frame(135))


def test_stale_frame_recovery_chooses_real_latest_and_preserves_skipped():
    stream=NativeStream();stream.begin_trial()
    old=frame(100);old['captured_at_ms']=0
    middle=frame(105);middle['captured_at_ms']=300
    fresh=frame(110);fresh['captured_at_ms']=950
    stream.ingest(dict(event='native_partial_frame',frame=middle))
    stream.ingest(dict(event='native_partial_frame',frame=fresh))
    chosen,skipped=stream.fresh_trial_frame(old,now_ms=1000)
    assert chosen is fresh and skipped==[old,middle]
    assert stream.next_trial() is None


def test_catch_up_uses_latest_even_when_first_frame_is_under_age_limit():
    stream=NativeStream();stream.begin_trial()
    old=frame(100);old['captured_at_ms']=700
    fresh=frame(105);fresh['captured_at_ms']=950
    stream.ingest(dict(event='native_partial_frame',frame=fresh))
    chosen,skipped=stream.fresh_trial_frame(old,now_ms=1000)
    assert chosen is fresh and skipped==[old]


def test_opponent_deployment_frame_wakes_model_and_clears_when_consumed():
    stream=NativeStream();stream.begin_trial()
    regular=frame(100);urgent=frame(101);urgent['urgent_opponent_deploy']=True
    stream.ingest(dict(event='native_partial_frame',frame=regular))
    assert not stream.urgent_event.is_set()
    stream.ingest(dict(event='native_partial_frame',frame=urgent))
    assert stream.urgent_event.is_set()
    chosen,skipped=stream.fresh_trial_frame(stream.next_trial())
    assert chosen is urgent and skipped==[regular]
    assert not stream.urgent_event.is_set()


def test_urgent_frame_marker_reaches_the_model_observation():
    f=frame()
    f['urgent_opponent_deploy']=True
    observation=LiveMapping().observation(f)
    assert observation.metadata['urgent_opponent_deploy'] is True
