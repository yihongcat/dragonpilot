import importlib

from openpilot.system.manager.process_config import managed_processes


def test_serverd_runtime_dependency_available():
  assert managed_processes["serverd"].enabled
  importlib.import_module("dragonpilot.dashy.serverd")


def test_device_ui_qr_dependencies_available():
  importlib.import_module("dragonpilot.selfdrive.ui.dashy_qr")
  importlib.import_module("PIL.Image")
