// Every researcher's displayed name is the EXACT name Google Scholar shows
// on their profile (`full_name_en`, sourced server-side from
// Users.ScholarDisplayName - see backend/citations/backfill_scholar_names.py).
// The Arabic name (FullName_Ar) is kept in the database but is never shown
// in the UI, anywhere - not even as a fallback. When a researcher has no
// Scholar name yet, `full_name_en` falls back server-side to FirstName +
// LastName, which is why isLatinName() still guards here: that fallback can
// itself hold an Arabic short form for not-yet-scraped users, and that must
// not leak into the UI either.
export function isLatinName(s: string | null | undefined): boolean {
  const v = (s || '').trim();
  if (!v) return false;
  const isAscii = [...v].every(ch => ch.charCodeAt(0) < 128);
  return isAscii && /[A-Za-z]/.test(v);
}

export function joinEnglishName(
  first?: string | null,
  last?: string | null,
): string {
  return [first, last].filter(Boolean).join(' ').trim();
}

export interface NameSource {
  full_name_en?: string | null;
}

export function researcherPrimaryName(p: NameSource | null | undefined, fallback = ''): string {
  const en = p?.full_name_en || '';
  return (isLatinName(en) ? en : '') || fallback;
}
