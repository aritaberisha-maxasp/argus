# Exception queue page files

`queue.html`, `app.js` and `app.css` are the exception queue page (#126). The server fills in the sign in settings and serves these files as they are.

`msal-browser.min.js` is Microsoft's sign in library for single page apps, `@azure/msal-browser` version 4.30.0 (MIT licence, `msal-browser.LICENSE`), taken unchanged from `lib/msal-browser.min.js` in the npm package. It is kept here so the page loads no script from another site. Its SHA-256 is pinned in `tests/unit/test_queue_page.py`: to update it, take the file from a newer npm package, check the npm integrity of the package, and update the version here and the hash in the test.
