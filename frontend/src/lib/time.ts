/** Timestamp formatting shared by the run list, run panel and version list.
 *
 *  Everything the API sends is UTC ISO; these render in the viewer's own zone,
 *  because "when did this run" is a question people ask in local time.
 */

/** "6 Sep, 14:32" - absolute, compact, unambiguous across a long run history. */
export function formatAbsolute(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  return d.toLocaleString(undefined, {
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/** "just now" / "12m ago" / "3d ago" - the form that answers "is this current?"
 *  at a glance. Pairs with formatAbsolute as a tooltip for the exact moment. */
export function formatRelative(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return null;

  const seconds = Math.round((Date.now() - then) / 1000);
  if (seconds < 45) return "just now";
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`;
  return `${Math.round(seconds / 86400)}d ago`;
}

/** "1m 12s" - how long a run took, once it has both ends. Returns null while a
 *  run is still going, rather than a duration that silently means "so far". */
export function formatElapsed(
  startedAt: string | null | undefined,
  finishedAt: string | null | undefined,
): string | null {
  if (!startedAt || !finishedAt) return null;
  const ms = new Date(finishedAt).getTime() - new Date(startedAt).getTime();
  if (Number.isNaN(ms) || ms < 0) return null;

  const seconds = Math.round(ms / 1000);
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  const remainder = seconds % 60;
  return remainder === 0 ? `${minutes}m` : `${minutes}m ${remainder}s`;
}
