'use strict';

const LIB_NAME = 'libg.so';
const O = globalThis.__tencentOffsets;
if (!O) throw new Error('Missing native offset profile');
const GET_MANA = O.get_mana;
const DEPLOY_GET_MANA_RETURN = O.get_mana_return;
const RESOLVE_PLAYER = O.resolve_player;
const LOCAL_RESOLVE_RETURN = O.resolve_return;
const LOCAL_IDENTITY_HOLDER = O.identity_holder;
const STOCK_SUBMIT = O.stock_submit;
const PLAYER_MANA_RAW = 0x300;
const SAMPLE_INTERVAL_MS = 100;
const RECORD_CONSTRUCTOR = O.record_ctor;
const QUEUE_DEDUPE_MS = 3000;
const CARD_ID_MIN = 26000000;
const CARD_ID_MAX = 28000018;

const players = new Map();
const seenQueueEvents = new Map();
let localPlayer = null;
let localIdentity = null;
let activeBattleState = null;
let activeOpponentState = null;
let localTeamSide = null;
const diagnosticHits = { queueConstructor: 0, getMana: 0, acceptedGetMana: 0, resolvePlayer: 0, acceptedResolvePlayer: 0, stockSubmit: 0 };
let lastConstructorLayout = null;
let lastSubmission = null;
let liveManager = null;
let liveTick = -1;

function captureConstructorLayout(record) {
  const words = [];
  const pointers = [];
  for (let offset = 0; offset < 0x100; offset += 4) words.push([offset, readS32(record, offset)]);
  for (let offset = 0; offset < 0x100; offset += 8) {
    const target = readPointer(record, offset);
    if (target.isNull()) continue;
    const cards = [];
    for (let inner = 0; inner < 0x100; inner += 4) {
      const value = readS32(target, inner);
      if (value !== null && value >= CARD_ID_MIN && value <= CARD_ID_MAX) cards.push([inner, value]);
    }
    if (cards.length) pointers.push({ offset: offset, cards: cards });
  }
  // A copied diagnostic at the constructor boundary, not a retained freed pointer.
  return { words: words, pointers: pointers, constructorSequence: diagnosticHits.queueConstructor };
}

function nowMs() {
  return Date.now();
}

function emit(payload) {
  payload.t_ms = nowMs();
  send(payload);
}

function readS32(base, offset) {
  try {
    return base.add(offset).readS32();
  } catch (_) {
    return null;
  }
}

function readPointer(base, offset) {
  try {
    return base.add(offset).readPointer();
  } catch (_) {
    return ptr(0);
  }
}

function tileCenter(value) {
  return Number.isInteger(value) ? Math.round((value - 500) / 1000) + 0.5 : null;
}

function emitQueueRecord(record) {
  const action = readPointer(record, 0x38);
  if (action.isNull()) return;
  const cardId = readS32(action, 0x40);
  const x = readS32(record, 0x28);
  const y = readS32(record, 0x2c);
  const ownerKey0 = readS32(record, 0x14);
  const ownerKey1 = readS32(record, 0x18);
  if (!Number.isInteger(cardId) || cardId < CARD_ID_MIN || cardId > CARD_ID_MAX) return;

  const tileX = tileCenter(x);
  const tileY = tileCenter(y);
  if (tileX === null || tileY === null) return;

  const dedupeKey = action.toString() + ':' + cardId + ':' + x + ':' + y;
  const now = nowMs();
  const previous = seenQueueEvents.get(dedupeKey);
  if (previous !== undefined && now - previous <= QUEUE_DEDUPE_MS) return;
  seenQueueEvents.set(dedupeKey, now);

  const ownerKey = Number.isInteger(ownerKey0) && Number.isInteger(ownerKey1)
    ? ownerKey0 + ':' + ownerKey1
    : null;
  const side = localIdentity === null || ownerKey === null
    ? 'unknown'
    : ownerKey === localIdentity ? 'self' : 'opponent';

  emit({
    event: 'queue_deploy',
    battle_clock: null,
    source: 'tencent_elixir_overlay_constructor',
    action_ptr: action.toString(),
    card_id: cardId,
    owner_key: ownerKey,
    side: side,
    target_raw: { x: x, y: y },
    target_tile_center: { x: tileX, y: tileY }
  });
}

function manaSnapshot(player) {
  const raw = readS32(player, PLAYER_MANA_RAW);
  if (raw === null) return null;
  return {
    mana_raw: raw,
    mana: raw / 10000.0
  };
}

function sideFor(player, identity) {
  if (localIdentity !== null && identity !== null) {
    return identity === localIdentity ? 'self' : 'opponent';
  }
  if (localPlayer !== null) {
    return player.equals(localPlayer) ? 'self' : 'opponent';
  }
  return 'unknown';
}

