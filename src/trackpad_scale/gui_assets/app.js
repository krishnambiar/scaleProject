'use strict';
const $ = (id) => document.getElementById(id);
const format = (value) => Number.isFinite(value) ? value.toFixed(1) : '—';
let state;
let token;
let connected = false;
let busy = false;
let requestError = null;
function setText(id, text) {
  const element = $(id);
  if (element.textContent !== text) element.textContent = text;
}

function renderReading(next) {
  const view = scaleView(next, connected);
  setText('reading', format(view.value));
  setText('reading-status', view.label);
  setText('instruction', view.instruction);
  $('zero').disabled = busy || !view.canZero;
  setText('unit', next.mode === 'demo' ? 'g · simulated' : 'g · estimate');
}

async function request(url, payload) {
  const options = {cache: 'no-store'};
  if (payload !== undefined) {
    if (!token) token = (await request('/api/session')).token;
    Object.assign(options, {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-Pebble-Token': token},
      body: JSON.stringify(payload),
    });
  }
  const response = await fetch(url, options);
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || 'Please try again.');
  return value;
}

function error(message) {
  requestError = message;
  showError(message);
}

function showError(message) {
  $('error').textContent = message || '';
  $('error').hidden = !message;
}

function render(next) {
  state = next;
  renderReading(next);
  $('demo').hidden = next.mode !== 'demo';
  $('start').textContent = next.running ? 'Stop' : 'Start';
  $('start').disabled = busy || !connected;
  showError(next.error || next.zero_issue || requestError);
}

async function control(url, payload = {}) {
  if (busy) return;
  busy = true;
  if (state) render(state);
  try {
    const next = await request(url, payload);
    error(null);
    render(next);
  } catch (failure) { error(failure.message); }
  finally { busy = false; if (state) render(state); }
}

async function poll() {
  try {
    const next = await request('/api/state');
    connected = true;
    render(next);
  } catch (_) {
    connected = false;
    token = null;
    if (state) renderReading(state);
    if (!state) setText('instruction', 'Waiting for the local app…');
    $('start').disabled = $('zero').disabled = true;
  } finally { setTimeout(poll, document.hidden ? 1000 : 200); }
}

$('start').addEventListener('click', () => control(state?.running ? '/api/stop' : '/api/start', {mode: state?.mode ?? 'live'}));
$('zero').addEventListener('click', () => control('/api/zero'));
document.addEventListener('keydown', (event) => {
  if (event.code !== 'Space' || event.repeat || $('zero').disabled ||
      event.target.closest('input, select, textarea, a, [contenteditable="true"]') ||
      (event.target.closest('button') && !['start', 'zero'].includes(event.target.id))) return;
  event.preventDefault();
  control('/api/zero');
});
poll();
