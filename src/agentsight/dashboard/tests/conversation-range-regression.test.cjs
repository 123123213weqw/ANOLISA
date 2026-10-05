const assert = require('node:assert/strict');
const { join } = require('node:path');
const { readFileSync } = require('node:fs');
const test = require('node:test');

// Pins the observability page's query range: the End Time picker is a real
// filter, so pressing Query must send the picked end (not Date.now()) to every
// range-scoped endpoint - exactly like the sibling pages (token-savings,
// skill-metrics, security observability) and like this page's own URL-restore
// path, which honors the `end` parameter it reads back.
//
// The dashboard has no component-test harness, so - like the stale-load
// deferred suite - these tests transpile the real page with the dashboard's
// babel toolchain and drive it against a minimal hooks driver with deferred
// fetch stubs.

const babel = require('@babel/core');

function transpile(relativePath) {
  const out = babel.transformFileSync(join(process.cwd(), relativePath), {
    presets: [
      ['@babel/preset-env', { targets: { node: 'current' } }],
      ['@babel/preset-typescript', { isTSX: true, allExtensions: true }],
      ['@babel/preset-react', { runtime: 'classic' }],
    ],
  });
  return out.code;
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

function createHooksDriver() {
  const slots = [];
  const driver = {
    slots,
    render(Component, props = {}) {
      driver._cursor = 0;
      driver._effects = [];
      driver._callbacks = [];
      const element = Component(props);
      return { element, effects: driver._effects, callbacks: driver._callbacks };
    },
    useState(initial) {
      const slot = slots[driver._cursor] ?? {
        value: typeof initial === 'function' ? initial() : initial,
      };
      slots[driver._cursor] = slot;
      if (!slot.setter) {
        slot.setter = (update) => {
          slot.value = typeof update === 'function'
            ? update(slot.value)
            : update;
        };
      }
      driver._cursor += 1;
      return [slot.value, slot.setter];
    },
    useRef(initial) {
      const slot = slots[driver._cursor] ?? { value: { current: initial } };
      slots[driver._cursor] = slot;
      driver._cursor += 1;
      return slot.value;
    },
    useEffect(fn) {
      driver._effects.push(fn);
    },
    useCallback(fn) {
      driver._callbacks.push(fn);
      return fn;
    },
    useMemo(factory) {
      return factory();
    },
  };
  return driver;
}

function deferredFetchStubs(names) {
  const calls = {};
  const stubs = {};
  for (const name of names) {
    calls[name] = [];
    stubs[name] = (...args) => {
      const d = deferred();
      calls[name].push({ args, ...d });
      return d.promise;
    };
  }
  return { calls, stubs };
}

const componentStub = (name) => ({ [name]: () => null });

/** Depth-first walk of the createElement-stub element tree. */
function walkElements(node, visit) {
  if (Array.isArray(node)) {
    for (const child of node) walkElements(child, visit);
    return;
  }
  // React text children are primitives, so every object node is an element
  // (including fragments, whose `type` is undefined under the stub).
  if (node && typeof node === 'object') {
    visit(node);
    walkElements(node.children, visit);
  }
}

function loadPageModule(relativePath, moduleStubs, driver) {
  const code = transpile(relativePath);
  const module = { exports: {} };
  const hooks = {
    useState: driver.useState,
    useRef: driver.useRef,
    useEffect: driver.useEffect,
    useCallback: driver.useCallback,
    useMemo: driver.useMemo,
  };
  const reactStub = {
    __esModule: true,
    default: { createElement: (type, props, ...children) => ({ type, props, children }), ...hooks },
    createElement: (type, props, ...children) => ({ type, props, children }),
    ...hooks,
  };
  const requireStub = (name) => {
    if (name === 'react') return reactStub;
    if (moduleStubs[name]) return moduleStubs[name];
    throw new Error(`unexpected require from ${relativePath}: ${name}`);
  };
  const fn = new Function('require', 'module', 'exports', code);
  fn(requireStub, module, module.exports);
  return module.exports;
}

function renderConversationPage() {
  const { calls, stubs } = deferredFetchStubs([
    'fetchSessions',
    'fetchTimeseries',
    'fetchInterruptionCount',
    'fetchInterruptionStats',
    'fetchInterruptionSessionCounts',
    'fetchInterruptionConversationCounts',
    'fetchTokenSavings',
  ]);
  const driver = createHooksDriver();
  const dateTimePicker = componentStub('DateTimePicker');
  const searchParamCalls = [];
  const moduleStubs = {
    'react-router-dom': {
      useNavigate: () => () => undefined,
      useSearchParams: () => [
        new URLSearchParams(''),
        (next) => searchParamCalls.push(
          next instanceof URLSearchParams ? next.toString() : JSON.stringify(next),
        ),
      ],
    },
    recharts: {
      ...componentStub('LineChart'), ...componentStub('Line'), ...componentStub('BarChart'),
      ...componentStub('Bar'), ...componentStub('XAxis'), ...componentStub('YAxis'),
      ...componentStub('CartesianGrid'), ...componentStub('Tooltip'), ...componentStub('Legend'),
      ...componentStub('ResponsiveContainer'),
    },
    '../components/InterruptionBadge': componentStub('InterruptionBadge'),
    '../components/InterruptionPanel': componentStub('InterruptionPanel'),
    '../components/EvaluationBadge': componentStub('EvaluationBadge'),
    '../components/EvaluationPanel': componentStub('EvaluationPanel'),
    '../components/DateTimePicker': dateTimePicker,
    '../components/SessionIdHelp': componentStub('SessionIdHelp'),
    '../components/SessionResourceChart': componentStub('SessionResourceChart'),
    '../i18n': {
      useI18n: () => ({ t: (key) => key }),
      useLocaleTag: () => 'en',
    },
    '../utils/datetime': { formatNsPadded: () => '' },
    '../utils/timeseriesBuckets': {
      fillModelBuckets: (data) => data,
      fillTokenBuckets: (data) => data,
    },
    '../utils/apiClient': {
      ...stubs,
      conversationInterruptionKey: (sessionId, conversationId) => `${sessionId}|${conversationId}`,
      UNASSIGNED_INTERRUPTION_BUCKET: '__unassigned__',
    },
  };
  const pageModule = loadPageModule('src/pages/ConversationList.tsx', moduleStubs, driver);
  const page = pageModule.ConversationList;
  assert.equal(typeof page, 'function', 'ConversationList must be a component');
  const rendered = driver.render(page);
  return { calls, driver, page, rendered, dateTimePicker, searchParamCalls };
}

/** Resolve one successful runQuery batch (all seven endpoints). */
async function resolveQueryBatch(calls, index, sessions = []) {
  calls.fetchSessions[index].resolve(sessions);
  calls.fetchTimeseries[index].resolve({ token_series: [], model_series: [] });
  calls.fetchInterruptionCount[index].resolve({
    total: 0,
    by_severity: { critical: 0, high: 0, medium: 0, low: 0 },
  });
  calls.fetchInterruptionStats[index].resolve([]);
  calls.fetchInterruptionSessionCounts[index].resolve([]);
  calls.fetchInterruptionConversationCounts[index].resolve([]);
  calls.fetchTokenSavings[index].resolve(null);
  await settle();
  await settle();
}

test('source pin: handleQuery must not recompute the end time', () => {
  const source = readFileSync(join(process.cwd(), 'src/pages/ConversationList.tsx'), 'utf8');
  const bodyStart = source.indexOf('const handleQuery = useCallback');
  const bodyEnd = source.indexOf('}, [startMs, endMs, selectedAgent, syncParams, runQuery, t]);');
  assert.ok(bodyStart >= 0 && bodyEnd > bodyStart, 'handleQuery body must be found');
  const handleQuery = source.slice(bodyStart, bodyEnd);
  assert.doesNotMatch(
    handleQuery,
    /Date\.now\(\)/,
    'handleQuery must use the picker end time, not Date.now()',
  );
});

test('querying a historical window sends the picked end to every endpoint', async () => {
  const { calls, driver, page, rendered, dateTimePicker, searchParamCalls } = renderConversationPage();

  // Pick a fully historical window far from the real clock (2023-11-14, 1 h).
  const START_MS = 1_700_000_000_000;
  const END_MS = 1_700_003_600_000;
  const pickers = [];
  walkElements(rendered.element, (el) => {
    if (el.type === dateTimePicker.DateTimePicker) pickers.push(el);
  });
  assert.equal(pickers.length, 2, 'the filter bar renders a start and an end picker');
  const byLabel = new Map(pickers.map((p) => [p.props.label, p]));
  assert.ok(byLabel.has('common.endTime'), 'the end picker must carry its label');
  byLabel.get('common.startTime').props.onChange(START_MS);
  byLabel.get('common.endTime').props.onChange(END_MS);

  // Re-render so the query callback closes over the picked values.
  const reRendered = driver.render(page);
  const handleQuery = reRendered.callbacks[4];
  assert.ok(
    String(handleQuery).includes('runQuery'),
    'callback 4 must be handleQuery (it awaits runQuery)',
  );
  const queryDone = handleQuery();
  assert.equal(calls.fetchSessions.length, 1, 'a query must fetch sessions');
  await resolveQueryBatch(calls, 0);
  await queryDone;
  await settle();

  // Every range-scoped request must use the picked end, not Date.now().
  assert.deepEqual(
    calls.fetchSessions[0].args,
    [START_MS * 1_000_000, END_MS * 1_000_000],
    'fetchSessions must receive the picked end time',
  );
  assert.deepEqual(
    calls.fetchTimeseries[0].args,
    [START_MS * 1_000_000, END_MS * 1_000_000, undefined],
    'fetchTimeseries must receive the picked end time',
  );
  assert.deepEqual(
    calls.fetchInterruptionCount[0].args,
    [START_MS * 1_000_000, END_MS * 1_000_000, undefined],
    'the interruption count must receive the picked end time',
  );

  // The picker must still show what the user picked, and the URL state too.
  const afterQuery = driver.render(page);
  const pickersAfter = [];
  walkElements(afterQuery.element, (el) => {
    if (el.type === dateTimePicker.DateTimePicker) pickersAfter.push(el);
  });
  const endAfter = pickersAfter.find((p) => p.props.label === 'common.endTime');
  assert.equal(endAfter.props.value, END_MS, 'the end picker must keep the picked value');
  assert.ok(
    searchParamCalls.some((c) => c.includes(String(END_MS))),
    'the synced URL must carry the picked end',
  );
});
