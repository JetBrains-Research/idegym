"""Backend-neutral stateful terminal sessions.

One logical terminal handle owns exactly one retained backend session — a pinned tmux pane or a
long-lived subprocess shell — for the lifetime of a generation. The backend is chosen at creation
and never silently switched.
"""
