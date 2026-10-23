/* Shared CRM session timer. Check inactivity once per minute; only the
 * last two minutes use a one-second countdown. Renew only for active users. */
(() => {
    'use strict';
    const SESSION_LIFETIME_MS = 120 * 60 * 1000;
    const WARNING_THRESHOLD_MS = 2 * 60 * 1000;
    const RENEWAL_INTERVAL_MS = 10 * 60 * 1000;
    const CHECK_INTERVAL_MS = 60 * 1000;
    const RETRY_INTERVAL_MS = 60 * 1000;
    let lastActivityAt = Date.now();
    let nextRenewalAt = Date.now() + RENEWAL_INTERVAL_MS;
    let renewalInFlight = false;
    let warningTimer = null;
    let expired = false;

    function expireSession() {
        if (expired) return;
        expired = true;
        window.location.assign('/logout?reason=timeout');
    }

    function modal() {
        const el = document.getElementById('sessionWarningModal');
        if (!el || !window.bootstrap?.Modal) return null;
        return bootstrap.Modal.getOrCreateInstance(el);
    }

    function hideWarning() {
        if (warningTimer !== null) {
            clearInterval(warningTimer);
            warningTimer = null;
        }
        modal()?.hide();
    }

    function maybeRenew(now) {
        if (expired || document.hidden || renewalInFlight) return;
        if (now - lastActivityAt >= RENEWAL_INTERVAL_MS || now < nextRenewalAt) return;
        renewalInFlight = true;
        // Throttle failures as well as successes; mouse activity cannot cause retries.
        nextRenewalAt = now + RETRY_INTERVAL_MS;
        fetch('/api/session/renew', { method: 'POST', credentials: 'same-origin' })
            .then(response => {
                if (response.status === 401) {
                    expireSession();
                } else if (response.ok) {
                    nextRenewalAt = Date.now() + RENEWAL_INTERVAL_MS;
                }
            })
            .catch(error => console.debug('[SESSION] Renovación fallida:', error))
            .finally(() => { renewalInFlight = false; });
    }

    function checkSession() {
        if (expired) return;
        const now = Date.now();
        const inactiveFor = now - lastActivityAt;
        if (inactiveFor >= SESSION_LIFETIME_MS) {
            expireSession();
            return;
        }
        const remaining = SESSION_LIFETIME_MS - inactiveFor;
        if (remaining <= WARNING_THRESHOLD_MS) {
            const text = document.getElementById('sessionTimeLeft');
            if (text) text.textContent = Math.ceil(remaining / 1000) + 's';
            modal()?.show();
            if (warningTimer === null) warningTimer = setInterval(checkSession, 1000);
        } else if (warningTimer !== null) {
            hideWarning();
        }
        maybeRenew(now);
    }

    // Compatibility with existing "Mantener Activa" buttons in the templates.
    window.updateActivity = function updateActivity() {
        if (expired) return;
        const now = Date.now();
        // Do not revive an already expired session via input after inactivity.
        if (now - lastActivityAt >= SESSION_LIFETIME_MS) {
            expireSession();
            return;
        }
        lastActivityAt = now;
        if (warningTimer !== null) hideWarning();
        maybeRenew(now);
    };

    ['mousemove', 'click', 'keydown', 'scroll', 'touchstart'].forEach(name => {
        document.addEventListener(name, window.updateActivity, { passive: true });
    });
    document.addEventListener('visibilitychange', () => {
        if (!document.hidden) checkSession();
    });
    window.addEventListener('pageshow', event => {
        if (event.persisted) checkSession();
    });
    setInterval(checkSession, CHECK_INTERVAL_MS);
})();
