(function () {
    'use strict';

    const state = { leadId: null, phone: null, assignmentCycleId: null, resultType: null,
        managementRequestId: null, row: null, onSuccess: null, onStale: null, closeOnStale: false };
    const dateResults = [];
    const reasonResults = ['NOT_INTERESTED', 'INVALID_NUMBER'];
    const channelResults = ['CALL_NO_ANSWER', 'MESSAGE_SENT_WAITING_RESPONSE'];
    const $ = id => document.getElementById(id);
    const requestId = () => crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
    const setExtra = (id, visible) => $(id)?.classList.toggle('is-visible', visible);
    const resultLabel = {
        CALL_NO_ANSWER: 'No respondió', MESSAGE_SENT_WAITING_RESPONSE: 'Mensaje enviado',
        EFFECTIVE_CONTACT: 'Contactado', FOLLOW_UP_REQUESTED: 'En seguimiento',
        VISIT_SCHEDULED: 'Visita agendada', NOT_INTERESTED: 'No interesado',
        INVALID_NUMBER: 'Número inválido', CLOSED_WON: 'Cerrado ganado',
        CLOSED_LOST: 'Cerrado perdido', DISCARDED_VALID_REASON: 'Descartado'
    };

    function configure(resultType) {
        state.resultType = resultType;
        document.querySelectorAll('[data-quick-result]').forEach(button =>
            button.classList.toggle('is-selected', button.dataset.quickResult === resultType));
        setExtra('quickChannelField', channelResults.includes(resultType));
        setExtra('quickDateField', dateResults.includes(resultType));
        setExtra('quickReasonField', reasonResults.includes(resultType));
        const reason = $('quickReason');
        if (reason) {
            const invalidReasons = new Set(['Número no existe', 'Número equivocado', 'No corresponde al cliente']);
            [...reason.options].forEach(option => {
                option.hidden = resultType === 'INVALID_NUMBER'
                    ? (!invalidReasons.has(option.value) && option.value !== '')
                    : invalidReasons.has(option.value);
            });
            reason.value = '';
        }
        if ($('quickManagementSave')) $('quickManagementSave').disabled = false;
    }

    function valid() {
        if (!state.resultType) return false;
        if (dateResults.includes(state.resultType) && !$('quickNextDate')?.value) return false;
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
        $('quickNextDate').value = '';
        $('quickReason').value = '';
        $('quickChannel').value = '';
        error('');
        document.querySelectorAll('[data-quick-result]').forEach(item => item.classList.remove('is-selected'));
        ['quickChannelField', 'quickDateField', 'quickReasonField'].forEach(id => setExtra(id, false));
        $('quickManagementSave').disabled = true;
        const detail = $('quickGoDetail');
        if (detail) {
            const returnUrl = `${window.location.pathname}${window.location.search}`;
            const detailUrl = context.detailUrl || context.row?.dataset?.leadUrl || '#';
            detail.href = detailUrl === '#' ? '#' : `${detailUrl}${detailUrl.includes('?') ? '&' : '?'}return_url=${encodeURIComponent(returnUrl)}`;
        }
        bootstrap.Modal.getOrCreateInstance($('quickManagementModal')).show();
    }

    async function save() {
        const button = $('quickManagementSave');
        if (!valid() || button.disabled) { error('Completa el dato adicional solicitado antes de guardar.'); return; }
        button.disabled = true;
        $('quickManagementProgress').textContent = 'Guardando…';
        error('');
        const details = {};
        if ($('quickChannel').value) details.channel = $('quickChannel').value;
        if ($('quickReason').value.trim()) details.reason = $('quickReason').value.trim();
        const submittedId = state.managementRequestId;
        const payload = { lead_id: state.leadId, phone: state.phone, assignment_cycle_id: state.assignmentCycleId,
            management_request_id: submittedId, idempotency_key: submittedId, result_type: state.resultType,
            next_follow_up_at: $('quickNextDate').value || null, details_json: details };
        try {
            if (window.CRM_REVIEW_MODE) {
                const result = state.resultType;
                state.managementRequestId = null;
                $('quickManagementProgress').textContent = 'Gestión registrada';
                bootstrap.Modal.getOrCreateInstance($('quickManagementModal')).hide();
                state.onSuccess?.(result, resultLabel[result]);
                window.CRM_REVIEW_NOTICE?.('Gestión registrada (simulada).');
                return;
            }
            const response = await fetch('/api/crm/management-result', { method: 'POST',
                headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload) });
            if (response.status === 409) throw new Error('STALE_ASSIGNMENT_CYCLE');
            if (!response.ok) throw new Error('SERVER_ERROR');
            const result = state.resultType;
            state.managementRequestId = null;
            $('quickManagementProgress').textContent = 'Gestión registrada';
            bootstrap.Modal.getOrCreateInstance($('quickManagementModal')).hide();
            state.onSuccess?.(result, resultLabel[result]);
        } catch (exception) {
            button.disabled = false;
            $('quickManagementProgress').textContent = '';
            const stale = exception.message === 'STALE_ASSIGNMENT_CYCLE';
            error(stale ? 'Este lead cambió de asignación. Actualizamos su información.' : 'No se pudo registrar la gestión. Puedes reintentarlo.');
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
    window.CRMQuickManagement = { open, save, configure, state, resultLabel };
}());
