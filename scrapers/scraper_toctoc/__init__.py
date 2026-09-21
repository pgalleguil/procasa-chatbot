"""TOCTOC scraper package.

The scraper is executed locally, while its classification and captación
integration modules are also imported by the server-side test suite. Keeping
the directory a package prevents local sibling modules from shadowing the
CRM's root-level ``config`` module during imports.
"""

