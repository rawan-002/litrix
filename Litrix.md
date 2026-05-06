# Litrix: Automated Data Ingestion & Scraping Protocol

## 1. Objective
To automate the retrieval of academic publications from **Google Scholar** and other academic sources upon the approval of a researcher's registration.

## 2. Technical Stack for Scraping
- **Orchestration:** Django Signals + Celery.
- **Broker:** Redis.
- **Scraping Engine:** Python (BeautifulSoup / Selenium / Playwright) or scholarly API.
- **Data Persistence:** PostgreSQL.

## 3. The Trigger & Workflow
1. **The Signal:** When a `Head of Department (HoD)` updates the `Status` of a `RegistrationRequest` to **'Approved'**.
2. **Task Dispatch:** A Django `post_save` signal triggers an asynchronous Celery task.
3. **Identification:** The scraper uses the `GoogleScholar_URL` from the `Researcher` profile to locate the correct profile.
4. **Ingestion:**
    - Fetch all papers from the profile.
    - Check for duplicates using `DOI` or `Title` to maintain data integrity.
    - Store records in `ResearchPaper`, `Authors`, and `Journals` tables.

## 4. Database Mapping Logic
Based on the **"Litrix Database Schema.pdf"**, the scraper must populate:
- **`ResearchPaper` Table:** `Title`, `Abstract`, `DOI`, `Source`, and the full JSON log in `RawData_Log`[cite: 1].
- **`Authors` Table:** Create links between the `UserID` (The primary researcher) and the `PaperID`[cite: 1].
- **`ExternalAuthors` / `AuthorNameRaw`:** Store co-authors who are not yet registered in the system[cite: 1].
- **`Journals` & `JournalRankings`:** Link the paper to its respective journal and fetch its **Quartile (Q1-Q4)** and **Impact Factor**[cite: 1].

## 5. Critical Technical Constraints
- **Anti-Blocking Strategy:** Implement random delays (sleep), User-Agent rotation, and proxy support to avoid IP blocks from Google.
- **Fuzzy Matching:** Use basic NLP to match journal names with existing records in the `Journals` table to avoid duplicates[cite: 1].
- **Progress Tracking:** Update a `ScrapedAt` timestamp or a progress percentage to show the user real-time status in the Angular Dashboard[cite: 1].
- **Audit Logging:** Every scraping session must create an entry in `AuditLogs` for transparency[cite: 1].

## 6. Error Handling
- **Invalid URL:** If the `GoogleScholar_URL` is dead, mark the request as `Error` and notify the admin[cite: 1].
- **Partial Success:** If only 50/100 papers are fetched, log the reason and allow for a manual "Retry Sync" from the Researcher's dashboard.