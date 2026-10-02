"""Five-field numeric cron schedules; queue writes and cursors commit together."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.queue.postgres import PostgresQueue, QueueConnection
from app.triggers.manual import PIPELINE_STEPS, RUN_PIPELINE_TASK


def cron_fields(expression: str) -> tuple[set[int], ...]:
    parts = expression.split()
    if len(parts) != 5:
        raise ValueError("Cron needs five fields: minute hour day month weekday")
    result = []
    for part, (low, high) in zip(parts, ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))):
        values: set[int] = set()
        for item in part.split(","):
            base, slash, stride = item.partition("/")
            if slash and (not stride.isdecimal() or int(stride) < 1):
                raise ValueError("Cron steps must be positive integers")
            step = int(stride) if slash else 1
            if base == "*":
                start, end = low, high
            elif "-" in base:
                bounds = base.split("-")
                if len(bounds) != 2 or not all(v.isdecimal() for v in bounds):
                    raise ValueError("Cron ranges must be numeric")
                start, end = map(int, bounds)
            elif base.isdecimal():
                start = int(base)
                end = high if slash else start
            else:
                raise ValueError("Use numeric cron fields, *, ranges, lists, or steps")
            if not low <= start <= end <= high:
                raise ValueError(f"Cron value outside {low}–{high}")
            values.update(range(start, end + 1, step))
        result.append(values)
    result[4] = {v % 7 for v in result[4]}
    return tuple(result)


def cron_matches(expression: str, local: datetime) -> bool:
    minute, hour, day, month, weekday = cron_fields(expression)
    parts = expression.split()
    dom, dow = local.day in day, (local.weekday() + 1) % 7 in weekday
    # Traditional cron ORs day-of-month and weekday when both are restricted.
    day_match = dom or dow if not parts[2].startswith("*") and not parts[4].startswith("*") else dom and dow
    return local.minute in minute and local.hour in hour and local.month in month and day_match


class ScheduleCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1)
    cron_expression: str
    pipeline_step: str | None = None

    @field_validator("cron_expression")
    @classmethod
    def valid_cron(cls, value: str) -> str:
        cron_fields(value)
        return " ".join(value.split())

    @field_validator("pipeline_step")
    @classmethod
    def valid_step(cls, value: str | None) -> str | None:
        if value is not None and value not in PIPELINE_STEPS:
            raise ValueError("Unknown pipeline step")
        return value


def enqueue_due(connection: QueueConnection, *, now: datetime, timezone_name: str) -> int:
    if now.tzinfo is None:
        raise ValueError("Scheduler time must be timezone-aware")
    minute = now.astimezone(timezone.utc).replace(second=0, microsecond=0)
    local = minute.astimezone(ZoneInfo(timezone_name))
    queued = 0
    with connection.transaction():
        rows = list(connection.execute(
            "SELECT id, cron_expression, pipeline_step, last_enqueued_at FROM schedules WHERE active ORDER BY id FOR UPDATE SKIP LOCKED"
        ))
        for schedule_id, expression, step, last in rows:
            if last is not None and last >= minute:
                continue
            try:
                schedule = ScheduleCreate(name="schedule", cron_expression=expression, pipeline_step=step)
            except ValueError:
                continue  # A legacy invalid row must not stop valid schedules.
            if not cron_matches(schedule.cron_expression, local):
                continue
            item = PostgresQueue(connection).enqueue(
                idempotency_key=f"schedule:{schedule_id}:{minute.isoformat()}", run_id=uuid4(),
                task=RUN_PIPELINE_TASK,
                payload={"trigger": "schedule", "steps": list(PIPELINE_STEPS) if step is None else [step]},
            )
            connection.execute("UPDATE schedules SET last_enqueued_at = %s WHERE id = %s", (minute, schedule_id))
            queued += item is not None
    return queued


def main() -> None:
    import argparse
    from app.config import RuntimeSettings, SecretSettings, read_settings_table
    from app.db.connection import open_pool

    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    secrets = SecretSettings()
    if secrets.database_url is None:
        raise SystemExit("DATABASE_URL must be set in .env")
    with open_pool(secrets.database_url.get_secret_value()) as pool, pool.connection() as connection:
        settings = RuntimeSettings.model_validate(read_settings_table(connection))
        enqueue_due(connection, now=datetime.now(timezone.utc), timezone_name=settings.automation_timezone)


if __name__ == "__main__":
    main()
