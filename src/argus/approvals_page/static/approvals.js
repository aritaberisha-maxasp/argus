// The ARGUS approval page (#38, AP-02, AP-03, AP-07, AP-08, PR-04), in the look of the queue
// page (#152).
//
// The approval request email links to /approvals/<case ID>. After Microsoft sign in the
// approver sees the invoices that wait for them on the left and the chosen one on the right:
// the invoice image, the key data and the G/L account and tax key ARGUS proposes, with where
// that came from. They approve, or reject with a comment, or send the invoice back with a
// question (#192). Benjamin answers it in the exception queue and the invoice comes back with
// the answer shown above it. Before deciding they may change the account or tax key, with a
// reason. Finance and admin see every waiting invoice, read only.
//
// ARGUS and its database check every step again: only the person asked may decide, once.
// A second click, a second tab or an old link shows what was decided, by whom and when.
//
// Sign in goes through the exception queue page, the one address registered in Entra
// (docs/operations/sign-in.md). Everything from the server is put on the page as text
// (textContent), never as HTML, so nothing read from an invoice can run here.
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
  const WIDE = window.matchMedia("(min-width: 960px)");

  const state = { token: null, rows: [], selected: null, view: null, fileUrl: null, done: "" };

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

  function berlinTime(iso) {
    if (!iso) return "";
    const out = {};
    const format = new Intl.DateTimeFormat("de-DE", {
      timeZone: BERLIN, day: "2-digit", month: "2-digit", year: "numeric",
      hour: "2-digit", minute: "2-digit", hourCycle: "h23",
    });
    for (const part of format.formatToParts(new Date(iso))) out[part.type] = part.value;
    return out.day + "." + out.month + "." + out.year + " " + out.hour + ":" + out.minute;
  }

  // A date as read on the invoice (2026-10-02) in German order; anything else as it came.
  function day(value) {
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value || "");
    return m ? m[3] + "." + m[2] + "." + m[1] : (value || "");
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

  function fact(label, value, mono) {
    const box = el("div", null, "fact");
    box.append(el("dt", label), el("dd", value || "not read", mono ? "mono" : ""));
    return box;
  }

  // --- sign in --------------------------------------------------------------------------

  function caseInAddress() {
    const m = /^\/approvals\/(\d+)$/.exec(window.location.pathname);
    return m ? m[1] : null;
  }

  function returnHere() {
    const id = caseInAddress();
    return id ? "/approvals/" + id : "/approvals";
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

  // A refusal with a sentence for the person (422, 409) shows that sentence. A 409 that says
  // the invoice no longer waits carries what stands now, so the page can show it.
  async function call(path, body) {
    const response = await fetch(path, {
      method: body === undefined ? "GET" : "POST",
      headers: Object.assign({ Authorization: "Bearer " + (await state.token()) },
        body === undefined ? {} : { "Content-Type": "application/json" }),
      body: body === undefined ? undefined : JSON.stringify(body),
      cache: "no-store",
    });
    if (response.ok) return response.json();
    if (response.status === 401) {
      sessionStorage.removeItem(DEV_TOKEN_KEY);
      throw new Error("Your sign in has run out. Reload the page to sign in again.");
    }
    if (response.status === 403) {
      throw new Error("This invoice does not wait for your decision. If you think it should, ask Benjamin.");
    }
    if (response.status === 404) throw new Error("ARGUS has no approval request for this invoice.");
    if (response.status === 422 || response.status === 409) {
      let answer = {};
      try { answer = await response.json(); } catch (e) { /* no sentence */ }
      const problem = new Error(answer.problem || "ARGUS did not accept this. Check it and try again.");
      problem.approval = answer.approval || null;
      throw problem;
    }
    throw new Error("ARGUS could not answer right now (" + response.status + "). Try again in a moment.");
  }

  async function loadFile(caseId, documentId) {
    const response = await fetch("/approval-requests/" + caseId + "/files/" + documentId, {
      headers: { Authorization: "Bearer " + (await state.token()) },
      cache: "no-store",
    });
    if (!response.ok) throw new Error("The invoice file could not be loaded right now.");
    return response.blob();
  }

  // --- the list -------------------------------------------------------------------------

  function rowButton(row) {
    const button = el("button", null, "row");
    button.type = "button";
    if (String(row.case_id) === String(state.selected)) button.setAttribute("aria-current", "true");
    const top = el("span", null, "row-top");
    const name = el("span", null, "row-name");
    name.append(el("span", null, "dot" + (row.may_decide ? " flag" : "")),
      el("span", row.vendor_name || ("Vendor account " + (row.vendor_account || "?")), "row-subject"));
    top.append(name, el("span", money(row.gross_cents, row.currency), "row-time"));
    button.append(top, el("span", "Invoice " + (row.document_number || "without number") +
      (row.due_date ? ", due " + day(row.due_date) : ""), "row-reason"));
    const who = row.may_decide
      ? (row.recipient_role === "deputy" ? "Waits for you, as deputy of " + row.on_behalf_of_name : "Waits for you")
      : "Waits for " + row.recipient_name + (row.recipient_role === "deputy" ? ", deputy of " + row.on_behalf_of_name : "");
    button.append(el("span", who, "row-sender"));
    button.addEventListener("click", () => select(row.case_id));
    return button;
  }

  function listPanel() {
    const panel = el("section", null, "panel list");
    panel.setAttribute("aria-label", "Invoices waiting for approval");
    if (!state.rows.length) {
      panel.append(el("p", "No invoice waits for your decision right now.", "empty"));
      return panel;
    }
    const list = el("ul", null, "rows");
    for (const row of state.rows) {
      const item = el("li");
      item.append(rowButton(row));
      list.append(item);
    }
    panel.append(list);
    return panel;
  }

  // --- one invoice ----------------------------------------------------------------------

  function keyData(v) {
    const facts = el("dl", null, "facts");
    facts.append(
      fact("Vendor", v.vendor_name),
      fact("Vendor account", v.vendor_account, true),
      fact("Invoice number", v.document_number, true),
      fact("Invoice date", day(v.invoice_date), true),
      fact("Due date", day(v.due_date), true),
      fact("Net", money(v.net_cents, v.currency), true),
      fact("VAT", money(v.vat_cents, v.currency), true),
      fact("Gross", money(v.gross_cents, v.currency), true),
    );
    return facts;
  }

  function proposalBlock(v) {
    const block = el("section", null, "block");
    block.append(el("h3", "G/L account and tax key"));
    const p = v.proposal;
    if (!p) {
      // For example after the vendor changed while a question was open (#192): the person
      // asked enters the account and tax key, then approves.
      block.append(el("p", v.may_decide
        ? "ARGUS has no account proposal for this vendor. Enter the G/L account and tax key, then approve."
        : "ARGUS has no account proposal for this invoice, so it cannot be approved yet.", "problem"));
      if (v.may_decide) block.append(changeForm(v));
      return block;
    }
    const facts = el("dl", null, "facts");
    facts.append(fact("G/L account", p.gl_account + " (" + p.chart + ")", true));
    const tax = el("div", null, "fact");
    tax.append(el("dt", "Tax key"));
    if (p.tax_key) {
      tax.append(el("dd", p.tax_key, "mono"));
    } else {
      // A blank tax key is said in words, never shown as an empty field (compliance, PR 190).
      tax.append(el("dd", "No tax key", "no-tax-key"));
    }
    facts.append(tax);
    block.append(facts);
    if (!p.tax_key) {
      block.append(el("p", "No input VAT will be booked for this invoice. Change the tax key if VAT should be reclaimed.", "warning"));
    }
    block.append(el("p", (p.changed_at_approval ? "Changed" : p.source === "person" ? "Entered" : "Proposed") +
      " " + p.origin.replace(/^(changed|entered) /, ""), "origin" + (p.changed_at_approval ? " changed" : "")));
    if (v.may_decide) block.append(changeForm(v));
    return block;
  }

  function field(labelText, input) {
    const box = el("p", null, "field");
    const label = el("label", labelText);
    input.id = "f-" + Math.random().toString(36).slice(2);
    label.htmlFor = input.id;
    box.append(label, input);
    return box;
  }

  function changeForm(v) {
    const wrap = el("div", null, "change");
    const open = el("button", v.proposal ? "Change account" : "Enter account", "button");
    open.type = "button";
    const form = el("form", null, "correction");
    form.hidden = true;
    const gl = el("input");
    gl.inputMode = "numeric";
    gl.autocomplete = "off";
    gl.value = v.proposal ? v.proposal.gl_account : "";
    const key = el("input");
    key.inputMode = "numeric";
    key.autocomplete = "off";
    key.value = v.proposal ? v.proposal.tax_key : "";
    const reason = el("textarea", null, "reason");
    reason.maxLength = 500;
    reason.placeholder = "Why is this account right? For example: software licence, not office supplies";
    const result = el("p", "", "problem");
    result.setAttribute("role", "status");
    const save = el("button", "Save account", "button primary");
    save.type = "submit";
    const cancel = el("button", "Cancel", "button");
    cancel.type = "button";
    const actions = el("div", null, "actions");
    actions.append(save, cancel);
    form.append(field("G/L account (four digits)", gl), field("Tax key (leave empty for none)", key),
      field("Reason (required, at least 3 characters)", reason), result, actions);
    open.addEventListener("click", () => { form.hidden = false; open.hidden = true; gl.focus(); });
    cancel.addEventListener("click", () => { form.hidden = true; open.hidden = false; result.textContent = ""; });
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (reason.value.trim().length < 3) {
        result.textContent = "Say briefly why this account is right (at least 3 characters).";
        reason.focus();
        return;
      }
      save.disabled = true;
      result.textContent = "";
      try {
        state.view = await call("/approval-requests/" + v.case_id + "/account", {
          request_id: v.request.request_id, gl_account: gl.value, tax_key: key.value, reason: reason.value,
        });
        state.done = "The account is changed. Check it, then approve or reject.";
        drawDetail();
      } catch (e) {
        if (e.approval) { state.view = e.approval; state.done = e.message; drawDetail(); return; }
        result.textContent = e.message;
        save.disabled = false;
      }
    });
    wrap.append(open, form);
    return wrap;
  }

  function fileBlock(v) {
    const block = el("section", null, "block");
    block.append(el("h3", "The invoice"));
    if (v.in_email_text) {
      block.append(el("p", "This invoice was written in the email itself, so there is no file to show. The data above is what ARGUS read from it."));
      return block;
    }
    if (!v.file) {
      block.append(el("p", "ARGUS has no stored file for this invoice."));
      return block;
    }
    const frame = el("div", null, "invoice-file");
    frame.append(el("p", "Loading the invoice…", "sub"));
    block.append(frame);
    const f = v.file;
    loadFile(v.case_id, f.document_id).then((blob) => {
      if (state.fileUrl) URL.revokeObjectURL(state.fileUrl);
      state.fileUrl = URL.createObjectURL(blob);
      const open = el("a", f.viewable ? "Open the invoice in a new tab" : "Download the invoice file", "source-link");
      open.href = state.fileUrl;
      open.target = "_blank";
      open.rel = "noopener";
      if (!f.viewable) open.download = f.filename || ("invoice-" + v.case_id);
      if (f.viewable && f.content_type === "application/pdf") {
        const pdf = el("iframe");
        pdf.src = state.fileUrl + (f.pages && f.pages.length ? "#page=" + f.pages[0] : "");
        pdf.title = "Invoice " + (v.document_number || "");
        frame.replaceChildren(pdf, open);
      } else if (f.viewable) {
        const img = el("img");
        img.src = state.fileUrl;
        img.alt = "Invoice " + (v.document_number || "");
        frame.replaceChildren(img, open);
      } else {
        frame.replaceChildren(el("p", "This file cannot be shown here."), open);
      }
    }).catch((e) => frame.replaceChildren(el("p", e.message, "problem")));
    return block;
  }

  // Spaces of any kind, tabs and line breaks at either end do not count, as on the server.
  function trimmed(text) {
    return String(text || "").replace(/^[\s\u180e\u200b\ufeff]+|[\s\u180e\u200b\ufeff]+$/g, "");
  }

  // Questions the approver sent back and the answers from the exception queue (#192).
  function queriesBlock(v) {
    const block = el("section", null, "block queries");
    block.append(el("h3", v.queries.length === 1 ? "Question and answer" : "Questions and answers"));
    for (const q of v.queries) {
      const who = q.asked_by_name + (q.asked_as === "deputy" ? ", as deputy of " + q.on_behalf_of_name : "");
      block.append(el("p", "Question from " + who + " on " + berlinTime(q.asked_at) + ":", "sub"));
      block.append(el("blockquote", q.question, "comment"));
      if (q.outcome === "answered") {
        block.append(el("p", "Answer from " + q.answered_by_name + (q.same_person ? " (the same person who asked)" : "") +
          " on " + berlinTime(q.answered_at) + ":", "sub"));
        block.append(el("blockquote", q.answer, "comment answer"));
      } else if (q.outcome === "rejected") {
        block.append(el("p", "Rejected by " + q.answered_by_name + " on " + berlinTime(q.answered_at) + " instead of answering:", "sub"));
        block.append(el("blockquote", q.answer, "comment"));
      } else {
        block.append(el("p", "Waiting for an answer from Benjamin.", "note"));
      }
    }
    return block;
  }

  function decidedBlock(v) {
    const d = v.decision;
    const block = el("section", null, "block decided " + d.decision);
    block.append(el("h3", "Decision"));
    const verb = d.decision === "approved" ? "Approved" : "Rejected";
    const as = d.decided_as === "deputy" ? ", as deputy of " + d.on_behalf_of_name : "";
    block.append(el("p", verb + " by " + d.decided_by_name + as + " on " + berlinTime(d.decided_at) + ".", "sentence"));
    if (d.comment) {
      const quote = el("blockquote", d.comment, "comment");
      block.append(quote);
    }
    return block;
  }

  function decisionBlock(v) {
    const block = el("section", null, "block decide");
    block.append(el("h3", "Your decision"));
    const comment = el("textarea", null, "reason");
    comment.maxLength = 500;
    comment.placeholder = "Comment (required to reject or to ask a question, optional to approve)";
    const result = el("p", "", "problem");
    result.setAttribute("role", "status");
    const approve = el("button", "Approve", "button primary");
    approve.type = "button";
    const reject = el("button", "Reject", "button");
    reject.type = "button";
    const ask = el("button", "Send back with a question", "button");
    ask.type = "button";
    if (!v.proposal) approve.disabled = true;
    const actions = el("div", null, "actions");
    actions.append(approve, reject, ask);
    async function send(choice) {
      if (choice !== "approve" && trimmed(comment.value).length < 3) {
        result.textContent = choice === "reject"
          ? "Say briefly why you reject the invoice (at least 3 characters)."
          : "Write your question in the comment box (at least 3 characters).";
        comment.focus();
        return;
      }
      approve.disabled = true;
      reject.disabled = true;
      ask.disabled = true;
      result.textContent = "";
      try {
        if (choice === "question") {
          state.view = await call("/approval-requests/" + v.case_id + "/question", {
            request_id: v.request.request_id, question: comment.value,
          });
        } else {
          state.view = await call("/approval-requests/" + v.case_id + "/decision", {
            request_id: v.request.request_id, choice, comment: comment.value || null,
            proposal_id: v.proposal ? v.proposal.proposal_id : null,
          });
        }
        state.done = choice === "approve" ? "Thank you. The invoice is approved."
          : choice === "reject" ? "The invoice is rejected and closed."
            : "Your question is sent to Benjamin. You get a new approval email once he has answered it.";
        await refreshList();
        drawDetail();
      } catch (e) {
        if (e.approval) { state.view = e.approval; state.done = e.message; await refreshList(); drawDetail(); return; }
        result.textContent = e.message;
        approve.disabled = !v.proposal;
        reject.disabled = false;
        ask.disabled = false;
      }
    }
    approve.addEventListener("click", () => send("approve"));
    reject.addEventListener("click", () => send("reject"));
    ask.addEventListener("click", () => send("question"));
    block.append(field("Comment", comment), result, actions);
    return block;
  }

  function detailPanel() {
    const panel = el("section", null, "panel detail");
    panel.setAttribute("aria-live", "polite");
    const v = state.view;
    if (!v) {
      panel.append(el("p", state.rows.length ? "Pick an invoice on the left." : "Nothing to decide.", "state"));
      return panel;
    }
    const head = el("div", null, "detail-head");
    const title = el("div", null, "detail-title");
    title.append(el("span", v.decision ? (v.decision.decision === "approved" ? "Approved" : "Rejected") : v.status_text,
      "pill" + (v.may_decide ? " flag" : "")));
    title.append(el("h2", (v.kind === "incoming_credit_note" ? "Credit note " : "Invoice ") +
      (v.document_number || "without number") + " from " + (v.vendor_name || "an unread vendor")));
    const asked = v.request.recipient_role === "deputy"
      ? "Sent to " + v.request.recipient_name + " as deputy of " + v.request.on_behalf_of_name
      : "Sent to " + v.request.recipient_name;
    title.append(el("span", asked + (v.request.sent_at ? " on " + berlinTime(v.request.sent_at) : ""), "sub"));
    head.append(title);
    panel.append(head);
    if (state.done) panel.append(el("p", state.done, "done"));
    if (!v.may_decide && !v.decision && v.waiting) {
      panel.append(el("p", "You can look at this invoice, but only " + v.request.recipient_name + " can decide on it.", "note"));
    }
    if (v.queries && v.queries.length) panel.append(queriesBlock(v));
    panel.append(keyData(v), proposalBlock(v));
    if (v.decision) panel.append(decidedBlock(v));
    else if (v.may_decide) panel.append(decisionBlock(v));
    else if (v.waiting_for_answer) panel.append(el("p", "This invoice was sent back with a question. It comes back here once Benjamin has answered it.", "note"));
    else if (!v.waiting) panel.append(el("p", "This invoice is " + v.status_text.toLowerCase() + ".", "note"));
    panel.append(fileBlock(v));
    return panel;
  }

  function draw() {
    const intro = el("div", null, "intro");
    const text = el("div", null, "intro-text");
    text.append(el("span", "Approvals", "eyebrow"));
    const h1 = el("h1");
    const mine = state.rows.filter((r) => r.may_decide).length;
    h1.append(el("span", String(mine), "count"), mine === 1 ? " invoice waits for you" : " invoices wait for you");
    text.append(h1);
    intro.append(text);
    const panels = el("div", null, "panels");
    panels.append(listPanel(), detailPanel());
    show(intro, panels);
  }

  function drawDetail() {
    const old = main.querySelector(".detail");
    if (!old) { draw(); return; }
    old.replaceWith(detailPanel());
    const list = main.querySelector(".list");
    if (list) list.replaceWith(listPanel());
  }

  async function select(caseId) {
    state.selected = String(caseId);
    state.done = "";
    if (window.location.pathname !== "/approvals/" + caseId) {
      history.pushState(null, "", "/approvals/" + caseId);
    }
    try {
      state.view = await call("/approval-requests/" + encodeURIComponent(caseId));
    } catch (e) {
      state.view = null;
      draw();
      const detail = main.querySelector(".detail");
      if (detail) detail.replaceChildren(el("p", e.message, "state problem"));
      return;
    }
    draw();
    if (!WIDE.matches) {
      const detail = main.querySelector(".detail");
      if (detail) detail.scrollIntoView({ block: "start" });
    }
  }

  async function refreshList() {
    const answer = await call("/approval-requests");
    state.rows = answer.approvals;
    if (answer.sees_all) {
      for (const id of ["nav-queue", "nav-invoices"]) {
        const link = document.getElementById(id);
        if (link) link.hidden = false;
      }
    }
  }

  // --- start ----------------------------------------------------------------------------

  async function start() {
    try {
      const token = await signIn();
      if (!token) return;
      state.token = token;
      await refreshList();
      const wanted = caseInAddress() || (state.rows.find((r) => r.may_decide) || {}).case_id;
      if (wanted) await select(wanted);
      else draw();
    } catch (e) {
      message(e && e.message ? e.message : "Something went wrong. Reload the page.", "problem");
    }
  }

  window.addEventListener("popstate", () => {
    const id = caseInAddress();
    if (id) select(id);
  });

  start();
})();
