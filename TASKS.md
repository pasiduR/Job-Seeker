# Tasks

Built from `REQUIREMENT.md`, following the rules in `AGENTS.md`. There are three phases, and each one ends with something usable.

---

## Phase 1 — Core pipeline: find → score → tailor (manual run)
Goal: run the pipeline by hand from the dashboard and get scored jobs plus tailored CV PDFs.

### Foundation
- [x] Project skeleton matching the `AGENTS.md` layout (`app/`, `tests/fixtures/`, `tests/evals/`)
  - Done: `app/` package directories, `tests/fixtures/`, `tests/evals/`, `tests/conftest.py`, `tests/test_project_layout.py`, added and verified the prescribed project layout.
- [x] `pyproject.toml` (Python 3.11+, Pydantic, Playwright, python-jobspy, Crawl4AI, DB driver)
  - Done: `pyproject.toml`, `tests/conftest.py`, `tests/test_project_metadata.py`, declared Python 3.11+, required runtime packages, and pytest configuration with metadata tests.
- [x] `.env.example` and a `.gitignore` covering `.env`, CVs, screenshots, and browser profiles
  - Done: `.env.example`, `.gitignore`, `tests/fixtures/env_example_required_keys.txt`, `tests/test_environment_files.py`, documented secret keys and ignored sensitive runtime artifacts.
- [x] `config.py`: load `.env` secrets plus the `settings` table, with defaults for threshold, N=7, M=3, placement=`currently_learning`, auto-submit=off, and daily caps
  - Done: `app/config.py`, `tests/test_config.py`, `tests/fixtures/config.env`, `tests/fixtures/settings_table_rows.json`, added separated secret/runtime loading, typed defaults, and settings-table overrides.
- [x] Neon DB migrations for every table: `sources`, `search_filters`, `subscriptions`, `schedules`, `jobs`, `cv_versions`, `applications` (unique `job_id`), `skills_to_learn`, `profile`, `queue_jobs`, `notifications`, `run_logs`, `settings`, `llm_calls`, `form_traces`
  - Done: `app/db/migrations/0001_initial.sql`, `app/db/migrate.py`, `tests/test_migrations.py`, `tests/fixtures/required_tables.txt`, added the complete relational schema and an idempotent transactional migration runner.
- [x] Job status state machine (`found → scored → tailored → filled → approved → submitted` / `skipped` / `failed` / `needs_manual`) with one function per transition
  - Done: `app/queue/state_machine.py`, `tests/test_state_machine.py`, `tests/fixtures/status_transitions.json`, added pure idempotent transition functions and rejection of invalid workflow jumps.
- [x] Postgres-backed queue, worker, and `pipeline_runner` with locking so runs never overlap or repeat, plus `run_logs` entries
  - Done: `app/queue/postgres.py`, `app/queue/worker.py`, `app/queue/pipeline_runner.py`, `tests/test_queue.py`, `tests/fixtures/queue_job.json`, added atomic queue claims, contained retries, advisory run locking, status ownership, and run logging.
- [x] Shared HTTP helper with timeout, retry with backoff (max 3), and per-source rate limiting
  - Done: `app/http.py`, `tests/test_http.py`, `tests/fixtures/http_scenarios.json`, added bounded transient retries, per-attempt timeouts, Retry-After handling, and configured per-source pacing.

### LLM layer
- [x] `llm/client.py`: a single wrapper that validates output against a Pydantic schema, retries once with the validation error, logs every call to `llm_calls` (tokens, cost, latency, validity), and wraps untrusted text in delimited data blocks
  - Done: `app/llm/client.py`, `tests/test_llm_client.py`, `tests/fixtures/llm_responses.json`, added the validated single-call boundary, one schema retry, bounded transport retries, safe data blocks, and metadata-only call logs.
- [x] `llm/schemas.py`: Scorer, Tailor, Form mapper, and Listing extractor schemas
  - Done: `app/llm/schemas.py`, `tests/test_llm_schemas.py`, `tests/fixtures/schema_outputs.json`, added strict contracts for scoring, tailoring, form answers, and extracted listings.
