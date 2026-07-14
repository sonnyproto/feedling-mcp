import { docs } from 'collections/server';
import { loader } from 'fumadocs-core/source';
import { docsContentRoute, docsImageRoute, docsRoute } from './shared';
import { openapi } from './openapi';

// See https://fumadocs.dev/docs/headless/source-api for more info
export const source = loader(
  {
    docs: docs.toFumadocsSource(),
    openapi: await openapi.staticSource({
      baseDir: 'api-reference/endpoints',
      groupBy: 'tag',
    }),
  },
  {
    baseUrl: docsRoute,
    plugins: [openapi.loaderPlugin()],
  },
);

export function getPageImage(page: (typeof source)['$inferPage']) {
  const segments = [...page.slugs, 'image.png'];

  return {
    segments,
    url: `${docsImageRoute}/${segments.join('/')}`,
  };
}

export function getPageMarkdownUrl(page: (typeof source)['$inferPage']) {
  const segments = [...page.slugs, 'content.md'];

  return {
    segments,
    url: `${docsContentRoute}/${segments.join('/')}`,
  };
}

export async function getLLMText(page: (typeof source)['$inferPage']) {
  if (page.type === 'openapi') {
    const { payload: _payload, ...operation } = page.data.getOpenAPIPageProps();
    return `# ${page.data.title} (${page.url})

${page.data.description ?? ''}

\`\`\`json
${JSON.stringify(operation, null, 2)}
\`\`\``;
  }

  const processed = await page.data.getText('processed');

  return `# ${page.data.title} (${page.url})

${processed}`;
}
