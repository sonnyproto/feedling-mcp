import type { BaseLayoutProps } from 'fumadocs-ui/layouts/shared';
import { appName, gitConfig } from './shared';

const logo = (
  <svg
    aria-label={appName}
    className="size-5"
    role="img"
    viewBox="0 0 180 180"
  >
    <circle
      cx="90"
      cy="90"
      fill="url(#feedling-icon-gradient)"
      r="89"
      stroke="var(--color-fd-primary)"
      strokeWidth="1"
    />
    <defs>
      <linearGradient id="feedling-icon-gradient" gradientTransform="rotate(45)">
        <stop offset="45%" stopColor="var(--color-fd-background)" />
        <stop offset="100%" stopColor="var(--color-fd-primary)" />
      </linearGradient>
    </defs>
  </svg>
);

export function baseOptions(): BaseLayoutProps {
  return {
    nav: {
      title: (
        <>
          {logo}
          <span className="font-medium max-md:hidden">{appName}</span>
        </>
      ),
    },
    githubUrl: `https://github.com/${gitConfig.user}/${gitConfig.repo}`,
  };
}
