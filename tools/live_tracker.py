"""Public bounds and revealed cycle states; never reads an opponent deck.

Uses FirstLight's cycle representation and production regeneration schedule.
Initial enemy elixir is the conservative [0,10] bound, not a claimed exact value.
Unobserved abilities can only reduce it, so the enemy lower bound stays zero.
"""
from dataclasses import replace
from native_runner.training.tracking import (AvailabilityIndex, PublicCardState,
    PublicAbilityState, _generated_elixir)
from native_runner.training.v4.tensorizer import UniversalObservationTensorizerV4

class PublicLiveTracker:
    def __init__(self, actor_owner):
        self.actor_owner=actor_owner;self.ready=False
    def reset(self, *, tick, initial_elixir):
        self.tick=tick;self.bounds={0:(0.,10.),1:(0.,10.)}
        own=float(initial_elixir[self.actor_owner]);self.bounds[self.actor_owner]=(own,own)
        self.states={0:{},1:{}};self.seen=set();self.ready=True
    def end_episode(self):self.ready=False
    def advance(self,tick):
        generated=_generated_elixir(self.tick,tick)
        for p in (0,1):
            low,high=self.bounds[p]
            self.bounds[p]=(0. if p!=self.actor_owner else min(10.,low+generated),min(10.,high+generated))
        self.tick=tick
    def update(self, observation):
        if not self.ready or observation.owner!=self.actor_owner or observation.tick<self.tick:
            raise ValueError('公开状态跟踪身份或时序不一致')
        for event in sorted(observation.events,key=lambda e:e.tick):
            if event.event_type!='action_executed':continue
            token=event.data.get('action_id')
            if not token or token in self.seen:continue
            self.seen.add(token)
            if event.tick<self.tick:
                self.bounds[1-self.actor_owner]=(0.,10.)
                continue
            self.advance(event.tick)
            cost=float(event.data['effective_cost'])
            if not 0<=cost<=10:raise ValueError('公开命令圣水费用非法')
            p=event.owner
            if p not in (0,1) or event.card_id is None:raise ValueError('公开下牌身份缺失')
            for card,state in tuple(self.states[p].items()):
                if state.availability==AvailabilityIndex.CYCLING:
                    distance=max(0,state.cycle_distance-1)
                    self.states[p][card]=replace(state,cycle_distance=distance,
                        availability=AvailabilityIndex.CYCLING if distance else AvailabilityIndex.AVAILABLE)
            self.states[p][event.card_id]=PublicCardState(AvailabilityIndex.CYCLING,4,True)
            low,high=self.bounds[p];self.bounds[p]=(max(0.,low-cost),max(0.,high-cost))
        self.advance(observation.tick)
    def reconcile_owner_private_state(self, observation):
        actor=next(p for p in observation.players if p.owner==self.actor_owner)
        if actor.elixir_exact is None:raise ValueError('本机圣水不是原生实测')
        value=float(actor.elixir_exact);self.bounds[self.actor_owner]=(value,value)
    def card_state(self,owner,card_id):return self.states[owner][card_id]
    def revealed_cards(self,owner):return tuple(self.states[owner])
    def elixir_interval(self,owner):return self.bounds[owner]
    def ability_state(self,*args,**kwargs):
        # Compatibility only. LiveTensorizer below leaves all unknown enemy
        # ability features zero instead of treating these fields as measured.
        return PublicAbilityState(None,0,0,False)

class LiveTensorizer(UniversalObservationTensorizerV4):
    def start_episode(self, observation, *, initial_elixir=None):
        tracker=self.tracker;self.tracker=None
        try:super().start_episode(observation,initial_elixir={})
        finally:self.tracker=tracker
        actor=next(p for p in observation.players if p.owner==self.actor_owner)
        tracker.reset(tick=observation.tick,initial_elixir={self.actor_owner:actor.elixir_exact})
    def _card_runtime_features(self, observation, *, owner, card_id):
        features=super()._card_runtime_features(observation,owner=owner,card_id=card_id)
        if owner!=self.actor_owner:features[9:15]=[0.]*6
        return features
