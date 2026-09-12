import { cn } from '../lib/utils';

// §1b — surface last-fetched on every view.  Silent staleness is how an
// operator watches a run that finished ten minutes ago; the number is the
// "am I looking at something live?" signal.  `dataUpdatedAt` is a queryError
// out of react-query — epoch ms of the last successful fetch.

export function Freshness({
  updatedAt,
  className,
}: {
  updatedAt?: number;
  className?: string;
}) {
  return (
    <span
      className={cn('text-[11px] text-zinc-400 dark:text-zinc-500', className)}
      title={
        updatedAt
          ? `last fetched ${new Date(updatedAt).toLocaleString()}`
          : 'never fetched'
      }
    >
      updated {updatedAt ? new Date(updatedAt).toLocaleTimeString() : '—'}
    </span>
  );
}
