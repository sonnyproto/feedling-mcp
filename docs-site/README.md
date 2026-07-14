# Feedling API documentation

Static API documentation built with the official Fumadocs Next.js Static
template. The site consumes `openapi/public.json`, a filtered snapshot generated
from the FastAPI application; it does not expose FastAPI's runtime documentation
routes.

## Development

```bash
npm install
npm run dev
```

Open <http://localhost:3000/docs>.

## Refresh the OpenAPI snapshot

Install the backend Python dependencies, then run from this directory:

```bash
npm run openapi:generate
```

From the repository's local virtual environment, the equivalent command is:

```bash
../.venv/bin/python ../tools/export_public_openapi.py --output openapi/public.json
```

The exporter excludes operator-only `/admin` and `/debug` paths and adds the
documented API-key/runtime-token security schemes.

## Static build

```bash
npm run types:check
npm run lint
npm run build
```

The deployable static site is written to `out/`.
