import datetime
from typing import Any


def _as_text(value: Any) -> str:
  if value is None:
    return ""
  if isinstance(value, bytes):
    return value.decode("utf-8", "replace")
  return str(value)


def _format_commit_date(commit_date_raw: str) -> str:
  try:
    unix_ts = int(commit_date_raw.strip("'").split()[0])
    return datetime.datetime.fromtimestamp(unix_ts).strftime("%b %d")
  except (ValueError, IndexError, TypeError, AttributeError):
    return ""


def current_version_description(params) -> str:
  description = _as_text(params.get("UpdaterCurrentDescription"))
  if description:
    return description

  version = _as_text(params.get("Version"))
  branch = _as_text(params.get("GitBranch"))
  commit = _as_text(params.get("GitCommit"))[:7]
  commit_date = _format_commit_date(_as_text(params.get("GitCommitDate")))

  return " / ".join(part for part in (version, branch, commit, commit_date) if part)
