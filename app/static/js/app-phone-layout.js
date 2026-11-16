(() => {
    'use strict';
    const phone = window.matchMedia('(max-width: 575px)');
    function applyPhoneDisclosures() {
        for (const details of document.querySelectorAll('[data-phone-disclosure]')) {
            details.open = !phone.matches || details.dataset?.actionable === 'true';
        }
    }
    applyPhoneDisclosures();
    phone.addEventListener('change', applyPhoneDisclosures);
})();
