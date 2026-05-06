


BEGIN;


ALTER TABLE "Researcher"
    ADD COLUMN IF NOT EXISTS "OpenAlex_AuthorID" VARCHAR(50);


CREATE UNIQUE INDEX IF NOT EXISTS "uq_researcher_openalex_id"
    ON "Researcher" ("OpenAlex_AuthorID");

COMMIT;


