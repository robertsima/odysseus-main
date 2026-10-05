// The canvas spinners in static/js/spinner.js end their own animation loop.
//
// The whirlpool spinner drives itself with requestAnimationFrame and checks
// `element.isConnected`. It used to re-arm forever when the element had never
// been connected, so a caller that started a spinner and then returned early
// (an aborted request, a panel resolved from cache before the loading row was
// inserted) left a loop redrawing an 84-segment spiral into a detached canvas
// until the tab closed: ~110 frames per second on an idle app.
//
// Covers all four exits (never attached, attached then removed, stop(), tab
// hidden) and the case that must NOT stop: a spinner on screen. A fake clock
// and a manual frame pump replace performance.now() and requestAnimationFrame.
import assert from 'node:assert/strict';
import { beforeEach, test } from 'node:test';

let clock = 0;
Object.defineProperty(globalThis, 'performance', {
  value: { now: () => clock }, configurable: true, writable: true,
});

const pending = new Map();
let nextFrameId = 1;
let framesRun = 0;
globalThis.requestAnimationFrame = (cb) => {
  const id = nextFrameId++;
  pending.set(id, cb);
  return id;
};
globalThis.cancelAnimationFrame = (id) => { pending.delete(id); };

/** Advance the clock `steps` frames of `msPerFrame` and run whatever is queued. */
function pump(steps, msPerFrame = 16) {
  for (let i = 0; i < steps; i++) {
    clock += msPerFrame;
    const due = [...pending.values()];
    pending.clear();
    for (const cb of due) { framesRun++; cb(); }
  }
}
const framesPending = () => pending.size;
const frameMark = () => framesRun;
const framesSince = (mark) => framesRun - mark;

function makeCtx() {
  const noop = () => {};
  return {
    clearRect: noop, beginPath: noop, arc: noop, moveTo: noop, lineTo: noop,
    stroke: noop, fill: noop, save: noop, restore: noop,
    strokeStyle: '', fillStyle: '', lineWidth: 0, globalAlpha: 1,
    lineCap: '', lineJoin: '',
  };
}

function makeElement(tag) {
  return {
    tagName: tag, className: '', textContent: '', innerHTML: '',
    width: 0, height: 0, isConnected: false, parentNode: null,
    style: { cssText: '' },
    children: [],
    classList: { add: () => {}, remove: () => {}, contains: () => false },
    getContext: () => makeCtx(),
    appendChild(child) {
      child.parentNode = this;
      this.children.push(child);
      return child;
    },
    removeChild(child) {
      this.children = this.children.filter((c) => c !== child);
      child.parentNode = null;
      return child;
    },
  };
}

const docListeners = [];
globalThis.document = {
  hidden: false,
  documentElement: makeElement('html'),
  createElement: makeElement,
  createTextNode: (t) => ({ textContent: t }),
  addEventListener: (type, fn) => { docListeners.push([type, fn]); },
  removeEventListener: (type, fn) => {
    const i = docListeners.findIndex(([t, f]) => t === type && f === fn);
    if (i >= 0) docListeners.splice(i, 1);
  },
};
globalThis.getComputedStyle = () => ({ getPropertyValue: () => '' });

const visibilityListeners = () => docListeners.filter(([t]) => t === 'visibilitychange').length;
function fireVisibility(hidden) {
  document.hidden = hidden;
  for (const [t, fn] of [...docListeners]) if (t === 'visibilitychange') fn();
}

const { Spinner, createLoadingRow } = await import('../../../../static/js/spinner.js');

// Each case starts with no queued frames and no document listeners, as if in
// a fresh page.
beforeEach(() => {
  pending.clear();
  docListeners.length = 0;
  document.hidden = false;
});

/** A started whirlpool spinner whose element is not in the document. */
function startedWhirlpool() {
  const sp = new Spinner('', 'clean', 'whirlpool');
  sp.createElement();
  sp.start();
  return sp;
}

function attachedWhirlpool() {
  const sp = new Spinner('', 'clean', 'whirlpool');
  sp.createElement();
  sp.element.isConnected = true;
  sp.start();
  return sp;
}

test('a never-attached whirlpool stops itself after the grace window', () => {
  const sp = startedWhirlpool();
  pump(30); // 480 ms, inside the grace window
  assert.deepEqual({ running: sp.isRunning, pending: framesPending() }, { running: true, pending: 1 },
    'gave up during the grace window');
  pump(120); // ~2.4 s total, past it
  const mark = frameMark();
  pump(60);
  assert.equal(sp.isRunning, false);
  assert.equal(sp.rafId, null);
  assert.equal(framesPending(), 0);
  assert.equal(framesSince(mark), 0, 'loop kept drawing after it gave up');
});

test('an attached whirlpool keeps running past the grace window', () => {
  const sp = attachedWhirlpool();
  pump(400); // ~6.4 s
  const mark = frameMark();
  pump(10);
  assert.equal(sp.isRunning, true);
  assert.equal(framesPending(), 1);
  assert.equal(framesSince(mark), 10, 'a visible spinner stopped animating');
});

test('an attached whirlpool stops once its element is removed', () => {
  const sp = attachedWhirlpool();
  pump(200);
  assert.equal(sp.isRunning, true);
  sp.element.isConnected = false; // results arrived, row swapped out
  pump(3);
  const mark = frameMark();
  pump(20);
  assert.equal(sp.isRunning, false);
  assert.equal(framesPending(), 0);
  assert.equal(framesSince(mark), 0);
});

test('createLoadingRow stops when its row is never inserted', () => {
  const row = createLoadingRow('Loading...', 16);
  pump(200);
  const mark = frameMark();
  pump(40);
  assert.ok(row.children.length > 0, 'harness built the wrong row');
  assert.equal(framesPending(), 0);
  assert.equal(framesSince(mark), 0);
});

test('stop() cancels the pending frame and releases the visibility listener', () => {
  const before = visibilityListeners();
  const sp = attachedWhirlpool();
  const armed = visibilityListeners();
  sp.stop();
  const mark = frameMark();
  pump(20);
  assert.deepEqual([before, armed, visibilityListeners()], [0, 1, 0]);
  assert.equal(sp.isRunning, false);
  assert.equal(sp.rafId, null);
  assert.equal(framesPending(), 0);
  assert.equal(framesSince(mark), 0);
});

test('a hidden tab pauses frames and showing it resumes them', () => {
  const sp = attachedWhirlpool();
  pump(5);
  fireVisibility(true);
  const hiddenMark = frameMark();
  pump(30);
  assert.deepEqual({ drawn: framesSince(hiddenMark), pending: framesPending() }, { drawn: 0, pending: 0 },
    'kept drawing in a hidden tab');
  fireVisibility(false);
  const shownMark = frameMark();
  pump(10);
  assert.equal(sp.isRunning, true);
  assert.equal(framesSince(shownMark), 10, 'did not resume when the tab came back');
});

test('a restarted spinner gets a fresh grace window', () => {
  const sp = startedWhirlpool();
  pump(200); // times out, never attached
  assert.equal(sp.isRunning, false);
  sp.element.isConnected = true; // now inserted for real
  sp.start();
  pump(30);
  assert.equal(sp.isRunning, true);
  assert.equal(framesPending(), 1);
});
