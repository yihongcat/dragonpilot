import os
import subprocess
import time
import unittest

from multiprocessing import Queue
from queue import Empty

from openpilot.common.test import OpenpilotTestCase
from openpilot.cereal import messaging
from openpilot.common.basedir import BASEDIR
from openpilot.tools.sim.bridge.common import QueueMessageType

SIM_DIR = os.path.join(BASEDIR, "openpilot/tools/sim")

class TestSimBridgeBase(OpenpilotTestCase):
  @classmethod
  def setup_class(cls):
    if cls is TestSimBridgeBase:
      raise unittest.SkipTest("Don't run this base class, run test_metadrive_bridge.py instead")

  def setup_method(self):
    self.processes = []

  def test_driving(self):
    # Startup manager and bridge.py. Check processes are running, then engage and verify.
    p_manager = subprocess.Popen("./launch_openpilot.sh", cwd=SIM_DIR)
    self.processes.append(p_manager)

    sm = messaging.SubMaster(['selfdriveState', 'onroadEvents', 'managerState', 'carState', 'carControl',
                              'longitudinalPlan', 'modelV2'])
    health_services = ['selfdriveState', 'onroadEvents', 'managerState']
    q = Queue()
    bridge = self.create_bridge()
    p_bridge = bridge.run(q, retries=10)
    self.processes.append(p_bridge)

    max_time_per_step = 60

    # Wait for bridge to startup
    start_waiting = time.monotonic()
    while not bridge.started.value and time.monotonic() < start_waiting + max_time_per_step:
      time.sleep(0.1)
    assert p_bridge.exitcode is None, f"Bridge process should be running, but exited with code {p_bridge.exitcode}"

    start_time = time.monotonic()
    no_car_events_issues_once = False
    car_event_issues = []
    not_running = []
    while time.monotonic() < start_time + max_time_per_step:
      sm.update()

      not_running = [p.name for p in sm['managerState'].processes if not p.running and p.shouldBeRunning]
      car_event_issues = [event.name for event in sm['onroadEvents'] if any([event.noEntry, event.softDisable, event.immediateDisable])]

      if sm.all_alive(health_services) and len(car_event_issues) == 0 and len(not_running) == 0:
        no_car_events_issues_once = True
        break

    assert no_car_events_issues_once, \
                    f"Failed because no messages received, or CarEvents '{car_event_issues}' or processes not running '{not_running}'"

    start_time = time.monotonic()
    min_counts_control_active = 100
    control_active = 0

    while time.monotonic() < start_time + max_time_per_step:
      sm.update()

      if sm.all_alive(health_services) and sm['selfdriveState'].active:
        control_active += 1

        if control_active == min_counts_control_active:
          break

    assert min_counts_control_active == control_active, f"Simulator did not engage a minimal of {min_counts_control_active} steps was {control_active}"

    max_speed = 0.0
    max_commanded_accel = float('-inf')
    finish_deadline = time.monotonic() + 240
    while bridge.started.value and time.monotonic() < finish_deadline:
      sm.update(100)
      max_speed = max(max_speed, sm['carState'].vEgo)
      max_commanded_accel = max(max_commanded_accel, sm['carControl'].actuators.accel)
    assert not bridge.started.value, "Simulator exceeded the wall-clock limit"

    done_info = None
    result_deadline = time.monotonic() + 2.0
    while time.monotonic() < result_deadline:
      try:
        state = q.get(timeout=max(0.0, result_deadline - time.monotonic()))
      except Empty:
        break
      if state.type == QueueMessageType.TERMINATION_INFO:
        done_info = state.info
        break
    assert done_info is not None, "Simulator exited without a result"
    failure_states = [state for state, failed in done_info.items() if state != "timeout" and failed]
    assert len(failure_states) == 0, (
      f"Simulator failed: {failure_states}; peak speed {max_speed:.2f} m/s, peak acceleration command " +
      f"{max_commanded_accel:.2f} m/s², final target {sm['longitudinalPlan'].aTarget:.2f} m/s², " +
      f"model target {sm['modelV2'].action.desiredAcceleration:.2f} m/s²"
    )

  def teardown_method(self):
    print("Test shutting down. CommIssues are acceptable")
    for p in reversed(self.processes):
      p.terminate()

    for p in reversed(self.processes):
      p.kill()