- [x] Sanitizer that strips instruction-like text from scraped content
  - Done: `app/llm/sanitizer.py`, `app/llm/client.py`, `tests/test_sanitizer.py`, `tests/fixtures/malicious_job_description.txt`, added instruction-line removal at the shared LLM trust boundary.

### Sources and scraping
- [x] Source model and CRUD (types: `job_board`, `ats_board`, `career_page`, `rss`, `email_alert`), with dedupe
  - Done: `app/sources/models.py`, `tests/test_sources_model.py`, `tests/fixtures/source.json`, added typed source CRUD, canonical URL dedupe, and rejection of secrets in stored config.
- [x] `sources/jobspy.py`: Indeed, Glassdoor, and other big boards at low volume
  - Done: `app/sources/jobspy.py`, `app/sources/types.py`, `tests/test_jobspy.py`, `tests/fixtures/jobspy_records.json`, added bounded low-volume JobSpy searches, normalized listings, and an explicit LinkedIn block.
- [x] `sources/ats_api.py`: public JSON APIs for Greenhouse, Lever, and Ashby
  - Done: `app/sources/ats_api.py`, `tests/test_ats_api.py`, `tests/fixtures/ats_responses.json`, added public Greenhouse, Lever, and Ashby fetchers with normalized job records.
- [x] `sources/rss.py`: remote boards (Remotive, RemoteOK, Arbeitnow APIs, plus RSS)
  - Done: `app/sources/rss.py`, `app/sources/parsing.py`, `app/sources/ats_api.py`, `tests/test_rss.py`, remote-board/RSS fixtures, added Remotive, RemoteOK, Arbeitnow, RSS, and Atom normalization with shared content parsing.
- [x] `sources/email_alert.py`: LinkedIn alert emails via Gmail API or IMAP, parsing job links (no LinkedIn scraping)
  - Done: `app/sources/email_alert.py`, `tests/test_email_alert.py`, `tests/fixtures/linkedin_alert.eml`, added timeout/retry IMAP ingestion and canonical LinkedIn alert-link parsing without page scraping.
- [x] `sources/career_page.py`: fetch with Crawl4AI or Playwright, then LLM listing extraction (`listing_extract_v1.md`)
  - Done: `app/sources/career_page.py`, `app/llm/prompts/listing_extract_v1.md`, `tests/test_career_page.py`, `tests/fixtures/career_page.txt`, added bounded Playwright page reads and schema-validated listing extraction.
- [x] Scraper service that applies `search_filters` (roles, locations, remote, exclude keywords), dedupes by URL and by (company, title), and saves jobs as `found`
  - Done: `app/sources/scraper.py`, `tests/test_scraper.py`, `tests/fixtures/scraped_jobs.json`, added deterministic filters, canonical URL and company/title dedupe, and idempotent `found` persistence.
- [x] Source Finder service that discovers platforms and career pages for the configured source types and saves new ones
  - Done: `app/sources/finder.py`, `tests/test_source_finder.py`, `tests/fixtures/source_candidates.json`, added configured-type discovery, a supported public catalog, typed provider results, and repository-backed dedupe.

### Scorer
- [x] `prompts/scorer_v1.md` and `steps/scorer.py`, which return a 1–10 score with reasons and missing skills; jobs below the threshold become `skipped`
  - Done: `app/llm/prompts/scorer_v1.md`, `app/steps/scorer.py`, `tests/test_scorer.py`, `tests/fixtures/scorer_cases.json`, added validated scoring, persisted score details, and threshold-driven runner decisions.

