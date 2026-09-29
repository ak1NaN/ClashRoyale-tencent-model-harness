from ai_client.debug_arena import arena_decoration_cells, oriented_position, scene_from_observation


def test_debug_arena_bridges_and_end_rows_match_displayed_cells():
    blocked, bridges = arena_decoration_cells()
    assert bridges == {(3, 15), (3, 16), (14, 15), (14, 16)}
    assert (4, 15) not in bridges and (13, 16) not in bridges
    for row in (0, 31):
        assert {col for col in range(18) if (col, row) not in blocked} == set(range(6, 12))


def test_debug_scene_uses_model_visible_projection_and_owner_colors():
    observation = {
        'owner': 0,
        'towers': [{'entity_id': 1, 'owner': 0, 'position': [9000, 3000],
                    'tower_kind': 'king', 'hitpoints': 4000}],
        'entities': [
            {'entity_id': 2, 'owner': 1, 'position': [8000, 22000], 'card_id': 10},
            {'entity_id': 3, 'owner': 0, 'position': [7000, 12000], 'card_id': 11},
            {'entity_id': 4, 'owner': 0, 'position': [7500, 12500], 'card_id': 12,
             'causal_group': {'handle': 'optimistic:queued'}},
        ],
        'metadata': {'optimistic_entities': [
            {'state': 'projected', 'request_id': 'queued', 'entity_ids': [4]}]},
    }
    scene = scene_from_observation(observation)
    assert [(item['entity_id'], item['side']) for item in scene] == [
        (1, 'own'), (2, 'opponent'), (3, 'own'), (4, 'virtual')]
    assert oriented_position(9000, 3000, 0) == (9000, 29000)
    assert oriented_position(9000, 29000, 1) == (9000, 29000)


def test_real_entity_is_not_colored_as_old_projection():
    observation = {'owner': 1, 'entities': [
        {'entity_id': 5, 'owner': 1, 'position': [5000, 27000], 'card_id': 13}],
        'metadata': {'optimistic_entities': [
            {'state': 'real_observed', 'request_id': 'queued', 'entity_ids': [5]}]}}
    assert scene_from_observation(observation)[0]['side'] == 'own'


def test_destroyed_tower_stays_in_model_observation_but_not_debug_scene():
    towers = [
        {'entity_id': 1, 'owner': 0, 'position': [3500, 6500],
         'tower_kind': 'princess_left', 'hitpoints': 0, 'active': False},
        {'entity_id': 2, 'owner': 1, 'position': [14500, 25500],
         'tower_kind': 'princess_right', 'hitpoints': 100, 'active': False},
        {'entity_id': 3, 'owner': 0, 'position': [9000, 3000],
         'tower_kind': 'king', 'hitpoints': 4000, 'active': True},
    ]
    observation = {'owner': 0, 'towers': towers, 'entities': []}
    assert [item['entity_id'] for item in scene_from_observation(observation)] == [3]
    assert len(observation['towers']) == 3
