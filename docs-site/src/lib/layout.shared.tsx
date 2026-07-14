import type { BaseLayoutProps } from 'fumadocs-ui/layouts/shared';
import { appName, gitConfig } from './shared';

function FeedlingLogo() {
  return (
    <svg aria-hidden="true" className="size-5" focusable="false" viewBox="0 0 180 180">
      <circle
        cx="90"
        cy="90"
        fill="var(--color-fd-primary)"
        r="89"
        stroke="var(--color-fd-primary)"
        strokeWidth="1"
      />
      <circle cx="65" cy="65" fill="var(--color-fd-background)" opacity="0.72" r="38" />
    </svg>
  );
}

export function baseOptions(): BaseLayoutProps {
  return {
    nav: {
      title: (
        <>
          <FeedlingLogo />
          <span className="font-medium max-md:sr-only">{appName}</span>
        </>
      ),
    },
    githubUrl: `https://github.com/${gitConfig.user}/${gitConfig.repo}`,
  };
}
