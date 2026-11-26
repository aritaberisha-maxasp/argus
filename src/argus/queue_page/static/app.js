// The ARGUS exception queue page (#126, EX-01, EX-02), in Arita's design of 13 Nov 2026 (#152).
//
// Signs the person in with Microsoft (ADR 0008), then asks ARGUS for the queue with the access
// token. The list sits on the left and the selected item on the right; picking a row changes the
// address to /queue?exception=<id>, so a link opens the same item and the back button works.
//
// Everything from the server is put on the page as text (textContent), never as HTML, so nothing
// in an email or a file name can run here. The token stays in session storage of this tab and is
// never put in a URL.
"use strict";

(function () {
  const setting = (name) => {
    const m = document.querySelector('meta[name="argus-sign-in-' + name + '"]');
    return m ? m.getAttribute("content") : "";
  };
  const main = document.getElementById("main");
  const RETURN_KEY = "argus-return-to";
  const DEV_TOKEN_KEY = "argus-dev-token";
  const BERLIN = "Europe/Berlin";
  const SVG = "http://www.w3.org/2000/svg";
  // Side by side from this width, as in app.css; below it the detail sits under the list.
  const WIDE = window.matchMedia("(min-width: 960px)");

  // Short words for each reason: the pill and tab label, and the counts at the top. The full
  // sentence comes from the server (reason_text). `flag` gives the reason the warning color.
  // This is the one place that decides the colors; later stories may flag more reasons.
  const REASONS = {
    uncertain_classification: { label: "Unsorted", one: "Unsorted email", many: "Unsorted emails" },
    unreadable_document: { label: "Unreadable", one: "Unreadable document", many: "Unreadable documents" },
    unknown_vendor: { label: "Unknown vendor", one: "Unknown vendor", many: "Unknown vendors" },
    unknown_customer: { label: "Unknown customer", one: "Unknown customer", many: "Unknown customers" },
    vendor_without_approver: { label: "No approver", one: "Vendor without approver", many: "Vendors without approver" },
    duplicate: { label: "Possible duplicate", tab: "Duplicates", one: "Possible duplicate", many: "Possible duplicates", flag: true },
    iban_mismatch: { label: "Bank account differs", one: "Bank account differs", many: "Bank accounts differ" },
    invoice_check_failed: { label: "Check failed", one: "Failed invoice check", many: "Failed invoice checks" },
    booking_error: { label: "Booking failed", one: "Failed booking", many: "Failed bookings" },
    unmatched_bank_transaction: { label: "Unmatched", one: "Unmatched transaction", many: "Unmatched transactions" },
    bank_balance_difference: { label: "Balance difference", one: "Balance difference", many: "Balance differences" },
    number_sequence_gap: { label: "Number gap", one: "Number gap", many: "Number gaps" },
    payment_failed: { label: "Payment failed", one: "Failed payment", many: "Failed payments" },
    customer_query: { label: "Customer question", one: "Customer question", many: "Customer questions" },
    forward_failed: { label: "Forward failed", one: "Failed forward", many: "Failed forwards" },
    audit_copy_failed: { label: "Audit copy failed", one: "Failed audit copy", many: "Failed audit copies" },
    audit_check_failed: { label: "Audit check failed", one: "Failed audit check", many: "Failed audit checks" },
    no_account_proposal: { label: "No G/L account", one: "No G/L account", many: "No G/L accounts" },
    approver_absent: { label: "Approver away", one: "Approver away", many: "Approvers away" },
    approver_address_outside: { label: "Approver address", one: "Outside approver address", many: "Outside approver addresses" },
    second_approver_missing: { label: "No second approver", one: "No second approver", many: "No second approver" },
    second_approver_same_person: { label: "Same person", one: "Both approvals by one person", many: "Both approvals by one person" },
  };

  function reasonInfo(reason, text) {
    const known = REASONS[reason];
    if (known) return known;
    const label = text || words(reason);
    return { label, one: label, many: label };
  }

  // --- small helpers --------------------------------------------------------------------

  function el(tag, text, className) {
    const node = document.createElement(tag);
    if (text !== undefined && text !== null) node.textContent = String(text);
    if (className) node.className = className;
    return node;
  }

  function show(...nodes) {
    main.replaceChildren(...nodes);
  }

  function message(text, className) {
    show(el("p", text, "state" + (className ? " " + className : "")));
  }

  function words(value) {
    return value ? String(value).replace(/_/g, " ") : "";
  }

  function capital(text) {
    return text ? text.charAt(0).toUpperCase() + text.slice(1) : "";
  }

  function plural(n, one, many) {
    return n === 1 ? one : many;
  }

  // Day and time in Berlin, as DD.MM.YYYY and HH:MM.
  function berlin(moment) {
    const out = {};
    const format = new Intl.DateTimeFormat("de-DE", {
      timeZone: BERLIN, day: "2-digit", month: "2-digit", year: "numeric",
      hour: "2-digit", minute: "2-digit", hourCycle: "h23",
    });
    for (const part of format.formatToParts(moment)) out[part.type] = part.value;
    return { day: out.day + "." + out.month + "." + out.year, time: out.hour + ":" + out.minute };
  }

  function money(cents, currency) {
    if (cents === null || cents === undefined || !currency) return "";
    try {
      return new Intl.NumberFormat("de-DE", { style: "currency", currency }).format(cents / 100);
    } catch (e) {
      return (cents / 100).toFixed(2) + " " + currency;
    }
  }

  function size(bytes) {
    if (bytes < 1024) return bytes + " B";
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
    return (bytes / 1024 / 1024).toFixed(1) + " MB";
  }

  function pairs(entries) {
    const dl = el("dl", null, "pairs");
    for (const [label, value] of entries) {
      if (value === null || value === undefined || value === "") continue;
      dl.append(el("dt", label), el("dd", value));
    }
    return dl;
  }

  function block(title, ...nodes) {
    const section = el("section", null, "block");
    section.append(el("h3", title), ...nodes);
    return section;
  }

  function subject(row) {
    if (row.case_id != null) return "Case " + row.case_id;
    if (row.email_id != null) return "Email " + row.email_id;
    if (row.statement_id != null) return "Bank statement " + row.statement_id;
    return "ARGUS itself";
  }

  function idFromAddress() {
    const id = new URLSearchParams(window.location.search).get("exception");
    return id && /^\d+$/.test(id) ? Number(id) : null;
  }

  // --- who is signed in -----------------------------------------------------------------

  function signedInAs(name) {
    const badge = document.getElementById("who");
    const parts = String(name || "").split(/[\s@._-]+/).filter(Boolean);
    const initials = parts.length > 1 ? parts[0][0] + parts[parts.length - 1][0] : (parts[0] || "").slice(0, 1);
    badge.textContent = initials.toUpperCase();
    badge.setAttribute("aria-label", "Signed in as " + name);
    badge.title = name;
  }

  function signedInLocally() {
    const badge = document.getElementById("who");
    badge.textContent = "";
    badge.setAttribute("aria-label", "local test sign in");
    badge.title = "local test sign in";
  }

  // --- sign in --------------------------------------------------------------------------

  // Returns a function that gives a fresh access token, or null while the browser is sent to
  // the Microsoft sign in page.
  async function signIn() {
    const mode = setting("mode");
    if (mode === "off") {
      message("Sign in is not set up for ARGUS yet. See docs/operations/sign-in.md.", "problem");
      return null;
    }
    if (mode === "token") return localToken();
    return microsoftToken();
  }

  async function microsoftToken() {
    if (!window.msal) {
      message("The Microsoft sign in library did not load. Reload the page.", "problem");
      return null;
    }
    const scopes = [setting("scope")];
    const app = new window.msal.PublicClientApplication({
      auth: {
        clientId: setting("client-id"),
        authority: setting("authority"),
        redirectUri: window.location.origin + "/queue",
        navigateToLoginRequestUrl: false,
      },
      cache: { cacheLocation: "sessionStorage" },
    });
    await app.initialize();
    const result = await app.handleRedirectPromise();
    const account = (result && result.account) || app.getAllAccounts()[0];
    if (!account) {
      sessionStorage.setItem(RETURN_KEY, window.location.search);
      message("Taking you to the Microsoft sign in…");
      await app.loginRedirect({ scopes });
      return null;
    }
    const back = sessionStorage.getItem(RETURN_KEY);
    if (back !== null) {
      sessionStorage.removeItem(RETURN_KEY);
      // The demo mailbox page (#135), the invoice status page (#62) and the approval page
      // (#38) sign in through this page and come back here. Only these fixed addresses, so
      // nothing can send people away.
      if (back === "/mailbox" || /^\/invoices(\?[A-Za-z0-9_=&%.+-]*)?$/.test(back) ||
          /^\/approvals(\/\d{1,19})?$/.test(back)) {
        window.location.replace(back);
        return null;
      }
      if (/^(\?exception=\d+)?$/.test(back)) history.replaceState(null, "", "/queue" + back);
    }
    app.setActiveAccount(account);
    signedInAs(account.name || account.username || "");
    return async function () {
      try {
        return (await app.acquireTokenSilent({ scopes, account })).accessToken;
      } catch (e) {
        if (e instanceof window.msal.InteractionRequiredAuthError) {
          sessionStorage.setItem(RETURN_KEY, window.location.search);
          await app.acquireTokenRedirect({ scopes, account });
        }
        throw e;
      }
    };
  }

  // Local development with the fake sign in (standins/entra): paste a token from the fake.
  function localToken() {
    const saved = sessionStorage.getItem(DEV_TOKEN_KEY);
    if (saved) {
      signedInLocally();
      return async () => saved;
    }
    const form = el("form", null, "state signin");
    const box = el("textarea");
    box.setAttribute("aria-label", "Access token");
    const button = el("button", "Sign in", "button primary");
    form.append(
      el("h1", "Local sign in"),
      el("p", "This ARGUS uses the fake sign in for development. Paste a token from the fake " +
        "(see standins/entra/README.md)."),
      box,
      button
    );
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      sessionStorage.setItem(DEV_TOKEN_KEY, box.value.trim());
      window.location.reload();
    });
    show(form);
    return null;
  }

  // --- talking to ARGUS -----------------------------------------------------------------

  // A GET by default. ``body`` is sent as JSON with ``method`` (the mode switch uses PUT).
  // ``sent`` is a response already fetched, by send(), that only needs checking.
  // A mode switch reason of only spaces, tabs or line breaks is no reason (#170). trim() keeps
  // zero width spaces, so they go too, like on the server and in the database.
  const BLANK_REASON =
    "Please give a reason of at least 3 characters. Spaces, tabs and line breaks alone do not count.";
  function trimReason(text) {
    return text.replace(/^[\s\u180e\u200b\ufeff]+|[\s\u180e\u200b\ufeff]+$/g, "");
  }

  async function call(token, path, method, body, sent) {
    let response = sent;
    if (!response) {
      const headers = { Authorization: "Bearer " + (await token()) };
      const options = { method: method || "GET", headers, cache: "no-store" };
      if (body !== undefined) {
        headers["Content-Type"] = "application/json";
        options.body = JSON.stringify(body);
      }
      response = await fetch(path, options);
    }
    if (response.ok) return response;
    if (response.status === 401) {
      sessionStorage.removeItem(DEV_TOKEN_KEY);
      throw new Error("Your sign in has run out. Reload the page to sign in again.");
    }
    if (response.status === 403) {
      throw new Error("Your account has no access to the exception queue. Ask Arita if you need it.");
    }
    if (response.status === 404) throw new Error("This exception is not open any more, or it does not exist.");
    if (response.status === 422) {
      const refused = new Error("ARGUS did not accept this. Check what you typed and try again.");
      refused.status = 422;
      // A sentence from the server, when it sent one, for forms that show it (#170).
      try {
        const body = await response.json();
        if (typeof body.detail === "string") refused.detail = body.detail;
      } catch (e) {
        // no sentence, the general one stays
      }
      throw refused;
    }
    throw new Error("ARGUS could not answer right now (" + response.status + "). Try again in a moment.");
  }

  // Sends a change to ARGUS. A refusal the person can fix (422) or that the database gave
  // (409) comes back with a sentence in "problem", shown as it is.
  async function send(token, path, body) {
    const response = await fetch(path, {
      method: "POST",
      headers: { Authorization: "Bearer " + (await token()), "Content-Type": "application/json" },
      body: JSON.stringify(body),
      cache: "no-store",
    });
    if (response.status === 409 || response.status === 422) {
      const answer = await response.json().catch(() => ({}));
      if (typeof answer.problem === "string") throw new Error(answer.problem);
    }
    return call(async () => "", path, undefined, undefined, response);
  }

  // --- the queue ------------------------------------------------------------------------

  const state = { token: null, rows: [], filter: "all", selected: null, loading: 0 };
  const view = { tabs: null, rows: null, detail: null };

  function visibleRows() {
    return state.rows.filter((r) => state.filter === "all" || r.reason === state.filter);
  }

  // Reasons in the queue, most common first; a tie keeps the order of the oldest item.
  function reasonCounts() {
    const counts = new Map();
    for (const row of state.rows) {
      const entry = counts.get(row.reason) || { reason: row.reason, text: row.reason_text, count: 0 };
      entry.count += 1;
      counts.set(row.reason, entry);
    }
    return [...counts.values()].sort((a, b) => b.count - a.count);
  }

  async function showQueue(token) {
    const [data, mode] = await Promise.all([
      call(token, "/exceptions").then((r) => r.json()),
      modePanel(token),
    ]);
    state.token = token;
    state.rows = data.exceptions;

    const today = berlin(new Date()).day;
    const eyebrow = el("span", "Exception queue · " + today, "eyebrow");
    const n = state.rows.length;

    if (!n) {
      const intro = el("div", null, "intro");
      const text = el("div", null, "intro-text");
      text.append(eyebrow, el("h1", "No items need a human"));
      intro.append(text);
      show(intro, mode, el("p", "Nothing waits for you. ARGUS handled everything on its own.", "state"));
      return;
    }

    const heading = el("h1");
    heading.append(el("span", n, "count"), plural(n, " item needs a human", " items need a human"));
    const text = el("div", null, "intro-text");
    text.append(eyebrow, heading);

    const figures = el("dl", null, "figures");
    for (const entry of reasonCounts().slice(0, 3)) {
      const info = reasonInfo(entry.reason, entry.text);
      figures.append(figure(entry.count, plural(entry.count, info.one, info.many)));
    }
    const oldest = Math.max(...state.rows.map((r) => r.age_working_days));
    figures.append(figure(oldest + "d", "Oldest, working days"));

    const intro = el("div", null, "intro");
    intro.append(text, figures);

    const list = el("section", null, "panel list");
    list.setAttribute("aria-label", "Queue");
    view.tabs = el("div", null, "tabs");
    view.tabs.setAttribute("role", "group");
    view.tabs.setAttribute("aria-label", "Show reason");
    view.rows = el("ul", null, "rows");
    list.append(view.tabs, view.rows);

    view.detail = el("section", null, "panel detail");
    view.detail.setAttribute("aria-label", "Selected exception");
    view.detail.setAttribute("aria-live", "polite");

    const panels = el("div", null, "panels");
    panels.append(list, view.detail);
    show(intro, mode, panels);

    renderTabs();
    const wanted = idFromAddress();
    select(wanted !== null ? wanted : state.rows[0].exception_id, "none");
  }

  // In the markup the label comes first, as a description list needs; app.css shows the
  // number above it.
  function figure(value, label) {
    const box = el("div", null, "figure");
    box.append(el("dt", label, "figure-label"), el("dd", value, "figure-value"));
    return box;
  }

  function renderTabs() {
    const tabs = [{ id: "all", label: "All", count: state.rows.length }].concat(
      reasonCounts().map((e) => {
        const info = reasonInfo(e.reason, e.text);
        return { id: e.reason, label: info.tab || info.label, count: e.count };
      })
    );
    view.tabs.replaceChildren(...tabs.map((t) => {
      const button = el("button", t.label, "tab");
      button.type = "button";
      button.setAttribute("aria-pressed", String(t.id === state.filter));
      button.append(el("span", t.count, "tab-count"));
      button.addEventListener("click", () => pickFilter(t.id));
      return button;
    }));
  }

  function pickFilter(id) {
    state.filter = id;
    renderTabs();
    const visible = visibleRows();
    if (visible.length && !visible.some((r) => r.exception_id === state.selected)) {
      select(visible[0].exception_id, "replace");
    } else {
      renderRows();
    }
  }

  function renderRows() {
    const today = berlin(new Date()).day;
    view.rows.replaceChildren(...visibleRows().map((r) => {
      const opened = berlin(new Date(r.opened_at));
      const info = reasonInfo(r.reason, r.reason_text);

      const name = el("span", null, "row-name");
      name.append(el("span", null, "dot" + (info.flag ? " flag" : "")), el("span", subject(r), "row-subject"));
      if (r.case_kind) name.append(el("span", words(r.case_kind), "chip"));
      const top = el("span", null, "row-top");
      const time = el("time", opened.day === today ? opened.time : opened.day + " " + opened.time, "row-time");
      time.dateTime = r.opened_at;
      top.append(name, time);

      const button = el("button", null, "row");
      button.type = "button";
      if (r.exception_id === state.selected) button.setAttribute("aria-current", "true");
      button.append(top, el("span", r.reason_text, "row-reason"));
      button.append(el("span", r.sender || "No sender known", "row-sender"));
      button.addEventListener("click", () => {
        if (r.exception_id !== state.selected) select(r.exception_id, "push");
        if (!WIDE.matches) view.detail.scrollIntoView({ block: "start" });
      });
      const item = el("li");
      item.append(button);
      return item;
    }));
    if (!visibleRows().length) {
      view.rows.replaceChildren(el("li", "No open items with this reason.", "empty"));
    }
  }

  // how: "push" adds a step to the browser history, "replace" changes the address in place,
  // "none" leaves it (the back button already changed it).
  function select(id, how) {
    state.selected = id;
    const address = "/queue?exception=" + encodeURIComponent(id);
    if (how === "push") history.pushState(null, "", address);
    else if (how === "replace") history.replaceState(null, "", address);
    renderRows();
    showOne(id);
  }

  window.addEventListener("popstate", () => {
    if (!view.detail || !state.rows.length) return;
    const id = idFromAddress();
    const target = id !== null ? id : state.rows[0].exception_id;
    if (target !== state.selected) select(target, "none");
  });

  // --- shadow or live mode (#72, #134) -------------------------------------------------

  const MODE_TEXT = {
    shadow: ["Shadow mode", "ARGUS only suggests where each email should go. It moves nothing and opens no cases, a person sorts the Inbox."],
    live: ["Live mode", "ARGUS sorts and files every email on its own and opens cases for invoices."],
  };

  // The mode for finance and admin. Only the admin gets the switch; the server checks the
  // role again on every switch, so hiding the button is for clarity, not for safety.
  async function modePanel(token) {
    const panel = el("section", null, "panel mode");
    panel.setAttribute("aria-label", "Mode");
    let data;
    try {
      data = await (await call(token, "/intake/mode")).json();
    } catch (e) {
      panel.append(el("p", "ARGUS could not say right now whether it runs in shadow or live mode.", "muted"));
      return panel;
    }
    const [title, text] = MODE_TEXT[data.mode] || [capital(words(data.mode)), ""];
    panel.classList.add(data.mode === "live" ? "live" : "shadow");
    const about = el("div", null, "mode-about");
    about.append(el("span", title, "mode-pill"), el("p", text, "mode-text"));
    panel.append(about);
    if (data.may_switch) panel.append(switchForm(token, data.mode));
    return panel;
  }

  function switchForm(token, current) {
    const target = current === "live" ? "shadow" : "live";
    const targetName = MODE_TEXT[target][0].toLowerCase();
    const open = el("button", "Switch to " + targetName, "button");
    open.type = "button";
    const form = el("form", null, "switch");
    form.hidden = true;
    const id = "mode-reason";
    const label = el("label", "Why do you switch? This goes into the audit log with your name.");
    label.htmlFor = id;
    const reason = el("input");
    reason.id = id;
    reason.type = "text";
    reason.required = true;
    reason.minLength = 3;
    reason.maxLength = 500;
    reason.placeholder = target === "live" ? "For example: demo for Benjamin" : "For example: demo is over";
    const confirm = el("button", "Switch to " + targetName, "button primary");
    confirm.type = "submit";
    const cancel = el("button", "Cancel", "button");
    cancel.type = "button";
    const problem = el("p", null, "problem");
    problem.setAttribute("role", "alert");
    const actions = el("div", null, "switch-actions");
    actions.append(confirm, cancel);
    form.append(label, reason, actions, problem);
    open.addEventListener("click", () => {
      open.hidden = true;
      form.hidden = false;
      reason.focus();
    });
    cancel.addEventListener("click", () => {
      form.hidden = true;
      open.hidden = false;
      problem.textContent = "";
    });
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const why = trimReason(reason.value);
      if (why.length < 3) {
        problem.textContent = BLANK_REASON;
        return;
      }
      confirm.disabled = true;
      problem.textContent = "";
      try {
        await call(token, "/intake/mode", "PUT", { mode: target, reason: why });
      } catch (e) {
        if (e && e.status === 422) {
          problem.textContent = e.detail || BLANK_REASON;
        } else {
          problem.textContent = e && e.message ? e.message : "The switch did not work. Try again.";
        }
        confirm.disabled = false;
        return;
      }
      // The switch is done. If only the reload fails, say so, so nobody switches twice.
      try {
        await showQueue(token);
      } catch (e) {
        message("ARGUS switched to " + targetName + ", but the page could not load again. " +
          "Reload the page.", "problem");
      }
    });
    const wrap = el("div", null, "mode-switch");
    wrap.append(open, form);
    return wrap;
  }

  // The fake mailbox page exists only in demo mode (#135). Its stylesheet is a fixed public
  // file, so asking for it says whether to show the nav item without touching any data.
  async function showMailboxLink() {
    const link = document.getElementById("nav-mailbox");
    if (!link) return;
    try {
      const response = await fetch("/mailbox/app.css", { method: "GET", cache: "no-store" });
      if (response.ok) link.hidden = false;
    } catch (e) {
      // no demo mailbox here
    }
  }

  // --- one exception --------------------------------------------------------------------

  async function showOne(id) {
    const ticket = ++state.loading;
    view.detail.setAttribute("aria-busy", "true");
    view.detail.replaceChildren(el("p", "Loading…", "state"));
    try {
      const x = await (await call(state.token, "/exceptions/" + encodeURIComponent(id))).json();
      if (ticket !== state.loading) return; // another row was picked meanwhile
      view.detail.replaceChildren(...detailParts(x));
    } catch (e) {
      if (ticket !== state.loading) return;
      view.detail.replaceChildren(el("p", e && e.message ? e.message : "Something went wrong. Reload the page.",
        "state problem"));
    } finally {
      if (ticket === state.loading) view.detail.removeAttribute("aria-busy");
    }
  }

  function detailParts(x) {
    const info = reasonInfo(x.reason, x.reason_text);
    const opened = berlin(new Date(x.opened_at));
    const parts = [];

    const title = el("div", null, "detail-title");
    title.append(el("span", info.label, "pill" + (info.flag ? " flag" : "")),
      el("h2", subject(x.case ? { case_id: x.case.id } : x)));
    const head = el("div", null, "detail-head");
    head.append(title);
    if (x.email_id !== null) head.append(sourceLink());
    parts.push(head);

    parts.push(el("p", x.reason_text + ".", "sentence"));

    const type = x.case ? capital(words(x.case.kind))
      : x.email_id !== null ? "Email, not yet a case"
        : x.statement_id !== null ? "Bank statement" : "ARGUS itself";
    const facts = el("dl", null, "facts");
    for (const [label, value, mono] of [
      ["Sender", x.sender || "Not known", true],
      ["Opened", opened.day + ", " + opened.time, true],
      ["Age", x.age_working_days + plural(x.age_working_days, " working day", " working days"), true],
      ["Type", type, false],
    ]) {
      const cell = el("div", null, "fact");
      cell.append(el("dt", label), el("dd", value, mono ? "mono" : ""));
      facts.append(cell);
    }
    parts.push(facts);

    const why = [el("p", x.detail)];
    // The server puts the facts into words (#188): labels, yes and no, lists without brackets.
    const extra = x.fact_lines || [];
    if (extra.length) why.push(pairs(extra));
    parts.push(block("Why ARGUS stopped", ...why));

    if (x.case) {
      const c = x.case;
      parts.push(block("Case", pairs([
        ["Case", c.id],
        ["Kind", words(c.kind)],
        ["Status", words(c.status)],
        ["Goes back to", words(c.resume_status)],
        ["Document number", c.document_number],
        ["Amount", money(c.gross_cents, c.currency)],
      ])));
    }

    if (x.correctable) parts.push(correction(x));
    if (x.account_enterable) parts.push(accountEntry(x));

    if (x.email_id !== null) {
      const email = x.email
        ? [pairs([
          ["From", x.email.sender],
          ["To", x.email.to],
          ["Cc", x.email.cc],
          ["Date", x.email.date],
          ["Subject", x.email.subject],
          ["Sender domain checked", x.sender],
        ]), el("pre", x.email.body || "(no text in the email)", "body")]
        : [el("p", x.email_problem || "The email is not available.", "problem")];
      const original = block("Original email", ...email);
      original.id = "original-email";
      original.tabIndex = -1;
      parts.push(original);
      parts.push(block("Files as stored", files(x)));
    }
    return parts;
  }

  // --- correcting the values of an invoice (#112) ----------------------------------------

  // The form for the invoice number, gross amount and currency, filled with what the case has
  // or what ARGUS read. The invoice itself is under "Files as stored", marked "this case".
  function correction(x) {
    const c = x.case;
    const read = x.read_values || {};
    const readValue = (name) => (read[name] && read[name].value) || "";
    const amount = c.gross_cents !== null && c.gross_cents !== undefined
      ? (c.gross_cents / 100).toFixed(2) : readValue("gross_amount");
    const form = el("form", null, "correction");
    const fields = [
      ["document_number", "Invoice number", c.document_number || readValue("invoice_number"), "text"],
      ["gross_amount", "Gross amount", amount, "text"],
      ["currency", "Currency (three letters)", c.currency || readValue("currency"), "text"],
    ];
    const inputs = {};
    for (const [name, label, value, type] of fields) {
      const id = "correct-" + name;
      const input = el("input");
      input.id = id;
      input.name = name;
      input.type = type;
      input.value = value || "";
      input.required = true;
      if (name === "gross_amount") input.inputMode = "decimal";
      if (name === "currency") input.maxLength = 3;
      const labelNode = el("label", label);
      labelNode.htmlFor = id;
      const row = el("p", null, "field");
      row.append(labelNode, input);
      const r = read[name];
      if (r && (r.flag || r.note)) {
        row.append(el("span", "ARGUS read " + (r.value === null ? "nothing" : "\u201c" + r.value + "\u201d") +
          (r.note ? ": " + r.note : ""), "muted hint"));
      }
      form.append(row);
      inputs[name] = input;
    }
    const reason = el("textarea");
    reason.id = "correct-reason";
    reason.required = true;
    reason.maxLength = 500;
    reason.className = "reason";
    const reasonLabel = el("label", "Why (kept in the audit log for ten years, so no personal details)");
    reasonLabel.htmlFor = reason.id;
    const reasonRow = el("p", null, "field");
    reasonRow.append(reasonLabel, reason);
    const button = el("button", "Save and let the case carry on", "button primary");
    button.type = "submit";
    const result = el("p");
    result.setAttribute("role", "status");
    form.append(reasonRow, button, result);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      button.disabled = true;
      result.className = "muted";
      result.textContent = "Saving…";
      try {
        const answer = await (await send(state.token, "/exceptions/" + encodeURIComponent(x.exception_id) +
          "/correction", {
          document_number: inputs.document_number.value,
          gross_amount: inputs.gross_amount.value,
          currency: inputs.currency.value,
          reason: reason.value,
        })).json();
        const back = el("a", "Back to all open exceptions");
        back.href = "/queue";
        const done = answer.outcome === "duplicate"
          ? el("p", "Saved. With these values the invoice looks like a second copy of case " +
            answer.duplicate_of + ", so it is back in the queue as a possible duplicate. ")
          : el("p", "Saved. ARGUS found no duplicate, so case " + answer.case_id +
            " carries on toward approval. ");
        done.append(back);
        form.replaceWith(done);
      } catch (e) {
        result.className = "problem";
        result.textContent = e.message;
        button.disabled = false;
      }
    });
    return block("Correct the invoice values",
      el("p", "Open the invoice under \u201cFiles as stored\u201d, then enter the values as printed. " +
        "ARGUS checks for duplicates again before the case goes on.", "muted"),
      form);
  }

  // --- entering the G/L account and tax key (#37) ----------------------------------------

  // ARGUS had no booking history to propose from, or the latest postings disagree. The person
  // enters the account and tax key the invoice is booked to; the case then carries on.
  function accountEntry(x) {
    const form = el("form", null, "correction");
    const inputs = {};
    const candidates = (x.facts && x.facts.candidates) || [];
    const first = candidates[0] || {};
    for (const [name, label, value, size] of [
      ["gl_account", "G/L account (four digits)", first.gl_account || "", 4],
      ["tax_key", "Tax key (empty when none)", first.tax_key || "", 4],
    ]) {
      const id = "account-" + name;
      const input = el("input");
      input.id = id;
      input.name = name;
      input.type = "text";
      input.inputMode = "numeric";
      input.maxLength = size;
      input.value = candidates.length > 1 ? "" : value;
      input.required = name === "gl_account";
      const labelNode = el("label", label);
      labelNode.htmlFor = id;
      const row = el("p", null, "field");
      row.append(labelNode, input);
      form.append(row);
      inputs[name] = input;
    }
    const reason = el("textarea");
    reason.id = "account-reason";
    reason.required = true;
    reason.maxLength = 500;
    reason.className = "reason";
    const reasonLabel = el("label", "Why (kept in the audit log for ten years, so no personal details)");
    reasonLabel.htmlFor = reason.id;
    const reasonRow = el("p", null, "field");
    reasonRow.append(reasonLabel, reason);
    const button = el("button", "Save and let the case carry on", "button primary");
    button.type = "submit";
    const result = el("p");
    result.setAttribute("role", "status");
    form.append(reasonRow, button, result);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      button.disabled = true;
      result.className = "muted";
      result.textContent = "Saving…";
      try {
        const answer = await (await send(state.token, "/exceptions/" + encodeURIComponent(x.exception_id) +
          "/account", {
          gl_account: inputs.gl_account.value,
          tax_key: inputs.tax_key.value,
          reason: reason.value,
        })).json();
        const back = el("a", "Back to all open exceptions");
        back.href = "/queue";
        const done = el("p", "Saved. Case " + answer.case_id +
          " carries on with this account, and the approver sees it. ");
        done.append(back);
        form.replaceWith(done);
      } catch (e) {
        result.className = "problem";
        result.textContent = e.message;
        button.disabled = false;
      }
    });
    return block("Enter the G/L account and tax key",
      el("p", "Open the invoice under \u201cFiles as stored\u201d to see what it is for. " +
        "The approver sees this account and tax key when they decide.", "muted"),
      form);
  }

  function sourceLink() {
    const link = el("a", "Open source email", "source-link");
    link.href = "#original-email";
    const icon = document.createElementNS(SVG, "svg");
    for (const [k, v] of [["width", "14"], ["height", "14"], ["viewBox", "0 0 24 24"], ["fill", "none"],
      ["stroke", "currentColor"], ["stroke-width", "2"], ["stroke-linecap", "round"],
      ["stroke-linejoin", "round"], ["aria-hidden", "true"]]) icon.setAttribute(k, v);
    const path = document.createElementNS(SVG, "path");
    path.setAttribute("d", "M7 17L17 7M9 7h8v8");
    icon.append(path);
    link.append(icon);
    // Stay on this item: scroll to the email instead of changing the address.
    link.addEventListener("click", (event) => {
      event.preventDefault();
      const target = document.getElementById("original-email");
      if (!target) return;
      target.scrollIntoView({ behavior: "smooth", block: "start" });
      target.focus({ preventScroll: true });
    });
    return link;
  }

  function files(x) {
    if (!x.files.length) return el("p", "No files are recorded for this email.", "muted");
    const list = el("ul", null, "files");
    for (const f of x.files) {
      const name = f.kind === "email" ? "The email as received (.eml)" : (f.filename || "Attachment " + f.document_id);
      const title = el("span", name, "file-title");
      if (x.case && f.linked_to_case) title.append(el("span", "this case", "chip"));
      const label = el("span", null, "file-name");
      label.title = "SHA-256 " + f.fingerprint;
      label.append(title, el("span", f.content_type + " · " + size(f.size_bytes), "file-meta"));
      const button = el("button", "Download", "button");
      button.type = "button";
      button.setAttribute("aria-label", "Download " + name);
      button.addEventListener("click", () => download(x.exception_id, f, button));
      const item = el("li", null, "file");
      item.append(label, button);
      list.append(item);
    }
    return list;
  }

  async function download(exceptionId, file, button) {
    button.disabled = true;
    try {
      const response = await call(state.token, "/exceptions/" + encodeURIComponent(exceptionId) +
        "/files/" + encodeURIComponent(file.document_id));
      const url = URL.createObjectURL(await response.blob());
      const a = el("a");
      a.href = url;
      a.download = file.kind === "email" ? "email-" + file.document_id + ".eml"
        : (file.filename || "attachment-" + file.document_id);
      document.body.append(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 10000);
    } catch (e) {
      alert(e.message);
    } finally {
      button.disabled = false;
    }
  }

  // --- start ----------------------------------------------------------------------------

  async function start() {
    try {
      const token = await signIn();
      if (!token) return;
      showMailboxLink();
      message("Loading…");
      await showQueue(token);
    } catch (e) {
      message(e && e.message ? e.message : "Something went wrong. Reload the page.", "problem");
    }
  }

  start();
})();
