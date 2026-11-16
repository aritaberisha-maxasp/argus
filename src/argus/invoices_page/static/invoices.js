// The ARGUS invoice status page (#62, NF-05, EX-01), in the look of the queue page (#152).
//
// Lists every case with its status, vendor or customer, amount and dates, newest first, with
// filters for status, kind, a search word and the day it came in. Status, kind and dates live
// in the address (/invoices?status=booked), so a link opens the same list and the back button
// works. A case with an open exception links to it in the exception queue.
//
// Sign in goes through the exception queue page, the one address registered in Entra
// (docs/operations/sign-in.md), like the mailbox page (#135). Everything from the server is put
// on the page as text (textContent), never as HTML, so nothing read from an invoice can run here.
"use strict";

(function () {
  const setting = (name) => {
    const m = document.querySelector('meta[name="argus-sign-in-' + name + '"]');
    return m ? m.getAttribute("content") : "";
  };
  const main = document.getElementById("main");
  // Shared with the queue page (/queue/app.js).
  const RETURN_KEY = "argus-return-to";
  const DEV_TOKEN_KEY = "argus-dev-token";
  const BERLIN = "Europe/Berlin";
  const FILTERS = ["status", "kind", "q", "received_from", "received_to"];

  const KINDS = {
    incoming_invoice: "Incoming invoice",
    incoming_credit_note: "Incoming credit note",
    outgoing_invoice: "Outgoing invoice",
    outgoing_credit_note: "Outgoing credit note",
    bank_transaction: "Bank transaction",
  };
  // Statuses that want attention get the warning color, finished ones a quiet one.
  const TONE = { exception: "flag", paid: "done", closed: "done" };
  const CLOSED_REASONS = {
    rejected: "rejected", duplicate: "duplicate", not_an_invoice: "not an invoice", withdrawn: "withdrawn",
  };

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

  function berlinDay(moment) {
    const out = {};
    const format = new Intl.DateTimeFormat("de-DE", {
      timeZone: BERLIN, day: "2-digit", month: "2-digit", year: "numeric",
    });
    for (const part of format.formatToParts(moment)) out[part.type] = part.value;
    return out.day + "." + out.month + "." + out.year;
  }

  // A date as read on the invoice. ISO dates become DD.MM.YYYY, anything else stays as read.
  function readDay(value) {
    if (!value) return "";
    const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(value));
    return m ? m[3] + "." + m[2] + "." + m[1] : String(value);
  }

  function money(cents, currency) {
    if (cents === null || cents === undefined || !currency) return "";
    try {
      return new Intl.NumberFormat("de-DE", { style: "currency", currency }).format(cents / 100);
    } catch (e) {
      return (cents / 100).toFixed(2) + " " + currency;
    }
  }

  function signedInAs(name) {
    const badge = document.getElementById("who");
    const parts = String(name || "").split(/[\s@._-]+/).filter(Boolean);
    const initials = parts.length > 1 ? parts[0][0] + parts[parts.length - 1][0] : (parts[0] || "").slice(0, 1);
    badge.textContent = initials.toUpperCase();
    badge.setAttribute("aria-label", name ? "Signed in as " + name : "local test sign in");
    badge.title = name || "local test sign in";
  }

  // --- sign in --------------------------------------------------------------------------

  function returnHere() {
    return "/invoices" + window.location.search;
  }

  async function signIn() {
    const mode = setting("mode");
    if (mode === "off") {
      message("Sign in is not set up for ARGUS yet. See docs/operations/sign-in.md.", "problem");
      return null;
    }
    if (mode === "token") {
      const saved = sessionStorage.getItem(DEV_TOKEN_KEY);
      if (saved) {
        signedInAs("");
        return async () => saved;
      }
      const p = el("p", "Sign in on the ", "state");
      const link = el("a", "exception queue page");
      link.href = "/queue";
      p.append(link, " first, then come back here in the same tab.");
      show(p);
      return null;
    }
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
        // The one address registered in Entra. The queue page sends the person back here.
        redirectUri: window.location.origin + "/queue",
        navigateToLoginRequestUrl: false,
      },
      cache: { cacheLocation: "sessionStorage" },
    });
    await app.initialize();
    const account = app.getAllAccounts()[0];
    if (!account) {
      sessionStorage.setItem(RETURN_KEY, returnHere());
      message("Taking you to the Microsoft sign in…");
      await app.loginRedirect({ scopes });
      return null;
    }
    app.setActiveAccount(account);
    signedInAs(account.name || account.username || "");
    return async function () {
      try {
        return (await app.acquireTokenSilent({ scopes, account })).accessToken;
      } catch (e) {
        if (e instanceof window.msal.InteractionRequiredAuthError) {
          sessionStorage.setItem(RETURN_KEY, returnHere());
          await app.acquireTokenRedirect({ scopes, account });
        }
        throw e;
      }
    };
  }

  // --- talking to ARGUS -----------------------------------------------------------------

  // The filters go in the body of a POST, never in an address, so a search word (often a
  // name) does not end up in a server log (NF-03).
  async function load(token, filters) {
    const body = {};
    for (const name of FILTERS) if (filters[name]) body[name] = filters[name];
    const response = await fetch("/cases/search", {
      method: "POST",
      headers: { Authorization: "Bearer " + (await token()), "Content-Type": "application/json" },
      body: JSON.stringify(body),
      cache: "no-store",
    });
    if (response.ok) return response.json();
    if (response.status === 401) {
      sessionStorage.removeItem(DEV_TOKEN_KEY);
      throw new Error("Your sign in has run out. Reload the page to sign in again.");
    }
    if (response.status === 403) {
      throw new Error("Your account has no access to the invoice list. Ask Arita if you need it.");
    }
    if (response.status === 422) throw new Error("ARGUS did not accept one of the filters. Check them and try again.");
    throw new Error("ARGUS could not answer right now (" + response.status + "). Try again in a moment.");
  }

  // --- the filters ----------------------------------------------------------------------

  // Status, kind and dates live in the address. The search word does not: the address goes
  // to the server when the page loads, and its log must not hold names (NF-03).
  const IN_ADDRESS = ["status", "kind", "received_from", "received_to"];

  function filtersFromAddress(keep) {
    const params = new URLSearchParams(window.location.search);
    const out = { q: keep || "" };
    for (const name of IN_ADDRESS) out[name] = params.get(name) || "";
    if (out.status && !/^[a-z_]+$/.test(out.status)) out.status = "";
    if (out.kind && !KINDS[out.kind]) out.kind = "";
    for (const name of ["received_from", "received_to"]) {
      if (out[name] && !/^\d{4}-\d{2}-\d{2}$/.test(out[name])) out[name] = "";
    }
    return out;
  }

  function addressFor(filters) {
    const query = new URLSearchParams();
    for (const name of IN_ADDRESS) if (filters[name]) query.set(name, filters[name]);
    return "/invoices" + (query.toString() ? "?" + query.toString() : "");
  }

  const state = { token: null, filters: filtersFromAddress(), ticket: 0, data: null };
  const view = { heading: null, figures: null, tabs: null, list: null, note: null, form: null };

  function field(labelText, input) {
    const label = el("label", null, "filter");
    label.append(el("span", labelText, "label"), input);
    return label;
  }

  function filterForm() {
    const form = el("form", null, "filters");
    form.setAttribute("role", "search");
    form.setAttribute("aria-label", "Filter invoices");

    const search = el("input");
    search.type = "search";
    search.name = "q";
    search.maxLength = 100;
    search.placeholder = "Name, number, account or case";
    search.value = state.filters.q;

    const kind = el("select");
    kind.name = "kind";
    kind.append(new Option("All kinds", ""));
    for (const [value, text] of Object.entries(KINDS)) kind.append(new Option(text, value));
    kind.value = state.filters.kind;

    const from = el("input");
    from.type = "date";
    from.name = "received_from";
    from.value = state.filters.received_from;
    const to = el("input");
    to.type = "date";
    to.name = "received_to";
    to.value = state.filters.received_to;

    const reset = el("button", "Clear filters", "button");
    reset.type = "button";
    reset.addEventListener("click", () => {
      search.value = "";
      kind.value = "";
      from.value = "";
      to.value = "";
      change({ status: "", kind: "", q: "", received_from: "", received_to: "" });
    });

    let typing = null;
    search.addEventListener("input", () => {
      clearTimeout(typing);
      typing = setTimeout(() => change({ q: search.value.trim() }), 300);
    });
    kind.addEventListener("change", () => change({ kind: kind.value }));
    from.addEventListener("change", () => change({ received_from: from.value }));
    to.addEventListener("change", () => change({ received_to: to.value }));
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      clearTimeout(typing);
      change({ q: search.value.trim() });
    });

    form.append(
      field("Search", search),
      field("Kind", kind),
      field("Received from", from),
      field("Received to", to),
      reset
    );
    view.form = { search, kind, from, to };
    return form;
  }

  function change(parts) {
    const next = Object.assign({}, state.filters, parts);
    if (FILTERS.every((name) => next[name] === state.filters[name])) return;
    const address = addressFor(next);
    state.filters = next;
    // A new search word alone keeps the address, so it adds no step to the back button.
    if (address !== window.location.pathname + window.location.search) {
      history.pushState(null, "", address);
    }
    refresh();
  }

  window.addEventListener("popstate", () => {
    if (!view.list) return;
    state.filters = filtersFromAddress(state.filters.q);
    if (view.form) {
      view.form.search.value = state.filters.q;
      view.form.kind.value = state.filters.kind;
      view.form.from.value = state.filters.received_from;
      view.form.to.value = state.filters.received_to;
    }
    refresh();
  });

  // --- the page -------------------------------------------------------------------------

  function build() {
    const eyebrow = el("span", "Invoices · " + berlinDay(new Date()), "eyebrow");
    view.heading = el("h1", "Every invoice and where it stands");
    const text = el("div", null, "intro-text");
    text.append(eyebrow, view.heading);
    view.figures = el("dl", null, "figures");
    const intro = el("div", null, "intro");
    intro.append(text, view.figures);

    const panel = el("section", null, "panel cases");
    panel.setAttribute("aria-label", "Invoices");
    view.tabs = el("div", null, "tabs");
    view.tabs.setAttribute("role", "group");
    view.tabs.setAttribute("aria-label", "Show status");
    view.note = el("p", null, "cases-note");
    view.note.setAttribute("aria-live", "polite");
    view.list = el("div", null, "cases-body");
    panel.append(view.tabs, filterForm(), view.note, view.list);
    show(intro, panel);
  }

  async function refresh() {
    const ticket = ++state.ticket;
    view.list.setAttribute("aria-busy", "true");
    view.note.textContent = "Loading…";
    try {
      const data = await load(state.token, state.filters);
      if (ticket !== state.ticket) return; // a newer filter was picked meanwhile
      state.data = data;
      render(data);
    } catch (e) {
      if (ticket !== state.ticket) return;
      view.note.textContent = "";
      view.list.replaceChildren(el("p", e && e.message ? e.message : "Something went wrong. Reload the page.",
        "state problem"));
    } finally {
      if (ticket === state.ticket) view.list.removeAttribute("aria-busy");
    }
  }

  function figure(value, label) {
    const box = el("div", null, "figure");
    box.append(el("dt", label, "figure-label"), el("dd", value, "figure-value"));
    return box;
  }

  function render(data) {
    const all = data.statuses.reduce((sum, s) => sum + s.count, 0);
    const count = (status) => (data.statuses.find((s) => s.status === status) || { count: 0 }).count;

    view.heading.replaceChildren(el("span", all, "count"), all === 1 ? " invoice" : " invoices");
    view.figures.replaceChildren(
      figure(count("awaiting_approval"), "Awaiting approval"),
      figure(count("exception"), "Exceptions"),
      figure(count("in_payment_run"), "In payment run")
    );

    const tabs = [{ id: "", label: "All", count: all }].concat(
      data.statuses.map((s) => ({ id: s.status, label: s.text, count: s.count }))
    );
    view.tabs.replaceChildren(...tabs.map((t) => {
      const button = el("button", t.label, "tab");
      button.type = "button";
      button.setAttribute("aria-pressed", String(t.id === state.filters.status));
      button.append(el("span", t.count, "tab-count"));
      button.addEventListener("click", () => change({ status: t.id }));
      return button;
    }));

    const shown = data.cases.length;
    if (data.truncated) {
      view.note.textContent = "Showing the newest " + shown + " of " + data.total +
        ". Use the filters to find older ones.";
    } else {
      view.note.textContent = shown === 1 ? "1 invoice" : shown + " invoices";
    }

    if (!shown) {
      const filtered = FILTERS.some((name) => state.filters[name]);
      view.list.replaceChildren(el("p",
        filtered ? "No invoice matches these filters." : "No invoices yet. They show here once ARGUS opens a case.",
        "empty"));
      return;
    }
    view.list.replaceChildren(table(data.cases));
  }

  const COLUMNS = [
    ["Case", ""], ["Vendor or customer", ""], ["Number", ""], ["Amount", "num"],
    ["Status", ""], ["Received", ""], ["Invoice date", ""], ["Due", ""],
  ];

  function table(cases) {
    const t = el("table", null, "case-table");
    const head = el("thead");
    const headRow = el("tr");
    for (const [label, cls] of COLUMNS) {
      const th = el("th", label, cls);
      th.scope = "col";
      headRow.append(th);
    }
    head.append(headRow);
    const body = el("tbody");
    for (const c of cases) body.append(caseRow(c));
    t.append(head, body);
    return t;
  }

  function cell(label, className, ...content) {
    const td = el("td", null, className);
    td.dataset.label = label;
    td.append(...content.filter((part) => part !== "" && part !== null && part !== undefined));
    return td;
  }

  function caseRow(c) {
    const tr = el("tr");

    const id = el("span", "#" + c.case_id, "mono strong");
    const kind = el("span", KINDS[c.kind] || words(c.kind), "sub");
    const first = el("span", null, "stack");
    first.append(id, kind);

    const partner = el("span", null, "stack");
    partner.append(el("span", c.partner_name || c.sender || "Not read yet", c.partner_name ? "strong" : "muted"));
    const extra = [];
    if (c.partner_account) extra.push("Account " + c.partner_account);
    if (c.partner_name && c.sender) extra.push(c.sender);
    if (extra.length) partner.append(el("span", extra.join(" · "), "sub mono"));

    const status = el("span", null, "stack");
    const pill = el("span", c.status_text, "pill status " + (TONE[c.status] || ""));
    status.append(pill);
    if (c.status === "exception") {
      if (c.exception_id !== null) {
        const link = el("a", c.exception_text || "Open in the queue", "sub");
        link.href = "/queue?exception=" + encodeURIComponent(c.exception_id);
        status.append(link);
      }
      if (c.resume_status) status.append(el("span", "Goes back to " + words(c.resume_status), "sub"));
    } else if (c.status === "closed" && c.closed_reason) {
      status.append(el("span", CLOSED_REASONS[c.closed_reason] || words(c.closed_reason), "sub"));
    }

    const received = el("time", berlinDay(new Date(c.received_at)), "mono");
    received.dateTime = c.received_at;

    tr.append(
      cell("Case", "", first),
      cell("Vendor or customer", "", partner),
      cell("Number", "mono", c.document_number),
      cell("Amount", "num mono", money(c.gross_cents, c.currency)),
      cell("Status", "", status),
      cell("Received", "", received),
      cell("Invoice date", "mono", readDay(c.invoice_date)),
      cell("Due", "mono", readDay(c.due_date))
    );
    return tr;
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

  // --- start ----------------------------------------------------------------------------

  async function start() {
    try {
      const token = await signIn();
      if (!token) return;
      state.token = token;
      showMailboxLink();
      build();
      await refresh();
    } catch (e) {
      message(e && e.message ? e.message : "Something went wrong. Reload the page.", "problem");
    }
  }

  start();
})();