function resolvePlayerIndex(battleState, key0, key1) {
  if (battleState.isNull() || !Number.isInteger(key0) || !Number.isInteger(key1)) return null;
  const count = readS32(battleState, 0x60);
  if (!Number.isInteger(count) || count < 1 || count > 8) return null;

  for (let index = 0; index < count; index += 1) {
    const keyObject = readPointer(battleState, 0x30 + index * 8);
    if (keyObject.isNull()) continue;
    if (readS32(keyObject, 0) === key0 && readS32(keyObject, 4) === key1) return index;
  }
  return null;
}

function publishBattleContext() {
  if (activeBattleState === null) return;
  const parts = localIdentity === null ? [] : localIdentity.split(':');
  const key0 = parts.length === 2 ? Number(parts[0]) : null;
  const key1 = parts.length === 2 ? Number(parts[1]) : null;
  const localIndex = resolvePlayerIndex(activeBattleState, key0, key1);
  localTeamSide = localIndex === null ? null : localIndex & 1;

  emit({
    event: 'elixir_battle_changed',
    battle_state: activeBattleState.toString(),
    local_key: localIdentity,
    local_player_index: localIndex,
    local_team_side: localTeamSide,
    auto_flip: localTeamSide === null ? null : localTeamSide === 0
  });
}

function selectBattle(battleState) {
  if (activeBattleState !== null && activeBattleState.equals(battleState)) return false;
  activeBattleState = ptr(battleState.toString());
  activeOpponentState = null;
  localPlayer = null;
  players.clear();
  publishBattleContext();
  return true;
}

function publishSideUpdates() {
  players.forEach(function (state) {
    const side = sideFor(state.player, state.identity);
    if (side === state.side) return;
    state.side = side;
    emit({
      event: 'elixir_side_update',
      player_key: state.identity,
      player: state.player.toString(),
      side: side
    });
  });
}

function rememberPlayer(record, player) {
  const key0 = readS32(record, 0x14);
  const key1 = readS32(record, 0x18);
  if (key0 === null || key1 === null) return null;

  const identity = key0 + ':' + key1;
  let state = players.get(identity);
  if (state === undefined || !state.player.equals(player)) {
    state = {
      identity: identity,
      key0: key0,
      key1: key1,
      player: player,
      side: sideFor(player, identity),
      lastRaw: null
    };
    players.set(identity, state);
    emit({
      event: 'elixir_player_discovered',
      player_key: identity,
      key0: key0,
      key1: key1,
      player: player.toString(),
      side: state.side
    });
  }
  return state;
}

