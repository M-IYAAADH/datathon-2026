"""Offline, deterministic extraction of published ferry timetables from PDF.

Modules:
    pdftext    character-level PDF text with coordinates (PyMuPDF)
    normalize  time, weekday, island-name and Dhivehi helpers
    parsers    layout-specific table parsers -> RawBlock / RawRow
    build      assemble normalised CSV records, versions and eligibility
    validate   integrity and policy checks over the written CSVs
"""
