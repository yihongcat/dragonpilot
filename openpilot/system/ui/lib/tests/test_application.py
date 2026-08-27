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
  monkeypatch.setattr(application, "as_file", Mock(return_value=nullcontext(Path("/fonts"))))
  loaded_font = SimpleNamespace(texture=SimpleNamespace())
  load_font = Mock(return_value=loaded_font)
  monkeypatch.setattr(application.rl, "load_font_ex", load_font)
  monkeypatch.setattr(application.rl, "gen_texture_mipmaps", Mock())
  monkeypatch.setattr(application.rl, "set_texture_filter", Mock())

  assert app.font(FontWeight.NORMAL) is loaded_font
  assert fallback.call_count == 1
  assert load_font.call_args.args[0] == "/fonts/OpFont-Medium.otf"
  assert load_font.call_args.args[1] == 48
  assert load_font.call_args.args[3] > 100


def test_all_font_sources_exist():
  with application.as_file(application.FONT_DIR) as font_dir:
    for font_weight in FontWeight:
      assert (font_dir / font_weight.value).is_file()
      if font_weight != FontWeight.UNIFONT:
        assert (font_dir / application._opfont_filename(font_weight.value)).is_file()
