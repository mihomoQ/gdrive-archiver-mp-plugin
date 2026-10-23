'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const fragment = fs.readFileSync(path.join(__dirname, '../plugins.v3/gdrivearchiverbridge/__init__.py'), 'utf8');
const source = fragment.match(/HOME_DASHBOARD_ACTION = r'''([\s\S]*?)'''/)[1]
  .replace('__DASHBOARD_ID__', JSON.stringify('GDriveArchiverBridge:archive'));
const widget = 'GDriveArchiverBridge:archive';
const keys = ['Dashboard', 'DashboardGridLayout', 'DashboardGridLayoutTablet', 'DashboardGridLayoutMobile'];
const storageKeys = ['MP_DASHBOARD', 'MP_DASHBOARD_GRID_LAYOUT', 'MP_DASHBOARD_GRID_LAYOUT_TABLET', 'MP_DASHBOARD_GRID_LAYOUT_MOBILE'];
const clone = x => JSON.parse(JSON.stringify(x));
const action = new Function('model', 'window', 'Date', `return (${source})();`);

function fixture(options = {}) {
  const server = options.server || {};
  const local = new Map(Object.entries(options.local || {}).map(([k, v]) => [k, JSON.stringify(v)]));
  const writes = [];
  const reads = [];
  const model = {home_busy: false, home_error: false, home_message: ''};
  const window = {
    localStorage: {
      getItem(key) { return local.has(key) ? local.get(key) : null; },
      setItem(key, value) {
        if (options.failCacheWrite) throw new Error('quota');
        local.set(key, value);
      },
    },
    MoviePilotAPI: {
      async get(url) {
        reads.push(url);
        if (options.failGet === url) throw new Error('network');
        if (options.getBarrier) await options.getBarrier;
        if (url === 'plugin/dashboard/meta') {
          const result = options.noMeta ? [] : [{id: 'GDriveArchiverBridge', key: 'archive'}];
          return options.metaEnvelope ? {success: true, message: '', data: result} : result;
        }
        const key = url.split('/').at(-1);
        const value = Object.hasOwn(server, key) ? clone(server[key]) : null;
        const envelope = {success: true, message: '', data: {value}};
        if (options.mixed && key === keys[1]) return {value};
        if (options.mixed && key === keys[2]) return {data: envelope};
        return envelope;
      },
      async post(url, body) {
        writes.push({url, body: clone(body)});
        if (options.failPost === writes.length) return {success: false, message: 'rejected', data: null};
        if (options.missingAck) return {};
        server[url.split('/').at(-1)] = clone(body);
        const result = {success: true, message: '', data: null};
        return options.mixed && writes.length === 2 ? {data: result} : result;
      },
    },
  };
  return {window, model, server, local, writes, reads, run: () => action(model, window, {now: () => 10000})};
}

const cases = [];
function test(name, fn) { cases.push([name, fn]); }

test('preserves every existing widget position and other visibility choice', async () => {
  const existing = {cpu: {x: 1, y: 4, w: 3, h: 8}, OtherPlugin: {x: 4, y: 7, w: 8}};
  const f = fixture({server: {
    Dashboard: {cpu: false, storage: true, OtherPlugin: false},
    DashboardGridLayout: {items: existing, enabled: {cpu: false, storage: true}, updatedAt: 100, custom: 'keep'},
    DashboardGridLayoutTablet: {items: {cpu: {x: 0, y: 0, w: 6}}, enabled: {memory: false}, updatedAt: 200},
    DashboardGridLayoutMobile: {items: {storage: {x: 0, y: 3, w: 1}}, enabled: {storage: false}, updatedAt: 300},
  }});
  await f.run();
  assert.equal(f.model.home_error, false);
  assert.equal(f.writes.length, 4);
  assert.deepEqual(f.server.DashboardGridLayout.items, existing);
  assert.equal(f.server.DashboardGridLayout.custom, 'keep');
  assert.equal(f.server.DashboardGridLayout.enabled.cpu, false);
  assert.equal(f.server.DashboardGridLayoutTablet.enabled.memory, false);
  assert.equal(f.server.DashboardGridLayoutMobile.enabled.storage, false);
  assert.equal(f.server.Dashboard.OtherPlugin, false);
  for (const key of keys) assert.equal(key === 'Dashboard' ? f.server[key][widget] : f.server[key].enabled[widget], true);
  assert.ok(f.writes.every(x => !x.url.includes('DashboardOrder')));
  assert.match(f.model.home_message, /刷新/);
});

test('accepts both host envelopes and supported unwrapped/axios-shaped reads', async () => {
  const f = fixture({mixed: true, metaEnvelope: true});
  await f.run();
  assert.equal(f.model.home_error, false);
  assert.equal(f.writes.length, 4);
});

