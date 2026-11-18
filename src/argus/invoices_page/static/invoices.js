// The ARGUS invoice status page (#62, NF-05, EX-01), in the look of the queue page (#152).
//
// Lists every case with its status, vendor or customer, amount and dates, newest first, with
// filters for status, kind, a search word and the day it came in. Status, kind and dates live
// in the address (/invoices?status=booked), so a link opens the same list and the back button
// works. A case with an open exception links to it in the exception queue and has a Resolve
// button: the person picks how the exception ends, from the endings ARGUS offers for it, and
// says why in a short comment (EX-01). ARGUS and its database check the choice again.
//
// Every case has a Trail button (#63, NF-01): it shows each step ARGUS recorded for the case,
// from its email to its last status, with time and actor, and downloads the same as a file.
// The trail panel only reads: it has no field and no button that changes anything.
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

  function berlinTime(moment) {
    const out = {};
    const format = new Intl.DateTimeFormat("de-DE", {
      timeZone: BERLIN, day: "2-digit", month: "2-digit", year: "numeric",
      hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
    });
    for (const part of format.formatToParts(moment)) out[part.type] = part.value;
    return out.day + "." + out.month + "." + out.year + " " + out.hour + ":" + out.minute + ":" + out.second;
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

  // Asks ARGUS for JSON with the person's token. A refusal with a sentence for the person
  // (422, 409) shows that sentence.
  async function call(token, path, body) {
    const response = await fetch(path, {
      method: body === undefined ? "GET" : "POST",
      headers: Object.assign({ Authorization: "Bearer " + (await token()) },
        body === undefined ? {} : { "Content-Type": "application/json" }),
      body: body === undefined ? undefined : JSON.stringify(body),
      cache: "no-store",
    });
    if (response.ok) return response.json();
    if (response.status === 401) {
      sessionStorage.removeItem(DEV_TOKEN_KEY);
      throw new Error("Your sign in has run out. Reload the page to sign in again.");
    }
    if (response.status === 403) throw new Error("Your account may not resolve exceptions. Ask Arita if you need it.");
    if (response.status === 404) throw new Error("This exception is no longer open. Reload the list.");
    if (response.status === 422 || response.status === 409) {
      let problem = "";
      try { problem = (await response.json()).problem || ""; } catch (e) { /* no sentence */ }
      throw new Error(problem || "ARGUS did not accept this. Check it and try again.");
    }
    throw new Error("ARGUS could not answer right now (" + response.status + "). Try again in a moment.");
  }

  // Asks ARGUS for the trail of a case, as JSON or as the file.
  async function trailRequest(token, caseId, file) {
    const response = await fetch("/cases/" + encodeURIComponent(caseId) + (file ? "/trail.csv" : "/trail"), {
      method: "GET",
      headers: { Authorization: "Bearer " + (await token()) },
      cache: "no-store",
    });
    if (response.ok) return response;
    if (response.status === 401) {
      sessionStorage.removeItem(DEV_TOKEN_KEY);
      throw new Error("Your sign in has run out. Reload the page to sign in again.");
    }
    if (response.status === 403) throw new Error("Your account has no access to the trail. Ask Arita if you need it.");
    if (response.status === 404) throw new Error("ARGUS does not know this case. Reload the list.");
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

  const state = { token: null, filters: filtersFromAddress(), ticket: 0, data: null, open: null, done: "" };
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
    if (state.done) {
      view.note.textContent = state.done + " " + view.note.textContent;
      state.done = "";
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
    state.open = null;
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
    const trailButton = el("button", "Trail", "button small");
    trailButton.type = "button";
    trailButton.setAttribute("aria-expanded", "false");
    trailButton.setAttribute("aria-label", "Trail of case " + c.case_id);
    trailButton.addEventListener("click", () => togglePanel(tr, trailButton, "trail-" + c.case_id,
      "Trail of case " + c.case_id, (panel, close) => loadTrail(panel, c, close)));
    first.append(trailButton);

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
      if (c.exception_id !== null) {
        const resolve = el("button", "Resolve", "button small");
        resolve.type = "button";
        resolve.setAttribute("aria-expanded", "false");
        resolve.addEventListener("click", () => togglePanel(tr, resolve, "resolve-" + c.exception_id,
          "Resolve the exception of case " + c.case_id, (panel, close) => loadChoices(panel, c, close)));
        status.append(resolve);
      }
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

  // --- panels under a row -----------------------------------------------------------------

  // Opens a panel under the case's row, or closes it. One panel at a time.
  function togglePanel(tr, button, panelId, label, fill) {
    const wasOpen = state.open && state.open.button === button;
    if (state.open) {
      state.open.row.remove();
      state.open.button.setAttribute("aria-expanded", "false");
      state.open = null;
    }
    if (wasOpen) return;
    const row = el("tr", null, "resolve-row");
    const td = el("td", null, "resolve-cell");
    td.colSpan = COLUMNS.length;
    const panel = el("div", null, "resolve");
    panel.id = panelId;
    panel.setAttribute("aria-label", label);
    panel.setAttribute("role", "region");
    panel.append(el("p", "Loading…", "muted"));
    td.append(panel);
    row.append(td);
    tr.after(row);
    button.setAttribute("aria-expanded", "true");
    button.setAttribute("aria-controls", panel.id);
    state.open = { row, button };
    fill(panel, () => togglePanel(tr, button, panelId, label, fill));
  }

  // --- the trail of a case ------------------------------------------------------------------

  async function loadTrail(panel, c, close) {
    try {
      const t = await (await trailRequest(state.token, c.case_id, false)).json();
      panel.replaceChildren(...trailView(t, c, close));
    } catch (e) {
      panel.replaceChildren(el("p", e.message, "problem"));
    }
  }

  function actorText(step) {
    if (step.actor_kind === "system") return "ARGUS · " + step.actor;
    return step.actor_name ? step.actor_name + " · " + step.actor : "Person · " + step.actor;
  }

  function trailView(t, c, close) {
    const parts = [];
    parts.push(el("h3", "Trail of case " + c.case_id, "resolve-title"));
    parts.push(el("p", "Every step ARGUS recorded for this case, oldest first, from the email to " +
      "where it stands now (" + t.case.status_text.toLowerCase() + "). It comes from the audit log " +
      "and cannot be changed here.", "muted"));
    if (t.truncated) parts.push(el("p", "Only the first " + t.steps.length + " steps are shown.", "problem"));

    const list = el("ol", null, "trail");
    for (const s of t.steps) {
      const item = el("li", null, "trail-step");
      const when = el("time", berlinTime(new Date(s.occurred_at)), "mono sub");
      when.dateTime = s.occurred_at;
      item.append(when, el("span", s.what, "trail-what"), el("span", actorText(s), "sub mono trail-who"));
      if (s.comment) item.append(el("q", s.comment, "trail-comment"));
      if (s.facts.length) {
        // The recorded details are long (fingerprints, IDs), so they fold away.
        const more = el("details", null, "trail-more");
        more.append(el("summary", "Recorded details", "sub"));
        const facts = el("ul", null, "trail-facts");
        for (const f of s.facts) facts.append(el("li", f, "sub mono"));
        more.append(facts);
        item.append(more);
      }
      list.append(item);
    }
    parts.push(list);

    const download = el("button", "Download as file", "button primary");
    download.type = "button";
    const done = el("p");
    done.setAttribute("role", "status");
    download.addEventListener("click", async () => {
      download.disabled = true;
      done.className = "muted";
      done.textContent = "Preparing the file…";
      try {
        const response = await trailRequest(state.token, c.case_id, true);
        const blob = await response.blob();
        const named = /filename="([^"]+)"/.exec(response.headers.get("Content-Disposition") || "");
        const link = el("a");
        link.href = URL.createObjectURL(blob);
        link.download = named ? named[1] : "argus-case-" + c.case_id + "-trail.csv";
        document.body.append(link);
        link.click();
        link.remove();
        setTimeout(() => URL.revokeObjectURL(link.href), 10000);
        done.textContent = "Downloaded " + link.download + ".";
      } catch (e) {
        done.className = "problem";
        done.textContent = e.message;
      } finally {
        download.disabled = false;
      }
    });
    const closeButton = el("button", "Close", "button");
    closeButton.type = "button";
    closeButton.addEventListener("click", close);
    const buttons = el("div", null, "resolve-buttons");
    buttons.append(download, closeButton);
    parts.push(buttons, done);
    return parts;
  }

  // --- resolving an exception -------------------------------------------------------------

  async function loadChoices(panel, c, close) {
    try {
      const x = await call(state.token, "/exceptions/" + encodeURIComponent(c.exception_id) + "/choices");
      panel.replaceChildren(...resolveForm(x, close));
    } catch (e) {
      panel.replaceChildren(el("p", e.message, "problem"));
    }
  }

  function resolveForm(x, close) {
    const parts = [];
    parts.push(el("h3", x.reason_text, "resolve-title"));
    if (x.detail) parts.push(el("p", x.detail, "resolve-detail"));
    if (x.resume_text) parts.push(el("p", "Before the exception the case was " + x.resume_text.toLowerCase() + ".", "muted"));
    if (x.note) {
      const note = el("p", x.note + " ", "muted");
      const link = el("a", "Open in the exception queue");
      link.href = "/queue?exception=" + encodeURIComponent(x.exception_id);
      note.append(link);
      parts.push(note);
    }
    const cancel = el("button", "Cancel", "button");
    cancel.type = "button";
    cancel.addEventListener("click", close);
    if (!x.choices.length) {
      parts.push(cancel);
      return parts;
    }

    const form = el("form", null, "resolve-form");
    const set = el("fieldset", null, "choices");
    set.append(el("legend", "How does it end?"));
    const name = "outcome-" + x.exception_id;
    x.choices.forEach((choice, i) => {
      const id = name + "-" + i;
      const input = el("input");
      input.type = "radio";
      input.name = name;
      input.id = id;
      input.value = choice.outcome;
      input.required = true;
      const label = el("label", null, "choice" + (choice.closes ? " closes" : ""));
      label.htmlFor = id;
      label.append(el("span", choice.label, "choice-label"), el("span", choice.explain, "sub"));
      const line = el("div", null, "choice-line");
      line.append(input, label);
      set.append(line);
    });

    const comment = el("textarea", null, "comment");
    comment.id = "comment-" + x.exception_id;
    comment.required = true;
    comment.minLength = 3;
    comment.maxLength = 500;
    const commentLabel = el("label", "Why (3 to 500 characters, no personal details)");
    commentLabel.htmlFor = comment.id;
    const commentRow = el("p", null, "field");
    commentRow.append(commentLabel, comment);

    const save = el("button", "Resolve", "button primary");
    save.type = "submit";
    const buttons = el("div", null, "resolve-buttons");
    buttons.append(save, cancel);
    const result = el("p");
    result.setAttribute("role", "status");

    form.append(set, commentRow, buttons, result);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const picked = form.querySelector('input[name="' + name + '"]:checked');
      if (!picked) {
        result.className = "problem";
        result.textContent = "Pick how the exception ends.";
        return;
      }
      save.disabled = true;
      result.className = "muted";
      result.textContent = "Saving…";
      try {
        const answer = await call(state.token, "/exceptions/" + encodeURIComponent(x.exception_id) + "/resolution",
          { outcome: picked.value, comment: comment.value });
        state.done = "Case " + answer.case_id + " is resolved and now " + answer.case_status_text.toLowerCase() + "." +
          (answer.others_open ? " It has another open exception." : "");
        await refresh();
      } catch (e) {
        result.className = "problem";
        result.textContent = e.message;
        save.disabled = false;
      }
    });
    parts.push(form);
    return parts;
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
