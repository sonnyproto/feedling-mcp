import { Geist, JetBrains_Mono } from 'next/font/google';
import { Provider } from '@/components/provider';
import type { Metadata } from 'next';
import './global.css';

const geist = Geist({
  variable: '--font-sans',
  subsets: ['latin'],
});

const mono = JetBrains_Mono({
  variable: '--font-mono',
  subsets: ['latin'],
});

export const metadata: Metadata = {
  metadataBase: new URL(process.env.NEXT_PUBLIC_DOCS_URL ?? 'https://docs.feedling.app'),
  title: {
    default: 'Feedling API',
    template: '%s | Feedling API',
  },
  description: 'API documentation for the Feedling agent platform.',
};

export default function Layout({ children }: LayoutProps<'/'>) {
  return (
    <html
      lang="en"
      className={`${geist.variable} ${mono.variable}`}
      suppressHydrationWarning
    >
      <body className="framework relative flex min-h-screen flex-col">
        <Provider>{children}</Provider>
      </body>
    </html>
  );
}
