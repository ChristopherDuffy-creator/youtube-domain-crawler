(() => {
  "use strict";

  if (navigator.webdriver) return;

  const script = document.currentScript;
  const measurementId = script && script.dataset.measurementId;
  if (!measurementId || !/^G-[A-Z0-9]+$/.test(measurementId)) return;

  const storageKey = "expandosaurus_analytics_consent_v1";
  let analyticsLoaded = false;

  const readChoice = () => {
    try {
      return window.localStorage.getItem(storageKey);
    } catch (_) {
      return null;
    }
  };

  const saveChoice = (choice) => {
    try {
      window.localStorage.setItem(storageKey, choice);
    } catch (_) {
      // A private browser may deny storage; the choice still applies to this page.
    }
  };

  const clearAnalyticsCookies = () => {
    document.cookie.split(";").forEach((cookie) => {
      const name = cookie.split("=", 1)[0].trim();
      if (name === "_ga" || name.startsWith("_ga_")) {
        document.cookie = `${name}=; Max-Age=0; path=/; SameSite=Lax`;
      }
    });
  };

  const loadAnalytics = () => {
    if (analyticsLoaded) return;
    analyticsLoaded = true;
    window.dataLayer = window.dataLayer || [];
    window.gtag = window.gtag || function gtag() {
      window.dataLayer.push(arguments);
    };
    window.gtag("consent", "default", {
      analytics_storage: "granted",
      ad_storage: "denied",
      ad_user_data: "denied",
      ad_personalization: "denied",
    });
    window.gtag("js", new Date());
    window.gtag("config", measurementId);

    const googleTag = document.createElement("script");
    googleTag.async = true;
    googleTag.src = `https://www.googletagmanager.com/gtag/js?id=${encodeURIComponent(measurementId)}`;
    document.head.appendChild(googleTag);
  };

  const banner = document.createElement("section");
  banner.className = "analytics-consent";
  banner.setAttribute("aria-label", "Analytics privacy choices");
  banner.innerHTML = `
    <strong>Your privacy choice</strong>
    <p>Allow anonymous Google Analytics so we can see which guides help. Advertising and personalisation stay off. <a href="/privacy">Privacy details</a>.</p>
    <div class="analytics-consent__actions">
      <button type="button" data-analytics-allow>Allow analytics</button>
      <button type="button" data-analytics-decline>Decline</button>
    </div>`;
  banner.hidden = true;
  document.body.appendChild(banner);

  const showBanner = () => {
    banner.hidden = false;
    const choice = readChoice();
    const focusTarget = choice === "granted"
      ? banner.querySelector("[data-analytics-decline]")
      : banner.querySelector("[data-analytics-allow]");
    focusTarget.focus();
  };

  const hideBanner = () => {
    banner.hidden = true;
  };

  banner.querySelector("[data-analytics-allow]").addEventListener("click", () => {
    saveChoice("granted");
    loadAnalytics();
    hideBanner();
  });

  banner.querySelector("[data-analytics-decline]").addEventListener("click", () => {
    saveChoice("denied");
    if (window.gtag) {
      window.gtag("consent", "update", {analytics_storage: "denied"});
    }
    clearAnalyticsCookies();
    hideBanner();
  });

  document.querySelectorAll("[data-analytics-settings]").forEach((control) => {
    control.addEventListener("click", showBanner);
  });

  if (readChoice() === "granted") {
    loadAnalytics();
  } else if (readChoice() !== "denied") {
    showBanner();
  }
})();
