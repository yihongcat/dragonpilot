from types import SimpleNamespace

import pytest

from openpilot.common.params import Params
from openpilot.common.prefix import OpenpilotPrefix
from openpilot.system.manager.process_config import managed_processes


def test_recording_disabled_by_default():
  with OpenpilotPrefix():
    params = Params()
    assert params.get_default_value('DisableLogging') is True


@pytest.mark.parametrize('started,disabled,expected', [
  (False, False, False),
  (False, True, False),
  (True, False, True),
  (True, True, False),
])
def test_recorder_and_encoder_follow_recording_setting(started, disabled, expected):
  params = SimpleNamespace(get_bool=lambda key: disabled if key == 'DisableLogging' else False)
  assert managed_processes['loggerd'].should_run(started, params, None) is expected
  assert managed_processes['encoderd'].should_run(started, params, None) is expected
  # Camera frames remain available to modeld and the driving display.
  assert managed_processes['camerad'].should_run(started, params, None) is started
