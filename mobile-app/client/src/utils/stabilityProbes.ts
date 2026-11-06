/**
 * Stability Real-Delay destinations (one sample each = 6 total).
 * Same family as v2rayNG generate_204 where available.
 */
export const STABILITY_PROBE_URLS = [
  'https://www.gstatic.com/generate_204',
  'https://www.google.com/generate_204',
  'https://cp.cloudflare.com/',
  'https://telegram.org/',
  'https://www.youtube.com/generate_204',
  'https://www.instagram.com/',
] as const;

export const STABILITY_SAMPLE_COUNT = STABILITY_PROBE_URLS.length;
