# How the web UI is built: spacing, text and controls

Applies to `src/switchboard/web/static/`. [REVIEW.md](REVIEW.md), beside this file, says what a review flags. This file says how to build it, so there is less to flag. Like REVIEW.md, it sits above `static/`, so it isn't served.

These rules come from a sweep of every dialog, menu and panel (#995). Without them, the stylesheet had grown:

- 28 different paddings, 22 margins, 15 gaps, 19 corner radii and 18 font sizes;
- hints floating away from their checkboxes;
- dialogs whose Save button left the window;
- button labels cut short;
- 24 px targets on phones.

Each rule below says which of those it prevents.

## The tokens

They are the custom properties in the first section of `style.css`. Every new rule uses them.

| Kind | Tokens | Values |
|---|---|---|
| Spacing | `--sp-1` … `--sp-6` | 4, 8, 12, 16, 24, 32 px |
| Type | `--fs-xs`, `--fs-sm`, `--fs-md`, `--fs-lg`, `--fs-xl`, `--fs-2xl` | 11, 12, 13, 15, 18, 24 px, each times `--text-scale` |
| Corners | `--r-sm`, `--r-md`, `--r-lg`, `--r-pill` | 4, 8, 12 px, and round |
| Controls | `--ctl-h` | 32 px times `--text-scale`: a control's least height |
| Touch | `--target` | 32 px, and 44 px where the pointer is coarse (a phone or tablet) |

`--fs-md` is the body text. `--fs-sm` is secondary text and labels, and `--fs-xs` is fine print. Nothing is smaller than `--fs-xs`, because text under 11 px isn't readable at arm's length.

**A raw pixel value needs a reason.** That covers padding, margin, gap, font size and corner radius: a hairline, say, or an icon's box. The reason goes in a comment on that line. `tests/unit/test_css_tokens.py` counts the raw values left in those properties, and the count only goes down. A change that converts rules lowers the test's number to the new count, the way the tool-list budget does.

## Spacing says what belongs together

The space between two things shows whether they belong together:

| Between | Space |
|---|---|
| a label and its hint | 4 px (`--sp-1`) |
| the parts of one control | 8 px (`--sp-2`) |
| fields | 12 px (`--sp-3`) |
| sections | 24 px (`--sp-5`) |

A hint as far from its label as from the next field belongs to neither. The Coordinator dialog's hints did that (#995).

## Text fits its box

- **A label is never cut.** Buttons, tabs, chips and menu items take `min-height` and padding, never a fixed `height` or `width`. A longer word, or the Larger text size, then makes them grow instead of cutting the text. No control gets `text-overflow: ellipsis`. The member panel's "Catch up on…" was cut in a fixed 34 px grid (#995).
- **A name may be cut**, such as a room, an agent or a branch, only where its full form is a hover or a focus away (`title`) and shown whole somewhere else.
- **Every surface works at all four text sizes** (Small, Default, Large, Larger) and at 390 px wide. Look at the pictures at Larger too: most of what #995 found shows only there.

## Buttons

- **One primary action** per dialog or panel. It is the rightmost button of the footer, with Cancel to its left. A destructive action (Remove, Kick, Delete) sits apart on the left, in the danger style. Then the same place always means the same thing.
- **A label is a verb, or what the button opens:** "Save", "Create room", "Coordinator…". "…" means the button opens a dialog that asks for more before anything happens. A button that opens a menu has a chevron and no "…", since two cues for one thing read as a cut label.
- **Full-width buttons only in a phone sheet's footer.** On a desktop form they read as a banner.

## Dialogs

- **A title, a body and a footer.** The body scrolls, and the footer stays in view. A dialog is never taller than the window: its `max-height` is the window less a margin. The Coordinator dialog was 957 px tall in an 844 px phone window, with Save out of sight (#995).
- **Width:** 480 px, or 640 px for a wide one. On a phone it is the full width less 16 px a side.
- **A checkbox with a hint is an option row:** the box, then its label, then the hint 4 px under the label and lined up with it. Rows sit 12 px apart. Clicking the hint ticks the box.
- **Three or more boxes get "Select all"** at the top. It is a real checkbox, ticked, empty or mixed.
- **An explanation is one or two sentences.** A paragraph that explains a measure means the measure needs a better name.

## Menus and toolbars

- **A menu item is at least `--target` tall.** Secondary text goes on a smaller second line.
- **A long menu is grouped.** Past eight items, group them under headings, or filter as you type, as the command palette does.
- **An icon-only button has an `aria-label` and a `title`.** A row of more than five icon buttons is crowded, so move the rarely used ones into a menu.

## Touch

- **A target is at least 44 × 44 px where the pointer is coarse** (`--target`). An icon can stay 20 px while its button grows around it. #995 measured 24 to 30 px targets on a phone.

## Words

- **Plain words for what a person sees.** "Times woken" rather than "wakes". A unit and a time span with every number. No internal names (section numbers, field names, tool names) unless the person types them.

## Checking it

- **Previews:** light, dark and the phone, as the repository's CLAUDE.md says, plus the Larger text size when the change has text in a control.
- **The layout sweep** is `tests/e2e/test_layout_sweep.py`. It opens every dialog and menu at both sizes (1440×900 and a 390×844 phone) and both text sizes (Default and Larger). It fails on a cut or spilling label (the text's own rectangles against the control's content box, not `scrollWidth`, which counts a corner badge), a dialog taller than the window or with its buttons out of view, or sideways scroll (#995, step 3). A phone button under 44×44 px is listed, not yet a failure: `KNOWN_SMALL_TARGETS` holds today's, `KNOWN_CUT` a cut label still on `main`, and both only shrink: fixing one means deleting its line. Run one surface by name:

  ```console
  uv run pytest -m e2e tests/e2e/test_layout_sweep.py -k coordinator
  ```
