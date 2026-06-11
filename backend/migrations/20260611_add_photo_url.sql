-- Profile photo (Google Scholar thumbnail), captured for free during the
-- existing citation refresh. Nullable; many Scholar profiles have no real
-- photo so the UI falls back to initials. Lives on Users so the co-authors
-- query (which already joins Users) can surface it without an extra join.
ALTER TABLE "Users" ADD COLUMN IF NOT EXISTS "PhotoURL" text;
