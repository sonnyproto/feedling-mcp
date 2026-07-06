"""Client diagnostic-log collection — a self-contained backend feature module.

Lets a client upload its persistent ``diagnostics.log`` so a developer can pull
it by ``user_id`` instead of asking testers to manually export-and-send. Logs go
to the ``io-user-logs`` R2 bucket (plaintext; see ``storage.py`` for the privacy
note); a light index row per upload lands in the Postgres ``client_diagnostics``
log stream.

Integration is one registry entry in asgi_app.py: ``diagnostics.routes_asgi``
is listed in the domain-package table there, and its ``register_asgi(app)``
wires the routes.
"""
