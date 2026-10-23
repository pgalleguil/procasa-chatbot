/* Control de sesión CRM (sin polling de servidor cada 5 minutos).
 * El navegador comprueba la inactividad local cada cinco minutos y utiliza
 * un temporizador puntual para mostrar el aviso dos minutos antes del límite.
 * Solamente renueva la cookie si hubo actividad desde la última renovación.
 */
(function () {
    'use strict';

    const SESSION_LIFETIME_MS = 120 * 60 * 1000;
    const WARNING_THRESHOLD_MS = 2 * 60 * 1000;
    const CHECK_INTERVAL_MS = 5 * 60 * 1000;
    const RENEWAL_INTERVAL_MS = 10 * 60 * 1000;
    const RETRY_INTERVAL_MS = 5 * 60 * 1000;

    let lastActivityAt = Date.now();
    let nextRenewalAt = lastActivityAt + RENEWAL_INTERVAL_MS;
    let activitySinceRenewal = false;
    let renewalInFlight = false;
    let expired = false;
    let milestoneTimer = null;
    let warningCountdown = null;
    let warningVisible = false;

    function sessionModal() {
        const element = document.getElementById('sessionWarningModal');
        if (!element || !window.bootstrap || !bootstrap.Modal) return null;
        return bootstrap.Modal.getOrCreateInstance(element);
    }

    function expireSession() {
        if (expired) return;
        expired = true;
        window.location.assign('/logout?reason=timeout');
    }

    function hideWarning() {
        if (warningCountdown !== null) {
            clearInterval(warningCountdown);
            warningCountdown = null;
        }
        if (warningVisible) {
            warningVisible = false;
            sessionModal()?.hide();
        }
    }

    function scheduleMilestone(now) {
        if (expired || milestoneTimer !== null) return;
        const remaining = SESSION_LIFETIME_MS - (now - lastActivityAt);
        // When warning time is reached in a hidden tab, schedule expiration
        // directly instead of running a per-second background countdown.
        const untilMilestone = remaining > WARNING_THRESHOLD_MS
            ? remaining - WARNING_THRESHOLD_MS
            : remaining;
        milestoneTimer = setTimeout(() => {
            milestoneTimer = null;
            checkSession();
        }, Math.max(1, untilMilestone));
    }

    function renewWhenActive(now) {
        if (expired || document.hidden || renewalInFlight) return;
        if (!activitySinceRenewal || now < nextRenewalAt) return;
        if (now - lastActivityAt >= RENEWAL_INTERVAL_MS) return;

        renewalInFlight = true;
        activitySinceRenewal = false;
        // Limit error retries. Activity/mouse events must not trigger a flood.
        nextRenewalAt = now + RETRY_INTERVAL_MS;
        fetch('/api/session/renew', { method: 'POST', credentials: 'same-origin' })
            .then(response => {
                if (response.status === 401 || response.status === 403) {
                    expireSession();
                } else if (response.ok) {
                    nextRenewalAt = Date.now() + RENEWAL_INTERVAL_MS;
                } else {
                    activitySinceRenewal = true;
                }
            })
            .catch(() => {
                activitySinceRenewal = true;
            })
            .finally(() => {
                renewalInFlight = false;
            });
    }

    function checkSession() {
        if (expired) return;
        const now = Date.now();
        const remaining = SESSION_LIFETIME_MS - (now - lastActivityAt);
        if (remaining <= 0) {
            expireSession();
            return;
        }

        const warningTime = remaining <= WARNING_THRESHOLD_MS;
        if (warningTime && !document.hidden) {
            const countdown = document.getElementById('sessionTimeLeft');
            if (countdown) countdown.textContent = Math.ceil(remaining / 1000) + 's';
            if (!warningVisible) {
                const modal = sessionModal();
                if (modal) {
                    warningVisible = true;
                    modal.show();
                }
            }
            if (warningCountdown === null) {
                warningCountdown = setInterval(checkSession, 1000);
            }
        } else {
            hideWarning();
            scheduleMilestone(now);
        }

        renewWhenActive(now);
    }

    // Los tres templates ya llaman updateActivity() desde "Mantener Activa".
    window.updateActivity = function updateActivity() {
        if (expired) return;
        const now = Date.now();
        // No permitir que una interacción tardía reactive una sesión vencida.
        if (now - lastActivityAt >= SESSION_LIFETIME_MS) {
            expireSession();
            return;
        }
        lastActivityAt = now;
        activitySinceRenewal = true;
        if (warningCountdown !== null || warningVisible) hideWarning();
        scheduleMilestone(now);
        renewWhenActive(now);
    };

    ['mousemove', 'click', 'keydown', 'scroll', 'touchstart'].forEach(eventName => {
        document.addEventListener(eventName, window.updateActivity, { passive: true });
    });

    // En pestañas suspendidas los temporizadores pueden retrasarse. Verificar
    // inmediatamente al regresar; la fecha efectiva se basa en Date.now().
    document.addEventListener('visibilitychange', () => {
        if (document.hidden) hideWarning();
        checkSession();
    });
    window.addEventListener('pageshow', event => {
        if (event.persisted) checkSession();
    });
    window.addEventListener('focus', checkSession);

    setInterval(checkSession, CHECK_INTERVAL_MS);
    scheduleMilestone(lastActivityAt);
})();