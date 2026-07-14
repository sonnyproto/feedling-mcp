import Link from 'next/link';

export default function HomePage() {
  return (
    <main className="mx-auto flex w-full max-w-5xl flex-1 flex-col justify-center px-6 py-24">
      <p className="mb-4 text-sm font-medium text-fd-muted-foreground">Feedling developer platform</p>
      <h1 className="max-w-3xl text-4xl font-semibold tracking-tight sm:text-6xl">
        Build private, persistent agent experiences.
      </h1>
      <p className="mt-6 max-w-2xl text-lg leading-8 text-fd-muted-foreground">
        Integrate accounts, encrypted chat, model routing, memory, identity,
        perception, and proactive workflows with the Feedling HTTP API.
      </p>
      <div className="mt-10 flex flex-wrap gap-3">
        <Link
          href="/docs/getting-started"
          className="rounded-lg bg-fd-primary px-5 py-3 font-medium text-fd-primary-foreground"
        >
          Get started
        </Link>
        <Link
          href="/docs/api-reference"
          className="rounded-lg border bg-fd-card px-5 py-3 font-medium"
        >
          API reference
        </Link>
      </div>
    </main>
  );
}
