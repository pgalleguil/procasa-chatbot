(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  root.CommercialPeriods = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';
  const TIME_ZONE = 'America/Santiago';

  function parts(now) {
    const values = new Intl.DateTimeFormat('en-CA', {
      timeZone: TIME_ZONE, year: 'numeric', month: '2-digit', day: '2-digit'
    }).formatToParts(now || new Date()).reduce((out, part) => {
      if (part.type !== 'literal') out[part.type] = Number(part.value);
      return out;
    }, {});
    return { year: values.year, month: values.month, day: values.day };
  }

  function iso(p) {
    return String(p.year).padStart(4, '0') + '-' + String(p.month).padStart(2, '0') + '-' + String(p.day).padStart(2, '0');
  }

  function utcDate(p) { return new Date(Date.UTC(p.year, p.month - 1, p.day)); }
  function fromUtc(d) { return { year: d.getUTCFullYear(), month: d.getUTCMonth() + 1, day: d.getUTCDate() }; }
  function addDays(p, days) { const d = utcDate(p); d.setUTCDate(d.getUTCDate() + days); return fromUtc(d); }
  function weekday(p) { return utcDate(p).getUTCDay(); }

  function presetRange(preset, now) {
    const end = parts(now);
    let start = end;
    if (preset === 'week') start = addDays(end, -(weekday(end) === 0 ? 6 : weekday(end) - 1));
    else if (preset === 'month') start = { year: end.year, month: end.month, day: 1 };
    else if (preset === '30d') start = addDays(end, -29);
    return { start: iso(start), end: iso(end) };
  }

  function clampRange(start, end, now) {
    const today = iso(parts(now));
    const safeEnd = end > today ? today : end;
    return { start: start > safeEnd ? safeEnd : start, end: safeEnd };
  }

  function validPreset(value) {
    return ['today', 'week', 'month', '30d', 'custom'].includes(value) ? value : null;
  }

  function comparisonLabel(mode, formattedRange) {
    if (mode === 'none') return '';
    return 'vs. ' + formattedRange + (mode === 'yoy' ? ' (año anterior)' : '');
  }

  return { TIME_ZONE, parts, iso, addDays, presetRange, clampRange, validPreset, comparisonLabel };
});
