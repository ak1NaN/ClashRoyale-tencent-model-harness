// Run with Node.js after editing the Frida probe. Exercise the real step
// handler's stale-request path without attaching to a live battle.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '..', 'probes', 'tencent_native_runtime.js'), 'utf8');
const start = source.indexOf('  const stepHandlers={');
const end = source.indexOf('  Interceptor.attach(mod.base.add(O.step),stepHandlers);', start);
assert(start >= 0 && end > start, 'native step handler must be present');
const handler = source.slice(start, end).replace('const stepHandlers=', 'globalThis.stepHandlers=');

function staleRequestReceipts() {
  const collected = [];
  const manager = {
    add(offset) {
      assert.equal(offset, 0x60);
      return {readS32() { return 112; }};
    },
    toString() { return 'manager'; },
    equals(other) { return other === manager; },
  };
  const world = {
    toString() { return 'world'; },
    add(offset) {
      assert.equal(offset, 0x198);
      return {readS32() { return 0; }};
    },
  };
  const controller = {
    add(offset) {
      assert.equal(offset, 0x90);
      return {readPointer() { return {equals(other) { return other === manager; }}; }};
    },
  };
  const context = {
    executing: false, controller, state: manager, snapshot: {world: 'world'},
    lastTick: 100, generation: 1, lastPublished: null, enabled: true,
    hits: {step: 0, mismatch: 0, invalidOwn: 0}, inflight: new Map(),
    pending: [{id: 'stale', kind: 'play_card', generation: 1, world: 'world',
      observed_tick: 100, due_tick: 101, card_id: 28000011}],
    publicDeploys: [],
    readOwn() { return {world, owner: 0, mana_raw: 100000, cards: [], abilities: []}; },
    receipt(value) { collected.push(value); },
    Process: {getCurrentThreadId() { return 1; }},
    emit() { throw new Error('unexpected emit'); },
  };
  vm.runInNewContext(handler, context);
  context.stepHandlers.onLeave.call({manager, callerController: null});
  return collected;
}

const receipts = staleRequestReceipts();
assert.equal(receipts.length, 1);
assert.equal(receipts[0].id, 'stale');
assert.equal(receipts[0].stage, 'state_changed');
assert.equal(receipts[0].error, 'Error: Deployment request belongs to a stale battle or tick');
assert.equal(receipts[0].tick, 112);

function immediateOpponentFrame() {
  const output = [];
  const manager = {
    add(offset) { assert.equal(offset, 0x60); return {readS32() { return 112; }}; },
    toString() { return 'manager'; },
    equals(other) { return other === manager; },
  };
  const world = {toString() { return 'world'; }, add(offset) {
    assert.equal(offset, 0x198); return {readS32() { return 0; }};
  }};
  const controller = {add(offset) {assert.equal(offset, 0x90);return {
    readPointer() {return {equals(other) {return other === manager; }}; },
  }; }};
  const deployment = {world: 'world', tick: 111, owner: 1, card_id: 28000011};
  const context = {
    executing: false, controller, state: manager, snapshot: {world: 'world'},
    lastTick: 111, generation: 1, lastPublished: null, enabled: true,
    hits: {step: 0, mismatch: 0, invalidOwn: 0}, inflight: new Map(),
    pending: [], publicDeploys: [deployment],
    readOwn() {return {world, owner: 0, mana_raw: 100000, cards: [], abilities: []}; },
    battlefield() {return {objects: [], deck: [], cycle: []}; },
    Process: {getCurrentThreadId() {return 1; }},
    emit(value) {output.push(value);},
  };
  vm.runInNewContext(handler, context);
  context.stepHandlers.onLeave.call({manager, callerController: null});
  assert.equal(output.length, 1);
  assert.equal(output[0].event, 'native_partial_frame');
  assert.equal(output[0].frame.tick, 112);
  assert.equal(output[0].frame.urgent_opponent_deploy, true);
  assert.equal(output[0].frame.public_deployments[0].card_id, 28000011);
  assert.equal(context.publicDeploys.length, 0);
}

immediateOpponentFrame();
