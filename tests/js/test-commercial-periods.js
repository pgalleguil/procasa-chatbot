const assert = require('assert');
const periods = require('../../static/js/commercial-periods.js');

assert.deepStrictEqual(periods.presetRange('today', new Date('2026-07-22T01:30:00Z')), {start:'2026-07-21', end:'2026-07-21'});
assert.deepStrictEqual(periods.presetRange('week', new Date('2026-07-22T01:30:00Z')), {start:'2026-07-20', end:'2026-07-21'});
assert.deepStrictEqual(periods.presetRange('month', new Date('2026-07-22T01:30:00Z')), {start:'2026-07-01', end:'2026-07-21'});
assert.deepStrictEqual(periods.presetRange('30d', new Date('2026-07-22T01:30:00Z')), {start:'2026-06-22', end:'2026-07-21'});
assert.deepStrictEqual(periods.presetRange('30d', new Date('2027-01-01T02:30:00Z')), {start:'2026-12-02', end:'2026-12-31'});
assert.deepStrictEqual(periods.presetRange('month', new Date('2024-03-01T02:30:00Z')), {start:'2024-02-01', end:'2024-02-29'});
assert.deepStrictEqual(periods.clampRange('2026-06-22', '2026-07-22', new Date('2026-07-22T01:30:00Z')), {start:'2026-06-22', end:'2026-07-21'});
assert.deepStrictEqual(periods.clampRange('2026-07-22', '2026-07-21', new Date('2026-07-22T01:30:00Z')), {start:'2026-07-21', end:'2026-07-21'});
assert.strictEqual(periods.validPreset('today'), 'today');
assert.strictEqual(periods.validPreset('week'), 'week');
assert.strictEqual(periods.validPreset('invalid'), null);
assert.strictEqual(periods.comparisonLabel('none', 'S/I'), '');
assert.strictEqual(periods.comparisonLabel('prev', '20 de julio de 2026'), 'vs. 20 de julio de 2026');
assert.strictEqual(periods.comparisonLabel('yoy', '21 de julio de 2025'), 'vs. 21 de julio de 2025 (año anterior)');
console.log('commercial period tests passed');
