(() => {
    "use strict";

    const securitySelect = document.querySelector("[data-wifi-security]");
    const passwordField = document.querySelector("[data-password-field]");
    const passwordInput = document.querySelector("[data-wifi-password]");
    const passwordToggle = document.querySelector("[data-password-toggle]");

    function syncSecurityFields() {
        if (!securitySelect || !passwordField || !passwordInput) return;
        const isOpen = securitySelect.value === "nopass";
        passwordField.classList.toggle("field-disabled", isOpen);
        passwordInput.disabled = isOpen;
        if (isOpen) {
            passwordInput.type = "password";
            if (passwordToggle) passwordToggle.textContent = "Show";
        }
        passwordToggle?.toggleAttribute("disabled", isOpen);
    }

    function togglePasswordVisibility() {
        if (!passwordInput || !passwordToggle) return;
        const showing = passwordInput.type === "text";
        passwordInput.type = showing ? "password" : "text";
        passwordToggle.textContent = showing ? "Show" : "Hide";
    }

    securitySelect?.addEventListener("change", syncSecurityFields);
    passwordToggle?.addEventListener("click", togglePasswordVisibility);
    syncSecurityFields();
})();
