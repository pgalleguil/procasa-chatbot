// Real-time clocks are not needed: advance a deterministic fake browser clock.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const SOURCE = fs.readFileSync(
    path.join(__dirname, '../../static/js/crm_session_activity.js'), 'utf8'
);
const MINUTE = 60 * 1000;

function makeBrowser(renewResponse = { ok: true, status: 200 }) {
    let now = 0;
    let timerId = 0;
    const timers = new Map();
    const events = new Map();
    const windowEvents = new Map();
    const redirects = [];
    const requests = [];
    const modalCalls = { show: 0, hide: 0 };
    const timeElement = { textContent: '' };
    const modalElement = {};

    function addTimer(fn, duration, repeating) {
        const id = ++timerId;
        timers.set(id, { at: now + Math.max(1, duration), fn, period: repeating ? duration : 0 });
        return id;
    }

    const clock = {
        advance(duration) {
            const until = now + duration;
            while (true) {
                const active = [...timers.entries()]
                    .filter(([, t]) => t.at <= until)
                    .sort((a, b) => a[1].at - b[1].at || a[0] - b[0])[0];
                if (!active) break;
                const [id, timer] = active;
                now = timer.at;
                if (timer.period) timer.at += timer.period;
                else timers.delete(id);
                timer.fn();
            }
            now = until;
        },
        get now() { return now; },
        timers
    };

    const document = {
        hidden: false,
        getElementById(id) {
            return id === 'sessionWarningModal' ? modalElement :
                id === 'sessionTimeLeft' ? timeElement : null;
        },
        addEventListener(type, callback) {
            const handlers = events.get(type) || [];
            handlers.push(callback);
            events.set(type, handlers);
        }
    };
    const bootstrap = {
        Modal: {
            getOrCreateInstance() {
                return {
                    show() { modalCalls.show++; },
                    hide() { modalCalls.hide++; }
                };
            }
        }
    };
    const window = {
        bootstrap,
        location: { assign(url) { redirects.push(url); } },
        addEventListener(type, callback) {
            const handlers = windowEvents.get(type) || [];
            handlers.push(callback);
            windowEvents.set(type, handlers);
        }
    };
    const context = {
        Date: { now: () => now },
        document,
        window,
        bootstrap,
        fetch(url, options) {
            requests.push({ url, options, at: now });
            return Promise.resolve(renewResponse);
        },
        setTimeout: (cb, delay) => addTimer(cb, delay, false),
        clearTimeout: id => timers.delete(id),
        setInterval: (cb, delay) => addTimer(cb, delay, true),
        clearInterval: id => timers.delete(id)
    };
    vm.runInNewContext(SOURCE, context, { filename: 'crm_session_activity.js' });
    return {
        clock,
        document,
        window,
        requests,
        redirects,
        modalCalls,
        timeElement,
        fire(type) { for (const listener of events.get(type) || []) listener(); },
        fireWindow(type, data) { for (const listener of windowEvents.get(type) || []) listener(data); }
    };
}

async function drainPromises() {
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
}

test('only one local watchdog at five minutes, and a precise warning timeout', () => {
    const { clock, requests } = makeBrowser();
    const intervals = [...clock.timers.values()].filter(timer => timer.period);
    assert.equal(intervals.length, 1);
    assert.equal(intervals[0].period, 5 * MINUTE);
    assert.equal([...clock.timers.values()].filter(timer => !timer.period).length, 1);
    clock.advance(60 * MINUTE);
    assert.equal(requests.length, 0);
});

test('warning arrives at minute 118 and session expires at minute 120', () => {
    const app = makeBrowser();
    app.clock.advance(118 * MINUTE);
    assert.equal(app.modalCalls.show, 1);
    assert.equal(app.timeElement.textContent, '120s');
    assert.deepEqual(app.redirects, []);
    app.clock.advance(119 * 1000);
    assert.equal(app.timeElement.textContent, '1s');
    app.clock.advance(1000);
    assert.deepEqual(app.redirects, ['/logout?reason=timeout']);
});

test('activity after warning cancels it and prevents premature logout', () => {
    const app = makeBrowser();
    app.clock.advance(119 * MINUTE);
    assert.equal(app.modalCalls.show, 1);
    app.fire('keydown');
    assert.equal(app.modalCalls.hide, 1);
    app.clock.advance(3 * MINUTE);
    assert.deepEqual(app.redirects, []);
    assert.equal(app.modalCalls.show, 1);
    app.clock.advance(115 * MINUTE);
    assert.equal(app.modalCalls.show, 2);
    assert.deepEqual(app.redirects, []);
});

test('background tab has no second-by-second counter and still expires', () => {
    const app = makeBrowser();
    app.document.hidden = true;
    app.clock.advance(118 * MINUTE);
    assert.equal(app.modalCalls.show, 0);
    assert.equal([...app.clock.timers.values()].filter(t => t.period === 1000).length, 0);
    app.clock.advance(2 * MINUTE);
    assert.deepEqual(app.redirects, ['/logout?reason=timeout']);
});

test('renewal requires user activity and is at least ten minutes apart', async () => {
    const app = makeBrowser();
    app.clock.advance(10 * MINUTE);
    assert.equal(app.requests.length, 0);
    app.fire('click');
    assert.equal(app.requests.length, 1);
    assert.equal(app.requests[0].url, '/api/session/renew');
    assert.equal(app.requests[0].options.method, 'POST');
    await drainPromises();
    app.clock.advance(9 * MINUTE);
    app.fire('keydown');
    assert.equal(app.requests.length, 1);
    app.clock.advance(1 * MINUTE);
    await drainPromises();
    assert.equal(app.requests.length, 2);
    assert.ok(app.requests[1].at - app.requests[0].at >= 10 * MINUTE);
});

test('no renewals in a hidden tab and no renewal storms on errors', async () => {
    const app = makeBrowser({ ok: false, status: 503 });
    app.clock.advance(10 * MINUTE);
    app.document.hidden = true;
    app.fire('keydown');
    assert.equal(app.requests.length, 0);
    app.document.hidden = false;
    app.fire('click');
    assert.equal(app.requests.length, 1);
    await drainPromises();
    app.clock.advance(4 * MINUTE);
    app.fire('click');
    assert.equal(app.requests.length, 1);
    app.clock.advance(MINUTE);
    app.fire('click');
    assert.equal(app.requests.length, 2);
});

test('401 renewal immediately invalidates the client session', async () => {
    const app = makeBrowser({ ok: false, status: 401 });
    app.clock.advance(10 * MINUTE);
    app.fire('click');
    await drainPromises();
    assert.deepEqual(app.redirects, ['/logout?reason=timeout']);
});

test('suspended tabs validate elapsed inactivity upon return', () => {
    const app = makeBrowser();
    app.clock.advance(121 * MINUTE);
    assert.equal(app.redirects.length, 1);
    app.fireWindow('pageshow', { persisted: true });
    assert.equal(app.redirects.length, 1);
});
