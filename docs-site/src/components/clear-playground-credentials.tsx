'use client';

import { useState } from 'react';

const AUTH_STORAGE_PREFIX = 'fumadocs-openapi-auth-';

export function ClearPlaygroundCredentials() {
  const [status, setStatus] = useState('');

  function clearCredentials() {
    try {
      let removed = 0;

      for (let index = window.localStorage.length - 1; index >= 0; index -= 1) {
        const key = window.localStorage.key(index);
        if (key?.startsWith(AUTH_STORAGE_PREFIX)) {
          window.localStorage.removeItem(key);
          removed += 1;
        }
      }

      setStatus(
        removed > 0
          ? `Removed ${removed} stored credential${removed === 1 ? '' : 's'}. Reload any open endpoint page.`
          : 'No Feedling playground credentials were stored in this browser.',
      );
    } catch {
      setStatus('The browser blocked local storage access. Clear this site\'s data in browser settings.');
    }
  }

  return (
    <div className="my-4 flex flex-col items-start gap-2 rounded-lg border bg-fd-card p-4">
      <button
        type="button"
        onClick={clearCredentials}
        className="rounded-md bg-fd-primary px-3 py-2 text-sm font-medium text-fd-primary-foreground hover:opacity-90"
      >
        Clear playground credentials
      </button>
      <span className="text-sm text-fd-muted-foreground" role="status" aria-live="polite">
        {status}
      </span>
    </div>
  );
}
