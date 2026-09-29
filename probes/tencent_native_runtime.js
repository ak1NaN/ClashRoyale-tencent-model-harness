'use strict';
// Versioned Tencent native runtime. Native calls run after GameState::step on its own thread.
(function () {
  const mod = Process.getModuleByName('libg.so');
  const O = globalThis.__tencentOffsets;
  if (!O) throw new Error('Missing native offset profile');
  const entries = [[O.step,0xa9bd7bfd],[O.clock,0x3945f008],
    [O.stock_submit,0xf9400028],[O.command_ctor,0xa9bd7bfd],[O.parse_json,0xb4000480],[O.selection,0xb40000a0],[O.alloc,0xd10243ff]];
  if (Process.arch !== 'arm64') throw new Error('Native runtime requires ARM64');
  for (const [off,word] of entries) {
    if ((globalThis.__tencentEntryWords ? globalThis.__tencentEntryWords[off] : mod.base.add(off).readU32())!==word) throw new Error('Unsupported native runtime entry '+off.toString(16));
  }
  function bridge(resultRegister,targetRegister,signature) {
    const page=Memory.alloc(Process.pageSize);
    [0xa9bf7bfd,0x910003fd,0xaa0003e8|(resultRegister<<16),
      0xd63f0000|(targetRegister<<5),0xa8c17bfd,0xd65f03c0]
      .forEach((word,index)=>page.add(4*index).writeU32(word>>>0));
    Memory.protect(page,Process.pageSize,'r-x');
    return {page:page,call:new NativeFunction(page,'void',signature)};
  }
  const selectionBridge=bridge(3,4,['pointer','pointer','int','pointer','pointer']);
  const commandBridge=bridge(2,3,['pointer','pointer','pointer','pointer']);
  const parseJson=new NativeFunction(mod.base.add(O.parse_json),'pointer',['pointer','pointer','int']);
  const uiContext=new NativeFunction(mod.base.add(O.ui_context),'pointer',[]);
  const submit=new NativeFunction(mod.base.add(O.stock_submit),'bool',['pointer','pointer','int']);
  let controller=null, state=null, snapshot=null, lastTick=-1, generation=0, executing=false, lastPublished=null;
  let enabled=false, pending=[], lastReceipt=null;
  const hits={clock:0,step:0,update:0,tick:0,mismatch:0,invalidOwn:0};
  const getController=new NativeFunction(mod.base.add(O.get_controller),'pointer',[]);
  const receipts=new Map();
  const inflight=new Map();
  let publicDeploys=[];
  const publicSeen=new Set();
  function emit(value) {value.t_ms=Date.now();send(value);}
  function identity() {
    const object=mod.base.add(O.identity_holder).readPointer().readPointer();
    return [object.add(0x20).readU32(),object.add(0x24).readU32()];
  }
  function readOwn(manager) {
    const world=manager.add(0xa8).readPointer();
    if (world.isNull() || world.add(0x60).readS32()!==2) return null;
    const [hi,lo]=identity();
    for (let owner=0;owner<2;owner++) {
      const id=world.add(0x30+8*owner).readPointer();
      if (id.isNull() || id.readU32()!==hi || id.add(4).readU32()!==lo) continue;
      const player=world.add(0xe0+8*owner).readPointer(), deck=world.add(0x88+8*owner).readPointer();
      if (player.isNull() || deck.isNull() || player.add(0x224).readS32()!==4 || deck.add(0x2c).readS32()!==8) return null;
      const slots=deck.add(0x20).readPointer(), hand=player.add(0x218).readPointer(), cards=[];
      for (let index=0;index<4;index++) {
        const slot=hand.add(4*index).readS32();
        if (slot<0 || slot>7) return null;
        const item=slots.add(8*slot).readPointer(), root=item.add(0x10).readPointer(), output=Memory.alloc(24);
        selectionBridge.call(item,player,0,output,mod.base.add(O.selection));
        const packed=output.add(16).readU32(), secondary=output.add(8).readPointer();
        if (!output.readPointer().equals(root) || ((packed>>>22)&63)-1!==slot) throw new Error('Card selection ABI changed');
        const cardId=root.add(0x40).readS32();
        cards.push({hand_slot:index,deck_slot:slot,card_id:cardId,packed:packed,cost:packed>>>28,form:packed&15,
          command_card_id:(packed&15)===0&&!secondary.isNull()?secondary.add(0x40).readS32():cardId});
      }
      return {world:world,owner:owner,hi:hi,lo:lo,player:player,cards:cards,mana_raw:player.add(0x300).readS32(),abilities:readAbilities(player,owner)};
    }
    return null;
  }
  function readable(p,size) {
    if (p.isNull()) return false;
    const address=p.and(ptr('0x00ffffffffffffff'));
    const range=Process.findRangeByAddress(address);
    return range!==null && range.protection.includes('r') && address.add(size).compare(range.base.add(range.size))<=0;
  }
  function readAbilities(player,owner) {
    const result=[];
    for(const [slot,offset] of [[1,0x3a8],[2,0x3b0]]) {
      const ctl=player.add(offset).readPointer();
      if(!readable(ctl,0xa8)||!ctl.readPointer().equals(mod.base.add(O.ability_controller_vtable)))continue;
      const vector=ctl.add(0xa0).readPointer();
      if(!readable(vector,0x10))continue;
      const count=vector.add(0x0c).readS32(),members=vector.readPointer();
      if(count<0||count>2||count&&!readable(members,count*8))continue;
      for(let i=0;i<count;i++){
        const member=members.add(i*8).readPointer();
        if(!readable(member,0xb0)||member.add(0x78).readS32()!==owner)continue;
        result.push({controller_slot:slot,source_entity:member.add(8).readU32(),card_id:member.add(0xac).readS32(),
          button_state:ctl.add(0x98).readS32(),charges_raw:ctl.add(0x80).readS32(),
          remaining_cooldown_ms:ctl.add(0x78).readS32(),configured_cooldown_ms:ctl.add(0x7c).readS32()});
      }
    }
    return result;
  }
  function hasOwnEntity(own,source) {
    const manager=own.player.add(0x10).readPointer();
    if(!readable(manager,0x18))throw new Error('Own object manager unavailable');
    const count=manager.add(0x14).readS32(),capacity=manager.add(0x10).readS32();
    if(count<0||count>512||capacity<count||capacity>4096)throw new Error('Own object vector bounds');
    const vector=manager.add(8).readPointer();
    if(count&&!readable(vector,count*8))throw new Error('Own object vector unavailable');
    for(let i=0;i<count;i++){
      const entity=vector.add(i*8).readPointer();
      if(readable(entity,0x80)&&entity.add(8).readU32()===source&&entity.add(0x78).readS32()===own.owner)return true;
    }
    return false;
  }
  function battlefield(own) {
    const deck=own.world.add(0x88+8*own.owner).readPointer(), slots=deck.add(0x20).readPointer();
    const ids=[],deckRuntime=[];
    for(let i=0;i<8;i++) {
      const item=slots.add(i*8).readPointer(),root=item.add(0x10).readPointer(),out=Memory.alloc(24);
      selectionBridge.call(item,own.player,0,out,mod.base.add(O.selection));
      const packed=out.add(16).readU32(),cardId=root.add(0x40).readS32();
      if(!out.readPointer().equals(root)||((packed>>>22)&63)-1!==i)throw new Error('Deck selection ABI changed');
      ids.push(cardId);deckRuntime.push({card_id:cardId,deck_slot:i,form:packed&15,cost:packed>>>28});
    }
    const cycleCount=own.player.add(0x234).readS32(), cycleVector=own.player.add(0x228).readPointer(), cycle=[];
    if(cycleCount!==4) throw new Error('Native cycle count is not four: '+cycleCount);
    for(let i=0;i<cycleCount;i++){const slot=cycleVector.add(i*4).readS32();if(slot<0||slot>7)throw new Error('Invalid cycle slot');cycle.push(ids[slot]);}
    const objectsManager=own.player.add(0x10).readPointer(), count=objectsManager.add(0x14).readS32(), capacity=objectsManager.add(0x10).readS32();
    if(count<0||count>512||capacity<count||capacity>4096)throw new Error('Battlefield vector bounds');
    const vector=objectsManager.add(8).readPointer(), objects=[];
    const objectIds=new Map();
    for(let i=0;i<count;i++){const o=vector.add(i*8).readPointer();if(!o.isNull())objectIds.set(o.toString(),o.add(8).readU32());}
    for(let i=0;i<count;i++){
      const o=vector.add(i*8).readPointer();if(o.isNull())continue;
      const item={id:o.add(8).readU32(),owner:o.add(0x78).readS32(),x:o.add(0x7c).readS32(),y:o.add(0x80).readS32(),
        target_x:o.add(0x84).readS32(),target_y:o.add(0x88).readS32(),card_id:o.add(0xac).readS32(),slot:i};
      try{
        const data=o.add(0x48).readPointer();item.data_id=data.isNull()?null:data.add(0x10).readS32();item.data_id_40=data.isNull()?null:data.add(0x40).readU32();
        if(!data.isNull()){
          const length=data.add(0x28).readS32();
          if(length>0&&length<=80){const chars=length<=7?data.add(0x30):data.add(0x30).readPointer();item.data_name=chars.readUtf8String(length);}
        }
      }catch(e){item.data_id=null;}
      const n=o.add(0x24).readS32(),cap=o.add(0x20).readS32();
      if(n>=3&&n<=32&&cap>=n&&cap<=128){const hp=o.add(0x18).readPointer().add(16).readPointer();if(!hp.isNull()&&hp.add(8).readPointer().equals(o)){item.hp=hp.add(0x10).readS32();item.max_hp=hp.add(0x14).readS32();}}
      if(n>=1&&n<=32&&cap>=n&&cap<=128){
        const movement=o.add(0x18).readPointer().readPointer();
        if(!movement.isNull()&&movement.add(8).readPointer().equals(o)){
          const target=movement.add(0x10).readPointer();
          if(target.isNull()){item.target_entity_known=true;item.target_entity_id=null;}
          else if(objectIds.has(target.toString())){item.target_entity_known=true;item.target_entity_id=objectIds.get(target.toString());}
        }
      }
      objects.push(item);
    }
    return {deck:ids,deck_runtime:deckRuntime,cycle:cycle,objects:objects,mode:'experimental-measured-battlefield',unknown_runtime_semantics:true};
  }
  function releaseJson(value) {new NativeFunction(value.readPointer().add(8).readPointer(),'void',['pointer'])(value);}
  function releaseCommand(value) {new NativeFunction(value.readPointer().add(16).readPointer(),'void',['pointer'])(value);}
  function createCommand(manager,own,card,tick,x,y) {
    // Keep 64-bit identity exact even if an account exceeds JavaScript's integer range.
    const id=(BigInt(own.hi)*4294967296n+BigInt(own.lo)).toString();
    const body='{"ct":153,"c":{"t":'+tick+',"t2":'+(tick+1)+',"idHi":'+own.hi+',"idLo":'+id+
      ',"px":'+x+',"py":'+y+',"sid":-1,"sel":{"os":'+card.command_card_id+',"pd":'+card.packed+'}}}';
    const text=Memory.allocUtf8String(body), str=Memory.alloc(16), result=Memory.alloc(8);
    str.writeS32(-1);str.add(4).writeS32(body.length);str.add(8).writePointer(text);result.writePointer(ptr(0));
    let context=manager.add(0x210).readPointer();
    if (context.isNull()) context=uiContext();
    if (context.isNull() || Process.findRangeByAddress(context.and(ptr('0x00ffffffffffffff')))===null) throw new Error('Command context is unavailable: '+context);
    const json=parseJson(str,ptr(0),100);
    if (json.isNull()) throw new Error('Native JSON rejected');
    try {commandBridge.call(json,context,result,mod.base.add(O.command_ctor));} finally {releaseJson(json);}
    const command=result.readPointer();
    if (command.isNull()) throw new Error('Native command rejected');
    try {
      if (!command.readPointer().equals(mod.base.add(O.deploy_vtable)) || command.add(0x14).readU32()!==own.hi ||
        command.add(0x18).readU32()!==own.lo || command.add(0x28).readS32()!==x || command.add(0x2c).readS32()!==y ||
        command.add(0x48).readU32()!==card.packed) throw new Error('Constructed deployment mismatch');
    } catch (error) {releaseCommand(command);throw error;}
    return {owner:result,command:command};
  }
  function createAbilityCommand(manager,own,source,tick) {
    const id=(BigInt(own.hi)*4294967296n+BigInt(own.lo)).toString();
    const body='{"ct":189,"c":{"t":'+tick+',"t2":'+(tick+1)+',"idHi":'+own.hi+',"idLo":'+id+',"cgid":'+source+'}}';
    const chars=Memory.allocUtf8String(body),str=Memory.alloc(16),result=Memory.alloc(8);
    str.writeS32(-1);str.add(4).writeS32(body.length);str.add(8).writePointer(chars);result.writePointer(ptr(0));
    const context=manager.add(0x210).readPointer();
    if(!readable(context,8))throw new Error('Ability command context unavailable');
    const json=parseJson(str,ptr(0),100);
    if(json.isNull())throw new Error('Native ability JSON rejected');
    try{commandBridge.call(json,context,result,mod.base.add(O.command_ctor));}finally{releaseJson(json);}
    const command=result.readPointer();
    if(command.isNull())throw new Error('Native ability command rejected');
    if(!command.readPointer().equals(mod.base.add(O.ability_vtable))||command.add(0x14).readU32()!==own.hi||
       command.add(0x18).readU32()!==own.lo||command.add(0x28).readS32()!==source){
      releaseCommand(command);throw new Error('Constructed ability identity mismatch');
    }
    return {owner:result,command:command};
  }
  function receipt(value) {
    lastReceipt=value;receipts.set(value.id,value);
    if (receipts.size>100) receipts.delete(receipts.keys().next().value);
    emit(Object.assign({event:'native_deploy_receipt'},value));
  }
  function publicObjectIds(world,owner) {
    const player=world.add(0xe0+8*owner).readPointer();
    const manager=player.add(0x10).readPointer(),count=manager.add(0x14).readS32(),capacity=manager.add(0x10).readS32();
    if(count<0||count>512||capacity<count||capacity>4096)throw new Error('Public object bounds');
    const vector=manager.add(8).readPointer(),ids=new Set();
    for(let i=0;i<count;i++){const o=vector.add(i*8).readPointer();if(!o.isNull())ids.add(o.add(8).readU32());}
    return ids;
  }
  // Read only successful phase-2 stock deployments, for both visible players.
  // Phase-1 only reserves elixir; the phase-2 branch changes the hand and creates
  // battle objects. Return zero is success (f6c4b8); rejected commands are ignored.
  Interceptor.attach(mod.base.add(O.alloc),{
    onEnter(args){
      this.publicDeployment=null;this.beforeObjectIds=null;this.publicWorld=null;
      if ((args[2].toInt32()&2)===0) return;
      try {
        const command=args[0],manager=args[1],world=manager.add(0xa8).readPointer();
        if(world.isNull()||world.add(0x60).readS32()!==2)return;
        const hi=command.add(0x14).readU32(),lo=command.add(0x18).readU32();
        let owner=-1;
        for(let p=0;p<2;p++){
          const identity=world.add(0x30+8*p).readPointer();
          if(!identity.isNull()&&identity.readU32()===hi&&identity.add(4).readU32()===lo)owner=p;
        }
        if(owner<0)return;
        const root=command.add(0x38).readPointer(),packed=command.add(0x48).readU32();
        if(root.isNull())return;
        const tick=manager.add(0x60).readS32();
        this.beforeObjectIds=publicObjectIds(world,owner);this.publicWorld=world;
        this.publicDeployment={action_id:world.toString()+':'+command.toString()+':'+tick,
          world:world.toString(),tick:tick,owner:owner,card_id:root.add(0x40).readS32(),
          x:command.add(0x28).readS32(),y:command.add(0x2c).readS32(),
          effective_cost:packed>>>28,form_code:packed&15,source:'stock-deploy-phase2-success'};
      }catch(error){emit({event:'native_public_event_error',message:String(error)});}
    },
    onLeave(result){
      const event=this.publicDeployment;
      if(!event||result.toInt32()!==0||publicSeen.has(event.action_id))return;
      try {const after=publicObjectIds(this.publicWorld,event.owner);event.created_entity_ids=[...after].filter(id=>!this.beforeObjectIds.has(id));}
      catch(error){emit({event:'native_public_group_error',message:String(error)});}
      publicSeen.add(event.action_id);
      if(publicSeen.size>1024)publicSeen.delete(publicSeen.values().next().value);
      publicDeploys.push(event);
      if(publicDeploys.length>256)publicDeploys.shift();
      emit({event:'native_public_deployment',deployment:event});
    }
  });
  // Observe the normal deploy validator; never use the return value as server ack.
  Interceptor.attach(mod.base.add(O.get_mana),{
    onEnter(args){
      if (!inflight.size || !this.returnAddress.equals(mod.base.add(O.get_mana_return))) return;
      try {
        const record=this.context.x19, card=record.add(0x38).readPointer();
        for(const submitted of inflight.values()) {
        if (record.add(0x14).readU32()===submitted.hi && record.add(0x18).readU32()===submitted.lo &&
            record.add(0x28).readS32()===submitted.x && record.add(0x2c).readS32()===submitted.y &&
            card.add(0x40).readS32()===submitted.card_id) {
          submitted.validated=true;
          submitted.application_mana_before=args[0].add(0x300).readS32();
          emit({event:'native_deploy_validation',id:submitted.id,card_id:submitted.card_id,
            x:submitted.x,y:submitted.y,thread_id:Process.getCurrentThreadId(),server_ack:false});
        }
        }
      } catch(error){emit({event:'native_runtime_error',message:String(error)});}
    }
  });
  Interceptor.attach(mod.base.add(O.clock),{onEnter(args){hits.clock++;controller=args[0];}});
  const stepHandlers={
    onEnter(args){hits.step++;this.manager=args[0];this.callerController=this.context.x19;},
    onLeave() {
      if (executing) return;
      let processing=null, tick=null;
      try {
        const manager=this.manager;
        tick=manager.add(0x60).readS32();
        if (this.callerController && Process.findRangeByAddress(this.callerController)!==null && this.callerController.add(0x90).readPointer().equals(manager)) controller=this.callerController;
        if (controller===null || !controller.add(0x90).readPointer().equals(manager)) {
          const resolved=getController();
          if (!resolved.isNull() && resolved.add(0x90).readPointer().equals(manager)) controller=resolved;
          else {hits.mismatch++;return;}
        }
        const own=readOwn(manager);
        if (own===null) {hits.invalidOwn++;return;}
        if (state===null || !state.equals(manager) || tick<lastTick || snapshot&&snapshot.world!==own.world.toString()) {
          state=manager;generation++;lastTick=-1;
          publicDeploys=publicDeploys.filter(e=>e.world===own.world.toString()&&e.tick<=tick);publicSeen.clear();
          for(const request of pending) receipt({id:request.id,ok:false,error:'Battle changed'});
          pending=[];
          for(const request of inflight.values()) receipt({id:request.id,ok:false,error:'Battle changed before application'});
          inflight.clear();
        }
        lastTick=tick;
        for(const submitted of inflight.values()) {
        if(submitted.kind==='activate_ability'){
          const current=own.abilities.find(a=>a.source_entity===submitted.source_entity&&a.controller_slot===submitted.controller_slot);
          if(submitted.world===own.world.toString()&&current&&current.charges_raw<submitted.charges_before){
            receipt({id:submitted.id,kind:submitted.kind,ok:true,stage:'battle_applied',tick:tick,world:submitted.world,
              source_entity:submitted.source_entity,ability_id:submitted.ability_id,card_id:submitted.card_id,
              charges_before:submitted.charges_before,charges_after:current.charges_raw,server_ack:false});
            inflight.delete(submitted.id);
          }else if(submitted.world===own.world.toString()&&!hasOwnEntity(own,submitted.source_entity)){
            receipt({id:submitted.id,kind:submitted.kind,ok:false,stage:'source_despawned',
              error:'source_despawned_before_application',tick:tick,world:submitted.world,
              source_entity:submitted.source_entity,ability_id:submitted.ability_id,
              card_id:submitted.card_id,server_ack:false});
            inflight.delete(submitted.id);
          }else if(tick-submitted.tick>80){
            receipt({id:submitted.id,kind:submitted.kind,ok:false,error:'Ability application timed out'});
            inflight.delete(submitted.id);
          }
        }else if (submitted.validated && submitted.world===own.world.toString() && own.cards[submitted.hand_slot].card_id!==submitted.card_id) {
          receipt({id:submitted.id,ok:true,stage:'battle_applied',card_id:submitted.card_id,tick:tick,world:submitted.world,
            x:submitted.x,y:submitted.y,mana_before:submitted.application_mana_before,mana_after:own.mana_raw,
            hand_changed:true,server_ack:false,thread_id:Process.getCurrentThreadId()});
          inflight.delete(submitted.id);
        }
        }
        snapshot={schema:'tencent-native-partial.v1',generation:generation,manager:manager.toString(),world:own.world.toString(),
          tick:tick,battle_finished:own.world.add(0x198).readS32()>0,native_end_marker:own.world.add(0x198).readS32(),thread_id:Process.getCurrentThreadId(),owner:own.owner,mana_raw:own.mana_raw,hand:own.cards,abilities:own.abilities,captured_at_ms:Date.now(),
          coverage:['tick','own_hand','own_elixir','forms','own_abilities'],complete:false};
        const frameKey=generation+':'+tick;
        const readyDeploys=publicDeploys.filter(e=>e.world===own.world.toString()&&e.tick<=tick);
        const urgentOpponentDeploy=readyDeploys.some(e=>e.owner!==own.owner);
        // Preserve the usual five-tick cadence, but publish a complete native
        // frame immediately after a confirmed opposing deployment. A public
        // event alone lacks the object vector needed by the policy.
        if ((tick%5===0 || urgentOpponentDeploy) && frameKey!==lastPublished) {
          lastPublished=frameKey;
          snapshot.public_deployments=readyDeploys;
          snapshot.urgent_opponent_deploy=urgentOpponentDeploy;
          publicDeploys=publicDeploys.filter(e=>e.world===own.world.toString()&&e.tick>tick);
          try{snapshot.battlefield=battlefield(own);}catch(e){snapshot.mapping_error=String(e);}
          emit({event:'native_partial_frame',frame:snapshot});
        }
        if (snapshot.battle_finished) {
          for(const request of pending) receipt({id:request.id,kind:request.kind,ok:false,
            stage:'battle_ended',error:'Battle ended before application',tick:tick,
            world:snapshot.world,card_id:request.card_id});
          pending=[];
          for(const request of inflight.values()) receipt({id:request.id,kind:request.kind,ok:false,
            stage:'battle_ended',error:'Battle ended before application',tick:tick,
            world:snapshot.world,card_id:request.card_id});
          inflight.clear();enabled=false;return;
        }
        if (!enabled || pending.length===0) return;
        const request=pending[0];
        processing=request;
        if (tick<request.due_tick) return;
        pending.shift();
        if (request.generation!==generation || request.world!==snapshot.world || request.observed_tick>tick || tick-request.observed_tick>10)
          throw new Error('Deployment request belongs to a stale battle or tick');
        if(request.kind==='activate_ability'){
          const ability=own.abilities.find(a=>a.source_entity===request.source_entity&&a.controller_slot===request.controller_slot&&a.card_id===request.card_id);
          if(!ability||![2,4].includes(ability.button_state)||ability.remaining_cooldown_ms!==0||ability.charges_raw<=0||
             own.mana_raw<request.ability_cost*10000)throw new Error('Hero ability state or elixir changed');
          executing=true;
          try{
            const built=createAbilityCommand(manager,own,request.source_entity,tick);
            const submitted=Object.assign({},request,{tick:tick,charges_before:ability.charges_raw});
            inflight.set(request.id,submitted);
            const queuedBefore=controller.add(0x2c).readS32();
            const ok=submit(controller,built.owner,1),deferred=controller.add(0x2c).readS32()>queuedBefore;
            if(!ok&&!deferred){inflight.delete(request.id);throw new Error('Stock ability submit rejected');}
            receipt({id:request.id,kind:request.kind,ok:!!ok||deferred,submit_result:!!ok,
              stage:deferred?'client_deferred':'stock_submit',deferred:deferred,tick:tick,world:snapshot.world,
              source_entity:request.source_entity,ability_id:request.ability_id,card_id:request.card_id,
              charges_before:ability.charges_raw,server_ack:false});
          }finally{executing=false;}
          return;
        }
        const card=own.cards[request.hand_slot];
        if (!card || card.card_id!==request.card_id || request.form_code!==undefined&&card.form!==request.form_code || card.cost*10000>own.mana_raw) throw new Error('Hand or elixir changed');
        executing=true;
        try {
          const built=createCommand(manager,own,card,tick,request.x,request.y);
          // The stock function owns the command after invocation, including rejection.
          const submitted=Object.assign({},request,{hi:own.hi,lo:own.lo,validated:false});
          inflight.set(request.id,submitted);
          const queuedBefore=controller.add(0x2c).readS32();
          const ok=submit(controller,built.owner,1);
          const deferred=controller.add(0x2c).readS32()>queuedBefore;
          receipt({id:request.id,ok:!!ok||deferred,submit_result:!!ok,stage:deferred?'client_deferred':'stock_submit',deferred:deferred,card_id:card.card_id,tick:tick,world:snapshot.world,
            x:request.x,y:request.y,thread_id:Process.getCurrentThreadId(),mana_before:own.mana_raw,
            owner_after:built.owner.readPointer().toString()});
        } finally {executing=false;}
      } catch (error) {
        const message=String(error);
        // A human can spend elixir or rotate the hand between observation and
        // due_tick. Only this request became invalid; the battle is healthy.
        const changedState=processing && (message.includes('Hand or elixir changed')||
          message.includes('Hero ability state or elixir changed')||
          message.includes('Deployment request belongs to a stale battle or tick'));
        if (changedState) {
          receipt({id:processing.id,kind:processing.kind,ok:false,stage:'state_changed',
            error:message,tick:tick,world:snapshot.world,card_id:processing.card_id});
          return;
        }
        if (processing) receipt({id:processing.id,ok:false,error:message});
        for(const request of pending) receipt({id:request.id,ok:false,error:String(error)});
        pending=[];
        if(!processing) emit({event:'native_runtime_error',message:String(error),stack:error.stack});
        enabled=false;
      }
    }
  };
  Interceptor.attach(mod.base.add(O.step),stepHandlers);
  Interceptor.attach(mod.base.add(O.post_step),{
    onEnter(args){hits.update++;this.manager=args[0];},
    onLeave(){stepHandlers.onLeave.call(this);}
  });
  Object.assign(rpc.exports,{
    nativeclock(){return Date.now();},
    nativestatus(){return {enabled:enabled,hits:hits,partial:snapshot,receipt:lastReceipt,pending:pending.map(r=>r.id),inflight:Array.from(inflight.keys())};},
    nativeenable(value){enabled=value===true;if (!enabled) pending=[];return {enabled:enabled};},
    nativecancel(){pending=[];enabled=false;return true;},
    nativereceipt(id){return receipts.get(id)||null;},
    nativequeue(request){
      if (!enabled || snapshot===null || Date.now()-snapshot.captured_at_ms>750) throw new Error('Native runtime not enabled or battle unavailable');
      if (typeof request.id!=='string'||!request.id||pending.length>=8||pending.some(r=>r.id===request.id)||inflight.has(request.id)||receipts.has(request.id)) throw new Error('Duplicate or pending deployment');
      const ability=request.kind==='activate_ability';
      if(request.kind!==undefined&&request.kind!=='play_card'&&!ability)throw new Error('Unknown native action kind');
      for (const key of (ability?['generation','observed_tick','due_tick','source_entity','controller_slot','card_id','ability_cost']:
          ['generation','observed_tick','due_tick','hand_slot','card_id','x','y']))
        if (!Number.isInteger(request[key])) throw new Error('Invalid request '+key);
      if(ability){
        if(typeof request.ability_id!=='string'||!request.ability_id||request.source_entity<0||
           request.controller_slot<1||request.controller_slot>2||request.ability_cost<0||request.ability_cost>10)
          throw new Error('Invalid ability request');
      }else if(request.hand_slot<0||request.hand_slot>3||request.x<500||request.x>17500||request.y<500||request.y>31500)
        throw new Error('Invalid deployment coordinates');
      if(request.due_tick<request.observed_tick||request.due_tick>request.observed_tick+10)throw new Error('Invalid action timing');
      if (request.generation!==snapshot.generation || request.world!==snapshot.world || request.observed_tick>snapshot.tick || snapshot.tick-request.observed_tick>10) throw new Error('Stale battle identity');
      if(ability){
        const current=snapshot.abilities.find(a=>a.source_entity===request.source_entity);
        const reason=!current?'source missing':
          current.controller_slot!==request.controller_slot||current.card_id!==request.card_id?'controller changed':
          ![2,4].includes(current.button_state)?'button unavailable':
          current.remaining_cooldown_ms!==0?'cooldown active':
          current.charges_raw<=0?'no charges':
          snapshot.mana_raw<request.ability_cost*10000?'insufficient elixir':null;
        if(reason)throw new Error('Ability state changed before queue: '+reason+' at tick '+snapshot.tick);
        if(pending.some(r=>r.kind==='activate_ability'&&r.source_entity===request.source_entity)||
           [...inflight.values()].some(r=>r.kind==='activate_ability'&&r.source_entity===request.source_entity))
          throw new Error('Ability already queued: source '+request.source_entity+' at tick '+snapshot.tick);
      }
      pending.push(Object.assign({},request));pending.sort((a,b)=>a.due_tick-b.due_tick);return {queued:true,id:request.id};
    }
  });
  emit({event:'native_runtime_ready',execution:false,partial_only:true});
})();
