# VPS installation (pending approval)

These files are local templates. Nothing has been installed or started on a VPS.
Approval is required before deployment or real applications/notifications.

Use a Linux VPS with systemd, Python 3.11+, Chromium's system libraries,
Xvfb and tectonic or pdflatex. Create a dedicated `job-seeker` system account.
Install the repository and its virtual environment under `/opt/job-seeker`,
then install the package into `/opt/job-seeker/.venv` with `pip install .`.
Install Playwright Chromium as `job-seeker` with
`PLAYWRIGHT_BROWSERS_PATH=/var/lib/job-seeker/browsers` and put that same
environment variable in `/etc/job-seeker/.env`. Protect the directory and
`.env` (root:job-seeker, directory 0750, file 0640); populate credentials
from `.env.example`. Keep secrets out of the checkout, units and shell history.

Create `/var/lib/job-seeker` owned by `job-seeker` with mode 0700. All services
use it as their working directory, so CVs, screenshots and persistent browser
profiles share paths. The worker uses Xvfb display :99 for headed Chromium;
the display is not exposed over TCP. Initial login/CAPTCHA work needs a secured
interactive session and must not be bypassed automatically.

Before enabling services, run `app.db.migrate.apply_migrations` using a
psycopg connection loaded from the `.env` DATABASE_URL (do not paste the
URL into a command). Back up the database and review migrations first.
Set dashboard URL, timezone, source polling/rate limits, caps and schedules
through the dashboard. Keep auto-submit off until explicitly authorized.
Serve the existing authenticated dashboard over HTTPS using your approved
reverse proxy configuration. Its entrypoint is `python -m app.dashboard`.

After approval, copy the files in `systemd/` into `/etc/systemd/system/`, run
`systemd-analyze verify /etc/systemd/system/job-seeker-*.service
/etc/systemd/system/job-seeker-*.timer`, then `systemctl daemon-reload`.
Enable the worker, scheduler timer and watcher timer. Enable the notification
service only after configuring Telegram/ntfy and authorizing real messages.
Use `journalctl -u job-seeker-worker` and dashboard run logs to verify health.
The timers wake once per minute; actual schedules and subscription intervals
come from the database. Missed schedule minutes are skipped.

Rollback: stop the timers, worker and notification service before changing
the installed version. Preserve `/var/lib/job-seeker` and the database;
application reservations must survive rollback to prevent double applying.
Restore code from the previous version, review migration compatibility,
and re-enable only after offline tests and the health checks pass.

Deployment needs the VPS host, SSH access method, target paths/domain and
explicit approval. Linux unit validation and runtime smoke checks are pending.
