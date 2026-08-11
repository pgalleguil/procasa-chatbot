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
    // “Semana” representa una ventana móvil de 7 días incluido hoy.
    if (preset === 'week') start = addDays(end, -6);
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

  function validComparison(value) {
    return ['auto', 'prev', 'yoy', 'none'].includes(value) ? value : null;
  }

  function canonicalPreset(start, end, declared) {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(start || '') || !/^\d{4}-\d{2}-\d{2}$/.test(end || '')) return 'custom';
    const s = utcDate({ year: +start.slice(0, 4), month: +start.slice(5, 7), day: +start.slice(8, 10) });
    const e = utcDate({ year: +end.slice(0, 4), month: +end.slice(5, 7), day: +end.slice(8, 10) });
    const days = Math.round((e - s) / 86400000) + 1;
    const matches = {
      today: days === 1,
      week: days === 7,
      month: s.getUTCDate() === 1 && s.getUTCFullYear() === e.getUTCFullYear() && s.getUTCMonth() === e.getUTCMonth(),
      '30d': days === 30,
      custom: true
    };
    return matches[declared] ? declared : 'custom';
  }

  function comparisonLabel(mode, formattedRange) {
    if (mode === 'none') return '';
    return 'vs. ' + formattedRange + (mode === 'yoy' ? ' (año anterior)' : '');
  }

  return { TIME_ZONE, parts, iso, addDays, presetRange, clampRange, validPreset, validComparison, canonicalPreset, comparisonLabel };
});
