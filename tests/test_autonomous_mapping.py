from test_live_mapping import frame
from live_mapping import LiveMapping

def test_reservation_disables_native_inflight_and_reduces_available_elixir():
    f=frame();o=LiveMapping().observation(f,reserved_actions=[{'card_id':26000017,'hand_slot':0}])
    assert not o.action_mask.hand_slots[0]
    assert o.action_mask.reasons['effective_elixir']==1
    assert o.action_mask.reasons['reserved_elixir']==5

def test_native_form_deployment_is_explicit_opt_in():
    f=frame();f['hand'][0]['form']=2
    o=LiveMapping().observation(f,allow_forms=True)
    assert o.action_mask.hand_slots[0]
    assert o.action_mask.placement_masks['0']['form_code']==2

def test_tower_removal_is_annotated_and_updates_placement():
    m=LiveMapping();m.observation(frame())
    f=frame(5);gone=f['battlefield']['objects'].pop(1)
    o=m.observation(f)
    tower=next(t for t in o.towers if t.entity_id==gone['id'])
    assert tower.hitpoints==0 and not tower.active
    assert o.metadata['tower_destruction_from_vector_removal']==(gone['id'],)


def test_live_gap_uses_actual_time_without_inventing_frames():
    m=LiveMapping();f=frame();unit=dict(id=5000010,owner=0,x=4000,y=9000,target_x=4000,target_y=9500,card_id=26000003,data_id_40=34000003,data_name='Giant',hp=4000,max_hp=5766)
    f['battlefield']['objects'].append(unit);m.observation(f)
    f=frame(10);f['battlefield']['objects'].append(dict(unit,x=4200));o=m.observation(f)
    assert o.tick==10 and o.metadata['observed_tick_gap']==10
    assert o.entities[0].velocity==(400.0,0.0)
    import pytest
    with pytest.raises(ValueError,match='向前'):m.observation(f)
    m.observation(frame(90))
    with pytest.raises(ValueError,match='两秒'):m.observation(frame(135))
