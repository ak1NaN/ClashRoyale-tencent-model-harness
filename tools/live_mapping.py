"""Measured Tencent observations for a bounded experimental policy trial.

Unknown combat runtime stays explicitly unknown. No hidden opponent deck/hand is
read; the upstream tracker=None path encodes its elixir bound as [0,10].
"""
from native_runner.contracts import (ObservationV1, PlayerStateV1, TowerStateV1,
    EntityStateV1, TimeStateV1, ActionMaskV1, EventV1, CausalGroupRefV1,
    AbilityRuntimeStateV1, AbilityPhase, SemanticProvenanceV1,
    SemanticEvidenceLevel, ABILITY_RUNTIME_STATE_FIELDS,
    PLAYER_RUNTIME_SEMANTIC_FIELDS)
from native_runner.arena import card_placement_mask, OccupiedFootprintV1, native_building_footprint
from native_runner.training.v4.factory import production_semantic_bundle
from native_runner.training.v4.tensorizer import UniversalObservationTensorizerV4
from native_runner.training.v4.policy_session import PolicySessionV4
from native_runner.normal_form_evidence import NORMAL_MODE_HERO_FORM_TO_BASE_CARD
from dataclasses import replace
import hashlib
import math
from statistics import median
from live_tracker import LiveTensorizer, PublicLiveTracker

RULESET = 'a' * 64
LIMITATIONS = ['combat_runtime_unknown', 'ability_execution_disabled',
    'opponent_elixir_is_public_interval', 'equipped_evolution_roles_unmapped',
    'timer_derived_from_standard_timeline', 'events_are_snapshot_deltas']

# Exact Tencent object identities observed in classic 1v1 captures. Royal
# Chef's side towers still use the Princess troop in FirstLight's contract;
# the Chef identity belongs to its king tower.
TOWER_OBJECTS = {
    (35000000, 'KingTower'): ('king', None),
    (35000001, 'PrincessTower'): ('princess', 159000000),
    (2545895475, 'DaggerDuchess'): ('princess', 159000002),
    (1471932914, 'ChefTowerKing'): ('king', 159000004),
    (3191249505, 'ChefTower'): ('princess', 159000000),
}
TOWER_START_POSITIONS = {
    (9000,3000),(3500,6500),(14500,6500),
    (9000,29000),(3500,25500),(14500,25500),
}
# Only projectile forms verified against Tencent's live object vector. These
# are model-input projections; they do not deal damage or alter enemy state.
SPELL_PROJECTILES = {
    28000000: (10000000,),  # FireballSpell
    28000011: (10000031, 10000032),  # LogProjectile / LogProjectileRolling
    28000015: (10000041, 10000042),  # BarbLogProjectile / Rolling
}

def hero_source_card(runtime_card_id):
    """Use upstream's exact form-to-source table with Tencent's class-306 prefix."""
    if type(runtime_card_id) is int and 306000000<=runtime_card_id<306001000:
        return NORMAL_MODE_HERO_FORM_TO_BASE_CARD.get(203000000+(runtime_card_id-306000000))
    return None

