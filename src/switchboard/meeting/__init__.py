"""A live call in a room (DESIGN.md §52).

The broker joins a meeting through joinly and posts the transcript into the room.
Matching spoken names and merging segments are pure; ``bridge`` is the only async part.
"""
