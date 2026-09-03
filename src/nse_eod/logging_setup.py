"""Structured logging plus the per-run summary object."""

from __future__ import annotations

import dataclasses
import logging
import sys
import uuid
from pathlib import Path
from typing import Any

import structlog


def setup_logging(log_dir: Path | None = None, level: str = "INFO", json_console: bool = False) -> None:
    """Configure structlog: human-readable console, JSON to file."""
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, level.upper(), logging.INFO),
    )
    # Quiet the noisy third parties; we do our own request logging.
    for noisy in ("httpx", "httpcore", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_dir / "pipeline.jsonl", encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(message)s"))
        logging.getLogger().addHandler(fh)

    renderer = (
        structlog.processors.JSONRenderer()
        if json_console
        else structlog.dev.ConsoleRenderer(colors=False)
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=False),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str = "nse_eod"):
    return structlog.get_logger(name)


def new_run_uid() -> str:
    return uuid.uuid4().hex


def bind_run(run_uid: str, command: str) -> None:
    """Attach run identity to every subsequent log line in this process."""
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(run_uid=run_uid, command=command)


@dataclasses.dataclass
class RunSummary:
    """Counters emitted at the end of every run.

    Mirrors the columns of ``ops.pipeline_run`` so persisting is a plain dict copy.
    """

    command: str
    run_uid: str
    target_date: Any = None
    bhav_rows_ingested: int = 0
    bhav_rows_restated: int = 0
    ca_rows_ingested: int = 0
    ca_events_parsed: int = 0
    ca_flagged_for_review: int = 0
    factors_computed: int = 0
    gold_isins_rebuilt: int = 0
    gold_rows_written: int = 0
    alerts_raised: int = 0
    notes: list[str] = dataclasses.field(default_factory=list)
    extra: dict[str, Any] = dataclasses.field(default_factory=dict)

    def note(self, msg: str) -> None:
        self.notes.append(msg)

    def counters(self) -> dict[str, int]:
        return {
            f.name: getattr(self, f.name)
            for f in dataclasses.fields(self)
            if f.type is int or isinstance(getattr(self, f.name), int)
        }

    def as_jsonb(self) -> dict[str, Any]:
        d = dataclasses.asdict(self)
        d["target_date"] = str(self.target_date) if self.target_date else None
        return d

    def log(self, log=None) -> None:
        log = log or get_logger()
        log.info(
            "run_summary",
            **{k: v for k, v in self.counters().items()},
            notes=self.notes or None,
        )
