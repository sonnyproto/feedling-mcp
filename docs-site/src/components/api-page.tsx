'use client';

import { createOpenAPIPage } from 'fumadocs-openapi/ui';

export const OpenAPIPage = createOpenAPIPage({
  // The browser calls the selected API server directly. The backend permits
  // only the documentation site's exact origin and still authenticates every
  // protected request normally.
  playground: {
    enabled: true,
  },
});