class LiveMapping:
    def __init__(self):
        self.bundle = production_semantic_bundle()
        self.episode_id = None
        self.previous = {}
        self.birth = {}
        self.revealed = set()
        self.towers = {}
        self.last_tick = None
        self.deployment_roots = {}
        self.spell_public_ticks = {}
        self.character_names = dict(self.bundle.entity_archetype_catalog.character_global_ids)
        self.character_ids_by_name = {name: gid for gid,name in self.bundle.entity_archetype_catalog.character_global_ids}
    def _optimistic_entities(self, frame, pending_deployments, real_entities):
        """Expose queued troops to the policy until a matching real spawn appears.

        Ghosts are observation-only. They never enter the measured object delta,
        public events, native commands, or receipt accounting.
        """
        owner=frame['owner'];tick=frame['tick'];world=frame['world']
        objects=frame['battlefield']['objects']
        ratios=[]
        for obj in objects:
            if obj.get('owner')!=owner or obj.get('card_id',-1)<=0:continue
            spec=self.bundle.card_specs.get(obj['card_id'])
            if spec and spec.hitpoints and obj.get('max_hp',0)>0:
                ratio=obj['max_hp']/spec.hitpoints
                if 1<=ratio<=8:ratios.append(ratio)
        hp_scale=median(ratios) if ratios else 3.5
        ghosts=[];statuses=[]
        active_ids={item.get('request',{}).get('id') for item in pending_deployments}
        for stale_id in self.spell_public_ticks.keys()-active_ids:
            del self.spell_public_ticks[stale_id]
        for item in pending_deployments:
            request=item.get('request',{})
            action=item.get('action',{})
            request_id=request.get('id');card_id=request.get('card_id')
            if (not isinstance(request_id,str) or not request_id or
                action.get('kind')!='play_card' or action.get('card_id')!=card_id or
                action.get('owner',owner)!=owner or
                card_id not in frame['battlefield']['deck'] or
                request.get('world')!=world or request.get('generation')!=frame['generation'] or
                not isinstance(request.get('observed_tick'),int) or
                not request['observed_tick']<=tick<=request['observed_tick']+80):
                continue
            x,y=request.get('x'),request.get('y')
            if not isinstance(x,int) or not isinstance(y,int):continue
            spec=self.bundle.card_specs.get(card_id)
            if spec is None:continue
            status=dict(request_id=request_id,card_id=card_id,x=x,y=y)
            if spec.kind.value=='spell' and card_id in SPELL_PROJECTILES:
                form=request.get('form_code',0)
                if form != 0:
                    status.update(state='unsupported_form');statuses.append(status);continue
                gids=SPELL_PROJECTILES[card_id]
                for public in frame.get('public_deployments',()):
                    if (public.get('owner')==owner and public.get('card_id')==card_id and
                        public.get('x')==x and public.get('y')==y and
                        public.get('world')==world and
                        public.get('tick',-1)>=request['observed_tick']):
                        self.spell_public_ticks[request_id]=public['tick']
                real_match=any(e.owner==owner and e.card_id==card_id and
                    e.native_data_global_id in gids and
                    self.birth.get(e.entity_id,-1)>=request['observed_tick']
                    for e in real_entities)
                if real_match:
                    self.spell_public_ticks.pop(request_id,None)
                    status.update(state='real_observed',kind='spell');statuses.append(status);continue
                # A fast spell may be gone before the next 250 ms capture.
                # Its measured public application still bounds the projection.
                public_tick=self.spell_public_ticks.get(request_id)
                if public_tick is not None and tick-public_tick>10:
                    self.spell_public_ticks.pop(request_id,None)
                    status.update(state='spell_effect_elapsed',kind='spell');statuses.append(status);continue
                direction=1 if owner==0 else -1
                if card_id==28000000:
                    gid=10000000
                    sx,sy=9000,3000 if owner==0 else 29000
                    distance=math.hypot(x-sx,y-sy) or 1
                    velocity=((x-sx)*59000/distance,(y-sy)*59000/distance)
                    position=(sx,sy)
                    target=(x,y)
                else:
                    gid=gids[-1]
                    position=(x,y)
                    target=(x,max(0,min(32000,y+direction*10100)))
                    velocity=(0,direction*4000)
                base=1_000_000_000+int.from_bytes(hashlib.blake2s(request_id.encode(),digest_size=8).digest(),'big')*32
                ghosts.append(EntityStateV1(entity_id=base,owner=owner,card_id=card_id,
                    entity_kind='projectile',position=position,velocity=velocity,
                    movement_target=target,native_data_global_id=gid,
                    age_ms=(tick-request['observed_tick'])*50,
                    causal_group=CausalGroupRefV1(kind='deployment',
                        handle='optimistic-spell:'+request_id,source_card_id=card_id)))
                status.update(state='projected',kind='spell',count=1,entity_ids=[base],
                    public_tick=public_tick)
                statuses.append(status);continue
            if spec.kind.value not in ('troop','hero','building') or not spec.hitpoints or not spec.count or spec.count>20:
                status.update(state='unsupported_card');statuses.append(status);continue
            form=request.get('form_code',0)
            if form not in (0,1,2):
                status.update(state='unsupported_form');statuses.append(status);continue
            name=spec.name+('_EV1' if form==1 else 'Hero' if form==2 else '')
            if name not in self.character_ids_by_name and form==0 and name.endswith('s'):
                name=name[:-1]
            gid=self.character_ids_by_name.get(name)
            if gid is None:
                status.update(state='unmapped_archetype');statuses.append(status);continue
            # A public deploy can precede the actual entity spawn. Keep the
            # prediction until the measured object list contains its unit.
            real_match=any(e.owner==owner and e.card_id==card_id and
                self.birth.get(e.entity_id,-1)>=request['observed_tick'] and
                math.hypot(e.position[0]-x,e.position[1]-y)<=5000
                for e in real_entities)
            if real_match:
                status.update(state='real_observed');statuses.append(status);continue
            count=int(spec.count)
            base=1_000_000_000+int.from_bytes(hashlib.blake2s(request_id.encode(),digest_size=8).digest(),'big')*32
            hp=round(spec.hitpoints*hp_scale)
            for index in range(count):
                angle=2*math.pi*index/count
                radius=0 if count==1 else 300 if count<=5 else 500
                px=max(0,min(18000,x+round(math.cos(angle)*radius)))
                py=max(0,min(32000,y+round(math.sin(angle)*radius)))
                ghosts.append(EntityStateV1(entity_id=base+index,owner=owner,card_id=card_id,
                    entity_kind='hero' if spec.kind.value=='hero' or form==2 else spec.kind.value,
                    position=(px,py),hitpoints=hp,max_hitpoints=hp,
                    native_data_global_id=gid,age_ms=(tick-request['observed_tick'])*50,
                    causal_group=CausalGroupRefV1(kind='deployment',handle='optimistic:'+request_id,
                        source_card_id=card_id)))
            status.update(state='projected',count=count,entity_ids=[e.entity_id for e in ghosts[-count:]])
            statuses.append(status)
        return ghosts,statuses

    def observation(self, frame, *, reserved_actions=(), allow_forms=False,
                    allow_abilities=False, extended_public_mapping=False,
                    block_play_cards=False,pending_deployments=()):
        tick, owner = frame['tick'], frame['owner']
        field = frame.get('battlefield')
        if field is None: raise ValueError(frame.get('mapping_error', '缺少原生战场数据'))
        deck = tuple(field['deck'])
        if len(deck)!=8 or len(set(deck))!=8: raise ValueError('本机卡组不完整')
        hand = sorted(frame['hand'],key=lambda c:c['hand_slot'])
        cycle = tuple(field['cycle'])
        runtime_forms={row['card_id']:row['form'] for row in field.get('deck_runtime',hand)}
        if set(deck) != set(cycle).union(c['card_id'] for c in hand) or len(cycle)!=4:
            raise ValueError('手牌与原生循环不构成完整卡组')
        if any(c['form'] not in (0,1,2,3) for c in hand): raise ValueError('原生形态超出模型词表')
        for card in deck: self.bundle.card_catalog.require_vocab_id(card)
        episode = f"tencent:{frame['generation']}:{frame['world']}"
        if self.episode_id is None:
            if tick != 0: raise ValueError('请在进入新对局之前启动，需要从 tick 0 绑定')
            self.episode_id = episode
        if self.episode_id != episode: raise ValueError('对局已变更')
        if self.last_tick is not None and tick<=self.last_tick:
            raise ValueError('实时观测 tick 没有向前推进')
        tick_gap=None if self.last_tick is None else tick-self.last_tick
        warmup_gap=self.last_tick is not None and self.last_tick<90 and tick<=90
        if tick_gap is not None and tick_gap>(90 if warmup_gap else 40):
            raise ValueError('实时观测间隔超过两秒')
        objects=field['objects']; ids=[o['id'] for o in objects]
        if len(set(ids))!=len(ids): raise ValueError('原生对象 ID 不唯一')
        entities=[]; events=[]; unknown=[]; current={}
        if extended_public_mapping:
            for event in frame.get('public_deployments',()):
                if event['world']!=frame['world'] or event['tick']>tick or event['owner'] not in (0,1):
                    raise ValueError('公开下牌事件与当前对局不匹配')
                for entity_id in event.get('created_entity_ids',()):
                    root=(event['action_id'],event['owner'],event['card_id'])
                    if entity_id in self.deployment_roots and self.deployment_roots[entity_id]!=root:
                        raise ValueError('实体公开部署来源冲突')
                    self.deployment_roots[entity_id]=root
        for obj in objects:
            oid, cid = obj['id'], obj['card_id']
            current[oid]=obj
            # Tencent LogicData's global ID is at +0x40, verified against actual
            # card roots and six stationary owner/tower objects.
            gid=obj.get('data_id_40')
            if extended_public_mapping and isinstance(gid,int) and gid<0:gid &= 0xffffffff
            name=obj.get('data_name')
            if gid in self.character_names and name and name!=self.character_names[gid]:
                raise ValueError('腾讯实体名称与 FL 词表不一致: '+str(gid))
            tower_identity=TOWER_OBJECTS.get((gid,name)) if cid==-1 else None
            if tower_identity is not None:
                tower_kind,tower_troop_id=tower_identity
                kind='king' if tower_kind=='king' else ('princess_left' if obj['x']<9000 else 'princess_right')
                expected_y=(3000 if kind=='king' else 6500)
                if obj['owner']==1: expected_y=32000-expected_y
                expected_x=9000 if kind=='king' else (3500 if kind=='princess_left' else 14500)
                if obj['x']!=expected_x or obj['y']!=expected_y or obj.get('max_hp',0)<=0:
                    raise ValueError('塔身份/位置/血量不匹配')
                self.towers[oid]=TowerStateV1(entity_id=oid,owner=obj['owner'],tower_kind=kind,
                    position=(obj['x'],obj['y']),hitpoints=max(0,obj['hp']),max_hitpoints=obj['max_hp'],
                    tower_troop_id=tower_troop_id,active=obj['hp']>0)
                continue
            if (tick==0 and cid==-1 and (obj['x'],obj['y']) in TOWER_START_POSITIONS
                    and obj.get('max_hp',0)>0 and not str(name).endswith('Projectile')):
                raise ValueError(f'未知塔兵身份：{name} / {gid}')
            if oid not in self.birth: self.birth[oid]=tick
            card=cid if cid>0 and cid in self.bundle.card_specs else None
            spec=self.bundle.card_specs.get(card)
            vocab=self.bundle.entity_archetype_catalog.runtime_global_vocab_id(gid or -1)
            metadata=self.bundle.entity_archetype_catalog.metadata_for_vocab_id(vocab)
            source=hero_source_card(cid)
            source_spec=self.bundle.card_specs.get(source)
            measured_hero=bool((extended_public_mapping or allow_abilities) and
                source_spec and source_spec.ability_ids and
                name and metadata.archetype_key=='form:'+name)
            if measured_hero:card=source
            kind='hero' if measured_hero else (metadata.child_kind if metadata.child_kind_known else (spec.kind.value if spec else 'unknown'))
            if vocab==1:unknown.append({'id':oid,'data_id':gid,'card_id':cid})
            previous=self.previous.get(oid)
            velocity=None if previous is None else ((obj['x']-previous['x'])/(tick_gap*0.05),(obj['y']-previous['y'])/(tick_gap*0.05))
            root=self.deployment_roots.get(oid)
            causal=CausalGroupRefV1(kind='deployment',handle=root[0],source_card_id=card) if root and root[1]==obj['owner'] and root[2]==card else None
            entities.append(EntityStateV1(causal_group=causal,entity_id=oid,owner=obj['owner'] if obj['owner'] in (0,1) else None,
                card_id=card,entity_kind=kind,position=(obj['x'],obj['y']),velocity=velocity,
                hitpoints=max(0,obj['hp']) if 'hp' in obj else None,max_hitpoints=obj.get('max_hp'),
                native_data_global_id=gid if gid and gid>0 else None,
                visible_target=obj.get('target_entity_id') if obj.get('target_entity_known') else None,
                movement_target=(obj['target_x'],obj['target_y']),age_ms=(tick-self.birth[oid])*50))
            if obj['owner']!=owner and card: self.revealed.add(card)
            if previous is None and tick>0:
                events.append(EventV1(tick=tick,event_type='spawn',owner=obj['owner'] if obj['owner'] in (0,1) else None,
                    entity_id=oid,card_id=card,position=(obj['x'],obj['y']),data={'source':'native-object-vector-delta'}))
        for event in frame.get('public_deployments',()):
            if event['world']!=frame['world'] or event['tick']>tick or event['owner'] not in (0,1):
                raise ValueError('公开下牌事件与当前对局不匹配')
            if event['card_id'] not in self.bundle.card_specs:
                unknown.append({'public_card_id':event['card_id']});continue
            if event['owner']!=owner:self.revealed.add(event['card_id'])
            events.append(EventV1(tick=event['tick'],event_type='action_executed',owner=event['owner'],
                card_id=event['card_id'],position=(event['x'],event['y']),data={
                    'action_id':event['action_id'],'effective_cost':event['effective_cost'],
                    'form_code':event['form_code'],'source':event['source']}))
        optimistic,optimistic_status=self._optimistic_entities(frame,pending_deployments,entities)
        if set(e.entity_id for e in entities).intersection(e.entity_id for e in optimistic):
            raise ValueError('预测实体 ID 与原生实体冲突')
        entities.extend(optimistic)
        if len(self.towers)!=6:raise ValueError('缺少六座已识别的塔')
        removed_towers=[]
        # Stock tower objects leave this live vector on destruction. A lethal
        # hit can remove the object between two 250 ms captures; record that
        # measured removal as derived destruction, rather than stale HP.
        for oid,tower in self.towers.items():
            if oid not in current and tower.hitpoints>0:
                removed_towers.append(oid)
                self.towers[oid]=replace(tower,hitpoints=0,active=False)
        towers=tuple(self.towers.values())
        active_footprints=[OccupiedFootprintV1(t.position[0],t.position[1],4 if t.tower_kind=='king' else 3,
            4 if t.tower_kind=='king' else 3,f'tencent-tower:{t.entity_id}') for t in towers if t.hitpoints>0]
        building_footprints=[]; unverified_buildings=[]
        for entity in entities:
            if entity.entity_kind=='building':
                spec=self.bundle.card_specs.get(entity.card_id)
                fp=native_building_footprint(spec) if spec else None
                if fp is None:unverified_buildings.append(entity.entity_id)
                else:building_footprints.append(OccupiedFootprintV1(*entity.position,fp.width_tiles,fp.height_tiles,f'tencent-building:{entity.entity_id}'))
        destroyed=[t.tower_kind.split('_')[-1] for t in towers if t.owner!=owner and t.tower_kind!='king' and t.hitpoints<=0]
        placements={}; allowed=[]
        reserved_cost=0
        for action in reserved_actions:
            if action.get('kind')=='activate_ability':
                reserved_cost+=self.bundle.ability_specs[action['ability_id']].elixir_cost
            else:
                reserved_cost+=self.bundle.card_specs[action['card_id']].elixir_cost
        effective_elixir=max(0,frame['mana_raw']/10000-reserved_cost)
        ability_states=[];ability_sources=[]
        if allow_abilities:
            entity_by_id={entity.entity_id:entity for entity in entities}
            seen_sources=set()
            for raw in frame.get('abilities',()):
                source=raw.get('source_entity')
                card_id=hero_source_card(raw.get('card_id')) or raw.get('card_id')
                entity=entity_by_id.get(source)
                if (type(source) is not int or source in seen_sources or entity is None or
                    entity.owner!=owner or entity.card_id!=card_id or card_id not in deck):
                    continue
                spec=self.bundle.card_specs[card_id]
                if len(spec.ability_ids)!=1 or (spec.kind.value!='hero' and runtime_forms.get(card_id)!=2):
                    continue
                ability_id=spec.ability_ids[0]
                ability=self.bundle.ability_specs[ability_id]
                slot=raw.get('controller_slot')
                button=raw.get('button_state')
                charges=raw.get('charges_raw')
                cooldown=raw.get('remaining_cooldown_ms')
                if (slot not in (1,2) or type(button) is not int or type(charges) is not int or
                    type(cooldown) is not int or cooldown<0 or charges< -1):
                    continue
                seen_sources.add(source)
                ready=button in (2,4) and cooldown==0 and charges!=0
                phase=(AbilityPhase.READY if ready else
                       AbilityPhase.EXHAUSTED if charges==0 else
                       AbilityPhase.COOLDOWN if cooldown>0 else AbilityPhase.UNAVAILABLE)
                ability_states.append(AbilityRuntimeStateV1(
                    ability_id=ability_id,source_entity=source,phase=phase,
                    elixir_cost=ability.elixir_cost,
                    cooldown_ms=raw.get('configured_cooldown_ms'),
                    remaining_cooldown_ms=cooldown,
                    charges=None if charges==-1 else charges,
                    available=ready,
                    attributes={'controller_slot':slot,'source_card_id':card_id,
                                'button_state':button,'remaining_charges_raw':charges},
                    provenance=SemanticProvenanceV1(field_evidence={
                        name:(SemanticEvidenceLevel.STATIC_DECLARED if name=='elixir_cost' else
                              SemanticEvidenceLevel.NATIVE_DERIVED if name in (
                                  'source_entity','phase','remaining_cooldown_ms','available') or
                                  name=='cooldown_ms' and raw.get('configured_cooldown_ms') is not None or
                                  name=='charges' and charges!=-1 else SemanticEvidenceLevel.UNKNOWN)
                        for name in ABILITY_RUNTIME_STATE_FIELDS},observed_tick=tick)))
                if (ready and ability.elixir_cost<=effective_elixir and
                    not any(a.get('kind')=='activate_ability' and a.get('source_entity')==source
                            for a in reserved_actions)):
                    ability_sources.append(source)
        for card in hand:
            spec=self.bundle.card_specs[card['card_id']]
            artifact=card_placement_mask(spec,owner,ruleset_id=RULESET,active_tower_footprints=active_footprints,
                occupied_building_footprints=building_footprints,destroyed_enemy_princess_lanes=destroyed)
            rows=artifact.rows
            if spec.kind.value=='building' and unverified_buildings:rows=tuple((False,)*18 for _ in range(32))
            if card['form']!=0 and not allow_forms:rows=tuple((False,)*18 for _ in range(32))
            if any(a.get('kind')!='activate_ability' and a['hand_slot']==card['hand_slot'] for a in reserved_actions):
                rows=tuple((False,)*18 for _ in range(32))
            if block_play_cards:rows=tuple((False,)*18 for _ in range(32))
            entry=artifact.to_dict();entry.update(shape=[32,18],row_major=rows,effective_cost=card['cost'],form_code=card['form'])
            placements[str(card['hand_slot'])]=entry
            allowed.append(card['cost']<=effective_elixir and any(any(r) for r in rows))
        crowns={p:sum(t.owner!=p and t.tower_kind!='king' and t.hitpoints<=0 for t in towers) for p in (0,1)}
        players=tuple(PlayerStateV1(owner=p,crowns=crowns[p],tower_ids=tuple(t.entity_id for t in towers if t.owner==p),
            **({'elixir_exact':min(10,frame['mana_raw']/10000),'elixir_visible':min(10,frame['mana_raw']/10000),
                'hand':tuple(c['card_id'] for c in hand),'deck':deck,'cycle':cycle,'next_card':cycle[0],
                'private_state_visible':True,'metadata':{'hand_slot_by_card':{str(c['card_id']):c['hand_slot'] for c in hand},
                'hand_runtime_by_slot':{str(c['hand_slot']):{'form_code':c['form']} for c in hand}},
                'ability_runtime_states':tuple(ability_states),
                'runtime_provenance':SemanticProvenanceV1(field_evidence={
                    name:(SemanticEvidenceLevel.NATIVE_DERIVED if name=='ability_runtime_states' and ability_states
                          else SemanticEvidenceLevel.UNKNOWN) for name in PLAYER_RUNTIME_SEMANTIC_FIELDS},
                    observed_tick=tick)}
              if p==owner else {'revealed_cards':tuple(sorted(self.revealed))})) for p in (0,1))
        elapsed=max(0,(tick if extended_public_mapping else tick-90)*50)
        remaining=max(0,300000-elapsed) if extended_public_mapping else max(0,(180000 if elapsed<180000 else 300000)-elapsed)
        tower_runtime_unknown=any(t.tower_troop_id in (159000002,159000004) for t in towers)
        observation=ObservationV1(tier='fair',owner=owner,tick=tick,episode_id=episode,phase='regulation' if elapsed<180000 else 'overtime',
            time=TimeStateV1(elapsed_ms=elapsed,remaining_ms=remaining,capture_time_ms=frame['captured_at_ms'],
                elixir_multiplier=1. if elapsed<120000 else (2. if elapsed<240000 else 3.)),players=players,towers=towers,entities=tuple(entities),events=tuple(events),
            action_mask=ActionMaskV1(kinds={'wait':True,'play_card':not block_play_cards,'activate_ability':bool(ability_sources)},hand_slots=tuple(allowed),
                placement_masks=placements,ability_sources=tuple(ability_sources),
                reasons={'effective_elixir':effective_elixir,'reserved_elixir':reserved_cost,
                    'min_card_gap_active':block_play_cards}),
            metadata={'source':'live-tencent-experimental','complete':False,'limitations':
                [item for item in LIMITATIONS if item!='ability_execution_disabled' or not allow_abilities]+
                (['tower_troop_runtime_unknown'] if tower_runtime_unknown else [])+
                ([] if extended_public_mapping else ['opponent_elixir_interval_0_to_10','hero_entity_card_identity_unmapped']),
                'unknown_archetypes':unknown,'tower_destruction_from_vector_removal':removed_towers,
                'optimistic_entities':optimistic_status,
                'observed_tick_gap':tick_gap,
                'urgent_opponent_deploy':bool(frame.get('urgent_opponent_deploy')),
                'mapping_version':'tencent-public-observation.v4' if extended_public_mapping else 'tencent-basic-observation.v2'})
        self.previous=current;self.last_tick=tick
        return observation