function install() {
  const module = Process.findModuleByName(LIB_NAME);
  if (module === null) {
    setTimeout(install, 250);
    return;
  }

  const getMana = module.base.add(GET_MANA);
  const deployReturn = module.base.add(DEPLOY_GET_MANA_RETURN);
  const resolvePlayer = module.base.add(RESOLVE_PLAYER);
  const localResolveReturn = module.base.add(LOCAL_RESOLVE_RETURN);
  const localIdentityHolder = module.base.add(LOCAL_IDENTITY_HOLDER);

  // Entry fingerprints prevent using this version profile against a different binary.
  const entries = [[GET_MANA, 0x529175a8], [RESOLVE_PLAYER, 0xb940600a],
                   [RECORD_CONSTRUCTOR, 0xa9be7bfd], [STOCK_SUBMIT, 0xf9400028]];
  for (const [offset, expected] of entries) {
    const actual = globalThis.__tencentEntryWords ? globalThis.__tencentEntryWords[offset] : module.base.add(offset).readU32();
    if (actual !== expected) throw new Error('Unsupported libg entry at ' + offset.toString(16)
      + ': ' + actual.toString(16));
  }
  Interceptor.attach(module.base.add(O.tick_accessor), {
    onEnter(args) { this.manager = args[0]; },
    onLeave(result) {
      const world = readPointer(this.manager, 0xa8);
      const count = readS32(world, 0x60);
      if (!world.isNull() && count === 2) {
        liveManager = this.manager;
        liveTick = result.toInt32();
        selectBattle(world);
      }
    }
  });
  Interceptor.attach(module.base.add(STOCK_SUBMIT), {
    onEnter(args) {
      diagnosticHits.stockSubmit += 1;
      const manager = readPointer(args[0], 0x90);
      const command = readPointer(args[1], 0);
      this.isDeploy = readPointer(command, 0).equals(module.base.add(O.deploy_vtable));
      this.submission = { event: 'native_stock_submit', controller: args[0].toString(),
        manager: manager.toString(), world: readPointer(manager, 0xa8).toString(),
        tick: readS32(manager, 0x60), thread_id: Process.getCurrentThreadId(),
        flag: args[2].toInt32(), command: command.toString(),
        key0: readS32(command, 0x14), key1: readS32(command, 0x18),
        x: readS32(command, 0x28), y: readS32(command, 0x2c),
        command_vtable: readPointer(command, 0).toString(),
        card_data: readPointer(command, 0x38).toString(),
        selection_secondary: readPointer(command, 0x40).toString(),
        selection_packed: readS32(command, 0x48) };
    },
    onLeave(result) {
      this.submission.result = result.toInt32();
      if (this.isDeploy) {
        lastSubmission = this.submission;
        emit(this.submission);
      }
    }
  });

  Interceptor.attach(module.base.add(RECORD_CONSTRUCTOR), {
    onEnter(args) {
      diagnosticHits.queueConstructor += 1;
      this.record = args[0];
    },

    onLeave() {
      const record = ptr(this.record.toString());
      if (diagnosticHits.queueConstructor <= 8) lastConstructorLayout = captureConstructorLayout(record);
      // Fields are populated after construction. Read completed records synchronously
      // at deploy validation instead of retaining potentially freed heap pointers.
    }
  });

  const identityTimer = setInterval(function () {
    try {
      const holder = localIdentityHolder.readPointer();
      const identityObject = holder.readPointer();
      const key0 = identityObject.add(0x20).readS32();
      const key1 = identityObject.add(0x24).readS32();
      const identity = key0 + ':' + key1;
      if (key0 === 0 && key1 === 0) return;
      localIdentity = identity;
      clearInterval(identityTimer);
      emit({
        event: 'local_elixir_identity_resolved',
        player_key: identity,
        key0: key0,
        key1: key1,
        identity_object: identityObject.toString(),
        side: 'self'
      });
      if (activeBattleState !== null && localTeamSide === null) publishBattleContext();
      publishSideUpdates();
    } catch (_) {
      // The identity object is populated after the game session is ready.
    }
  }, 250);

  Interceptor.attach(resolvePlayer, {
    onEnter(args) {
      diagnosticHits.resolvePlayer += 1;
      this.accepted = this.returnAddress.equals(localResolveReturn);
      if (this.accepted) diagnosticHits.acceptedResolvePlayer += 1;
      if (!this.accepted) return;
      this.key0 = args[1].toInt32();
      this.key1 = args[2].toInt32();
    },

    onLeave(retval) {
      if (!this.accepted || retval.isNull()) return;
      const resolved = ptr(retval.toString());
      if (localPlayer !== null && localPlayer.equals(resolved)) return;
      localPlayer = resolved;
      localIdentity = this.key0 + ':' + this.key1;
      emit({
        event: 'local_elixir_player_resolved',
        player: localPlayer.toString(),
        key0: this.key0,
        key1: this.key1,
        side: 'self'
      });
      if (activeBattleState !== null && localTeamSide === null) publishBattleContext();
      publishSideUpdates();
    }
  });

  Interceptor.attach(getMana, {
    onEnter(args) {
      diagnosticHits.getMana += 1;
      this.accepted = this.returnAddress.equals(deployReturn);
      if (this.accepted) diagnosticHits.acceptedGetMana += 1;
      if (!this.accepted) return;

      this.player = args[0];
      this.record = this.context.x19;
      this.battleState = this.context.x24;
      selectBattle(this.battleState);
      emitQueueRecord(this.record);
      const state = rememberPlayer(this.record, this.player);
      this.identity = state === null ? null : state.identity;
      const ownerKey0 = readS32(this.record, 0x14);
      const ownerKey1 = readS32(this.record, 0x18);
      const ownerIndex = resolvePlayerIndex(this.battleState, ownerKey0, ownerKey1);
      const ownerTeamSide = ownerIndex === null ? null : ownerIndex & 1;
      const side = this.identity !== null && localIdentity !== null
        ? this.identity === localIdentity ? 'self' : 'opponent'
        : 'unknown';
      if (state !== null) {
        state.battleState = ptr(this.battleState.toString());
        state.teamSide = ownerTeamSide;
        state.side = side;
        state.lastRaw = null;
        if (side === 'self') localPlayer = this.player;
        if (side === 'opponent') activeOpponentState = state;
      }
      const action = readPointer(this.record, 0x38);
      const snapshot = manaSnapshot(this.player);

      emit({
        event: 'deploy_elixir_before',
        player_key: state === null ? null : state.identity,
        side: side,
        battle_state: this.battleState.toString(),
        owner_player_index: ownerIndex,
        owner_team_side: ownerTeamSide,
        local_team_side: localTeamSide,
        player: this.player.toString(),
        record: this.record.toString(),
        action: action.toString(),
        card_id: action.isNull() ? null : readS32(action, 0x40),
        target_x: readS32(this.record, 0x28),
        target_y: readS32(this.record, 0x2c),
        mana_raw: snapshot === null ? null : snapshot.mana_raw,
        mana: snapshot === null ? null : snapshot.mana
      });
    },

    onLeave(retval) {
      if (!this.accepted) return;
      emit({
        event: 'deploy_elixir_getter_result',
        side: this.identity !== null && localIdentity !== null && this.identity === localIdentity ? 'self' : 'opponent',
        player: this.player.toString(),
        mana_floor: retval.toInt32()
      });
    }
  });

  setInterval(function () {
    const state = activeOpponentState;
    if (state === null || activeBattleState === null) return;
    if (state.battleState === undefined || !state.battleState.equals(activeBattleState)) return;
    const snapshot = manaSnapshot(state.player);
    if (snapshot === null || snapshot.mana_raw === state.lastRaw) return;
    state.lastRaw = snapshot.mana_raw;
    emit({
      event: 'elixir_sample',
      battle_state: activeBattleState.toString(),
      player_key: state.identity,
      side: 'opponent',
      team_side: state.teamSide,
      player: state.player.toString(),
      mana_raw: snapshot.mana_raw,
      mana: snapshot.mana
    });
  }, SAMPLE_INTERVAL_MS);

  setInterval(function () {
    const now = nowMs();
    seenQueueEvents.forEach(function (timestamp, key) {
      if (now - timestamp > QUEUE_DEDUPE_MS) seenQueueEvents.delete(key);
    });
  }, QUEUE_DEDUPE_MS);

  emit({
    event: 'opponent_elixir_probe_ready',
    lib: LIB_NAME,
    base: module.base.toString(),
    get_mana: getMana.toString(),
    deploy_return: deployReturn.toString(),
    resolve_player: resolvePlayer.toString(),
    local_resolve_return: localResolveReturn.toString(),
    local_identity_holder: localIdentityHolder.toString(),
    queue_constructor: module.base.add(RECORD_CONSTRUCTOR).toString(),
    player_mana_offset: '0x300',
    sample_interval_ms: SAMPLE_INTERVAL_MS
  });

  Object.assign(rpc.exports, {
    battlelayout() {
      if (activeBattleState === null) return null;
      const world = activeBattleState;
      const result = { manager: liveManager === null ? null : liveManager.toString(), tick: liveTick, world: world.toString(), count: readS32(world, 0x60), players: [] };
      for (let owner = 0; owner < 2; owner++) {
        const player = readPointer(world, 0xe0 + owner * 8);
        const identity = readPointer(world, 0x30 + owner * 8);
        const deck = readPointer(world, 0x88 + owner * 8);
        const row = { owner: owner, player: player.toString(), key0: readS32(identity, 0),
          key1: readS32(identity, 4), mana_raw: readS32(player, 0x300), vectors: [],
          deck: deck.toString(), deck_count: readS32(deck, 0x2c), slots: [] };
        const slots = readPointer(deck, 0x20);
        for (let slot = 0; slot < 8; slot++) {
          const item = readPointer(slots, slot * 8);
          const data = readPointer(item, 0x10);
          row.slots.push({ slot: slot, address: item.toString(), card_id: readS32(data, 0x40) });
        }
        for (let offset = 0x200; offset < 0x280; offset += 8) {
          const count = readS32(player, offset + 12);
          const capacity = readS32(player, offset + 8);
          if (count === null || count < 1 || count > 10 || capacity < count || capacity > 32) continue;
          const data = readPointer(player, offset);
          const values = [];
          for (let index = 0; index < count; index++) values.push(readS32(data, index * 4));
          row.vectors.push({ offset: offset, count: count, capacity: capacity, values: values });
        }
        result.players.push(row);
      }
      return result;
    },
    submission() { return lastSubmission; },
    inspectrecord() {
      return lastConstructorLayout === null ? { error: 'no constructor record' } : lastConstructorLayout;
    },
    diagnostics() {
      let identityReadable = false;
      try {
        const holder = localIdentityHolder.readPointer();
        const object = holder.readPointer();
        object.add(0x20).readS32();
        object.add(0x24).readS32();
        identityReadable = true;
      } catch (_) {}
      return { hits: diagnosticHits, identityReadable: identityReadable,
        localIdentityResolved: localIdentity !== null,
        battleState: activeBattleState === null ? null : activeBattleState.toString(),
        playerCount: players.size };
    }
  });
}

install();
