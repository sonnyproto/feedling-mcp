'use client';

import { createOpenAPIPage } from 'fumadocs-openapi/ui';

export const OpenAPIPage = createOpenAPIPage({
  // The docs are exported as a static site and the API does not currently
  // expose browser CORS headers. Code samples remain available; an interactive
  // playground can be enabled once docs.feedling.app is allow-listed.
  playground: {
    enabled: false,
  },
});
