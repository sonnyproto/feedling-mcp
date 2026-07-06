"""Server-managed UI copy — a self-contained backend feature module.

The iOS app overlays a bundle of managed copy keys on top of its compiled-in
Localizable.xcstrings (which stays the offline fallback). Editing copy needs
neither an App Store release nor a backend deploy — only a DB row change via
the admin-gated POST /v1/copytext.

Integration with the rest of the backend is one registry entry in asgi_app.py:
``copytext.routes_asgi`` is listed in the domain-package table there, and its
``register_asgi(app)`` wires the routes.

Everything else (routing, DB access, validation) lives here.
"""
