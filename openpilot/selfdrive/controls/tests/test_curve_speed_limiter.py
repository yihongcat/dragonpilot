import math
from types import SimpleNamespace

import numpy as np

from openpilot.common.constants import CV
from openpilot.cereal import log
from openpilot.common.realtime import DT_MDL
from openpilot.selfdrive.modeld.constants import ModelConstants
from dragonpilot.selfdrive.controls.lib.curve_speed_limiter import (
  CURVE_ACCEL_RELEASE_HOLD_FRAMES,
  CURVE_BRAKE_MIN_SPEED,
  CURVE_CONFIRMATION_FRAMES,
  CURVE_DECEL_JERK,
  CURVE_RELEASE_JERK,
  CURVE_THROTTLE_RELEASE_JERK,
  MAX_CURVE_DECEL,
  MAX_STRENGTH_LAT_ACCEL_FACTOR,
  MIN_CURVE_SPEED,
  NORMAL_CURVE_DECEL,
  RELEASE_ACCEL,
  RELEASE_HOLD_FRAMES,
  CurveAccelerationController,
  CurveSpeedLimit,
  CurveSpeedLimiter,
  get_curve_accel,
  _CurveCandidate,
)


MODEL_SIZE = len(ModelConstants.T_IDXS)


def model_with_curve(curvature: float = 0.0, speed: float = 25.0,
                     curve_start: int = 0, curve_points: int | None = None,
                     spacing: float = 10.0):
  curve_points = MODEL_SIZE - curve_start if curve_points is None else curve_points
  rates = np.zeros(MODEL_SIZE)
  rates[curve_start:curve_start + curve_points] = curvature * speed
  return SimpleNamespace(
    position=SimpleNamespace(
      x=(np.arange(MODEL_SIZE) * spacing).tolist(),
      y=[0.0] * MODEL_SIZE,
    ),
    velocity=SimpleNamespace(
      x=[speed] * MODEL_SIZE,
      y=[0.0] * MODEL_SIZE,
    ),
    orientationRate=SimpleNamespace(z=rates.tolist()),
  )


def update(limiter, model, strength=100, speed=25.0, cruise=35.0,
           enabled=True, overriding=False, previous_accel=0.0):
  return limiter.update(model, speed, cruise, strength, enabled, overriding, previous_accel)

def confirmed_update(limiter, model, frames=8, **kwargs):
  position_x = np.asarray(model.position.x, dtype=float)
  position_y = np.asarray(model.position.y, dtype=float)
  distances = limiter._distances(position_x, position_y)
  velocity_x = np.asarray(model.velocity.x, dtype=float)
  velocity_y = np.asarray(model.velocity.y, dtype=float)
  speed_plan = np.hypot(velocity_x, velocity_y)
  rate_plan = np.asarray(model.orientationRate.z, dtype=float)
  base_curvatures = np.zeros_like(rate_plan)
  valid_speed = speed_plan > 1.0
  base_curvatures[valid_speed] = rate_plan[valid_speed] / speed_plan[valid_speed]

  ego_speed = kwargs.get("speed", 25.0)
  result = CurveSpeedLimit()
  for frame in range(frames):
    traveled = frame * ego_speed * DT_MDL
    shifted_curvatures = np.interp(
      distances + traveled, distances, base_curvatures,
      left=base_curvatures[0], right=base_curvatures[-1],
    )
    shifted_model = SimpleNamespace(
      position=model.position,
      velocity=model.velocity,
      orientationRate=SimpleNamespace(z=(shifted_curvatures * speed_plan).tolist()),
    )
    result = update(limiter, shifted_model, **kwargs)
  return result


