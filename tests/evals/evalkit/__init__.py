"""Evals: real agents in a throwaway room, graded from the database (DESIGN.md §12.7).

Test-only; never packaged.

``scenario`` is the data (a room, its agents, what the person says, the checks), ``graders`` the checks (plain
functions of a ``RunRecord``), ``record`` what they read, ``trial`` one run of a scenario against a world
(a real one, ``claude_world``, or a scripted one, ``scripted``), ``results`` the file a run writes and
``compare`` the report on two of them. Nothing here runs a model unless a person asks (``SWITCHBOARD_EVAL``).
"""
