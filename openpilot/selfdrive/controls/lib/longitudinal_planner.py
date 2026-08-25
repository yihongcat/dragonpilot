#!/usr/bin/env python3
import math
import numpy as np

import openpilot.cereal.messaging as messaging
from opendbc.car.interfaces import ACCEL_MIN, ACCEL_MAX
from openpilot.common.constants import CV
from openpilot.common.filter_simple import FirstOrderFilter
from openpilot.common.realtime import DT_MDL
from openpilot.selfdrive.modeld.constants import ModelConstants
from openpilot.selfdrive.controls.lib.longcontrol import LongCtrlState
from openpilot.selfdrive.controls.lib.longitudinal_mpc_lib.long_mpc import LongitudinalMpc, LongitudinalPlanSource, STOP_DISTANCE
from openpilot.selfdrive.controls.lib.longitudinal_mpc_lib.long_mpc import T_IDXS as T_IDXS_MPC
from openpilot.selfdrive.controls.lib.drive_helpers import CONTROL_N, get_accel_from_plan, should_stop
from openpilot.selfdrive.car.cruise import V_CRUISE_MAX, V_CRUISE_UNSET
from openpilot.common.swaglog import cloudlog
from dragonpilot.selfdrive.controls.lib.acm import ACM
from dragonpilot.selfdrive.controls.lib.aem import AEM
from dragonpilot.selfdrive.controls.lib.apm import APM
from dragonpilot.selfdrive.controls.lib.curve_speed_limiter import (CurveAccelerationController, CurveSpeedLimiter,
                                                                   select_accel_with_curve)

A_CRUISE_MAX_VALS = [1.6, 1.2, 0.8, 0.6]
A_CRUISE_MAX_BP = [0., 10.0, 25., 40.]
J_CRUISE_VALS = [1.6, 1.2, 0.8, 0.6]
A_CRUISE_MIN = -1.2
CONTROL_N_T_IDX = ModelConstants.T_IDXS[:CONTROL_N]
ALLOW_THROTTLE_THRESHOLD = 0.4
MIN_ALLOW_THROTTLE_SPEED = 2.5

# Lookup table for turns
_A_TOTAL_MAX_V = [1.7, 3.2]
_A_TOTAL_MAX_BP = [20., 40.]


class DPFlags:
  ACM = 1
  AEM = 2
  APM = 2 ** 2


def get_max_accel(v_ego):
  return np.interp(v_ego, A_CRUISE_MAX_BP, A_CRUISE_MAX_VALS)

def get_coast_accel(pitch):
  return np.sin(pitch) * -5.65 - 0.3  # fitted from data using xx/projects/allow_throttle/compute_coast_accel.py

def get_cruise_accel(e2e, v_cruise, v_ego, a_cruise_prev, angle_steers, CP, dt,
                     accel_coast, allow_throttle, min_accel=A_CRUISE_MIN):
  max_accel = ACCEL_MAX if e2e else get_max_accel(v_ego)

  if not e2e:
    a_total_max = np.interp(v_ego, _A_TOTAL_MAX_BP, _A_TOTAL_MAX_V)
    a_y = v_ego ** 2 * angle_steers * CV.DEG_TO_RAD / (CP.steerRatio * CP.wheelbase)
    a_x_allowed = math.sqrt(max(a_total_max ** 2 - a_y ** 2, 0.))
    max_accel = min(max_accel, a_x_allowed)
    if not allow_throttle:
      clipped_accel_coast = max(accel_coast, ACCEL_MIN)
      coast_limit = np.interp(v_ego, [MIN_ALLOW_THROTTLE_SPEED, MIN_ALLOW_THROTTLE_SPEED*2], [max_accel, clipped_accel_coast])
      max_accel = min(max_accel, coast_limit)

  target_accel = np.clip(v_cruise - v_ego, min_accel, max_accel)
  if not e2e:
    j_cruise = np.interp(v_ego, A_CRUISE_MAX_BP, J_CRUISE_VALS)
    target_accel = float(np.clip(target_accel, a_cruise_prev - j_cruise * dt, a_cruise_prev + j_cruise * dt))

  return target_accel


