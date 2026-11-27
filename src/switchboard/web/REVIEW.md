# Review rules: the web UI

Applies to `src/switchboard/web/`, the UI in `static/`. The root [REVIEW.md](../../../REVIEW.md) applies too. This file sits above `static/` so it is not served at `/static`.

## Flag

- **A layout not seen on a phone.** The pull request needs the phone preview (light, dark and phone) for anything a person sees. Long room names, wide tables and the composer are what breaks.
- **Keyboard focus lost.** Where does focus go when a click hides what had it, a sheet closes, or a list re-renders? Walk it by keyboard.
- **Wording that does not match what the code does.** Check every user-facing line against the behavior: a count, who a feature reaches, what a button does.
- **Anything that loosens the CSP.** An inline script or style, an `eval`, a new origin. Any console error, page error or CSP violation fails the e2e tests.
- **DOM built from text.** `md.js` builds Markdown with `createElement` and `textContent` only. No `innerHTML`, no `insertAdjacentHTML`, no string-built links; a link's scheme is checked.
- **A dialog whose buttons can leave the window, or a label cut short.** A dialog is never taller than the window, with its middle scrolling and Save in view, and a button's label is never cut at any of the four text sizes, on a phone or at the narrowest desktop pane. Check Larger text at 390×844 (#995).
- **A change to something a person does with no recording** in the pull request's Verification.
- **A new image or logo not pinned** by hash in `tests/unit/test_web_static_lint.py`.

## Skip

- Line-by-line review of `static/vendor/` (the vendored board and Mermaid builds). Instead, for a change there: the lockfile (`scripts/board/package-lock.json` for the board) or the upstream release it names, the hashes the static tests pin, and advisories for the dependency that changed. `scripts/security_scan.py` queries OSV for Mermaid only, not the board's Excalidraw and React.
- Formatting that ESLint accepts.
