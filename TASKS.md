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

- [x] Concrete `LLMTransport` for the chosen provider in `llm/client.py` (model from settings, key from `LLM_API_KEY`, token usage and cost mapped into `LLMResponse`)
  - Done: `app/llm/client.py`, `app/config.py`, `.env.example`, `pyproject.toml`, `app/dashboard/pages.py`, `app/dashboard/templates/settings.html`, `tests/test_anthropic_transport.py`, `tests/fixtures/anthropic_messages.json`, config/env/dashboard/metadata tests, added `AnthropicTransport` for Claude Platform on AWS (key from `ANTHROPIC_API_KEY` instead of `LLM_API_KEY`, base URL + workspace header from `.env`), with model `claude-sonnet-4-6`, $3/$15 per MTok prices, max tokens, and timeout in `settings`; 429/5xx/connection errors retry via `LLMClient`, 4xx/refusal/truncation fail without retry.
- [x] Wire the LLM steps into the worker: build `AnthropicTransport` + `LLMClient` from config in `python -m app.queue` and pass the scorer, tailor, and career-page extractor (model from `llm_model`) so `run_pipeline` / `run_job` score and tailor jobs
  - Done: `app/queue/__main__.py`, `app/queue/tasks.py`, `tests/test_worker_entrypoint.py`, the worker builds one `LLMClient` over `AnthropicTransport` and passes the scorer, tailor, and career-page extractor (all on `llm_model`); without `ANTHROPIC_API_KEY` they stay unwired and LLM queue items fail with a clear message.

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
- [x] Base CV (LaTeX) stored in the DB and compiled once to a base PDF
  - Done: `app/steps/latex.py`, `app/steps/base_cv.py`, `tests/test_base_cv.py`, `tests/fixtures/base_cv.tex`, added a sandboxed tectonic/pdflatex compiler, LaTeX-to-text extraction, and base-CV storage that recompiles only when the LaTeX or its PDF changes.
- [x] `prompts/tailor_v1.md` and `steps/tailor.py`: skip tailoring if the base CV scores at or above the threshold; otherwise reorder and reword existing content
  - Done: `app/llm/prompts/tailor_v1.md`, `app/steps/tailor.py`, `app/config.py`, `tests/test_tailor.py`, `tests/test_config.py`, `tests/fixtures/job_backend_python.txt`, `tests/fixtures/tailor_outputs.json`, added the tailor prompt and step; tailoring is skipped (base PDF used) when the job score is at or above the new `tailor_skip_threshold` setting (default 9), because reusing `score_threshold` would skip every job that passes scoring.
- [x] Code checks: reject output if it adds new employers, projects, degrees, dates, or metrics (entity diff); enforce the `added_skills` limits (≤ M, `est_days` ≤ N); apply the placement toggle
  - Done: `app/steps/cv_checks.py`, `app/steps/tailor.py`, `tests/test_cv_checks.py`, added entity diffs for names, degrees, dates, and numbers, rejection of file/shell LaTeX commands, skill limit enforcement, and code-applied `currently_learning` / `skills_section` placement.
- [x] Compile with `tectonic` or `pdflatex`; on error make one LLM fix attempt, then mark the job `failed`
  - Done: `app/steps/tailor.py`, `app/llm/schemas.py`, `app/llm/prompts/latex_fix_v1.md`, `tests/test_tailor.py`, `tests/test_cv_checks.py`, compiled tailored CVs through the shared compiler with one schema-validated `latex_fix_v1` attempt, re-checked the fix for invented or unsafe content, and failed the job on a second error.
- [x] Save the result to `cv_versions` (tex, pdf path, diff vs base) and `skills_to_learn`
  - Done: `app/steps/tailor.py`, `tests/test_tailor.py`, `tests/test_cv_checks.py`, wrote tailored PDFs atomically, upserted `cv_versions` with a unified diff vs base, and replaced the job's `skills_to_learn` rows in the same transaction; jobs that skip tailoring reuse the base CV version.

### Dashboard v1
- [x] Web app and API foundation
  - Done: `app/dashboard/app.py`, `app/dashboard/__main__.py`, `app/dashboard/templates/`, `app/dashboard/static/style.css`, `app/db/connection.py`, `app/config.py`, `pyproject.toml`, `.env.example`, `tests/test_dashboard_app.py`, added a FastAPI + Jinja2 app factory with HTTP Basic auth from `.env`, same-origin checks on writes, security headers, per-request repositories, and a pooled-connection entry point.
