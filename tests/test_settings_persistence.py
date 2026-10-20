"""설정 변경이 실제 저장 완료와 일치하는지 검증한다."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import cogs.tts as tts
import core.web_admin as web_admin
import utils.file_utils as files


def test_tts_command_write_failure_keeps_settings_and_sends_no_success(monkeypatch):
    monkeypatch.setattr(tts, "load_tts_settings", lambda: {"1_2": {"slow": False}})
    monkeypatch.setattr(tts, "save_tts_settings", Mock(side_effect=OSError("disk full")))
    cog = tts.TTS(None)
    ctx = SimpleNamespace(author=SimpleNamespace(id=2), guild=SimpleNamespace(id=1), send=AsyncMock())
    with pytest.raises(OSError, match="disk full"):
        asyncio.run(tts.TTS.tts_slow_toggle.callback(cog, ctx))
    ctx.send.assert_not_called()
    assert cog.tts_settings == {"1_2": {"slow": False}}


def test_concurrent_tts_settings_updates_preserve_both_changes(monkeypatch, tmp_path):
    path = tmp_path / "tts_settings.json"
    monkeypatch.setattr(files, "TTS_SETTINGS_FILE", str(path))
    monkeypatch.setattr(tts, "load_tts_settings", lambda: {})
    cog = tts.TTS(None)

    async def scenario():
        await asyncio.gather(
            cog.set_user_settings(2, 1, enabled=True),
            cog.set_user_settings(2, 1, slow=True),
        )

    asyncio.run(scenario())
    assert json.loads(path.read_text(encoding="utf-8")) == cog.tts_settings
    assert cog.tts_settings["1_2"]["enabled"] is True
    assert cog.tts_settings["1_2"]["slow"] is True


def test_dashboard_reports_active_json_backend_after_mysql_fallback(monkeypatch):
    monkeypatch.setattr(web_admin, "USE_MYSQL", True)
    cog = SimpleNamespace(use_mysql=False)
    bot = SimpleNamespace(get_cog=lambda name: cog)
    status = asyncio.run(web_admin.WebAdmin(bot)._db_status())
    assert status["backend"] == "JSON 파일"
    assert status["use_mysql"] is False
    assert status["host"] is None