def nonuniform_model_with_world_curve(curvature: float, distance: float,
                                      speed: float = 25.0, curve_points: int = 1):
  positions = speed * np.asarray(ModelConstants.T_IDXS)
  center = int(np.argmin(np.abs(positions - distance)))
  start = max(0, center - curve_points // 2)
  end = min(MODEL_SIZE, start + curve_points)
  rates = np.zeros(MODEL_SIZE)
  rates[start:end] = curvature * speed
  return SimpleNamespace(
    position=SimpleNamespace(x=positions.tolist(), y=[0.0] * MODEL_SIZE),
    velocity=SimpleNamespace(
      x=[speed] * MODEL_SIZE,
      y=[0.0] * MODEL_SIZE,
    ),
    orientationRate=SimpleNamespace(z=rates.tolist()),
  )


def test_disabled_does_not_limit():
  limiter = CurveSpeedLimiter(1.8)
  result = update(limiter, model_with_curve(0.02), strength=0)
  assert not result.active
  assert math.isinf(result.speed)


def test_strength_uses_vehicle_lateral_accel_without_raising_cap():
  low_cap_limiter = CurveSpeedLimiter(1.3)
  rav4_limiter = CurveSpeedLimiter(1.8)

  assert low_cap_limiter._allowed_lateral_accel(1) == 1.3
  assert np.isclose(low_cap_limiter._allowed_lateral_accel(100), 1.3 * MAX_STRENGTH_LAT_ACCEL_FACTOR)
  assert np.isclose(rav4_limiter._allowed_lateral_accel(100), 1.8 * MAX_STRENGTH_LAT_ACCEL_FACTOR)


def test_higher_strength_reduces_curve_speed():
  model = model_with_curve(0.01, curve_start=8)
  weak = update(CurveSpeedLimiter(1.8), model, strength=20)
  strong = update(CurveSpeedLimiter(1.8), model, strength=80)

  assert weak.active and strong.active
  assert strong.speed < weak.speed


def test_future_curve_limits_speed_while_current_path_is_straight():
  limiter = CurveSpeedLimiter(1.8)
  result = update(limiter, model_with_curve(0.012, curve_start=10))

  assert result.active
  assert limiter.target_distance > 0.0
  assert math.isfinite(result.speed)


def test_nearer_curve_has_lower_current_speed_boundary():
  near = update(CurveSpeedLimiter(1.8), model_with_curve(0.012, curve_start=5))
  far = update(CurveSpeedLimiter(1.8), model_with_curve(0.012, curve_start=15))

  assert near.active and far.active
  assert near.speed < far.speed


def test_boundary_uses_comfortable_deceleration_distance():
  limiter = CurveSpeedLimiter(1.8)
  result = update(limiter, model_with_curve(0.012, curve_start=10), speed=25.0)
  expected = math.sqrt(limiter.target_curve_speed ** 2 + 2.0 * NORMAL_CURVE_DECEL * limiter.target_distance)

  assert np.isclose(result.speed, expected)


def test_future_curve_requests_decel_before_speed_boundary_is_exceeded():
  limiter = CurveSpeedLimiter(1.8)
  result = confirmed_update(limiter, model_with_curve(0.003, curve_start=25), speed=25.0, cruise=35.0)

  assert result.active
  assert result.speed > 25.0
  assert result.required_decel > 0.0
  assert get_curve_accel(result.required_decel, 0.0) < 0.0


def test_curve_accel_is_jerk_limited_when_applied_and_released():
  applied = get_curve_accel(0.8, 0.0)
  assert np.isclose(applied, -CURVE_DECEL_JERK * 0.05)
  throttle_released = get_curve_accel(0.8, 0.6)
  assert np.isclose(throttle_released, 0.6 - CURVE_THROTTLE_RELEASE_JERK * DT_MDL)


  released = get_curve_accel(0.0, -0.5)
  assert np.isclose(released, -0.5 + CURVE_RELEASE_JERK * DT_MDL)


def test_actuator_delay_starts_slowing_earlier():
  model = model_with_curve(0.012, curve_start=10)
  no_delay_limiter = CurveSpeedLimiter(1.8, longitudinal_actuator_delay=0.0)
  delayed_limiter = CurveSpeedLimiter(1.8, longitudinal_actuator_delay=1.0)
  no_delay = update(no_delay_limiter, model)
  delayed = update(delayed_limiter, model)

  assert delayed_limiter.target_distance < no_delay_limiter.target_distance
  assert delayed.speed < no_delay.speed


def test_single_frame_prediction_spike_never_requests_deceleration():
  limiter = CurveSpeedLimiter(1.8)
  spike = update(limiter, model_with_curve(0.04, curve_start=12, curve_points=1))

  assert spike.active
  assert not spike.confirmed
  assert spike.required_decel == 0.0

  for _ in range(CURVE_CONFIRMATION_FRAMES):
    straight = update(limiter, model_with_curve())
    assert not straight.confirmed
    assert straight.required_decel == 0.0


def test_persistent_single_point_curve_is_confirmed_on_nonuniform_model_grid():
  limiter = CurveSpeedLimiter(1.8)
  results = []
  speed = 25.0
  distance = 80.0
  for frame in range(12):
    model = nonuniform_model_with_world_curve(0.04, distance - frame * speed * DT_MDL, speed)
    results.append(update(limiter, model, speed=speed))

  assert all(result.active for result in results)
  confirmed_frames = [index for index, result in enumerate(results) if result.confirmed]
  assert confirmed_frames
  assert confirmed_frames[0] >= CURVE_CONFIRMATION_FRAMES - 1
  assert all(result.required_decel == 0.0 for result in results[:confirmed_frames[0]])
  assert results[confirmed_frames[0]].required_decel > 0.0


def test_prediction_space_fixed_spike_never_confirms_as_a_world_curve():
  limiter = CurveSpeedLimiter(1.8)
  fixed_model = nonuniform_model_with_world_curve(0.04, 80.0, speed=25.0)
  results = [update(limiter, fixed_model, speed=25.0) for _ in range(12)]

  assert all(result.active for result in results)
  assert all(not result.confirmed for result in results)
  assert all(result.required_decel == 0.0 for result in results)


def test_adjacent_curve_predictions_are_preserved():
  limiter = CurveSpeedLimiter(1.8)
  result = update(limiter, model_with_curve(0.04, curve_start=12, curve_points=2))

  assert result.active
  assert math.isfinite(result.speed)


def test_curve_boundary_has_minimum_speed():
  limiter = CurveSpeedLimiter(1.3)
  result = update(limiter, model_with_curve(1.0), speed=25.0)

  assert result.speed == MIN_CURVE_SPEED == 20.0 * CV.KPH_TO_MS


def test_late_detection_caps_required_deceleration():
  limiter = CurveSpeedLimiter(1.8)
  result = confirmed_update(limiter, model_with_curve(0.02, curve_start=2), frames=8, speed=30.0, cruise=35.0)

  assert result.active
  assert result.required_decel == MAX_CURVE_DECEL


def test_driver_override_releases_boundary():
  limiter = CurveSpeedLimiter(1.8)
  assert update(limiter, model_with_curve(0.02)).active

  result = update(limiter, model_with_curve(0.02), overriding=True)
  assert not result.active
  assert math.isinf(result.speed)


def test_curve_release_holds_then_raises_boundary_gradually():
  limiter = CurveSpeedLimiter(1.8)
  active = update(limiter, model_with_curve(0.02))
  straight = model_with_curve()

  held = update(limiter, straight)
  assert held.active
  assert held.speed == 25.0

  for _ in range(RELEASE_HOLD_FRAMES - 1):
    held = update(limiter, straight)
  released = update(limiter, straight)
  assert released.active
  assert np.isclose(released.speed, held.speed + RELEASE_ACCEL * 0.05)
  assert released.speed > active.speed


def test_invalid_plan_does_not_limit():
  model = model_with_curve(0.02)
  model.position.y = model.position.y[:-1]
  result = update(CurveSpeedLimiter(1.8), model)

  assert math.isinf(result.speed)
  assert not result.active


def confirmed_curve_limit(required_decel=0.8, speed=MIN_CURVE_SPEED):
  return CurveSpeedLimit(
    speed=speed,
    required_decel=required_decel,
    active=True,
    confirmed=True,
    target_speed=speed,
  )


def test_curve_acceleration_cap_starts_from_previous_output_without_a_step():
  controller = CurveAccelerationController()
  previous_output = 0.7
  result = controller.update(
    confirmed_curve_limit(),
    v_ego=20.0,
    enabled=True,
    overriding=False,
    previous_output_accel=previous_output,
  )

  assert result.active
  assert np.isclose(result.accel, previous_output - CURVE_THROTTLE_RELEASE_JERK * DT_MDL)


def test_curve_acceleration_cap_respects_apply_jerk_every_frame():
  controller = CurveAccelerationController()
  outputs = [0.6]
  for _ in range(40):
    result = controller.update(
      confirmed_curve_limit(1.2),
      v_ego=20.0,
      enabled=True,
      overriding=False,
      previous_output_accel=outputs[-1],
    )
    outputs.append(min(0.6, result.accel))

  downward_jerk = -np.diff(outputs) / DT_MDL
  starting_accels = np.asarray(outputs[:-1])
  assert np.max(downward_jerk[starting_accels > 0.0]) <= CURVE_THROTTLE_RELEASE_JERK + 1e-9
  assert np.max(downward_jerk[starting_accels <= 0.0]) <= CURVE_DECEL_JERK + 1e-9
  assert outputs[-1] >= -1.2


def test_unconfirmed_or_low_speed_curve_never_starts_braking():
  controller = CurveAccelerationController()
  preview = CurveSpeedLimit(
    speed=MIN_CURVE_SPEED,
    target_speed=MIN_CURVE_SPEED,
    required_decel=1.0,
    active=True,
    confirmed=False,
  )

  preview_cap = controller.update(preview, 20.0, True, False, 0.4)
  assert preview_cap.active
  assert 0.0 <= preview_cap.accel < 0.4

  low_speed_cap = controller.update(confirmed_curve_limit(), CURVE_BRAKE_MIN_SPEED - 0.01, True, False, 0.4)
  assert low_speed_cap.active
  assert low_speed_cap.accel >= 0.0


def test_curve_acceleration_release_holds_then_respects_release_jerk():
  controller = CurveAccelerationController()
  active = confirmed_curve_limit(1.0)
  for _ in range(30):
    applied = controller.update(active, 20.0, True, False, 0.0)
  assert applied.accel < 0.0

  missing = CurveSpeedLimit()
  held_accel = applied.accel
  for _ in range(CURVE_ACCEL_RELEASE_HOLD_FRAMES):
    held = controller.update(missing, 20.0, True, False, held_accel)
    assert held.active
    assert held.accel == held_accel

  released = controller.update(missing, 20.0, True, False, held_accel)
  assert released.active
  assert np.isclose(released.accel, held_accel + CURVE_RELEASE_JERK * DT_MDL)


def test_driver_override_clears_curve_acceleration_cap_immediately():
  controller = CurveAccelerationController()
  assert controller.update(confirmed_curve_limit(), 20.0, True, False, 0.0).active
  assert not controller.update(confirmed_curve_limit(), 20.0, True, True, 0.0).active


def test_curve_cap_can_begin_before_current_speed_exceeds_backward_envelope():
  controller = CurveAccelerationController()
  far_curve = CurveSpeedLimit(
    speed=25.0,
    target_speed=10.0,
    required_decel=0.25,
    active=True,
    confirmed=True,
  )

  result = controller.update(far_curve, 20.0, True, False, 0.4)

  assert far_curve.speed > 20.0
  assert result.active
  assert result.accel < 0.4


def test_curvature_uses_planar_speed_through_a_ninety_degree_arc():
  radius = 100.0
  curvature = 1.0 / radius
  speed = 25.0
  angle = np.linspace(0.0, math.pi / 2.0, MODEL_SIZE)
  model = SimpleNamespace(
    position=SimpleNamespace(
      x=(radius * np.sin(angle)).tolist(),
      y=(radius * (1.0 - np.cos(angle))).tolist(),
    ),
    velocity=SimpleNamespace(
      x=(speed * np.cos(angle)).tolist(),
      y=(speed * np.sin(angle)).tolist(),
    ),
    orientationRate=SimpleNamespace(z=[curvature * speed] * MODEL_SIZE),
  )
  limiter = CurveSpeedLimiter(1.8)

  result = update(limiter, model, speed=speed)
  expected_curve_speed = math.sqrt(limiter.allowed_lateral_accel / curvature)

  assert result.active
  assert np.isclose(limiter.target_curve_speed, expected_curve_speed, rtol=0.02)


def test_curve_braking_releases_early_enough_to_preserve_minimum_speed():
  controller = CurveAccelerationController()
  curve = confirmed_curve_limit(MAX_CURVE_DECEL)
  base_accel = 0.5
  output_accel = base_accel
  speed = 15.0
  speeds = [speed]

  for _ in range(400):
    cap = controller.update(curve, speed, True, False, output_accel, base_accel)
    output_accel = min(base_accel, cap.accel) if cap.active else base_accel
    speed = max(0.0, speed + output_accel * DT_MDL)
    speeds.append(speed)

  assert min(speeds) >= MIN_CURVE_SPEED


def test_curve_release_to_positive_base_accel_has_no_acceleration_pulse():
  controller = CurveAccelerationController()
  curve = confirmed_curve_limit(1.0)
  output_accel = 0.0
  for _ in range(20):
    cap = controller.update(curve, 20.0, True, False, output_accel, 0.6)
    output_accel = min(0.6, cap.accel)

  missing = CurveSpeedLimit()
  released_outputs = [output_accel]
  for _ in range(CURVE_ACCEL_RELEASE_HOLD_FRAMES + 40):
    cap = controller.update(missing, 20.0, True, False, released_outputs[-1], 0.6)
    output_accel = min(0.6, cap.accel) if cap.active else 0.6
    released_outputs.append(output_accel)

  positive_jerk = np.diff(released_outputs) / DT_MDL
  assert np.max(positive_jerk) <= CURVE_RELEASE_JERK + 1e-9
  assert released_outputs[-1] == 0.6


def test_positive_acceleration_reserves_more_response_distance():
  model = model_with_curve(0.012, curve_start=15)
  coasting_limiter = CurveSpeedLimiter(1.8)
  accelerating_limiter = CurveSpeedLimiter(1.8)

  coasting = update(coasting_limiter, model, previous_accel=0.0)
  accelerating = update(accelerating_limiter, model, previous_accel=1.2)

  assert coasting.active and accelerating.active
  assert accelerating_limiter.target_distance < coasting_limiter.target_distance
  assert accelerating.speed < coasting.speed


def test_half_speed_drifting_prediction_never_confirms():
  limiter = CurveSpeedLimiter(1.8)
  frame_travel = 25.0 * DT_MDL

  for frame in range(12):
    limiter.ego_distance += frame_travel
    distance = 80.0 - 0.5 * frame * frame_travel
    candidate = _CurveCandidate(10.0, MIN_CURVE_SPEED, distance, distance)
    _, track = limiter._update_tracks([candidate])
    assert track is not None
    assert not track.confirmed


def test_confirmed_prediction_is_downgraded_when_it_stops_approaching():
  limiter = CurveSpeedLimiter(1.8)
  frame_travel = 25.0 * DT_MDL
  start_distance = 80.0
  track = None

  for frame in range(6):
    limiter.ego_distance += frame_travel
    distance = start_distance - frame * frame_travel
    candidate = _CurveCandidate(10.0, MIN_CURVE_SPEED, distance, distance)
    _, track = limiter._update_tracks([candidate])

  assert track is not None and track.confirmed

  fixed_distance = start_distance - 5 * frame_travel
  for _ in range(4):
    limiter.ego_distance += frame_travel
    candidate = _CurveCandidate(10.0, MIN_CURVE_SPEED, fixed_distance, fixed_distance)
    _, track = limiter._update_tracks([candidate])

  assert track is not None
  assert not track.confirmed

  for valid_frame in range(CURVE_CONFIRMATION_FRAMES):
    limiter.ego_distance += frame_travel
    distance = track.world_anchor - limiter.ego_distance
    candidate = _CurveCandidate(10.0, MIN_CURVE_SPEED, distance, distance)
    _, track = limiter._update_tracks([candidate])
    if valid_frame < CURVE_CONFIRMATION_FRAMES - 1:
      assert track is not None
      assert not track.confirmed
  assert track is not None and track.confirmed


def test_alternating_world_curves_are_tracked_independently():
  limiter = CurveSpeedLimiter(1.8)
  frame_travel = 25.0 * DT_MDL

  for frame in range(12):
    limiter.ego_distance += frame_travel
    world_anchor = 80.0 if frame % 2 == 0 else 120.0
    distance = world_anchor - limiter.ego_distance
    candidate = _CurveCandidate(10.0, MIN_CURVE_SPEED, distance, distance)
    limiter._update_tracks([candidate])

  assert len(limiter.tracks) == 2
  assert all(track.confirmed for track in limiter.tracks)


def test_lane_change_trajectory_does_not_enable_curve_braking():
  model = model_with_curve(0.02, curve_start=8)
  model.meta = SimpleNamespace(laneChangeState=log.LaneChangeState.laneChangeStarting)

  result = update(CurveSpeedLimiter(1.8), model)

  assert not result.active
  assert result.required_decel == 0.0


def test_missing_curve_near_target_releases_without_crossing_speed_floor():
  controller = CurveAccelerationController()
  curve = confirmed_curve_limit(MAX_CURVE_DECEL)
  output_accel = 0.0
  for _ in range(40):
    cap = controller.update(curve, 20.0, True, False, output_accel, 0.5)
    output_accel = min(0.5, cap.accel)

  speed = MIN_CURVE_SPEED + 1.1
  speeds = [speed]
  missing = CurveSpeedLimit()
  for _ in range(80):
    cap = controller.update(missing, speed, True, False, output_accel, 0.5)
    output_accel = min(0.5, cap.accel) if cap.active else 0.5
    speed += output_accel * DT_MDL
    speeds.append(speed)

  assert min(speeds) >= MIN_CURVE_SPEED


def test_temporary_stronger_base_braking_cannot_create_release_pulse():
  controller = CurveAccelerationController()
  curve = confirmed_curve_limit(1.0)
  output_accel = 0.0
  for _ in range(25):
    cap = controller.update(curve, 20.0, True, False, output_accel, 0.6)
    output_accel = min(0.6, cap.accel)

  missing = CurveSpeedLimit()
  for _ in range(CURVE_ACCEL_RELEASE_HOLD_FRAMES + 2):
    cap = controller.update(missing, 20.0, True, False, output_accel, 0.6)
    output_accel = min(0.6, cap.accel) if cap.active else 0.6

  stronger_output = -1.0
  cap = controller.update(missing, 20.0, True, False, output_accel, stronger_output)
  output_accel = min(stronger_output, cap.accel) if cap.active else stronger_output
  previous_output = output_accel

  cap = controller.update(missing, 20.0, True, False, previous_output, 0.6)
  output_accel = min(0.6, cap.accel) if cap.active else 0.6

  assert cap.active
  assert (output_accel - previous_output) / DT_MDL <= CURVE_RELEASE_JERK + 1e-9


def test_mode_change_uses_smooth_missing_curve_release():
  controller = CurveAccelerationController()
  curve = confirmed_curve_limit(1.0)
  output_accel = 0.0
  for _ in range(25):
    cap = controller.update(curve, 20.0, True, False, output_accel, 0.6)
    output_accel = min(0.6, cap.accel)

  cap = controller.update(CurveSpeedLimit(), 20.0, True, False, output_accel, 0.6)

  assert cap.active
  assert cap.accel == output_accel


def test_curve_cap_never_inherits_emergency_base_deceleration():
  controller = CurveAccelerationController()
  curve = confirmed_curve_limit(0.8)
  output_accel = 0.0
  for _ in range(20):
    cap = controller.update(curve, 20.0, True, False, output_accel, 0.6)
    output_accel = min(0.6, cap.accel)

  emergency_output = -3.5
  cap = controller.update(curve, 20.0, True, False, emergency_output, 0.6)

  assert cap.active
  assert cap.accel >= -MAX_CURVE_DECEL

  fresh_controller = CurveAccelerationController()
  fresh_cap = fresh_controller.update(curve, 20.0, True, False, emergency_output, 0.6)

  assert fresh_cap.active
  assert fresh_cap.accel >= -MAX_CURVE_DECEL