- [x] Pages: Sources, Search filters, Base CV + profile editor, Jobs list (status, score, filters), Skills to learn, Run logs, Settings
  - Done: `app/dashboard/pages.py`, `app/dashboard/repository.py`, `app/dashboard/templates/*.html`, `app/dashboard/app.py`, `app/dashboard/__main__.py`, `app/db/connection.py`, `tests/test_dashboard_pages.py`, `tests/fixtures/dashboard_data.json`, added server-rendered pages with validated forms (sources, filters, base CV compile + PDF download, profile JSON, settings), job status/score filtering, and http(s)-only links for scraped URLs.
- [x] "Run now" (full pipeline or a single step) and a "Run for this job" button on each job row
  - Done: `app/triggers/manual.py`, `app/queue/postgres.py`, `app/dashboard/pages.py`, `app/dashboard/app.py`, `app/dashboard/repository.py`, `app/dashboard/__main__.py`, `app/dashboard/templates/_run_now.html`, `home.html`, `jobs.html`, `tests/test_manual_trigger.py`, `tests/test_dashboard_pages.py`, buttons enqueue `run_pipeline` (all steps or one) and `run_job` items tagged `trigger: manual`, skipping duplicates already queued or running; executing them is the new worker task below.

### Worker
- [x] Worker entrypoint (`python -m app.queue`) that executes `run_pipeline` and `run_job` queue items: Source Finder, a per-source-type scrape dispatcher (jobspy, ATS API, RSS/remote boards, email alerts, career pages) using active `search_filters`, then the scorer and tailor through `pipeline_runner` with jobs, base CV, and settings loaded from the DB
  - Done: `app/queue/__main__.py`, `app/queue/tasks.py`, `app/sources/dispatch.py`, `app/config.py`, `app/dashboard/pages.py`, `app/dashboard/templates/settings.html`, `tests/test_worker_tasks.py`, `tests/test_source_dispatch.py`, `tests/test_dashboard_pages.py`, `tests/fixtures/worker_listings.json`, worker runs find_sources/scrape (per-source errors logged, others continue) and score/tailor through `pipeline_runner`; added `jobspy_results_wanted` and `source_finder_types` settings. Scorer, tailor, and career pages stay unwired until the LLM transport task is unblocked (their queue items fail without touching jobs); email alerts need the new task below.
- [x] Email-alert jobs: extract title, company, and location from LinkedIn alert email bodies so alert links can be saved as `found` jobs and scraped by the worker (no LinkedIn page fetches)
  - Done: `app/sources/email_alert.py`, `app/sources/dispatch.py`, `app/queue/__main__.py`, `tests/test_email_alert.py`, `tests/test_source_dispatch.py`, `tests/fixtures/linkedin_alert_cards.eml`, parsed alert job cards (HTML first, plain-text fallback) into listings with a short email-only description, and wired them into the worker when IMAP settings exist. The card format is inferred, not checked against a real alert email.

### Tests and evals
- [x] Fixture tests for each source parser, the dedupe, the state machine, and the queue (no live network calls)
  - Done: `tests/test_parser_edge_cases.py`, `tests/fixtures/jobs_feed_atom.xml`, audited existing parser/dedupe/state-machine/queue tests and filled gaps: Atom feeds, malformed feed and ATS payloads, JobSpy retry limit, store-level duplicates, queue backoff/final failure, unknown worker tasks, and pipeline stop after skip.
- [x] Eval sets of 10–20 fixtures each for the scorer and the tailor (tailor checks: no invented entities, LaTeX compiles, N/M limits)
  - Blocked: harness and cases are in (`tests/evals/harness.py`, `scorer_cases.json` with 12 cases, `tailor_cases.json` with 10 cases, `tests/test_eval_harness.py`; it reports schema validity, score agreement, rule violations, compile success, and regressions between prompt versions). Running it for real needs the LLM transport (blocked above, and it costs API spend, so I will ask first) and `tectonic` or `pdflatex` installed for the compile check.
  - Update (2026-10-02): the LLM transport is done (Claude Sonnet 4.6 on Claude Platform on AWS). Still blocked on: (1) your OK to spend on a live run, estimated under $1 per prompt version for the 22 cases at $3/$15 per MTok; (2) `tectonic` or `pdflatex` installed; (3) a small live runner (`python -m tests.evals`) that builds the real client, which I will add once you approve the spend.
  - Done: `tests/evals/__main__.py`, `tests/test_eval_runner.py`, `tests/evals/results/scorer_v1__claude-sonnet-4-6.json`, `tests/evals/results/tailor_v1__claude-sonnet-4-6.json`, added a live runner (`python -m tests.evals [scorer|tailor] [--baseline FILE]`) and saved baselines from a live run (approved spend, about $0.16 in total): scorer 12/12 schema-valid, 11/12 agreement (`senior-staff-architect` scored 1, expected 2-6); tailor 10/10 valid, 0 rule violations, 10/10 compiled with tectonic 0.17.