test('a failed GET produces zero writes', async () => {
  const f = fixture({failGet: 'user/config/DashboardGridLayoutMobile'});
  await f.run();
  assert.equal(f.writes.length, 0);
  assert.equal(f.model.home_error, true);
  assert.equal(f.model.home_busy, false);
});

test('partial POST failure is never announced as complete', async () => {
  const f = fixture({failPost: 3});
  await f.run();
  assert.equal(f.writes.length, 3);
  assert.equal(f.model.home_error, true);
  assert.match(f.model.home_message, /部分设置已保存/);
  assert.doesNotMatch(f.model.home_message, /已为当前用户显示到首页/);
  assert.equal(f.local.has(storageKeys[2]), false);
  assert.equal(f.local.has(storageKeys[3]), false);
});

test('missing save acknowledgement does not claim success', async () => {
  const f = fixture({missingAck: true});
  await f.run();
  assert.equal(f.model.home_error, true);
  assert.equal(f.writes.length, 1);
  assert.equal(f.local.size, 0);
});

test('new browser layout wins using the same timestamp rule as MP', async () => {
  const f = fixture({
    server: {DashboardGridLayout: {items: {cpu: {x: 1}}, enabled: {cpu: true}, updatedAt: 100}},
    local: {MP_DASHBOARD_GRID_LAYOUT: {items: {cpu: {x: 9, y: 7}}, enabled: {cpu: false}, updatedAt: 300}},
  });
  await f.run();
  assert.deepEqual(f.server.DashboardGridLayout.items, {cpu: {x: 9, y: 7}});
  assert.equal(f.server.DashboardGridLayout.enabled.cpu, false);
  assert.equal(JSON.parse(f.local.get(storageKeys[1])).enabled[widget], true);
});

test('new server geometry keeps browser enabled flags when server has no enabled map', async () => {
  const f = fixture({
    server: {DashboardGridLayout: {items: {cpu: {x: 6}}, updatedAt: 400}},
    local: {MP_DASHBOARD_GRID_LAYOUT: {items: {cpu: {x: 1}}, enabled: {cpu: false}, updatedAt: 300}},
  });
  await f.run();
  assert.deepEqual(f.server.DashboardGridLayout.items, {cpu: {x: 6}});
  assert.equal(f.server.DashboardGridLayout.enabled.cpu, false);
});

test('missing settings do not disable any default host widget or invent coordinates', async () => {
  const f = fixture();
  await f.run();
  for (const key of keys.slice(1)) {
    assert.deepEqual(f.server[key].items, {});
    assert.deepEqual(f.server[key].enabled, {[widget]: true});
  }
});

test('legacy flat layouts and explicit disabled flags are preserved', async () => {
  const f = fixture({server: {Dashboard: {cpu: false}, DashboardGridLayout: {storage: {x: 2, y: 8, w: 4}}}});
  await f.run();
  assert.deepEqual(f.server.DashboardGridLayout.items, {storage: {x: 2, y: 8, w: 4}});
  assert.equal(f.server.DashboardGridLayout.enabled.cpu, false);
});

test('unsupported layouts are rejected before writing anything', async () => {
  const f = fixture({server: {DashboardGridLayoutTablet: ['wrong']}});
  await f.run();
  assert.equal(f.model.home_error, true);
  assert.equal(f.writes.length, 0);
});

test('a plugin that is not yet enabled gets a clear save-first instruction', async () => {
  const f = fixture({noMeta: true});
  await f.run();
  assert.equal(f.model.home_error, true);
  assert.match(f.model.home_message, /请先保存并启用插件/);
  assert.equal(f.writes.length, 0);
});

test('double clicks share the busy guard', async () => {
  let release;
  const barrier = new Promise(resolve => {release = resolve;});
  const f = fixture({getBarrier: barrier});
  const first = f.run();
  assert.equal(f.model.home_busy, true);
  await f.run();
  assert.equal(f.reads.length, 5);
  release();
  await first;
  assert.equal(f.writes.length, 4);
  assert.equal(f.model.home_busy, false);
});

test('cache write failure is distinguished from acknowledged server success', async () => {
  const f = fixture({failCacheWrite: true});
  await f.run();
  assert.equal(f.model.home_error, false);
  assert.match(f.model.home_message, /浏览器缓存未同步/);
  assert.equal(f.writes.length, 4);
});

(async () => {
  for (const [name, fn] of cases) {
    await fn();
    process.stdout.write(`ok - ${name}\n`);
  }
  process.stdout.write(`${cases.length} homepage action tests passed\n`);
})().catch(error => { console.error(error); process.exitCode = 1; });
