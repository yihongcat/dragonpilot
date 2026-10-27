from types import SimpleNamespace

import pytest

from openpilot.selfdrive.controls.lib.accel_boost import AccelBoost, ACCEL_BOOST_MAX, ACCEL_BOOST_PER_OVERRIDE


def state(enabled=True, gas=False, speed=20.0):
  return {'selfdriveState': SimpleNamespace(enabled=enabled), 'carState': SimpleNamespace(gasPressed=gas, vEgo=speed)}


def test_gas_override_boost_is_bounded_and_resets_when_disengaged():
  boost = AccelBoost()
  for _ in range(10):
    boost.update(state(), 0.0, 1.0, 1.0)
    before = boost.total_boost
    for _ in range(150):
      output = boost.update(state(gas=True), 0.0, 1.0, 1.0)
    assert boost.total_boost - before <= ACCEL_BOOST_PER_OVERRIDE + 1e-9
    assert output <= ACCEL_BOOST_MAX + 1e-9
  assert boost.total_boost == pytest.approx(ACCEL_BOOST_MAX)
  assert boost.update(state(enabled=False), 0.0, 1.0, 1.0) == 0.0
  assert boost.total_boost == 0.0


@pytest.mark.parametrize('model_accel,mpc_accel,speed', [(0.0, -1.0, 20.0), (0.0, 1.0, 0.0)])
def test_override_does_not_boost_when_not_model_limited_or_outside_speed_range(model_accel, mpc_accel, speed):
  boost = AccelBoost()
  boost.update(state(speed=speed), model_accel, mpc_accel, 6.0)
  for _ in range(100):
    boost.update(state(gas=True, speed=speed), model_accel, mpc_accel, 6.0)
  assert boost.total_boost == 0.0


def test_boost_is_not_applied_above_model_acceleration_range():
  boost = AccelBoost()
  boost.total_boost = ACCEL_BOOST_MAX
  assert boost.update(state(), 5.1, 6.0, 6.0) == 5.1