---

## Phase 2 — Apply and approve
Goal: filled applications reach the review queue, and I approve them before anything is submitted.

### Applier
- [x] ApplyPilot integration for ATS and job-board forms using the tailored PDF
  - Blocked: ApplyPilot 0.3.0 conflicts with `AGENTS.md`. Its `applypilot apply` runs the Claude Code CLI with `--permission-mode bypassPermissions` as a fully autonomous agent that clicks Submit itself (no approve-then-submit-in-separate-code handoff; `--dry-run` only skips the click, and a later real run re-fills and submits on its own), solves CAPTCHAs via CapSolver instead of stopping, needs its own `~/.applypilot` SQLite DB, profile, and Gemini key, and gives an agent with shell permissions untrusted page content. Decide: (a) drop ApplyPilot and send ATS forms (Greenhouse, Lever, Ashby, Workday) through our own form filler, which already enforces the rules; (b) use ApplyPilot only with `--dry-run` as a fill preview and submit manually; or (c) accept the rule exceptions and change `AGENTS.md`.
  - Done: dropped by your decision (a) on 2026-10-02: ATS and job-board forms go through our own form filler instead (next task). `REQUIREMENT.md` still names ApplyPilot; I have not edited it.
- [x] ATS forms through our own filler: open the direct application form for Greenhouse (embed form from the board token and job id), Lever (`/apply`), and Ashby (`/application`) jobs; other job-board pages still stop as `needs_manual` on login walls
  - Done: `app/steps/fill.py`, `app/queue/tasks.py`, `app/sources/dispatch.py`, `tests/test_ats_forms.py`, the fill step now opens the direct form: Greenhouse jobs from an `ats_board` source use the board's embed form (`job-boards.greenhouse.io/embed/job_app`) even when the posting lives on a company career site; Lever and Ashby use their apply pages. Test fills a real captured Cloudflare Greenhouse form end to end offline (text fields, CV upload, flagged consent). Job-board pages (Indeed, etc.) go through the same filler and stop as `needs_manual` at login walls.
- [x] `browser/`: Playwright headed Chromium with a persistent profile; field extraction from the DOM or accessibility snapshot (label, type, options, required)
  - Done: `app/browser/session.py`, `app/browser/fields.py`, `app/browser/extract_fields.js`, `app/config.py`, `pyproject.toml`, `tests/test_browser.py`, `tests/test_browser_session.py`, `tests/fixtures/forms/greenhouse_like.html`, added a persistent-profile Chromium session (headed by default via `browser_headless`, timeout from `browser_timeout_seconds`, http(s)-only navigation with 3 attempts) and DOM field extraction across iframes: labels from label/aria/legend, required from attributes or a trailing `*`, select options, radio/checkbox groups, file inputs, comboboxes, and per-page selectors kept out of the LLM view.
- [x] `prompts/form_map_v1.md` and form mapper: answers come from `profile` and the CV, open-ended questions are drafted in my voice, and unsure answers are `unknown`; legal, visa, salary, and demographic questions are answered from profile fields only
  - Done: `app/llm/prompts/form_map_v1.md`, `app/steps/form_mapper.py`, `tests/test_form_mapper.py`, `tests/fixtures/form_mapper_profile.json`, added the mapper (fields sent one JSON line each, no selectors or file inputs) and code enforcement: unknown or unsupported answers become `unknown`, choice values must be listed options and never drafted, profile answers must exist in the profile, legal/visa/salary/demographic questions (detected by code) need a matching profile field and are always flagged for review, the resume upload is decided in code, and any unanswered required field gives a `needs_manual` reason.
- [x] Agentic form filler with only these tools: `extract_fields`, `fill_field`, `upload_file`, `click_next`, `screenshot`; limited to 25 steps and 6 pages
  - Done: `app/steps/form_filler.py`, `app/llm/prompts/form_fill_v1.md`, `app/llm/schemas.py`, `app/browser/page.py`, `app/browser/extract_buttons.js`, `app/browser/extract_fields.js`, `app/config.py`, `tests/test_form_filler.py`, `tests/fixtures/forms/two_step_form.html`, added the agent loop: the LLM picks one tool per step (plus `done`/`stop`), code fills only mapper-approved values, uploads only the tailored CV, refuses final-submit buttons, refuses `done` while required fields are empty, and ends as `needs_manual` at `form_max_steps` (25) or `form_max_pages` (6). An unknown required answer already stops the run.
