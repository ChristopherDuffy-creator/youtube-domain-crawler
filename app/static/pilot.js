(() => {
  "use strict";

  if (navigator.webdriver) return;

  document.querySelectorAll("form[data-public-form]").forEach((form) => {
    const guard = form.querySelector("input[name='form_guard']");
    if (guard) guard.value = "ready";
  });

  const sessionId = typeof crypto.randomUUID === "function"
    ? crypto.randomUUID()
    : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 18)}`;

  const record = (eventType, offerId = null) => {
    const payload = {
      event_type: eventType,
      path: window.location.pathname,
      session_id: sessionId,
      referrer: document.referrer,
      offer_id: offerId,
    };
    fetch("/track/site-event", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(payload),
      credentials: "same-origin",
      keepalive: true,
    }).catch(() => {});
  };

  const recordGoogleEvent = (eventName, parameters = {}) => {
    if (typeof window.gtag !== "function") return;
    window.gtag("event", eventName, {
      site_host: window.location.hostname,
      ...parameters,
    });
  };

  record("pageview");
  const query = new URLSearchParams(window.location.search);
  if (query.get("subscribed") === "1") {
    recordGoogleEvent("newsletter_signup");
  }
  if (query.get("sent") === "true") {
    recordGoogleEvent("contact_submit");
  }

  document.addEventListener("click", (event) => {
    const link = event.target.closest("a[href^='/go/']");
    if (!link) return;
    const offerId = link.dataset.track
      || link.getAttribute("href").slice(4).split(/[?#]/, 1)[0];
    if (offerId) {
      record("interest_click", offerId);
      recordGoogleEvent("affiliate_click", {offer_id: offerId});
    }
  }, {capture: true});
})();
