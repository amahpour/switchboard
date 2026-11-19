# Review rules: the web UI

Applies to `src/switchboard/web/static/`. The root [REVIEW.md](../../../../REVIEW.md) applies too.

## Flag

- **A layout not seen on a phone.** The pull request needs the phone preview (light, dark and phone) for anything a person sees. Long room names, wide tables and the composer are what breaks.
- **Keyboard focus lost.** Where does focus go when a click hides what had it, a sheet closes, or a list re-renders? Walk it by keyboard.
- **Wording that does not match what the code does.** Check every user-facing line against the behavior: a count, who a feature reaches, what a button does.
- **Anything that loosens the CSP.** An inline script or style, an `eval`, a new origin. Any console error, page error or CSP violation fails the e2e tests.
- **DOM built from text.** `md.js` builds Markdown with `createElement` and `textContent` only. No `innerHTML`, no `insertAdjacentHTML`, no string-built links; a link's scheme is checked.
- **A change to something a person does with no recording** in the pull request's Verification.
- **A new image or logo not pinned** by hash in `tests/unit/test_web_static_lint.py`.

## Skip

- `vendor/` (the vendored board and Mermaid files): review a bump by its pinned version and the OSV check, not line by line.
- Formatting that ESLint accepts.
