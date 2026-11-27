import multiprocessing
import time
from openpilot.common.test import OpenpilotTestCase
from openpilot.selfdrive.test.helpers import with_processes


class TestRaylibUi(OpenpilotTestCase):
  @with_processes(["ui"])
  def test_raylib_ui(self):
    """Test initialization of the UI widgets is successful."""
    time.sleep(1)

  def test_open_dp_settings(self):
    # Keep the native renderer out of the process that forks the UI startup test.
    process = multiprocessing.get_context("spawn").Process(target=_open_dp_settings)
    process.start()
    try:
      process.join(timeout=30)
      assert process.exitcode == 0
    finally:
      if process.is_alive():
        process.kill()
        process.join()
      process.close()


def _open_dp_settings():
  import raylib_nb as rl
  from opendbc.car.structs import CarParams
  from openpilot.selfdrive.ui.layouts.settings.settings import PanelType, SettingsLayout
  from openpilot.selfdrive.ui.ui_state import ui_state
  from openpilot.system.ui.lib.application import MousePos, gui_app

  gui_app.init_window("DP settings test")
  try:
    ui_state.CP = None
    layout = SettingsLayout()
    layout.show_event()
    ui_state.CP = CarParams.new_message(brand="toyota", openpilotLongitudinalControl=True)
    rect = rl.Rectangle(0, 0, 1920, 1080)

    rl.begin_drawing()
    try:
      layout.render(rect)
      panel = layout._panels[PanelType.DRAGONPILOT]
      button = panel.button_rect
      layout._handle_mouse_release(MousePos(button.x + button.width / 2, button.y + button.height / 2))
      assert layout._current_panel is PanelType.DRAGONPILOT
      layout.render(rect)
    finally:
      rl.end_drawing()

    assert "dp_lon_curve_lookahead_seconds" in panel.instance._toggles
  finally:
    gui_app.close()