class LongitudinalPlanner:
  def __init__(self, CP, init_v=0.0, init_a=0.0, dt=DT_MDL):
    self.CP = CP
    self.mpc = LongitudinalMpc(dt=dt)
    self.fcw = False
    self.dt = dt
    self.allow_throttle = True

    self.v_desired_filter = FirstOrderFilter(init_v, 2.0, self.dt)
    self.a_cruise = init_a
    self.a_curve = 0.0
    self.curve_active_prev = False
    self.output_a_target = init_a
    self.output_should_stop = False

    self.v_desired_trajectory = np.zeros(CONTROL_N)
    self.a_desired_trajectory = np.zeros(CONTROL_N)
    self.j_desired_trajectory = np.zeros(CONTROL_N)
    self.acm = ACM()
    self.aem = AEM()
    self.apm = APM()
    self.curve_speed_limiter = CurveSpeedLimiter(CP.maxLateralAccel, CP.longitudinalActuatorDelay)
    self.curve_accel_controller = CurveAccelerationController(dt)

  def update(self, sm, dp_flags=0, dp_curve_speed_reduction=0, dp_stop_distance=STOP_DISTANCE):
    if len(sm['carControl'].orientationNED) == 3:
      accel_coast = get_coast_accel(sm['carControl'].orientationNED[1])
    else:
      accel_coast = ACCEL_MAX

    v_ego = sm['carState'].vEgo
    v_cruise_kph = min(sm['carState'].vCruise, V_CRUISE_MAX)
    v_cruise = v_cruise_kph * CV.KPH_TO_MS
    if sm['controlsState'].forceDecel:
      v_cruise = 0.0

    long_control_off = sm['controlsState'].longControlState == LongCtrlState.off

    # Reset current state when not engaged, or user is controlling the speed
    reset_state = long_control_off if self.CP.openpilotLongitudinalControl else not sm['selfdriveState'].enabled
    # PCM cruise speed may be updated a few cycles later, check if initialized
    v_cruise_initialized = sm['carState'].vCruise != V_CRUISE_UNSET
    reset_state = reset_state or not v_cruise_initialized

    throttle_probs = sm['modelV2'].meta.disengagePredictions.gasPressProbs
    throttle_prob = throttle_probs[1] if len(throttle_probs) > 1 else 1.0
    self.allow_throttle = throttle_prob > ALLOW_THROTTLE_THRESHOLD or v_ego <= MIN_ALLOW_THROTTLE_SPEED

    steer_angle_without_offset = sm['carState'].steeringAngleDeg - sm['vehicleParameters'].angleOffsetDeg

    if reset_state:
      self.v_desired_filter.x = v_ego
      self.output_a_target = np.clip(sm['carState'].aEgo, ACCEL_MIN, ACCEL_MAX)
      self.a_cruise = self.output_a_target

    # Prevent divergence, smooth in current v_ego
    self.v_desired_filter.x = max(0.0, self.v_desired_filter.update(v_ego))

    # No change cost when user is controlling the speed, or when standstill
    prev_accel_constraint = not (reset_state or sm['carState'].standstill)

    personality = sm['selfdriveState'].personality
    if dp_flags & DPFlags.APM:
      personality = self.apm.get_personality(v_ego, personality)

    mode = 'blended' if sm['selfdriveState'].experimentalMode else 'acc'
    if dp_flags & DPFlags.AEM:
      self.aem.update_states(model_msg=sm['modelV2'], radar_msg=sm['radarState'], v_ego=v_ego)
      mode = self.aem.get_mode(mode)

    v_cruise_setpoint = v_cruise
    curve_overriding = sm['carControl'].cruiseControl.override
    curve_enabled = mode == 'blended' and not reset_state
    curve_limit = self.curve_speed_limiter.update(
      sm['modelV2'], v_ego, v_cruise,
      dp_curve_speed_reduction, curve_enabled, curve_overriding, self.output_a_target,
    )

    self.mpc.set_weights(prev_accel_constraint, personality=personality)
    self.mpc.set_cur_state(self.v_desired_filter.x, self.output_a_target)
    self.mpc.update(sm['radarState'], personality=personality, stop_distance=dp_stop_distance)

    self.v_desired_trajectory = np.interp(CONTROL_N_T_IDX, T_IDXS_MPC, self.mpc.v_solution)
    self.a_desired_trajectory = np.interp(CONTROL_N_T_IDX, T_IDXS_MPC, self.mpc.a_solution)
    if dp_flags & DPFlags.ACM:
      self.acm.enabled = True
      user_control = long_control_off if self.CP.openpilotLongitudinalControl else not sm['selfdriveState'].enabled
      self.acm.update_states(sm['carControl'], sm['radarState'], user_control, v_ego, v_cruise)
      self.a_desired_trajectory = self.acm.update_a_desired_trajectory(self.a_desired_trajectory)
    else:
      self.acm.enabled = False
    self.j_desired_trajectory = np.interp(CONTROL_N_T_IDX, T_IDXS_MPC[:-1], self.mpc.j_solution)

    # TODO counter is only needed because radar is glitchy, remove once radar is gone
    self.fcw = self.mpc.crash_cnt > 2 and not sm['carState'].standstill
    if self.fcw:
      cloudlog.info("FCW triggered")

    # Save starting point for next iteration
    a_prev = self.output_a_target

    action_t =  self.CP.longitudinalActuatorDelay + DT_MDL
    output_a_target_mpc = get_accel_from_plan(self.v_desired_trajectory, self.a_desired_trajectory, CONTROL_N_T_IDX,
                                              action_t=action_t)
    output_should_stop_mpc = should_stop(v_ego, output_a_target_mpc)
    output_a_target_e2e = sm['modelV2'].action.desiredAcceleration
    output_should_stop_e2e = sm['modelV2'].action.shouldStop

    self.a_cruise = get_cruise_accel(mode == 'blended', v_cruise, v_ego,
                                     self.a_cruise, steer_angle_without_offset, self.CP, self.dt,
                                     accel_coast, self.allow_throttle)
    cruise_accel_before_curve = self.a_cruise
    cruise_should_stop = should_stop(v_ego, self.a_cruise)

    base_candidates = [(output_a_target_mpc, self.mpc.source, output_should_stop_mpc),
                       (self.a_cruise, LongitudinalPlanSource.cruise, cruise_should_stop)]
    if mode == 'blended':
      base_candidates.append((output_a_target_e2e, LongitudinalPlanSource.e2e, output_should_stop_e2e))

    base_accel = min(base_candidates, key=lambda candidate: candidate[0])[0]
    # Curve deceleration is an independent, jerk-limited acceleration cap. It is
    # deliberately excluded from shouldStop; stopping remains owned by MPC/E2E.
    curve_accel_limit = self.curve_accel_controller.update(
      curve_limit, v_ego, not reset_state, curve_overriding, self.output_a_target, base_accel,
    )
    self.a_curve = curve_accel_limit.accel if curve_accel_limit.active else 0.0
    output_a_target, self.mpc.source, self.output_should_stop = select_accel_with_curve(
      base_candidates, curve_accel_limit, LongitudinalPlanSource.cruise,
    )
    self.output_a_target = np.clip(output_a_target, ACCEL_MIN, ACCEL_MAX)

    curve_transition = curve_limit.active != self.curve_active_prev
    if dp_curve_speed_reduction > 0 and (curve_transition or getattr(sm, 'frame', 0) % round(1.0 / self.dt) == 0):
      def finite_or_negative(value):
        return value if math.isfinite(value) else -1.0

      curve_log_format = "".join((
        "dp_curve_speed mode=%s enabled=%s active=%s confirmed=%s unreachable=%s ",
        "cap_active=%s confirm_frames=%d override=%s strength=%d ",
        "v_ego=%.1f cruise=%.1f limit=%.1f curve=%.1f distance=%.1f required_decel=%.2f ",
        "curve_accel=%.2f cruise_accel=%.2f output_accel=%.2f source=%s",
      ))
      cloudlog.info(
        curve_log_format,
        mode, curve_enabled, curve_limit.active, curve_limit.confirmed, curve_limit.unreachable,
        curve_accel_limit.active, self.curve_speed_limiter.confirmation_frames,
        curve_overriding, dp_curve_speed_reduction,
        v_ego * CV.MS_TO_KPH, v_cruise_setpoint * CV.MS_TO_KPH,
        finite_or_negative(curve_limit.speed) * CV.MS_TO_KPH,
        finite_or_negative(self.curve_speed_limiter.target_curve_speed) * CV.MS_TO_KPH,
        finite_or_negative(self.curve_speed_limiter.target_distance),
        curve_limit.required_decel, self.a_curve, cruise_accel_before_curve,
        self.output_a_target, self.mpc.source,
      )
    self.curve_active_prev = curve_limit.active
    self.v_desired_filter.x = self.v_desired_filter.x + self.dt * (self.output_a_target + a_prev) / 2.0

  def publish(self, sm, pm):
    plan_send = messaging.new_message('longitudinalPlan')

    plan_send.valid = sm.all_checks()

    longitudinalPlan = plan_send.longitudinalPlan
    longitudinalPlan.modelMonoTime = sm.logMonoTime['modelV2']
    longitudinalPlan.processingDelay = (plan_send.logMonoTime / 1e9) - sm.logMonoTime['modelV2']
    longitudinalPlan.solverExecutionTime = self.mpc.solve_time

    longitudinalPlan.speeds = self.v_desired_trajectory.tolist()
    longitudinalPlan.accels = self.a_desired_trajectory.tolist()
    longitudinalPlan.jerks = self.j_desired_trajectory.tolist()

    longitudinalPlan.hasLead = sm['radarState'].leadOne.present
    longitudinalPlan.longitudinalPlanSource = self.mpc.source
    longitudinalPlan.fcw = self.fcw

    longitudinalPlan.aTarget = float(self.output_a_target)
    longitudinalPlan.shouldStop = bool(self.output_should_stop)
    longitudinalPlan.allowBrake = True
    longitudinalPlan.allowThrottle = bool(self.allow_throttle)

    pm.send('longitudinalPlan', plan_send)