- [x] Stop conditions (CAPTCHA, login wall, unexpected page, same action repeated 3×, unknown required field) set the job to `needs_manual`, and the worker moves on to the next job
  - Done: `app/browser/stop_conditions.py`, `app/browser/detect_signals.js`, `app/browser/page.py`, `app/steps/form_filler.py`, `tests/test_stop_conditions.py`, `tests/test_form_filler.py`, checked before every agent step: CAPTCHA (widgets, iframes, challenge text), login walls (SSO hosts, login paths, password fields, "sign in to apply"), LinkedIn pages, leaving the starting site, pages with no form fields, the same action 3 times in a row, and unknown required answers. Each returns a `needs_manual` result with the reason instead of raising, so the worker carries on with the next job (the fill step itself is wired into the worker in the next task).
- [x] Store form-fill traces (each tool call and its result) plus the final screenshot, then mark the job `filled`
  - Done: `app/steps/fill.py`, `app/db/migrations/0002_form_fills.sql`, `app/browser/session.py`, `app/queue/tasks.py`, `app/queue/__main__.py`, `app/triggers/manual.py`, `tests/test_fill_step.py`, `tests/test_worker_tasks.py`, `tests/test_worker_entrypoint.py`, trigger/dashboard/migration tests, added the `fill` pipeline step (tailored -> filled / needs_manual) using the job's tailored CV or the base CV, Lever/Ashby application URLs, a new `form_fills` table (answers, outcome, reason, CV version, final screenshot) and `form_traces` rows replaced on each run; LinkedIn URLs are never opened. "Run now" and "Run for this job" now include `fill`.
- [x] Separate submit code: requires status `approved`, is guarded by the `applications(job_id)` unique constraint, and respects the batch daily cap
  - Done: `app/steps/submit.py`, `app/db/migrations/0003_application_lanes.sql`, `app/steps/form_filler.py`, `tests/test_submit.py`, `tests/test_migrations.py`, added LLM-free submit: requires `approved`, checks the lane's daily cap (job stays `approved` when reached), reserves the `applications` row first (unique `job_id`, so a second submit is impossible), replays the approved answers and recorded Next clicks on a fresh page, aborts and releases the row if the form changed or a CAPTCHA/login wall appears, and keeps the row with `needs_manual` if anything fails after the submit click. Added `applications.lane` and `screenshot_path`. Queue wiring comes with the Approve task.
- [x] LinkedIn Easy Apply-only jobs trigger a notification only
  - Done: `app/steps/fill.py`, `app/queue/__main__.py`, `tests/test_fill_step.py`, LinkedIn jobs (only from alert emails) are never opened: the fill step sets `needs_manual` and queues one pending `apply_manually` row in `notifications` per job; the `notify/` task delivers it.

### Review and notifications
- [x] Review queue page showing the screenshot, filled answers with flagged fields, the CV diff, and Approve / Reject
  - Done: `app/dashboard/templates/review.html`, `app/dashboard/pages.py`, `app/dashboard/repository.py`, `app/dashboard/templates/base.html`, `app/dashboard/static/style.css`, `tests/test_review_page.py`, `tests/test_dashboard_pages.py`, added `/review` listing `filled` jobs with the final screenshot (served only from `data/screenshots/`), every answer with its source, flagged rows highlighted (code flags, drafted, unknown), the CV diff or "base CV used", and Approve / Reject buttons (handled by the Approve task). "Run for this job" now also shows for `tailored` jobs.
- [x] `notify/`: Telegram bot or ntfy push with title, company, score, screenshot link, and Approve / Reject buttons
  - Done: `app/notify/channels.py`, `app/notify/service.py`, `app/notify/__main__.py`, `app/triggers/review.py`, `app/steps/fill.py`, `app/config.py`, `app/dashboard/templates/settings.html`, `tests/test_notify.py`, `tests/fixtures/telegram_updates.json`, `tests/test_fill_step.py`, added `python -m app.notify`: sends pending `notifications` (filled jobs get a `review` message with title, company, score, and a dashboard link from the new `dashboard_url` setting; LinkedIn jobs get `apply_manually`), retries failed sends up to 3 times, and with Telegram long-polls Approve / Reject taps (only from `TELEGRAM_CHAT_ID`) and queues them as `review_decision` items. ntfy has no safe callback, so its button opens the review page. Errors never contain the bot token. The worker side of decisions is the next task.
