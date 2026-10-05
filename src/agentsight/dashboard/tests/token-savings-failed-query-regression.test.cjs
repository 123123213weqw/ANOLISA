const assert = require('node:assert/strict');
const { join } = require('node:path');
const { readFileSync } = require('node:fs');
const test = require('node:test');

// Pins the token-savings page's failure path: when the newest query fails,
// the page must stop showing the previous range's payload. handleQuery used
// to only set the error banner, so a failed range change left the summary
// cards, both pies, the optimization tips and the session table populated
// with the PREVIOUS range under a lone error banner - the same defect class
// the security overview cards got fixed for (#5313) and the conversation
// list got fixed for (#5537).
//
// The dashboard has no component-test harness, so - like the conversation
// failed-query suite - these tests transpile the real page with the
// dashboard's babel toolchain and drive it against a minimal hooks driver
// with deferred fetch stubs whose responses resolve in a controlled order.

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

function renderSavingsPage() {
  const { calls, stubs } = deferredFetchStubs([
    'fetchTokenSavings',
    'fetchAgentNames',
  ]);
  const driver = createHooksDriver();
  const moduleStubs = {
    'react-router-dom': {
      useSearchParams: () => [new URLSearchParams(''), () => undefined],
    },
    recharts: {
      ...componentStub('PieChart'), ...componentStub('Pie'), ...componentStub('Cell'),
      ...componentStub('ResponsiveContainer'),
    },
    '../utils/apiClient': { ...stubs },
    '../utils/savingsCsv': { downloadSavingsCsv: () => undefined },
    '../components/DateTimePicker': componentStub('DateTimePicker'),
    '../components/SessionIdHelp': componentStub('SessionIdHelp'),
    '../i18n': {
      useI18n: () => ({ t: (key) => key }),
      useLocaleTag: () => 'en',
    },
  };
  const pageModule = loadPageModule('src/pages/TokenSavingsPage.tsx', moduleStubs, driver);
  const page = pageModule.TokenSavingsPage;
  assert.equal(typeof page, 'function', 'TokenSavingsPage must be a component');
  const rendered = driver.render(page);
  const handleQuery = rendered.callbacks.find(
    (cb) => String(cb).includes('fetchTokenSavings'),
  );
  assert.ok(handleQuery, 'the page must expose its query callback');
  return { calls, driver, page, rendered, handleQuery };
}

/** Resolve one successful handleQuery fetch. */
async function resolveQuery(calls, index, payload) {
  calls.fetchTokenSavings[index].resolve(payload);
  await settle();
  await settle();
}

/** Reject one failed handleQuery fetch. */
async function rejectQuery(calls, index, message) {
  calls.fetchTokenSavings[index].reject(new Error(message));
  await settle();
  await settle();
}

const rangeA = {
  stats_available: true,
  summary: {
    total_input_tokens: 5000,
    total_output_tokens: 2000,
    total_tokens: 7000,
    baseline_tokens: 10000,
    total_saved_tokens: 3000,
    total_compounded_saved: 3000,
    savings_rate: 0.3,
    compounded_savings_rate: 0.3,
    total_tool_saved: 1800,
    total_mcp_saved: 1200,
    total_compounded_tool_saved: 1800,
    total_compounded_mcp_saved: 1200,
  },
  sessions: [{
    session_id: 'sess-A',
    agent_name: 'agent-A',
    total_input_tokens: 5000,
    total_output_tokens: 2000,
    total_tokens: 7000,
    baseline_tokens: 10000,
    saved_tokens: 3000,
    compounded_saved: 3000,
    savings_rate: 0.3,
    compounded_savings_rate: 0.3,
    optimization_items: [],
  }],
  optimization_tips: [{ level: 'success', title: 'tip-A', description: 'tip body' }],
};

test('a failed query drops the previous range payload', async () => {
  const { calls, driver, handleQuery } = renderSavingsPage();

  // Range A answers with a full payload.
  const first = handleQuery();
  await resolveQuery(calls, 0, rangeA);
  await first;
  await settle();

  const sessionsSlot = driver.slots.findIndex(
    (slot) => Array.isArray(slot.value) && slot.value[0] && slot.value[0].session_id === 'sess-A',
  );
  assert.ok(sessionsSlot >= 0, 'range A sessions must land in a slot');
  const summarySlot = driver.slots.findIndex(
    (slot) => slot.value && slot.value.total_tokens === 7000 && slot.value.baseline_tokens === 10000,
  );
  assert.ok(summarySlot >= 0, 'range A summary must land in a slot');
  const tipsSlot = driver.slots.findIndex(
    (slot) => Array.isArray(slot.value) && slot.value[0] && slot.value[0].title === 'tip-A',
  );
  assert.ok(tipsSlot >= 0, 'range A optimization tips must land in a slot');

  // Range B fails on the savings fetch.
  const second = handleQuery();
  assert.equal(calls.fetchTokenSavings.length, 2, 'the second query must fetch savings');
  await rejectQuery(calls, 1, 'boom');
  await second;
  await settle();

  // The newest request failed: nothing from range A may stay on screen under
  // the error banner.
  assert.deepEqual(
    driver.slots[sessionsSlot].value,
    [],
    'the session table must not keep the previous range after a failed query',
  );
  assert.equal(
    driver.slots[summarySlot].value,
    null,
    'the summary cards must not keep the previous range after a failed query',
  );
  assert.deepEqual(
    driver.slots[tipsSlot].value,
    [],
    'the optimization tips must not keep the previous range after a failed query',
  );

  // And the error must still be reported for the newest request.
  const errorSlot = driver.slots.findIndex((slot) => slot.value === 'boom');
  assert.ok(errorSlot >= 0, 'the failed query must surface its error message');
});

test('a newer successful query still wins after an older one failed', async () => {
  const { calls, driver, handleQuery } = renderSavingsPage();

  // Query A fails first.
  const first = handleQuery();
  await rejectQuery(calls, 0, 'boom');
  await first;
  await settle();

  // Query B succeeds and must populate the page.
  const second = handleQuery();
  await resolveQuery(calls, 1, rangeA);
  await second;
  await settle();

  const sessionsSlot = driver.slots.findIndex(
    (slot) => Array.isArray(slot.value) && slot.value[0] && slot.value[0].session_id === 'sess-A',
  );
  assert.ok(sessionsSlot >= 0, 'the successful newer query must populate the session table');
  const errorValues = driver.slots.filter((slot) => slot.value === 'boom');
  assert.deepEqual(errorValues, [], 'the older failure must not pin an error over the newer success');
});

test('source pin: handleQuery must clear the range state when the newest request fails', () => {
  const source = readFileSync(join(process.cwd(), 'src/pages/TokenSavingsPage.tsx'), 'utf8');
  const start = source.indexOf('} catch (e: any) {');
  const end = source.indexOf('} finally {', start);
  assert.ok(start >= 0 && end > start, 'handleQuery catch block must be found');
  const catchBlock = source.slice(start, end);
  for (const setter of ['setSessions([])', 'setSummary(null)', 'setStatsAvailable(true)', 'setTips([])']) {
    assert.ok(
      catchBlock.includes(setter),
      `handleQuery's failure path must clear the range payload (${setter})`,
    );
  }
  assert.ok(
    catchBlock.includes('requestId === loadRequestIdRef.current'),
    'the clearing must be gated on the newest request',
  );
});
