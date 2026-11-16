# Exception queue page files

`queue.html`, `app.js` and `app.css` are the exception queue page (#126), in Arita's design of 13 Nov 2026 (#152, source copy in `docs/design/queue-page/`). The server fills in the sign in settings and serves these files as they are. All styling lives in `app.css`, because the page CSP allows no inline style.

`favicon.svg` and `favicon-16.svg` are the ARGUS mark for the browser tab. The 16px one has a thicker A and a bigger dot so it still reads at that size.

`msal-browser.min.js` is Microsoft's sign in library for single page apps, `@azure/msal-browser` version 4.30.0 (MIT licence, `msal-browser.LICENSE`), taken unchanged from `lib/msal-browser.min.js` in the npm package. It is kept here so the page loads no script from another site. Its SHA-256 is pinned in `tests/unit/test_queue_page.py`: to update it, take the file from a newer npm package, check the npm integrity of the package, and update the version here and the hash in the test.

The `Geist-*.woff2` and `GeistMono-*.woff2` files are the Geist and Geist Mono fonts by Vercel (SIL Open Font License 1.1, `geist.LICENSE`), taken unchanged from `dist/fonts/` in the npm package `geist` version 1.9.2 (npm integrity `sha512-UU1x2q5Khob0+i4csFvXk1Ki4DePdu7xISuXyEcj1tzQMg64oN7Kbuy4e5z5QhcUWbbvOMJNs3ZxYQqpsEMV3w==`). Only the weights the page uses are kept: Geist 400, 500, 600 and Geist Mono 400, 500. They are served from `/queue/` so the page loads no font from another site. Their SHA-256 hashes are pinned in the same test; update them the same way as the sign in library.
