"""Hosted setup HTTP surface: /v1/model_api/{setup,get,test,delete,runtime,memory/repair}, /v1/state/receipts, /v1/memory/capture_jobs.

Thin Flask adapters over the framework-neutral ``hosted.setup_core`` (ASGI-migration
plan §5.3). Each route resolves auth + parses the request, then delegates to the
matching ``setup_core`` function so Flask and the native FastAPI router
(``hosted.setup_routes_asgi``) return byte-identical responses.
"""


from hosted import setup_core



