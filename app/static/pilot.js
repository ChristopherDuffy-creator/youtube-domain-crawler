(() => {
  "use strict";

  if (navigator.webdriver) return;

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

  record("pageview");
  document.addEventListener("click", (event) => {
    const link = event.target.closest("a[href^='/go/']");
    if (!link) return;
    const offerId = link.getAttribute("href").slice(4).split(/[?#]/, 1)[0];
    if (offerId) record("interest_click", offerId);
  }, {capture: true});
})();
