import json
import logging
from datetime import datetime, timezone


class JSONFormatter(logging.Formatter):
    """Structured JSON log formatter.

    Every entry includes: timestamp (ISO 8601), level, module, message.
    Extra keyword arguments passed via the `extra` dict are added as
    additional fields.
    """

    def format(self, record: logging.LogRecord) -> str:
        entry: dict = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "module": record.module,
            "message": record.getMessage(),
        }

        if record.exc_info:
            entry["exc_info"] = self.formatException(record.exc_info)

        # Merge any extra fields passed via logger.info(..., extra={...})
        skip = logging.LogRecord.__dict__.keys() | {
            "message", "asctime", "msg", "args", "exc_info", "exc_text",
            "stack_info", "levelno", "pathname", "filename", "lineno",
            "funcName", "created", "msecs", "relativeCreated", "thread",
            "threadName", "processName", "process", "taskName",
        }
        for key, value in record.__dict__.items():
            if key not in skip:
                entry[key] = value

        return json.dumps(entry)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JSONFormatter())
    logging.basicConfig(level=level, handlers=[handler], force=True)
