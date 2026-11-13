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

  async function call(token, path) {
    const response = await fetch(path, {
      headers: { Authorization: "Bearer " + (await token()) },
      cache: "no-store",
    });
    if (response.ok) return response;
    if (response.status === 401) {
      sessionStorage.removeItem(DEV_TOKEN_KEY);
      throw new Error("Your sign in has run out. Reload the page to sign in again.");
    }
    if (response.status === 403) {
      throw new Error("Your account has no access to the exception queue. Ask Arita if you need it.");
    }
    if (response.status === 404) throw new Error("This exception is not open any more, or it does not exist.");
    throw new Error("ARGUS could not answer right now (" + response.status + "). Try again in a moment.");
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
    const data = await (await call(token, "/exceptions")).json();
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
      show(intro, el("p", "Nothing waits for you. ARGUS handled everything on its own.", "state"));
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
    show(intro, panels);

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
    const extra = Object.entries(x.facts || {}).map(([k, v]) =>
      [capital(words(k)), typeof v === "string" ? v : JSON.stringify(v)]);
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
      message("Loading…");
      await showQueue(token);
    } catch (e) {
      message(e && e.message ? e.message : "Something went wrong. Reload the page.", "problem");
    }
  }

  start();
})();
