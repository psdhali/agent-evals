import { useQuery } from '@tanstack/react-query';
import { api, type RunItem } from './api';

// §1a — polling gates.  RunList and Capacity poll Aurora-backed endpoints
// every 20 s; once no run is active that poll defeats Aurora auto-pause
// (min_capacity = 0, 300 s idle window), so both must stop.  A run is
// active when it is non-terminal; `terminal` is computed per row
// (RunDetail's rule: aborted, or instance rows exist and none are active) —
// never read off runs.status, which does not record a clean completion.

export function isAnyRunActive(items: RunItem[] | undefined): boolean {
  return (items ?? []).some((r) => !r.terminal);
}

/**
 * Whether any run is non-terminal, polled adaptively: 20 s while a run is
 * active, `false` (stop polling) once every run is terminal.  Never polls in
 * the background (a hidden tab must not keep Aurora awake either).
 */
export function useAnyRunActive() {
  return useQuery({
    queryKey: ['runs', 'activity'],
    queryFn: () => api.listRuns({ limit: 500, offset: 0 }),
    refetchInterval: (q) =>
      isAnyRunActive(q.state.data?.items) ? 20000 : false,
    refetchIntervalInBackground: false,
    staleTime: 15000,
  });
}
