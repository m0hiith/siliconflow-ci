/**
 * The next (dir 1) or previous (dir -1) line in a sorted list of line
 * numbers, relative to `current`, wrapping around at either end.
 */
export function stepTo(list: readonly number[], current: number | undefined, dir: 1 | -1): number | undefined {
  if (list.length === 0) return undefined;
  if (current == null) return dir === 1 ? list[0] : list[list.length - 1];
  if (dir === 1) return list.find((n) => n > current) ?? list[0];
  for (let i = list.length - 1; i >= 0; i--) {
    if (list[i]! < current) return list[i];
  }
  return list[list.length - 1];
}

/** Splits `text` around case-insensitive occurrences of `q`, for highlighting. */
export function splitMatches(text: string, q: string): { text: string; match: boolean }[] {
  if (!q) return [{ text, match: false }];
  const parts: { text: string; match: boolean }[] = [];
  const hay = text.toLowerCase();
  const needle = q.toLowerCase();
  let at = 0;
  for (let i = hay.indexOf(needle); i !== -1; i = hay.indexOf(needle, i + needle.length)) {
    if (i > at) parts.push({ text: text.slice(at, i), match: false });
    parts.push({ text: text.slice(i, i + needle.length), match: true });
    at = i + needle.length;
  }
  if (at < text.length) parts.push({ text: text.slice(at), match: false });
  return parts;
}
