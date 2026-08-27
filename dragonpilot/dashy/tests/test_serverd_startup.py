import importlib

from openpilot.system.manager.process_config import managed_processes


def test_serverd_runtime_dependency_available():
  assert managed_processes["serverd"].enabled
  importlib.import_module("dragonpilot.dashy.serverd")
