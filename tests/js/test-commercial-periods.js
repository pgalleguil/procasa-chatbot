const assert = require('assert');
const fs = require('fs');
const path = require('path');
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
assert.strictEqual(periods.validComparison('auto'), 'auto');
assert.strictEqual(periods.validComparison('bogus'), null);
assert.strictEqual(periods.canonicalPreset('2026-07-21', '2026-07-21', 'today'), 'today');
assert.strictEqual(periods.canonicalPreset('2026-07-10', '2026-07-15', 'today'), 'custom');
assert.strictEqual(periods.canonicalPreset('2026-06-22', '2026-07-21', '30d'), '30d');
for (let d = new Date('2024-01-01T16:00:00-03:00'); d <= new Date('2028-12-31T16:00:00-03:00'); d.setUTCDate(d.getUTCDate() + 1)) {
  for (const preset of ['today', 'week', 'month', '30d']) {
    const range = periods.presetRange(preset, d);
    assert.ok(range.start <= range.end, `${preset} ${d.toISOString()}`);
    if (preset === '30d') assert.strictEqual((Date.parse(range.end) - Date.parse(range.start)) / 86400000 + 1, 30);
  }
}
assert.strictEqual(periods.comparisonLabel('none', 'S/I'), '');
assert.strictEqual(periods.comparisonLabel('prev', '20 de julio de 2026'), 'vs. 20 de julio de 2026');
assert.strictEqual(periods.comparisonLabel('yoy', '21 de julio de 2025'), 'vs. 21 de julio de 2025 (año anterior)');
const dashboard = fs.readFileSync(path.join(__dirname, '../../templates/analytics/commercial_dashboard.html'), 'utf8');
assert.match(dashboard, /\.cd-kpi-sla\{grid-column:1\/-1;min-height:0\}/);
assert.match(dashboard, /class="cd-kpi cd-kpi-sla"/);
console.log('commercial period tests passed');
