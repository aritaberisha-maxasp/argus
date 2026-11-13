// The ARGUS exception queue page (#126, EX-01, EX-02).
//
// Signs the person in with Microsoft (ADR 0008), then asks ARGUS for the queue with the access
// token. Everything from the server is put on the page as text (textContent), never as HTML,
// so nothing in an email or a file name can run here. The token stays in session storage of
// this tab and is never put in a URL.
"use strict";

(function () {
  const setting = (name) => {
    const m = document.querySelector('meta[name="argus-sign-in-' + name + '"]');
    return m ? m.getAttribute("content") : "";
  };
  const main = document.getElementById("main");
  const RETURN_KEY = "argus-return-to";
  const DEV_TOKEN_KEY = "argus-dev-token";

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
    show(el("p", text, className));
  }

  function words(value) {
    return value ? String(value).replace(/_/g, " ") : "";
  }

  function when(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    return d.toLocaleString("de-DE", { dateStyle: "medium", timeStyle: "short", timeZone: "Europe/Berlin" });
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
    const dl = el("dl");
    for (const [label, value] of entries) {
      if (value === null || value === undefined || value === "") continue;
      dl.append(el("dt", label), el("dd", value));
    }
    return dl;
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
      // The demo mailbox page (#135) signs in through this page and comes back here.
      if (back === "/mailbox") {
        window.location.replace("/mailbox");
        return null;
      }
      if (/^(\?exception=\d+)?$/.test(back)) history.replaceState(null, "", "/queue" + back);
    }
    app.setActiveAccount(account);
    document.getElementById("who").textContent = account.name || account.username || "";
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
      document.getElementById("who").textContent = "local test sign in";
      return async () => saved;
    }
    const form = el("form");
    const box = el("textarea");
    box.setAttribute("aria-label", "Access token");
    form.append(
      el("h1", "Local sign in"),
      el("p", "This ARGUS uses the fake sign in for development. Paste a token from the fake " +
        "(see standins/entra/README.md).", "muted"),
      box,
      el("p"),
      el("button", "Sign in")
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

  // --- the list -------------------------------------------------------------------------

  async function showList(token) {
    const data = await (await call(token, "/exceptions")).json();
    const rows = data.exceptions;
    const heading = el("h1", "Open exceptions");
    const count = el("p", rows.length === 1 ? "1 open exception, oldest first."
      : rows.length + " open exceptions, oldest first.", "muted");
    if (!rows.length) {
      show(heading, el("p", "Nothing waits for you. ARGUS handled everything on its own.", "muted"));
      return;
    }
    const table = el("table");
    const head = el("tr");
    for (const [label, cls] of [["Case", ""], ["Reason", ""], ["Age (working days)", "num"],
      ["Sender", ""], ["Opened", ""]]) {
      head.append(el("th", label, cls));
    }
    table.append(el("thead"));
    table.tHead.append(head);
    const body = el("tbody");
    for (const row of rows) {
      const tr = el("tr");
      const link = el("a", subject(row));
      link.href = "/queue?exception=" + encodeURIComponent(row.exception_id);
      const first = el("td");
      first.append(link);
      if (row.case_kind) first.append(" ", el("span", words(row.case_kind), "tag"));
      tr.append(
        first,
        el("td", row.reason_text),
        el("td", row.age_working_days, "num"),
        el("td", row.sender || "", row.sender ? "" : "muted"),
        el("td", when(row.opened_at))
      );
      body.append(tr);
    }
    table.append(body);
    show(heading, count, table);
  }

  function subject(row) {
    if (row.case_id != null) return "Case " + row.case_id;
    if (row.email_id != null) return "Email " + row.email_id;
    if (row.statement_id != null) return "Bank statement " + row.statement_id;
    return "ARGUS itself";
  }

  // --- one exception --------------------------------------------------------------------

  async function showOne(token, id) {
    const x = await (await call(token, "/exceptions/" + encodeURIComponent(id))).json();
    const back = el("a", "← All open exceptions");
    back.href = "/queue";
    const backLine = el("p");
    backLine.append(back);
    const parts = [backLine, el("h1", subject(x.case ? { case_id: x.case.id } : x) + ": " + x.reason_text)];
    parts.push(el("p", "Open for " + x.age_working_days + " working day" +
      (x.age_working_days === 1 ? "" : "s") + ", since " + when(x.opened_at) + ".", "muted"));

    parts.push(el("h2", "Why ARGUS stopped"));
    parts.push(el("p", x.detail));
    const facts = Object.entries(x.facts || {}).map(([k, v]) =>
      [words(k), typeof v === "string" ? v : JSON.stringify(v)]);
    if (facts.length) parts.push(pairs(facts));

    if (x.case) {
      const c = x.case;
      parts.push(el("h2", "Case"));
      parts.push(pairs([
        ["Case", c.id],
        ["Kind", words(c.kind)],
        ["Status", words(c.status)],
        ["Goes back to", words(c.resume_status)],
        ["Document number", c.document_number],
        ["Amount", money(c.gross_cents, c.currency)],
      ]));
    }

    if (x.email_id !== null) {
      parts.push(el("h2", "Original email"));
      if (x.email) {
        parts.push(pairs([
          ["From", x.email.sender],
          ["To", x.email.to],
          ["Cc", x.email.cc],
          ["Date", x.email.date],
          ["Subject", x.email.subject],
          ["Sender domain checked", x.sender],
        ]));
        parts.push(el("pre", x.email.body || "(no text in the email)", "body"));
      } else {
        parts.push(el("p", x.email_problem || "The email is not available.", "problem"));
      }
      parts.push(el("h2", "Files as stored"));
      parts.push(files(token, x));
    }
    show(...parts);
  }

  function files(token, x) {
    if (!x.files.length) return el("p", "No files are recorded for this email.", "muted");
    const table = el("table");
    const head = el("tr");
    for (const [label, cls] of [["File", ""], ["Type", ""], ["Size", "num"], ["", ""]]) {
      head.append(el("th", label, cls));
    }
    table.append(el("thead"));
    table.tHead.append(head);
    const body = el("tbody");
    for (const f of x.files) {
      const name = f.kind === "email" ? "The email as received (.eml)" : (f.filename || "Attachment " + f.document_id);
      const label = el("td", name);
      if (x.case && f.linked_to_case) label.append(" ", el("span", "this case", "tag"));
      const button = el("button", "Download");
      button.type = "button";
      button.addEventListener("click", () => download(token, x.exception_id, f, button));
      const action = el("td");
      action.append(button);
      label.title = "SHA-256 " + f.fingerprint;
      const tr = el("tr");
      tr.append(label, el("td", f.content_type), el("td", size(f.size_bytes), "num"), action);
      body.append(tr);
    }
    table.append(body);
    return table;
  }

  async function download(token, exceptionId, file, button) {
    button.disabled = true;
    try {
      const response = await call(token, "/exceptions/" + encodeURIComponent(exceptionId) +
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
      const id = new URLSearchParams(window.location.search).get("exception");
      if (id && /^\d+$/.test(id)) await showOne(token, id);
      else await showList(token);
    } catch (e) {
      message(e && e.message ? e.message : "Something went wrong. Reload the page.", "problem");
    }
  }

  start();
})();
