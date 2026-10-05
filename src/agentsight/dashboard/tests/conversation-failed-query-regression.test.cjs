const assert = require('node:assert/strict');
const { join } = require('node:path');
const { readFileSync } = require('node:fs');
const test = require('node:test');

// Pins the observability page's failure path: when the newest query's batch
// fails, the page must stop showing the previous range's payload. runQuery
// resolves the batch all-or-nothing, so a failed range change used to leave
// the sessions table, both charts, the interruption card and the savings
// column populated with the PREVIOUS range under a lone error banner - the
// same defect class kongche fixed for the security overview cards (#5313).
//
// The dashboard has no component-test harness, so - like the stale-load
// deferred suite - these tests transpile the real page with the dashboard's
// babel toolchain and drive it against a minimal hooks driver with deferred
// fetch stubs whose responses resolve in a controlled order.

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
  const moduleStubs = {
    'react-router-dom': {
      useNavigate: () => () => undefined,
      useSearchParams: () => [new URLSearchParams(''), () => undefined],
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
    '../components/DateTimePicker': componentStub('DateTimePicker'),
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
  return { calls, driver, page, rendered };
}

/** Resolve one successful runQuery batch (all seven endpoints). */
async function resolveQueryBatch(calls, index, payload) {
  const sessions = payload?.sessions ?? [];
  calls.fetchSessions[index].resolve(sessions);
  calls.fetchTimeseries[index].resolve({
    token_series: payload?.token_series ?? [],
    model_series: payload?.model_series ?? [],
  });
  calls.fetchInterruptionCount[index].resolve(payload?.interruption_count ?? {
    total: 0,
    by_severity: { critical: 0, high: 0, medium: 0, low: 0 },
  });
  calls.fetchInterruptionStats[index].resolve(payload?.interruption_stats ?? []);
  calls.fetchInterruptionSessionCounts[index].resolve(payload?.session_counts ?? []);
  calls.fetchInterruptionConversationCounts[index].resolve(payload?.conversation_counts ?? []);
  calls.fetchTokenSavings[index].resolve(payload?.savings ?? null);
  await settle();
  await settle();
}

const rangeA = {
  sessions: [{ session_id: 'sess-A', conversation_count: 1, total_input_tokens: 5, total_output_tokens: 2, first_seen_ns: 1, last_seen_ns: 2, model: null, agent_name: null }],
  token_series: [{ bucket_start_ns: 11, input_tokens: 5, output_tokens: 2, total_tokens: 7 }],
  model_series: [{ bucket_start_ns: 11, model: 'm-A', total_tokens: 7 }],
  interruption_count: { total: 3, by_severity: { critical: 1, high: 0, medium: 0, low: 2 } },
  session_counts: [{ session_id: 'sess-A', total: 3, by_severity: { critical: 1, high: 0, medium: 0, low: 2 }, types: [] }],
  conversation_counts: [{ session_id: 'sess-A', conversation_id: 'conv-A', total: 3, by_severity: { critical: 1, high: 0, medium: 0, low: 2 }, types: [] }],
};

test('a failed query drops the previous range payload', async () => {
  const { calls, driver, rendered } = renderConversationPage();
  const handleQuery = rendered.callbacks[4];
  assert.ok(String(handleQuery).includes('runQuery'), 'callback 4 must be handleQuery');

  // Range A answers with a full payload.
  const first = handleQuery();
  await resolveQueryBatch(calls, 0, rangeA);
  await first;
  await settle();

  const sessionsSlot = driver.slots.findIndex(
    (slot) => Array.isArray(slot.value) && slot.value[0] && slot.value[0].session_id === 'sess-A',
  );
  assert.ok(sessionsSlot >= 0, 'range A sessions must land in a slot');
  const tokenSeriesSlot = driver.slots.findIndex(
    (slot) => Array.isArray(slot.value) && slot.value[0] && slot.value[0].bucket_start_ns === 11,
  );
  assert.ok(tokenSeriesSlot >= 0, 'range A token series must land in a slot');
  const interruptionSlot = driver.slots.findIndex(
    (slot) => slot.value && slot.value.total === 3 && slot.value.by_severity,
  );
  assert.ok(interruptionSlot >= 0, 'range A interruption count must land in a slot');

  // Range B fails on the sessions call; the batch rejects as a whole.
  const second = handleQuery();
  assert.equal(calls.fetchSessions.length, 2, 'the second query must fetch sessions');
  calls.fetchSessions[1].reject(new Error('boom'));
  calls.fetchTimeseries[1].resolve({ token_series: [], model_series: [] });
  calls.fetchInterruptionCount[1].resolve(null);
  calls.fetchInterruptionStats[1].resolve([]);
  calls.fetchInterruptionSessionCounts[1].resolve([]);
  calls.fetchInterruptionConversationCounts[1].resolve([]);
  calls.fetchTokenSavings[1].resolve(null);
  await second;
  await settle();
  await settle();

  // The newest request failed: nothing from range A may stay on screen under
  // the error banner.
  assert.deepEqual(
    driver.slots[sessionsSlot].value,
    [],
    'the sessions table must not keep the previous range after a failed query',
  );
  assert.deepEqual(
    driver.slots[tokenSeriesSlot].value,
    [],
    'the token chart must not keep the previous range after a failed query',
  );
  assert.equal(
    driver.slots[interruptionSlot].value,
    null,
    'the interruption card must not keep the previous range after a failed query',
  );

  // And the error must still be reported for the newest request.
  const errorSlot = driver.slots.findIndex((slot) => slot.value === 'boom');
  assert.ok(errorSlot >= 0, 'the failed query must surface its error message');
});

test('a newer successful query still wins after an older one failed', async () => {
  const { calls, driver, rendered } = renderConversationPage();
  const handleQuery = rendered.callbacks[4];

  // Query A fails first.
  const first = handleQuery();
  calls.fetchSessions[0].reject(new Error('boom'));
  calls.fetchTimeseries[0].resolve({ token_series: [], model_series: [] });
  calls.fetchInterruptionCount[0].resolve(null);
  calls.fetchInterruptionStats[0].resolve([]);
  calls.fetchInterruptionSessionCounts[0].resolve([]);
  calls.fetchInterruptionConversationCounts[0].resolve([]);
  calls.fetchTokenSavings[0].resolve(null);
  await first;
  await settle();
  await settle();

  // Query B succeeds and must populate the page.
  const second = handleQuery();
  await resolveQueryBatch(calls, 1, rangeA);
  await second;
  await settle();

  const sessionsSlot = driver.slots.findIndex(
    (slot) => Array.isArray(slot.value) && slot.value[0] && slot.value[0].session_id === 'sess-A',
  );
  assert.ok(sessionsSlot >= 0, 'the successful newer query must populate the sessions table');
  const errorValues = driver.slots.filter((slot) => slot.value === 'boom');
  assert.deepEqual(errorValues, [], 'the older failure must not pin an error over the newer success');
});

test('source pin: runQuery must clear the range state when the newest request fails', () => {
  const source = readFileSync(join(process.cwd(), 'src/pages/ConversationList.tsx'), 'utf8');
  const start = source.indexOf('} catch (error) {');
  const end = source.indexOf('return { ok: false, error, requestId };', start);
  assert.ok(start >= 0 && end > start, 'runQuery catch block must be found');
  const catchBlock = source.slice(start, end);
  for (const setter of ['setSessions([])', 'setTokenSeries([])', 'setModelSeries([])']) {
    assert.ok(
      catchBlock.includes(setter),
      `runQuery's failure path must clear the range payload (${setter})`,
    );
  }
  assert.ok(
    catchBlock.includes('requestId === loadRequestIdRef.current'),
    'the clearing must be gated on the newest request',
  );
});
