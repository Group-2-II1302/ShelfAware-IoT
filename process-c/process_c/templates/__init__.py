"""Static templates served by Process C.

We package this directory so ``importlib.resources`` can locate the HTML
files at runtime regardless of how the package was installed (editable
checkout, wheel, zipapp, etc.). Without an ``__init__.py`` the directory
is data, not a package, and resource lookup gets fiddly across Python
versions.
"""
