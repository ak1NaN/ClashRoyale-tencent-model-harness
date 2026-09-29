from test_live_mapping import frame
from live_mapping import LiveMapping
from live_tracker import PublicLiveTracker
import pytest

def with_public(f,owner,card,cost,token,tick=None):
    f['public_deployments']=[dict(world=f['world'],owner=owner,card_id=card,effective_cost=cost,
        action_id=token,tick=f['tick'] if tick is None else tick,form_code=0,x=3500,y=14000,
        source='stock-deploy-phase2-success')]
    return f

def test_public_bound_dedup_and_cycle_without_enemy_private_state():
    m=LiveMapping();o=m.observation(frame());t=PublicLiveTracker(1)
    t.reset(tick=0,initial_elixir={1:6});t.update(o);t.reconcile_owner_private_state(o)
    assert t.elixir_interval(0)==(0,10)
    o=m.observation(with_public(frame(5),0,26000003,5,'first',3));t.update(o)
    upper=t.elixir_interval(0)[1]
    assert 5<upper<5.1 and t.card_state(0,26000003).cycle_distance==4
    t.update(o);assert t.elixir_interval(0)[1]==upper
    for tick,card in [(10,26000017),(15,26000018),(20,28000008),(25,26000027)]:
        o=m.observation(with_public(frame(tick),0,card,1,str(tick)));t.update(o)
    assert t.card_state(0,26000003).cycle_distance==0
    assert next(p for p in o.players if p.owner==0).deck==()

def test_verified_tencent_musketeer_hero_identity():
    f=frame();f['battlefield']['objects'].append(dict(id=5000009,owner=1,x=9500,y=25500,
        target_x=9500,target_y=25500,card_id=306000014,data_id_40=130283371,
        data_name='MusketeerHero',hp=721,max_hp=900))
    o=LiveMapping().observation(f,extended_public_mapping=True)
    assert o.entities[0].card_id==26000014 and o.entities[0].entity_kind=='hero'


def test_extended_clock_matches_upstream_five_minute_feature_contract():
    m=LiveMapping();o=m.observation(frame(),extended_public_mapping=True)
    assert o.time.remaining_ms==300000 and o.time.elapsed_ms==0
    for tick in range(20,3601,20):o=m.observation(frame(tick),extended_public_mapping=True)
    assert o.phase=='overtime' and o.time.remaining_ms==120000
    assert o.time.elapsed_ms==180000 and o.time.elixir_multiplier==2.0


@pytest.mark.parametrize('gid',[-1315463181,2979504115])
def test_measured_ice_golem_hero_unsigned_hash_identity(gid):
    f=frame();f['battlefield']['objects'].append(dict(id=5000009,owner=0,x=11500,y=23499,
        target_x=11500,target_y=23499,card_id=306000038,data_id_40=gid,
        data_name='IceGolemiteHero',hp=1228,max_hp=1228))
    o=LiveMapping().observation(f,extended_public_mapping=True)
    assert o.entities[0].card_id==26000038 and o.entities[0].entity_kind=='hero'
    assert o.entities[0].native_data_global_id==2979504115
    assert not o.metadata['unknown_archetypes']


def test_only_exact_native_created_ids_share_deployment_root():
    f=frame();m=LiveMapping();m.observation(f)
    f=with_public(frame(5),0,26000003,5,'deploy-1',5)
    f['public_deployments'][0]['created_entity_ids']=[5000010,5000011]
    for oid in [5000010,5000011,5000012]:
        f['battlefield']['objects'].append(dict(id=oid,owner=0,x=4000,y=9000,target_x=4000,target_y=9500,
            card_id=26000003,data_id_40=34000003,data_name='Giant',hp=4000,max_hp=5766))
    o=m.observation(f,extended_public_mapping=True)
    assert o.entities[0].causal_group.handle==o.entities[1].causal_group.handle=='deploy-1'
    assert o.entities[2].causal_group is None
    f['tick']=10;f['public_deployments']=[]
    o=m.observation(f,extended_public_mapping=True)
    assert o.entities[0].causal_group.handle=='deploy-1'
