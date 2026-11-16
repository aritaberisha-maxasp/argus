// The ARGUS fake mailbox page, demo mode only (#135, IN-04, IN-06).
//
// Shows every folder of the fake mailbox with the subject, sender and received time of each
// email, so the demo can show where ARGUS sorted an email. Read only.
//
// Sign in goes through the exception queue page, which is the address registered in Entra
// (docs/operations/sign-in.md): without a sign in in this tab, the page sends the person to the
// Microsoft sign in by way of /queue, and /queue sends them back here. Everything from the
// server is put on the page as text (textContent), never as HTML.
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

  // The round badge in the header (#152): initials, the full name for screen readers.
  function signedInAs(name) {
    const badge = document.getElementById("who");
    const parts = String(name || "").split(/[\s@._-]+/).filter(Boolean);
    const initials = parts.length > 1 ? parts[0][0] + parts[parts.length - 1][0] : (parts[0] || "").slice(0, 1);
    badge.textContent = initials.toUpperCase();
    badge.setAttribute("aria-label", name ? "Signed in as " + name : "local test sign in");
    badge.title = name || "local test sign in";
  }

  function when(iso) {
    if (!iso) return "";
    return new Date(iso).toLocaleString("de-DE", {
      dateStyle: "medium", timeStyle: "short", timeZone: "Europe/Berlin",
    });
  }

  // --- sign in --------------------------------------------------------------------------

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
      sessionStorage.setItem(RETURN_KEY, "/mailbox");
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
          sessionStorage.setItem(RETURN_KEY, "/mailbox");
          await app.acquireTokenRedirect({ scopes, account });
        }
        throw e;
      }
    };
  }

  // --- talking to ARGUS -----------------------------------------------------------------

  async function load(token) {
    const response = await fetch("/demo/mailbox", {
      headers: { Authorization: "Bearer " + (await token()) },
      cache: "no-store",
    });
    if (response.ok) return response.json();
    if (response.status === 401) {
      sessionStorage.removeItem(DEV_TOKEN_KEY);
      throw new Error("Your sign in has run out. Reload the page to sign in again.");
    }
    if (response.status === 403) {
      throw new Error("Your account has no access to the fake mailbox. Ask Arita if you need it.");
    }
    if (response.status === 404) {
      throw new Error("The fake mailbox is only there while ARGUS runs in demo mode.");
    }
    throw new Error("ARGUS could not read the fake mailbox right now (" + response.status +
      "). Try again in a moment.");
  }

  // --- the page -------------------------------------------------------------------------

  function anchor(path) {
    return "folder-" + path.toLowerCase().replace(/[^a-z0-9]+/g, "-");
  }

  function folderSection(folder) {
    const section = el("section", null, "folder");
    section.id = anchor(folder.path);
    const n = folder.emails.length;
    section.append(el("h2", folder.path + " (" + n + (folder.more ? "+" : "") + ")"));
    if (!n) {
      section.append(el("p", "No emails.", "muted"));
      return section;
    }
    const table = el("table");
    const head = el("tr");
    for (const label of ["Subject", "From", "Received"]) head.append(el("th", label));
    table.append(el("thead"));
    table.tHead.append(head);
    const body = el("tbody");
    for (const e of folder.emails) {
      const from = e.sender_name && e.sender_address
        ? e.sender_name + " <" + e.sender_address + ">"
        : (e.sender_name || e.sender_address);
      const tr = el("tr");
      tr.append(
        el("td", e.subject || "(no subject)", "subject"),
        el("td", from),
        el("td", when(e.received_at), "when")
      );
      body.append(tr);
    }
    table.append(body);
    section.append(table);
    if (folder.more) section.append(el("p", "Only the newest emails are shown.", "muted"));
    return section;
  }

  async function showMailbox(token) {
    const data = await load(token);
    const refresh = el("button", "Refresh", "button");
    refresh.type = "button";
    refresh.addEventListener("click", () => run(token));
    const toolbar = el("div", null, "toolbar");
    toolbar.append(
      el("span", "Mailbox " + data.mailbox + ", read at " +
        new Date().toLocaleTimeString("de-DE", { timeZone: "Europe/Berlin" }) + ".", "muted"),
      refresh
    );
    const list = el("ul", null, "folders");
    for (const folder of data.folders) {
      const a = el("a", folder.path, folder.emails.length ? "" : "empty");
      a.href = "#" + anchor(folder.path);
      a.append(el("span", folder.emails.length + (folder.more ? "+" : ""), "count"));
      const li = el("li");
      li.append(a);
      list.append(li);
    }
    const intro = el("div", null, "intro-text");
    intro.append(
      el("span", "Demo only · fake mailbox", "eyebrow"),
      el("h1", "Fake mailbox"),
      el("p", "The synthetic accounts mailbox of the demo, read only. It shows where ARGUS " +
        "sorted each email. At go live you look at the real mailbox in Outlook instead.", "lead")
    );
    const folders = el("div", null, "panel mailbox");
    folders.append(...data.folders.map(folderSection));
    show(intro, toolbar, list, folders);
  }

  async function run(token) {
    try {
      await showMailbox(token);
    } catch (e) {
      message(e && e.message ? e.message : "Something went wrong. Reload the page.", "problem");
    }
  }

  async function start() {
    try {
      const token = await signIn();
      if (!token) return;
      message("Loading…");
      await run(token);
    } catch (e) {
      message(e && e.message ? e.message : "Something went wrong. Reload the page.", "problem");
    }
  }

  start();
})();
