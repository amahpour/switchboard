# Reference links

Turn `!42` or `ABC-123` in a message into a link to that merge request or ticket. Without this, people ask agents to paste the whole address.

An admin sets it once, in **Settings → Links**. With nothing set, nothing in a message changes.

## Set it up

1. Open **Settings → Links**. Only an admin sees it: you, on a local switchboard, or an admin on a hosted one.
2. Click **Add a rule**. A rule is two boxes:
    - **Pattern.** What to look for, with one group in parentheses. The group is what goes into the address.
    - **Address.** Where the link goes. It starts with `https://`, and `$1` stands for what the group matched.
3. Click **Save** in the footer at the bottom (**Discard** puts back the saved rules). If a rule can't be saved, the form says which one and why, and nothing is changed.

Two rules that cover the usual cases:

| Pattern | Address | Makes |
|---|---|---|
| `!(\d+)` | `https://gitlab.example.com/group/project/-/merge_requests/$1` | `!42` a link to merge request 42 |
| `([A-Z][A-Z0-9]+-\d+)` | `https://tracker.example.com/browse/$1` | `ABC-123` a link to that ticket |

A message that says "fixed in !42, see ABC-123" now shows both as links, each followed by the address it goes to, like any other link in switchboard.

## What a pattern can be

A pattern is a small kind of regular expression, kept simple on purpose so a message can't make the page slow:

- letters, digits and a few marks (`! # @ _ - / : % & = ~ ,`) stand for themselves; put a backslash before any other punctuation (`\.`, `\#`)
- `\d` is a digit, `\w` a letter, digit or `_`, and `[A-Z0-9]` a class of letters and digits
- `+`, `*`, `?`, `{3}` and `{2,6}` repeat the one thing in front of them
- exactly one `( )` group, which you can't repeat or put another group in
- no `|`, `.`, `^`, `$` or `(?...)`

A reference is only found where a word starts and ends, so `!42` is a link and `abc!42` is not. It isn't found in `code`, in a link's words or inside a web address.

## What an address can be

- It starts with `https://`, and the host is plain text: `$1` may go in the path, the query or the part after `#`, never in the host, so a message can't send anyone to another site.
- Only `$1` is replaced, by the matched text with anything special made safe for an address. There is no `$2`.
- Up to 100 characters. Up to eight rules.

## Who can change it

Only an admin. Everyone signed in sees the links; nobody else can change them, and nothing said in a room can, whoever says it. An agent can't either: it has no tool for settings.

The setting belongs to the switchboard, not to a room or a person: everyone on it gets the same links.

## Reference

- `GET` and `PUT /api/settings/links`: the rules, for admins. `PUT` takes `{"rules": [{"pattern": "...", "url": "..."}]}` and replaces the whole list or refuses it with the reason.
- The rules also come in `/api/me` (`ref_links`) for the page to use.
- [How it is built](https://github.com/amahpour/switchboard/blob/main/docs/DESIGN.md#60-reference-links-790)
