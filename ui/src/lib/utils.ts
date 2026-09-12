import { clsx, type ClassValue } from 'clsx';
import { twMerge } from 'tailwind-merge';

/** join class names, later overrides earlier (shadcn/ui convention) */
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

export const POOLS = ['harness', 'eval', 'gateway'] as const;
export type Pool = (typeof POOLS)[number];

/** SWE-bench instance id is ``owner__repo``-shaped, no slashes; be safe anyway */
export function enc(s: string) {
  return encodeURIComponent(s);
}
