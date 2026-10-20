"""저장 순서, DB 장애 전환, 쓰기 오류와 손상 파일 복구에 대한 회귀 테스트."""
import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import cogs.music as music
import utils.db_utils as db
import utils.file_utils as files


@pytest.fixture
def music_env(monkeypatch, tmp_path):
    monkeypatch.setattr(music, "USE_MYSQL", False)
    monkeypatch.setattr(music, "load_history", lambda: {})
    monkeypatch.setattr(music, "load_playlists", lambda: {})
    monkeypatch.setattr(music, "ensure_logs_dir", lambda: None)
    monkeypatch.setattr(files, "HISTORY_FILE", str(tmp_path / "history.json"))
    monkeypatch.setattr(files, "PLAYLISTS_FILE", str(tmp_path / "playlists.json"))
    return music.Music(None)


@pytest.mark.parametrize("kind", ["trpg", "trpg_party", "trpg_world"])
def test_corrupt_save_stays_blocked_until_repaired(kind, monkeypatch, tmp_path):
    path = tmp_path / f"{kind}.json"
    broken = "{broken json"
    path.write_text(broken, encoding="utf-8")
    monkeypatch.setattr(files, f"{kind.upper()}_SAVES_FILE", str(path))
    load = getattr(files, f"load_{kind}_saves")
    save = getattr(files, f"set_{kind}_save")

    # 대화 AI가 읽기를 먼저 시도한 뒤에도, 연속 자동저장은 차단돼야 한다.
    assert load() == {}
    for _ in range(2):
        with pytest.raises(files.JsonStorageError):
            save("new-session", {"scene": "new"})
        assert path.read_text(encoding="utf-8") == broken
    backups = list(tmp_path.glob(f"{kind}.json.corrupt_*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == broken

    path.write_text(json.dumps({"restored-session": {"scene": "restored"}}), encoding="utf-8")
    save("new-session", {"scene": "new"})
    assert set(load()) == {"restored-session", "new-session"}


@pytest.mark.parametrize("name", [
    "save_history", "save_playlists", "save_tts_settings", "save_trpg_saves",
    "save_trpg_party_saves", "save_trpg_world_saves",
])
def test_json_write_errors_reach_the_caller(name, monkeypatch):
    monkeypatch.setattr(files, "USE_MYSQL", False)
    monkeypatch.setattr(files, "atomic_write_json", Mock(side_effect=OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        getattr(files, name)({})


@pytest.mark.parametrize("other_owner", ["1", "2"])
@pytest.mark.parametrize("cancel_first", [0, 1, 2])
def test_playlist_writes_keep_order_including_cancellation(music_env, monkeypatch, other_owner, cancel_first):
    cog = music_env
    cog.playlists = {"1": {"p": ["first"]}}
    entered, release = threading.Event(), threading.Event()
    writes = []

    def slow_save(snapshot):
        writes.append(snapshot)
        if len(writes) == 1:
            entered.set()
            assert release.wait(5), "test writer was not released"
        files.save_playlists(snapshot)

    monkeypatch.setattr(music, "save_playlists", slow_save)

    async def scenario():
        first = asyncio.create_task(cog.save_playlist("1", "p"))
        second = None
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            if cancel_first:
                first.cancel()
                await asyncio.sleep(0)
                if cancel_first == 2:
                    first.cancel()
            cog.playlists.setdefault(other_owner, {}).setdefault("p", []).append("second")
            # 단건/전체 저장 경로가 같은 순서를 공유해야 한다.
            second = asyncio.create_task(cog.save_playlists())
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert len(writes) == 1
        finally:
            release.set()
            results = await asyncio.gather(*(t for t in (first, second) if t), return_exceptions=True)
        if cancel_first:
            assert isinstance(results[0], asyncio.CancelledError)
        else:
            assert results[0] is None
        assert results[1] is None

    asyncio.run(scenario())
    assert json.loads(Path(files.PLAYLISTS_FILE).read_text(encoding="utf-8")) == cog.playlists
    assert "second" in cog.playlists[other_owner]["p"]


def test_concurrent_history_add_clear_add_preserves_order(music_env):
    cog = music_env

    async def scenario():
        await asyncio.gather(
            cog.add_to_history(1, {"title": "before"}),
            cog.clear_guild_history("1"),
            cog.add_to_history(1, {"title": "after"}),
        )
        await cog.save_history()

    asyncio.run(scenario())
    disk = json.loads(Path(files.HISTORY_FILE).read_text(encoding="utf-8"))
    assert disk == cog.history
    assert [item["title"] for item in disk["1"]] == ["after"]


@pytest.mark.parametrize("failure_stage", ["init", "history", "playlists"])
def test_mysql_startup_failure_uses_json_for_all_future_writes(music_env, monkeypatch, failure_stage):
    cog = music_env
    cog.use_mysql = True
    monkeypatch.setattr(files, "USE_MYSQL", True)
    for stage, name in [
        ("init", "init_db_pool"), ("history", "load_history_from_db"),
        ("playlists", "load_playlists_from_db"),
    ]:
        monkeypatch.setattr(music, name, AsyncMock(
            return_value={}, side_effect=RuntimeError("DB unavailable") if stage == failure_stage else None,
        ))
    for name in [
        "save_playlist_to_db", "save_playlists_to_db", "save_history_to_db",
        "add_history_item_to_db", "clear_guild_history_in_db",
    ]:
        monkeypatch.setattr(music, name, AsyncMock(side_effect=AssertionError("unexpected DB write")))
    files.save_playlists({"1": {"p": ["from-json"]}})
    files.save_history({})

    async def scenario():
        await cog._load_data()
        assert cog.use_mysql is False
        assert cog.playlists["1"]["p"] == ["from-json"]
        await cog.update_playlist("1", "p", lambda playlists: playlists["p"].append("new"))
        await cog.save_playlist("1", "p")
        await cog.save_playlists()
        await cog.add_to_history(1, {"title": "new song"})
        await cog.save_history()
        assert json.loads(Path(files.HISTORY_FILE).read_text(encoding="utf-8")) == cog.history
        await cog.clear_guild_history("1")

    asyncio.run(scenario())
    assert json.loads(Path(files.PLAYLISTS_FILE).read_text(encoding="utf-8")) == {"1": {"p": ["from-json", "new"]}}
    assert json.loads(Path(files.HISTORY_FILE).read_text(encoding="utf-8")) == {"1": []}


@pytest.mark.parametrize("backend", ["json", "mysql"])
def test_failed_playlist_command_does_not_claim_success_or_change_cache(music_env, monkeypatch, backend):
    cog = music_env
    cog.use_mysql = backend == "mysql"
    cog.playlists = {"1": {"p": ["existing"]}}
    monkeypatch.setattr(music, "save_playlists", Mock(side_effect=OSError("write failed")))
    monkeypatch.setattr(music, "save_playlist_to_db", AsyncMock(side_effect=OSError("write failed")))
    ctx = SimpleNamespace(author=SimpleNamespace(id=1), send=AsyncMock())
    with pytest.raises(OSError, match="write failed"):
        asyncio.run(music.Music.플레이리스트추가.callback(cog, ctx, "p", "new"))
    ctx.send.assert_not_called()
    assert cog.playlists == {"1": {"p": ["existing"]}}


def test_healthy_mysql_keeps_db_backend_for_updates_and_deletion(music_env, monkeypatch):
    cog = music_env
    cog.use_mysql = True
    monkeypatch.setattr(music, "init_db_pool", AsyncMock())
    monkeypatch.setattr(music, "load_history_from_db", AsyncMock(return_value={"1": []}))
    monkeypatch.setattr(music, "load_playlists_from_db", AsyncMock(return_value={"1": {"p": ["first"]}}))
    save_playlist = AsyncMock()
    add_history, clear_history = AsyncMock(), AsyncMock()
    monkeypatch.setattr(music, "save_playlist_to_db", save_playlist)
    monkeypatch.setattr(music, "add_history_item_to_db", add_history)
    monkeypatch.setattr(music, "clear_guild_history_in_db", clear_history)
    monkeypatch.setattr(music, "save_playlists", Mock(side_effect=AssertionError("unexpected JSON write")))
    monkeypatch.setattr(music, "save_history", Mock(side_effect=AssertionError("unexpected JSON write")))

    async def scenario():
        await cog._load_data()
        assert cog.use_mysql is True
        await cog.update_playlist("1", "p", lambda playlists: playlists["p"].append("second"))
        save_playlist.assert_awaited_with(1, "p", ["first", "second"])
        assert cog.playlists == {"1": {"p": ["first", "second"]}}
        await cog.update_playlist("1", "p", lambda playlists: playlists.pop("p"))
        save_playlist.assert_awaited_with(1, "p", [])
        assert cog.playlists == {"1": {}}
        await cog.add_to_history(1, {"title": "song"})
        assert cog.history["1"][0]["title"] == "song"
        await cog.clear_guild_history("1")
        assert cog.history["1"] == []

    asyncio.run(scenario())
    add_history.assert_awaited_once()
    clear_history.assert_awaited_once_with(1)


@pytest.mark.parametrize("name,args", [
    ("load_history_from_db", ()), ("load_playlists_from_db", ()),
    ("save_history_to_db", ({},)), ("save_playlists_to_db", ({},)),
    ("save_playlist_to_db", (1, "p", ["url"])),
    ("add_history_item_to_db", (1, "title", "url", 1)),
    ("clear_guild_history_in_db", (1,)), ("get_history_from_db", (1,)),
    ("get_playlist_from_db", (1, "p")),
])
def test_db_failure_is_not_treated_as_empty_or_success(name, args, monkeypatch):
    monkeypatch.setattr(db, "USE_MYSQL", True)
    monkeypatch.setattr(db, "get_pool", AsyncMock(side_effect=RuntimeError("DB unavailable")))
    with pytest.raises(RuntimeError, match="DB unavailable"):
        asyncio.run(getattr(db, name)(*args))


def test_failed_schema_init_closes_partial_pool(monkeypatch):
    pool = SimpleNamespace(close=Mock(), wait_closed=AsyncMock())
    monkeypatch.setattr(db, "USE_MYSQL", True)
    monkeypatch.setattr(db, "_pool", None)
    monkeypatch.setattr(db.aiomysql, "create_pool", AsyncMock(return_value=pool))
    monkeypatch.setattr(db, "create_tables", AsyncMock(side_effect=RuntimeError("schema failure")))
    with pytest.raises(RuntimeError, match="schema failure"):
        asyncio.run(db.init_db_pool())
    pool.close.assert_called_once()
    pool.wait_closed.assert_awaited_once()
    assert db._pool is None
