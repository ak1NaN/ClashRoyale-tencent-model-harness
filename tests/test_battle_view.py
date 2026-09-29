from ai_client.battle_view import summarize_observation
from ai_client.observability import describe_event


def test_summary_uses_model_visible_values_and_owner_identity():
    observation = {
        'tick': 175, 'owner': 1, 'phase': 'regulation',
        'players': [
            {'owner': 0, 'elixir_exact': None, 'revealed_cards': [26000021]},
            {'owner': 1, 'elixir_exact': 4.75, 'hand': [26000010, 28000000]},
        ],
        'towers': [
            {'owner': 0, 'tower_kind': 'king', 'hitpoints': 4000, 'max_hitpoints': 4824},
            {'owner': 1, 'tower_kind': 'king', 'hitpoints': 4500, 'max_hitpoints': 4824},
        ],
        'entities': [{}], 'events': [{}, {}],
        'pending_actions': [{}], 'action_mask': {'hand_slots': [True, False]},
    }
    result = summarize_observation(observation, {26000021: '野猪骑士', 26000010: '骷髅兵', 28000000: '火球'})
    assert result['own_elixir'] == '4.75'
    assert result['opponent_elixir'] == '未知'
    assert '4500/4824' in result['own_towers']
    assert '4000/4824' in result['opponent_towers']
    assert result['own_hand'] == '1 骷髅兵  ·  2 火球'
    assert result['opponent_revealed'] == '野猪骑士'
    assert result['playable_slots'] == '1'
    assert result['entities'] == 1 and result['input_events'] == 2 and result['pending'] == 1


def test_public_deployment_log_names_side_and_card():
    severity, category, message, details = describe_event('public_deployment',
        {'side': '对手', 'tick': 80, 'card_id': 26000021, 'x': 8000, 'y': 26000},
        {26000021: '野猪骑士'})
    assert (severity, category, message) == ('info', 'battle', '对手公开下牌：野猪骑士')
    assert details['tick'] == 80 and details['x'] == 8000


def test_special_tower_identity_is_visible_in_model_input_summary():
    observation={'owner':1,'players':[{'owner':0},{'owner':1}],
        'towers':[{'owner':0,'tower_kind':'princess_left','tower_troop_id':159000002,
                   'hitpoints':3500,'max_hitpoints':3500},
                  {'owner':1,'tower_kind':'king','tower_troop_id':159000004,
                   'hitpoints':6408,'max_hitpoints':6408}]}
    result=summarize_observation(observation,{})
    assert '飞刀女爵' in result['opponent_towers']
    assert '皇家大厨' in result['own_towers']


def test_model_input_summary_distinguishes_optimistic_units():
    observation={'owner':1,'players':[{'owner':0},{'owner':1}],
        'entities':[{}, {}, {}],
        'metadata':{'optimistic_entities':[{'state':'projected','count':2,'request_id':'queued'},
             {'state':'projected','kind':'spell','count':1,'request_id':'queued-log'}]}}
    result=summarize_observation(observation,{})
    assert result['entities']==3 and result['projected_entities']==3
    assert result['projected_spells']==1 and result['pending']==2


def test_ability_event_messages_include_source_and_application():
    decision=describe_event('battle_decision',{'tick':200,'actions':[
        {'kind':'activate_ability','owner':1,'source_entity':5000018,'ability_id':'SkeletonKing'}]})
    assert 'SkeletonKing' in decision[2] and decision[3]['actions'][0]['source_entity']==5000018
    request={'id':'skill','kind':'activate_ability','source_entity':5000018,
             'ability_id':'SkeletonKing','observed_tick':200,'due_tick':201}
    submitted=describe_event('native_request',{'request':request})
    assert submitted[1]=='deploy' and '技能' in submitted[2]
    applied=describe_event('battle_application',{'receipt':dict(request, tick=220,
        charges_before=1,charges_after=0),'applied':1})
    assert applied[3]['charges_after']==0
