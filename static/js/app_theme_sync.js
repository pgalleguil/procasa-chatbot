(function () {
    'use strict';

    const STORAGE_KEY = 'theme';

    function normalizeTheme(theme) {
        return theme === 'light' ? 'light' : 'dark';
    }

    function updateThemeControls(theme) {
        const icon = document.getElementById('themeIcon');
        const control = document.querySelector('.theme-toggle');
        const isLight = theme === 'light';

        if (icon) {
            icon.classList.remove('fa-sun', 'fa-moon');
            icon.classList.add(isLight ? 'fa-moon' : 'fa-sun');
        }
        if (control) {
            const nextTheme = isLight ? 'oscuro' : 'claro';
            control.setAttribute('aria-label', `Cambiar a tema ${nextTheme}`);
            control.setAttribute('title', `Cambiar a tema ${nextTheme}`);
        }
    }

    function applyAppTheme(theme) {
        const normalized = normalizeTheme(theme);
        document.documentElement.setAttribute('data-theme', normalized);
        try {
            localStorage.setItem(STORAGE_KEY, normalized);
        } catch (error) {
            // La interfaz sigue funcionando aunque el almacenamiento no esté disponible.
        }
        updateThemeControls(normalized);
        return normalized;
    }

    function getSavedTheme() {
        try {
            return normalizeTheme(localStorage.getItem(STORAGE_KEY));
        } catch (error) {
            return 'dark';
        }
    }

    window.applyAppTheme = applyAppTheme;

    // Órdenes de visita y otros módulos que no tenían su propio controlador.
    if (typeof window.toggleTheme !== 'function') {
        window.toggleTheme = function () {
            const current = document.documentElement.getAttribute('data-theme');
            applyAppTheme(current === 'light' ? 'dark' : 'light');
        };
    }

    applyAppTheme(getSavedTheme());
    document.addEventListener('DOMContentLoaded', () => {
        applyAppTheme(getSavedTheme());
    }, { once: true });

    window.addEventListener('storage', (event) => {
        if (event.key === STORAGE_KEY) {
            applyAppTheme(event.newValue);
        }
    });
})();
