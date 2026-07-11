import datetime

from openpilot.selfdrive.ui.version_text import current_version_description


class FakeParams:
  def __init__(self, values):
    self.values = values

  def get(self, key):
    return self.values.get(key)


def test_current_version_description_prefers_updater_description():
  params = FakeParams({
    "UpdaterCurrentDescription": "0.11.1 / custom / 1234567 / Jul 11",
    "Version": "ignored",
  })

  assert current_version_description(params) == "0.11.1 / custom / 1234567 / Jul 11"


def test_current_version_description_falls_back_to_manager_metadata():
  commit_timestamp = 1783734781
  params = FakeParams({
    "UpdaterCurrentDescription": b"",
    "Version": b"0.11.1",
    "GitBranch": b"test/no-driver-cam-fix",
    "GitCommit": b"5deeed9bbc8a638c73441ea009eb6990a0779c34",
    "GitCommitDate": f"{commit_timestamp} 2026-07-11 09:53:01 +0800".encode(),
  })

  expected_date = datetime.datetime.fromtimestamp(commit_timestamp).strftime("%b %d")
  assert current_version_description(params) == f"0.11.1 / test/no-driver-cam-fix / 5deeed9 / {expected_date}"


def test_current_version_description_omits_invalid_metadata():
  params = FakeParams({
    "GitCommit": "123456789",
    "GitCommitDate": "not-a-date",
  })

  assert current_version_description(params) == "1234567"
