"""Test-harness helpers for the live tests (DESIGN.md §12.4). Test-only: never packaged.

tmux is used here only to play the human at a terminal. The product never
types into a terminal; these helpers never answer a permission prompt except
to decline one with Esc (the approval-hold scenario).
"""
