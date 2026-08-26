(function () {
    'use strict';

    const state = { leadId: null, phone: null, assignmentCycleId: null, resultType: null,
        managementRequestId: null, row: null, onSuccess: null, onStale: null, closeOnStale: false };
    const dateResults = ['EFFECTIVE_CONTACT', 'CALL_NO_ANSWER', 'VISIT_SCHEDULED'];
    const noteResults = ['EFFECTIVE_CONTACT', 'CALL_NO_ANSWER', 'VISIT_SCHEDULED'];
    const reasonResults = ['NOT_INTERESTED'];
    const MIN_SCHEDULE_LEAD_MINUTES = 1;
    const reasonOptions = {
        NOT_INTERESTED: new Set(['Ya no busca', 'Esta propiedad no le interesa', 'Precio o condiciones', 'Ya resolvió']),
    };
    const channelResults = [];
    const otherResults = ['OTHER_EXPLICIT'];
    const $ = id => document.getElementById(id);
    const requestId = () => crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
    const setExtra = (id, visible) => $(id)?.classList.toggle('is-visible', visible);
    const followUpEnabled = () => Boolean($('quickFollowUpToggle')?.checked);
    const pad = value => String(value).padStart(2, '0');
    let quickDateInstance = null;

    function syncFlatpickrTheme(theme = document.documentElement.getAttribute('data-theme')) {
        const darkTheme = $('flatpickrDarkTheme');
        const lightTheme = $('flatpickrLightTheme');
        if (!darkTheme || !lightTheme) return;
        const isLight = theme === 'light';
        darkTheme.disabled = isLight;
        lightTheme.disabled = !isLight;
    }

    function initQuickDatePicker() {
        const input = $('quickNextDate');
        if (!input || typeof window.flatpickr !== 'function' || quickDateInstance) return;
        quickDateInstance = window.flatpickr(input, {
            enableTime: true,
            dateFormat: 'Y-m-d\\TH:i',
            minDate: new Date(Date.now() + MIN_SCHEDULE_LEAD_MINUTES * 60000),
            locale: 'es',
            time_24hr: true,
            disableMobile: true,
            onChange: () => {
                input.dispatchEvent(new Event('input', { bubbles: true }));
                input.dispatchEvent(new Event('change', { bubbles: true }));
            }
        });
        syncFlatpickrTheme();
    }

    function refreshScheduleMinimum() {
        const input = $('quickNextDate');
        if (!input) return;
        const minimum = new Date(Math.ceil((Date.now() + MIN_SCHEDULE_LEAD_MINUTES * 60000) / 60000) * 60000);
        input.min = `${minimum.getFullYear()}-${pad(minimum.getMonth() + 1)}-${pad(minimum.getDate())}T${pad(minimum.getHours())}:${pad(minimum.getMinutes())}`;
        quickDateInstance?.set('minDate', minimum);
    }
    function scheduleTooSoon() {
        const value = $('quickNextDate')?.value;
        if (!value) return false;
        const scheduled = new Date(value).getTime();
        return !Number.isFinite(scheduled) || scheduled < Date.now() + MIN_SCHEDULE_LEAD_MINUTES * 60000;
    }
    function scheduleLeadMessage() {
        return `La fecha y hora debe ser al menos ${MIN_SCHEDULE_LEAD_MINUTES} minuto${MIN_SCHEDULE_LEAD_MINUTES === 1 ? '' : 's'} a futuro.`;
    }
    function syncDateValidation() {
        const input = $('quickNextDate');
        const picker = $('quickDatePicker');
        const message = $('quickDateError');
        const invalid = Boolean(input?.value && scheduleTooSoon());
        picker?.classList.toggle('is-invalid', invalid);
        input?.classList.toggle('is-invalid', invalid);
        if (message) {
            message.textContent = invalid ? scheduleLeadMessage() : '';
            message.classList.toggle('d-none', !invalid);
        }
    }
    function syncDatePickerDisplay() {
        const input = $('quickNextDate');
        const display = $('quickDateDisplay');
        if (!input || !display) return;
        const value = input.value || '';
        if (!value) {
            display.textContent = 'Seleccionar fecha y hora';
            display.classList.remove('has-value');
            return;
        }
        const [datePart, timePart = ''] = value.split('T');
        const [year, month, day] = datePart.split('-');
        display.textContent = `${day}/${month}/${year}${timePart ? ` · ${timePart}` : ''}`;
        display.classList.add('has-value');
    }
    let datePickerOpening = false;
    function openDatePicker() {
        const input = $('quickNextDate');
        if (!input || datePickerOpening) return;
        datePickerOpening = true;
        try {
            initQuickDatePicker();
            if (quickDateInstance) quickDateInstance.open();
            else if (typeof input.showPicker === 'function') input.showPicker();
            else { input.focus(); input.click(); }
        } catch (_) {
            input.focus();
        } finally {
            window.setTimeout(() => { datePickerOpening = false; }, 0);
        }
    }
    const resultLabel = {
        CALL_NO_ANSWER: 'Sin respuesta', MESSAGE_SENT_WAITING_RESPONSE: 'Mensaje enviado',
        EFFECTIVE_CONTACT: 'Contactado', FOLLOW_UP_REQUESTED: 'En seguimiento',
        VISIT_SCHEDULED: 'Visita agendada', NOT_INTERESTED: 'No interesado', PROPERTY_UNAVAILABLE: 'Propiedad no disponible',
        INVALID_NUMBER: 'Número inválido', CLOSED_WON: 'Cerrado ganado',
        CLOSED_LOST: 'Cerrado perdido', DISCARDED_VALID_REASON: 'Descartado', OTHER_EXPLICIT: 'Otro'
    };
    const escapeHtml = value => String(value || '').replace(/[&<>'"]/g, character => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;'}[character]));

    function syncSaveState() {
        const save = $('quickManagementSave');
        if (!save) return;
        syncDateValidation();
        const requiresDate = state.resultType === 'VISIT_SCHEDULED'
            || (['EFFECTIVE_CONTACT', 'CALL_NO_ANSWER'].includes(state.resultType) && followUpEnabled());
        const requiresReason = state.resultType === 'NOT_INTERESTED';
        const requiresOther = otherResults.includes(state.resultType);
        save.disabled = !state.resultType
            || (requiresDate && (!$('quickNextDate')?.value || scheduleTooSoon()))
            || (requiresReason && !$('quickReason')?.value)
            || (requiresOther && !$('quickOtherOutcome')?.value.trim());
    }

    function configure(resultType) {
        state.resultType = resultType;
        document.querySelectorAll('[data-quick-result]').forEach(button =>
            button.classList.toggle('is-selected', button.dataset.quickResult === resultType));
        setExtra('quickChannelField', channelResults.includes(resultType));
        const canScheduleFollowUp = ['EFFECTIVE_CONTACT', 'CALL_NO_ANSWER'].includes(resultType);
        const isVisit = resultType === 'VISIT_SCHEDULED';
        const showNotes = noteResults.includes(resultType);
        setExtra('quickFollowUpField', canScheduleFollowUp);
        setExtra('quickDateField', isVisit || (canScheduleFollowUp && followUpEnabled()));
        setExtra('quickNotesField', showNotes);
        if (!showNotes && $('quickNotes')) $('quickNotes').value = '';
        if ($('quickDateLabel')) $('quickDateLabel').textContent = isVisit ? 'Fecha y hora de la visita' : 'Próximo contacto';
        if ($('quickDateHelp')) $('quickDateHelp').textContent = isVisit
            ? 'Se enviará un recordatorio por WhatsApp una hora antes de la visita.'
            : 'Se enviará un recordatorio por WhatsApp al ejecutivo asignado en esta fecha y hora.';
        setExtra('quickReasonField', reasonResults.includes(resultType));
        setExtra('quickOtherField', otherResults.includes(resultType));
        const reason = $('quickReason');
        if (reason) {
            const allowedReasons = reasonOptions[resultType] || new Set();
            [...reason.options].forEach(option => {
                option.hidden = option.value !== '' && !allowedReasons.has(option.value);
            });
            reason.value = '';
        }
        syncSaveState();
    }

    function valid() {
        if (!state.resultType) return false;
        if (['EFFECTIVE_CONTACT', 'CALL_NO_ANSWER'].includes(state.resultType)
            && followUpEnabled() && (!$('quickNextDate')?.value || scheduleTooSoon())) return false;
        if (state.resultType === 'VISIT_SCHEDULED'
            && (!$('quickNextDate')?.value || scheduleTooSoon())) return false;
        if (state.resultType === 'NOT_INTERESTED' && !$('quickReason')?.value) return false;
        if (otherResults.includes(state.resultType) && !$('quickOtherOutcome')?.value.trim()) return false;
        return true;
    }

    function error(message) {
        const target = $('quickManagementError');
        if (!target) return;
        target.textContent = message || '';
        target.classList.toggle('is-visible', Boolean(message));
    }

    function open(context) {
        state.leadId = context.leadId || null;
        state.phone = context.phone || null;
        state.assignmentCycleId = context.assignmentCycleId || null;
        state.managementRequestId = requestId();
        state.resultType = null;
        state.row = context.row || null;
        state.onSuccess = context.onSuccess || null;
        state.onStale = context.onStale || null;
        state.closeOnStale = Boolean(context.closeOnStale);
        $('quickManagementLeadName').textContent = context.leadName || 'Lead';
        $('quickManagementProgress').textContent = '';
        if ($('quickNextDate')) $('quickNextDate').value = '';
        quickDateInstance?.clear();
        refreshScheduleMinimum();
        syncDatePickerDisplay();
        syncDateValidation();
        if ($('quickNotes')) $('quickNotes').value = '';
        if ($('quickFollowUpToggle')) $('quickFollowUpToggle').checked = false;
        if ($('quickDateLabel')) $('quickDateLabel').textContent = 'Próximo contacto';
        $('quickReason').value = '';
        if ($('quickOtherOutcome')) $('quickOtherOutcome').value = '';
        error('');
        document.querySelectorAll('[data-quick-result]').forEach(item => item.classList.remove('is-selected'));
        ['quickChannelField', 'quickFollowUpField', 'quickDateField', 'quickNotesField', 'quickReasonField', 'quickOtherField'].forEach(id => setExtra(id, false));
        $('quickManagementSave').disabled = true;
        const detail = $('quickGoDetail');
        if (detail) {
            const returnUrl = `${window.location.pathname}${window.location.search}`;
            const detailUrl = context.detailUrl || context.row?.dataset?.leadUrl || '#';
            const isAlreadyInDetail = detailUrl === '#';
            detail.href = isAlreadyInDetail ? '#' : `${detailUrl}${detailUrl.includes('?') ? '&' : '?'}return_url=${encodeURIComponent(returnUrl)}`;
            detail.hidden = isAlreadyInDetail;
        }
        bootstrap.Modal.getOrCreateInstance($('quickManagementModal')).show();
    }

    async function save() {
        const button = $('quickManagementSave');
        if (!valid() || button.disabled) {
            error(scheduleTooSoon()
                ? scheduleLeadMessage()
                : 'Completa el dato adicional solicitado antes de guardar.');
            return;
        }
        button.disabled = true;
        $('quickManagementProgress').textContent = 'Guardando…';
        error('');
        const details = {};
        const reason = $('quickReason').value.trim();
        if (reason && reason.toLocaleLowerCase() !== 'seleccionar motivo (opcional)') details.reason = reason;
        if ($('quickOtherOutcome').value.trim()) details.outcome = $('quickOtherOutcome').value.trim();
        if (noteResults.includes(state.resultType) && $('quickNotes')?.value.trim()) {
            details.notes = $('quickNotes').value.trim();
        }
        if (state.resultType === 'VISIT_SCHEDULED') details.visit_at = $('quickNextDate')?.value || null;
        const submittedId = state.managementRequestId;
        const payload = { lead_id: state.leadId, phone: state.phone, assignment_cycle_id: state.assignmentCycleId,
            management_request_id: submittedId, idempotency_key: submittedId, result_type: state.resultType,
            next_follow_up_at: $('quickNextDate')?.value || null, details_json: details };
        try {
            if (window.CRM_REVIEW_MODE) {
                const result = state.resultType;
                state.managementRequestId = null;
                $('quickManagementProgress').textContent = 'Respuesta registrada';
                bootstrap.Modal.getOrCreateInstance($('quickManagementModal')).hide();
                state.onSuccess?.(result, resultLabel[result], details);
                window.CRM_REVIEW_NOTICE?.('Gestión registrada (simulada).');
                return;
            }
            const response = await fetch('/api/crm/management-result', { method: 'POST',
                headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload) });
            let responseData = null;
            try { responseData = await response.json(); } catch (_) { /* empty response */ }
            if (response.status === 409) {
                throw new Error(responseData?.detail === 'closed_lead'
                    ? 'CLOSED_LEAD' : 'STALE_ASSIGNMENT_CYCLE');
            }
            if (!response.ok) {
                throw new Error(responseData?.detail === 'scheduled_time_too_soon'
                    ? 'SCHEDULE_TOO_SOON' : 'SERVER_ERROR');
            }
            const result = state.resultType;
            state.managementRequestId = null;
            $('quickManagementProgress').textContent = 'Respuesta registrada';
            bootstrap.Modal.getOrCreateInstance($('quickManagementModal')).hide();
            state.onSuccess?.(result, resultLabel[result], details);
        } catch (exception) {
            button.disabled = false;
            $('quickManagementProgress').textContent = '';
            const stale = exception.message === 'STALE_ASSIGNMENT_CYCLE';
            const closed = exception.message === 'CLOSED_LEAD';
            error(closed
                ? 'Este lead ya está cerrado y no admite nuevas gestiones.'
                : stale
                    ? 'Este lead cambió de asignación. Actualizamos su información.'
                    : exception.message === 'SCHEDULE_TOO_SOON'
                        ? scheduleLeadMessage()
                    : 'No se pudo registrar la gestión. Puedes reintentarlo.');
            if (stale) {
                if (state.closeOnStale) {
                    state.managementRequestId = null;
                    state.resultType = null;
                    bootstrap.Modal.getOrCreateInstance($('quickManagementModal')).hide();
                }
                state.onStale?.();
            }
        }
    }

    document.addEventListener('click', event => {
        const result = event.target.closest?.('[data-quick-result]');
        if (result) { event.preventDefault(); configure(result.dataset.quickResult); }
    });
    $('quickManagementSave')?.addEventListener('click', save);
    $('quickNextDate')?.addEventListener('input', () => {
        refreshScheduleMinimum();
        syncDatePickerDisplay();
        syncDateValidation();
        syncSaveState();
    });
    $('quickNextDate')?.addEventListener('change', syncDatePickerDisplay);
    $('quickDatePicker')?.addEventListener('pointerdown', event => {
        if (event.button === 0) openDatePicker();
    });
    $('quickDatePicker')?.addEventListener('click', openDatePicker);
    $('quickOtherOutcome')?.addEventListener('input', () => {
        syncSaveState();
    });
    $('quickReason')?.addEventListener('change', syncSaveState);
    $('quickFollowUpToggle')?.addEventListener('change', () => {
        refreshScheduleMinimum();
        const enabled = followUpEnabled();
        setExtra('quickDateField', enabled);
        setExtra('quickNotesField', noteResults.includes(state.resultType));
        if (!enabled) {
            if ($('quickNextDate')) $('quickNextDate').value = '';
        }
        syncSaveState();
    });
    // The shared script is loaded while the page is still being parsed.  On
    // slower connections Flatpickr can finish just after this file, so retry
    // after the document and window are ready as well.
    initQuickDatePicker();
    document.addEventListener('DOMContentLoaded', initQuickDatePicker, { once: true });
    window.addEventListener('load', initQuickDatePicker, { once: true });
    window.setTimeout(initQuickDatePicker, 0);
    new MutationObserver(() => syncFlatpickrTheme()).observe(document.documentElement, {
        attributes: true,
        attributeFilter: ['data-theme']
    });
    syncFlatpickrTheme();
    window.CRMQuickManagement = { open, save, configure, state, resultLabel };
}());