### CV Tailor
- [ ] Base CV (LaTeX) stored in the DB and compiled once to a base PDF
- [ ] `prompts/tailor_v1.md` and `steps/tailor.py`: skip tailoring if the base CV scores at or above the threshold; otherwise reorder and reword existing content
- [ ] Code checks: reject output if it adds new employers, projects, degrees, dates, or metrics (entity diff); enforce the `added_skills` limits (≤ M, `est_days` ≤ N); apply the placement toggle
- [ ] Compile with `tectonic` or `pdflatex`; on error make one LLM fix attempt, then mark the job `failed`
- [ ] Save the result to `cv_versions` (tex, pdf path, diff vs base) and `skills_to_learn`

### Dashboard v1
- [ ] Web app and API foundation
- [ ] Pages: Sources, Search filters, Base CV + profile editor, Jobs list (status, score, filters), Skills to learn, Run logs, Settings
- [ ] "Run now" (full pipeline or a single step) and a "Run for this job" button on each job row

### Tests and evals
- [ ] Fixture tests for each source parser, the dedupe, the state machine, and the queue (no live network calls)
- [ ] Eval sets of 10–20 fixtures each for the scorer and the tailor (tailor checks: no invented entities, LaTeX compiles, N/M limits)

---

## Phase 2 — Apply and approve
Goal: filled applications reach the review queue, and I approve them before anything is submitted.

### Applier
- [ ] ApplyPilot integration for ATS and job-board forms using the tailored PDF
- [ ] `browser/`: Playwright headed Chromium with a persistent profile; field extraction from the DOM or accessibility snapshot (label, type, options, required)
- [ ] `prompts/form_map_v1.md` and form mapper: answers come from `profile` and the CV, open-ended questions are drafted in my voice, and unsure answers are `unknown`; legal, visa, salary, and demographic questions are answered from profile fields only
- [ ] Agentic form filler with only these tools: `extract_fields`, `fill_field`, `upload_file`, `click_next`, `screenshot`; limited to 25 steps and 6 pages
- [ ] Stop conditions (CAPTCHA, login wall, unexpected page, same action repeated 3×, unknown required field) set the job to `needs_manual`, and the worker moves on to the next job
- [ ] Store form-fill traces (each tool call and its result) plus the final screenshot, then mark the job `filled`
- [ ] Separate submit code: requires status `approved`, is guarded by the `applications(job_id)` unique constraint, and respects the batch daily cap
- [ ] LinkedIn Easy Apply-only jobs trigger a notification only

### Review and notifications
- [ ] Review queue page showing the screenshot, filled answers with flagged fields, the CV diff, and Approve / Reject
- [ ] `notify/`: Telegram bot or ntfy push with title, company, score, screenshot link, and Approve / Reject buttons
- [ ] Approve sends the job to submit right away; Reject sets it to `skipped`

### Tests and evals
- [ ] Form-mapper eval set (10–20 real form HTML fixtures) checking schema validity, no guesses, and correct `unknown` answers
- [ ] Tests for submit guards (no double-applying, approval required) and for stop conditions

---

## Phase 3 — Automation: schedules, watcher, fast lane
Goal: the pipeline runs on its own and reacts to new jobs within minutes.

- [ ] Schedules page and `schedules` table; the scheduler adds cron-style runs to the queue (for example, a daily full run at 06:00)
- [ ] VPS deployment: systemd units or timers for the scheduler, the worker, and the watcher
- [ ] Subscriptions page (source, search filter, polling interval with a 10 min default)
- [ ] Watcher detection for each source: alert emails, jobspy with a "posted within the last hour" filter every 15–30 min, ATS JSON APIs every 5–10 min, and RSS polling; a new match starts that job's pipeline right away
- [ ] Fast-lane flow: score → tailor (only if needed) → fill → notify
- [ ] Optional auto-submit (off by default), only when all conditions hold: score ≥ the configured value (default 8), the source is on the trusted list, no flagged fields, and no skills were added
- [ ] Separate daily cap for fast-lane applications; per-source polling and rate limits read from `settings`
- [ ] Observability on the dashboard: cost per day, failures per step, `needs_manual` reasons
- [ ] Optional: self-hosted Langfuse trace UI (not a hard dependency)
- [ ] Tests for schedule triggering, watcher dedupe, and auto-submit conditions