- [x] Approve sends the job to submit right away; Reject sets it to `skipped`
  - Done: `app/queue/tasks.py`, `app/queue/__main__.py`, `app/dashboard/pages.py`, `app/dashboard/repository.py`, `app/dashboard/__main__.py`, `app/triggers/manual.py`, `tests/test_worker_tasks.py`, `tests/test_review_page.py`, trigger/dashboard tests, dashboard and Telegram decisions queue a `review_decision` item; the worker runs it through `pipeline_runner` (filled -> approved -> submit at once with `batch_daily_cap`, or filled -> skipped) and ignores jobs that are no longer `filled`. Added a `submit` pipeline step so approved jobs held back by the daily cap are submitted by a later run.

### Tests and evals
- [x] Form-mapper eval set (10–20 real form HTML fixtures) checking schema validity, no guesses, and correct `unknown` answers
  - Done: `tests/evals/forms/` (13 real Greenhouse and Lever forms, scripts/styles/links stripped), `tests/evals/form_mapper_cases.json`, `tests/evals/form_profile.json`, `tests/evals/harness.py`, `tests/evals/__main__.py`, `tests/test_form_mapper_eval.py`, `app/browser/extract_fields.js`, `tests/evals/results/form_map_v1__claude-sonnet-4-6.json`, eval renders each form offline, maps it, and reports schema validity, agreement with 127 field expectations (names, email, CV upload; demographics, consents, salary, unknown eligibility must stay `unknown`), and violations (raw answers the code had to discard). Live baseline ($0.17): 13/13 valid, 127/127 expectations met, 4 discarded answers (a privacy acknowledgement, one sponsorship combobox value, two "how did you hear" options not literally in the profile). Real forms also showed extraction gaps, now fixed: hidden widget mirror inputs, "Attach" labels, Lever questions outside `<label>`, and the ✱ required marker.
- [x] Tests for submit guards (no double-applying, approval required) and for stop conditions
  - Done: `tests/test_submit.py`, `tests/test_worker_tasks.py` (plus the existing `tests/test_stop_conditions.py`, `tests/test_form_filler.py`, `tests/test_migrations.py`), audited and filled gaps: the `submit` step only touches `approved` jobs, two submit runs for one job submit once, a login wall during replay aborts before the click; already covered were approval required, the unique `applications(job_id)` reservation, daily cap, changed forms, CAPTCHA, failures after the click, and every filler stop condition.

---

## Phase 3 — Automation: schedules, watcher, fast lane
Goal: the pipeline runs on its own and reacts to new jobs within minutes.

- [x] Schedules page and `schedules` table; the scheduler adds cron-style runs to the queue (for example, a daily full run at 06:00)
  - Done: `app/triggers/scheduler.py`, `app/config.py`, dashboard repository/pages/templates, `tests/test_scheduler.py`, `tests/fixtures/schedules.json`; validated numeric cron, timezone-aware matching, atomic queue/cursor writes and dashboard CRUD; 34 fixture tests passed.
- [!] VPS deployment: systemd units or timers for the scheduler, the worker, and the watcher
  - Blocked: local `deploy/systemd/` units/timers and `deploy/README.md` prepared; two fixture tests pass. Actual installation needs VPS host/access, target domain/paths and your explicit deployment approval; Linux systemd/runtime validation is pending.
- [x] Subscriptions page (source, search filter, polling interval with a 10 min default)
  - Done: `app/triggers/subscriptions.py`, dashboard repository/pages/templates, `tests/test_subscriptions.py`, `tests/fixtures/subscription.json`; authenticated subscription controls with validated source/filter IDs and 10-minute default; 34 fixture tests passed.
- [~] Watcher detection for each source: alert emails, jobspy with a "posted within the last hour" filter every 15–30 min, ATS JSON APIs every 5–10 min, and RSS polling; a new match starts that job's pipeline right away
- [ ] Fast-lane flow: score → tailor (only if needed) → fill → notify
- [ ] Optional auto-submit (off by default), only when all conditions hold: score ≥ the configured value (default 8), the source is on the trusted list, no flagged fields, and no skills were added
- [ ] Separate daily cap for fast-lane applications; per-source polling and rate limits read from `settings`
- [ ] Observability on the dashboard: cost per day, failures per step, `needs_manual` reasons
- [ ] Optional: self-hosted Langfuse trace UI (not a hard dependency)
- [ ] Tests for schedule triggering, watcher dedupe, and auto-submit conditions
