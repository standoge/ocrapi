"""Terminal progress logging for ocrmypdf."""

from __future__ import annotations

import logging
from contextvars import ContextVar
from typing import Any

logger = logging.getLogger(__name__)

ocr_context: ContextVar[dict[str, str] | None] = ContextVar("ocr_context", default=None)

_LOG_INTERVAL_PCT = 5


def _format_prefix(ctx: dict[str, str] | None) -> str:
    if not ctx:
        return ""
    parts: list[str] = []
    if job_id := ctx.get("job_id"):
        parts.append(f"job={job_id[:8]}")
    if filename := ctx.get("filename"):
        parts.append(filename)
    if not parts:
        return ""
    return f"[{' | '.join(parts)}] "


class LoggingProgressBar:
    """ocrmypdf progress bar that writes page progress to the application log."""

    def __init__(
        self,
        *,
        total: int | float | None = None,
        desc: str | None = None,
        unit: str | None = None,
        disable: bool = False,
        **kwargs: Any,
    ) -> None:
        self.total = float(total) if total is not None else None
        self.desc = desc or "Processing"
        self.unit = unit or "unit"
        self.disable = disable
        self.current = 0.0
        self._last_logged_pct = -_LOG_INTERVAL_PCT

    def __enter__(self) -> LoggingProgressBar:
        if not self.disable:
            prefix = _format_prefix(ocr_context.get())
            if self.total is not None:
                logger.info(
                    "%s%s: starting (%d %ss)",
                    prefix,
                    self.desc,
                    int(self.total),
                    self.unit,
                )
            else:
                logger.info("%s%s: starting", prefix, self.desc)
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        if not self.disable:
            prefix = _format_prefix(ocr_context.get())
            if exc_type is None:
                if self.total is not None:
                    logger.info(
                        "%s%s: completed (%d/%d %ss)",
                        prefix,
                        self.desc,
                        int(self.current),
                        int(self.total),
                        self.unit,
                    )
                else:
                    logger.info("%s%s: completed", prefix, self.desc)
            else:
                logger.error("%s%s: failed (%s)", prefix, self.desc, exc_value)
        return False

    def update(self, n: float = 1, *, completed: float | None = None) -> None:
        if self.disable:
            return

        if completed is not None:
            self.current = completed
        else:
            self.current += n

        if self.total is None or self.total <= 0:
            return

        pct = int((self.current / self.total) * 100)
        if pct < self._last_logged_pct + _LOG_INTERVAL_PCT and pct < 100:
            return

        self._last_logged_pct = pct
        prefix = _format_prefix(ocr_context.get())
        logger.info(
            "%s%s: %d/%d %ss (%.0f%%)",
            prefix,
            self.desc,
            int(self.current),
            int(self.total),
            self.unit,
            pct,
        )
