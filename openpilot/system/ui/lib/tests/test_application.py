import os
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

os.environ.setdefault("SCALE", "1")

import openpilot.system.ui.lib.application as application
from openpilot.system.ui.lib.application import FontWeight, GuiApplication
from openpilot.system.ui.lib.multilang import multilang


def test_font_fallback_api_compatibility(monkeypatch):
  app = GuiApplication.__new__(GuiApplication)
  app._fonts = {}
  app._active_lang_code = "zh-CHS"

  fallback = Mock(return_value=True)
  monkeypatch.setattr(multilang, "requires_font_fallback", fallback)
  monkeypatch.setattr(application, "as_file", Mock(return_value=nullcontext(Path("/missing-fonts"))))
  loaded_font = SimpleNamespace(texture=SimpleNamespace())
  load_font = Mock(return_value=loaded_font)
  monkeypatch.setattr(application.rl, "load_font", load_font)
  monkeypatch.setattr(application.rl, "set_texture_filter", Mock())

  assert app.font(FontWeight.NORMAL) is loaded_font
  assert fallback.call_count == 2
  load_font.assert_called_once_with("/missing-fonts/OpFont-Regular-zh-CHS.fnt")