class LivePolicy:
    def __init__(self,loaded):
        self.loaded=loaded;self.mapping=None;self.session=None
    def end(self):
        if self.session:self.session.end_episode()
        self.session=None;self.mapping=None
    def handle(self,request):
        if request['op']=='live_start':self.end();self.mapping=LiveMapping()
        if self.mapping is None:raise ValueError('请先启动实时试验')
        observation=self.mapping.observation(request['snapshot'],reserved_actions=request.get('reserved_actions',()),
            pending_deployments=request.get('pending_deployments',()),
            allow_forms=request.get('allow_forms',False),allow_abilities=request.get('allow_abilities',False),
            extended_public_mapping=request.get('extended_public_mapping',False),
            block_play_cards=request.get('block_play_cards',False))
        if self.session is None:
            b=self.mapping.bundle;deck=next(p.deck for p in observation.players if p.owner==observation.owner)
            extended=request.get('extended_public_mapping',False)
            tensorizer=(LiveTensorizer if extended else UniversalObservationTensorizerV4)(actor_owner=observation.owner,card_catalog=b.card_catalog,
                ability_catalog=b.ability_catalog,entity_archetype_catalog=b.entity_archetype_catalog,effect_catalog=b.effect_catalog,
                card_specs=b.card_specs,deck=deck,deck_roles={c:(any(h['card_id']==c and h['form'] in (2,3) for h in
                    request['snapshot']['battlefield'].get('deck_runtime',request['snapshot']['hand'])),
                    any(h['card_id']==c and h['form']==1 for h in request['snapshot']['battlefield'].get('deck_runtime',request['snapshot']['hand']))) for c in deck},tracker=PublicLiveTracker(observation.owner) if extended else None,
                reject_unknown_public_semantics=False)
            self.session=PolicySessionV4(self.loaded.model,tensorizer,device='cpu',sample=False)
            self.session.start_episode(observation, initial_elixir={})
        terminal=any(t.tower_kind=='king' and t.hitpoints<=0 for t in observation.towers)
        if observation.tick<90 or terminal:
            self.session.tensorizer.tensorize(observation,validate=True)
            actions=[];latency=0
        else:
            decision=self.session.decide(observation)
            actions=[a.to_dict() for a in decision.decoded.actions];latency=decision.inference_ms
        serialized=observation.to_dict()
        return {'ok':True,'tick':observation.tick,'actions':actions,'inference_ms':latency,
            'terminal':terminal,
            'source':'live-tencent-experimental','complete':False,'limitations':serialized['metadata']['limitations'],
            'mapping':{'hand':[{k:c[k] for k in ('card_id','hand_slot','cost','form')} for c in request['snapshot']['hand']],'towers':len(observation.towers),'entities':len(observation.entities),'events':len(observation.events),
                'unknown_archetypes':serialized['metadata']['unknown_archetypes']},'observation':serialized}
