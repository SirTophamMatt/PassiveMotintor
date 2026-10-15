# Passive Monitor — Copyright (c) 2026 SirTophamMatt. All rights reserved.
"""Fire Danger Ratings + Total Fire Bans (Intel Tool, ``/intel/fdr``).

``model``    districts, the AFDRS rating hierarchy, name normalisation
``parse``    CFA RSS feed -> per-day, per-district ratings and TFB status
``store``    the SQLite history: runs, raw fetches, revisions, overrides, flags
``scraper``  one collection run (fetch -> parse -> validate -> store)
``resolve``  what a date's record IS, from stored revisions + overrides
``workbook`` the FDR Daily Statistics workbook: discovery + safe cell writes
``cli``      ``python -m app.modules.fdr ...`` for scheduled/manual runs
"""
