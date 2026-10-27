from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from opendbc.car.structs import car
from openpilot.cereal import log
from openpilot.selfdrive.controls.lib import longitudinal_planner as planner
from openpilot.selfdrive.controls.lib.longcontrol import LongCtrlState


@pytest.fixture
def setup_planner(monkeypatch):
  mpc = Mock()
  mpc.v_solution = np.full(len(planner.T_IDXS_MPC), 20.0)
  mpc.a_solution = np.full(len(planner.T_IDXS_MPC), 0.5)
  mpc.j_solution = np.zeros(len(planner.T_IDXS_MPC) - 1)
  mpc.crash_cnt = 0
  mpc.source = planner.LongitudinalPlanSource.lead0
  monkeypatch.setattr(planner, 'LongitudinalMpc', Mock(return_value=mpc))
  CP = SimpleNamespace(openpilotLongitudinalControl=True, longitudinalActuatorDelay=0.5, maxLateralAccel=2.0)
  sm = {
    'carState': car.CarState.new_message(vEgo=20.0, vCruise=100.0, aEgo=0.3),
    'controlsState': log.ControlsState.new_message(longControlState=LongCtrlState.pid),
    'selfdriveState': log.SelfdriveState.new_message(enabled=True, personality=1),
    'carControl': car.CarControl.new_message(),
    'radarState': log.RadarState.new_message(),
    'modelV2': log.ModelDataV2.new_message(),
  }
  sm['modelV2'].action.desiredAcceleration = -0.4
  return CP, sm, mpc


@pytest.mark.parametrize('stock_long', [False, True])
def test_saved_dp_settings_do_not_override_autospeed(setup_planner, stock_long):
  CP, sm, mpc = setup_planner
  CP.openpilotLongitudinalControl = not stock_long
  subject = planner.LongitudinalPlanner(CP, init_v=20.0, init_a=0.3)
  # These are deliberately non-default persisted settings from an older install.
  subject.update(sm, dp_flags=7, dp_curve_speed_reduction=100, dp_stop_distance=1)
  assert subject.dp_extensions is None
  assert subject.output_a_target == pytest.approx(-0.4)
  assert subject.mpc.source == planner.LongitudinalPlanSource.e2e
  mpc.update.assert_called_once_with(sm['radarState'], personality=sm['selfdriveState'].personality)
  if stock_long:
    mpc.set_cur_state.assert_called_once_with(20.0, sm['carState'].aEgo)


def test_model_stop_request_survives_another_candidate_winning(setup_planner):
  CP, sm, mpc = setup_planner
  mpc.a_solution[:] = -2.0
  sm['modelV2'].action.shouldStop = True
  subject = planner.LongitudinalPlanner(CP, init_v=20.0)
  subject.update(sm)
  assert subject.output_a_target == pytest.approx(-2.0)
  assert subject.output_should_stop
  assert subject.mpc.source == planner.LongitudinalPlanSource.lead0


def test_switching_extensions_off_discards_optional_state(setup_planner):
  CP, sm, _ = setup_planner
  subject = planner.LongitudinalPlanner(CP, init_v=20.0)
  subject.dp_extensions = Mock()
  subject.update(sm, dp_flags=7, dp_curve_speed_reduction=100)
  assert subject.dp_extensions is None
  assert subject.output_a_target == pytest.approx(-0.4)


def test_extensions_require_both_opt_in_and_supported_longitudinal(setup_planner):
  CP, sm, _ = setup_planner
  CP.openpilotLongitudinalControl = False
  subject = planner.LongitudinalPlanner(CP, init_v=20.0)
  subject.update(sm, dp_flags=7, dp_extensions_enabled=True)
  assert subject.dp_extensions is None


def test_toyota_bsm_and_dp_sdsu_flags_are_independent():
  from opendbc.car.toyota.values import ToyotaFlags
  assert not (ToyotaFlags.HAS_BSM & ToyotaFlags.SDSU)
