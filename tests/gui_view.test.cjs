const {test} = require('node:test');
const assert = require('node:assert/strict');
const {scaleView} = require('../src/trackpad_scale/gui_assets/view.js');

const live = (changes = {}) => ({
  running: true, status: 'listening', raw: 12,
  live_reading: {estimated_grams: 12, zeroed: false},
  contacts: [{path: 1, state: 4}],
  last_reading: {pressure_raw: 12, estimated_grams: 12}, error: null, ...changes,
});

test('first estimate appears immediately and explains continuous light contact', () => {
  const view = scaleView(live({last_reading: null}), true);
  assert.equal(view.value, 12);
  assert.equal(view.label, 'Live estimate');
  assert.equal(view.canZero, true);
  assert.match(view.instruction, /Keep one finger lightly in contact/);
  assert.match(view.instruction, /Zero or Space/);
  assert.doesNotMatch(view.instruction, /lift your finger/);
});

test('display uses zeroed estimate, not raw pressure or experimental baseline', () => {
  const view = scaleView(live({raw: 50, live_reading: {estimated_grams: 30, zeroed: true}}), true);
  assert.equal(view.value, 30);
  assert.equal(view.label, 'Live estimate · zeroed');
  assert.match(view.instruction, /Place your object/);
  assert.match(view.instruction, /Keep your finger lightly in contact/);
});

test('experimental state transitions never replace everyday weighing', () => {
  const reference = scaleView(live(), true);
  for (const status of ['countdown', 'taring', 'filter_warmup', 'unstable', 'monitoring',
    'stable_pending', 'stable_published', 'tare_required', 'tare_expired', 'position_changed']) {
    assert.deepEqual(scaleView(live({status}), true), reference);
  }
});

test('lifting labels history, disables zero, and explains that sensing ended', () => {
  const view = scaleView(live({raw: null, live_reading: null, contacts: []}), true);
  assert.equal(view.value, 12);
  assert.equal(view.label, 'Last estimate · not updating');
  assert.equal(view.canZero, false);
  assert.match(view.instruction, /Contact ended/);
});

test('no input is not invented as zero; measured zero and negative deltas remain valid', () => {
  const waiting = scaleView(live({raw: null, live_reading: null, last_reading: null, contacts: []}), true);
  assert.equal(waiting.value, null);
  assert.equal(waiting.canZero, false);
  for (const value of [0, -5]) {
    assert.equal(scaleView(live({live_reading: {estimated_grams: value, zeroed: true}}), true).value, value);
  }
});

test('stopping or disconnecting labels retained estimates and disables zero', () => {
  assert.equal(scaleView(live({running: false, raw: null}), true).label, 'Last estimate · stopped');
  const disconnected = scaleView(live(), false);
  assert.equal(disconnected.value, 12);
  assert.equal(disconnected.label, 'Last estimate · disconnected');
  assert.equal(disconnected.canZero, false);
});

test('multiple active contacts display the current estimate and allow zero', () => {
  const view = scaleView(live({
    live_reading: {estimated_grams: 35, zeroed: false},
    contacts: [{state: 4, pressure_raw: 12}, {state: 3, pressure_raw: 23}],
  }), true);
  assert.equal(view.value, 35);
  assert.equal(view.label, 'Live estimate');
  assert.equal(view.canZero, true);
  assert.match(view.instruction, /dry paper first/);
  assert.match(view.instruction, /Zero or Space before placing your object/);
});

test('pressure-bearing hover records display the current estimate and allow zero', () => {
  const view = scaleView(live({
    live_reading: {estimated_grams: 124, zeroed: true},
    contacts: [{state: 4, pressure_raw: 0}, {state: 2, pressure_raw: 124}],
  }), true);
  assert.equal(view.value, 124);
  assert.equal(view.label, 'Live estimate · zeroed');
  assert.equal(view.canZero, true);
});

test('invalid pressure hides the previous estimate and disables zero', () => {
  const view = scaleView(live({
    raw: null, live_reading: null, reading_issue: 'invalid_pressure',
    contacts: [{state: 4, pressure_raw: 0}, {state: 2, pressure_raw: null}],
  }), true);
  assert.equal(view.value, null);
  assert.equal(view.label, 'Pressure unavailable');
  assert.equal(view.canZero, false);
  assert.match(view.instruction, /valid pressure reading/);
  assert.doesNotMatch(view.instruction, /Contact ended/);
});

test('hardware failures do not present history as a current measurement', () => {
  const view = scaleView(live({running: false, error: 'Unsupported device'}), true);
  assert.equal(view.value, null);
  assert.equal(view.label, 'Unavailable');
  assert.equal(view.canZero, false);
});
