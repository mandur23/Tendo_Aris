import asyncio
import logging
import os
import socket
import sys
import time
import discord
from discord.ext import commands
from rapidfuzz import process
from utils.config import (
    AUTO_RESTART_ON_OFFLINE,
    OFFLINE_ALERT_WEBHOOK_URL,
    OFFLINE_RESTART_SECONDS,
    OFFLINE_STARTUP_GRACE_SECONDS,
)

logger = logging.getLogger(__name__)


class FuzzyBot(commands.Bot):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.remove_command('help')
        self._cmd_names_cached = None
        self._invokable_cache = None  # (names_list, name_to_cmd_map)
        self._offline_since = None
        self._restart_requested = False
        self._watchdog_task = None
        self._ready_once = False
        self._startup_monotonic = time.monotonic()

    def get_commands(self):
        return list(self.all_commands.values())

    def _get_cmd_names(self):
        """명령어 이름 리스트를 캐시하여 반환합니다 (기본 이름만)."""
        if self._cmd_names_cached is None:
            self._cmd_names_cached = [cmd.name for cmd in self.get_commands()]
        return self._cmd_names_cached

    def _get_all_invokable_names(self):
        """퍼지 매칭/제안용: 기본 이름 + 별칭 리스트와 (이름 -> 명령어) 매핑을 반환합니다. (캐싱)"""
        if self._invokable_cache is not None:
            return self._invokable_cache

        names = []
        name_to_cmd = {}
        for cmd in self.get_commands():
            name_to_cmd[cmd.name] = cmd
            names.append(cmd.name)
            for alias in getattr(cmd, "aliases", None) or []:
                name_to_cmd[alias] = cmd
                names.append(alias)
        
        self._invokable_cache = (names, name_to_cmd)
        return self._invokable_cache

    async def on_command_add(self, command):
        """명령어가 추가될 때 캐시를 무효화합니다."""
        self._cmd_names_cached = None
        self._invokable_cache = None

    async def on_command_remove(self, command):
        """명령어가 제거될 때 캐시를 무효화합니다."""
        self._cmd_names_cached = None
        self._invokable_cache = None

    async def setup_hook(self):
        """백그라운드 작업을 초기화합니다."""
        if self._watchdog_task is None:
            self._watchdog_task = asyncio.create_task(self._connection_watchdog())

    async def get_context(self, message, *, cls=commands.Context):
        ctx = await super().get_context(message, cls=cls)

        if ctx.command is None:
            command_name = ctx.invoked_with or ""
            names, name_to_cmd = self._get_all_invokable_names()

            # 매우 높은 유사도(95점 이상)에서만 자동 실행 (별칭 포함 매칭)
            match = process.extractOne(command_name, names, score_cutoff=95)
            if match:
                matched_name = match[0]
                ctx.command = name_to_cmd.get(matched_name)
            else:
                # 후보 리스트 (UI 피드백용) - 제안만 제공 (한글 별칭 포함)
                similar = process.extract(command_name, names, limit=5, score_cutoff=60)
                if similar:
                    suggestions = ", ".join([m[0] for m in similar])
                    try:
                        await message.channel.send(
                            f"어머나, '{command_name}' 명령어를 찾을 수 없어요. 비슷한 명령어: {suggestions}",
                            delete_after=10
                        )
                    except (discord.Forbidden, discord.HTTPException) as e:
                        # 전송 권한 부족 시 조용히 실패
                        logger.debug(f"명령어 제안 메시지 전송 실패: {e}")

        return ctx

    async def on_ready(self):
        self._ready_once = True
        await self._set_online(reason="on_ready")
        logger.info(f'아리스가 준비 완료했어요! {self.user}로 로그인했답니다~')
        await self._resync_slash_commands(reason="on_ready")

    async def on_disconnect(self):
        await self._set_offline(reason="on_disconnect")

    async def on_resumed(self):
        await self._set_online(reason="on_resumed")
        logger.info("디스코드 연결이 복구되어 세션이 재개되었습니다.")
        await self._resync_slash_commands(reason="on_resumed")

    async def on_command_error(self, ctx, error):
        """전역 명령어 에러 핸들러"""
        if isinstance(error, commands.CommandNotFound):
            # 이미 get_context에서 처리됨
            return
        elif isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(f"선생님, 명령어에 필요한 인자가 빠졌어요: `{error.param.name}`", delete_after=12)
        elif isinstance(error, commands.BadArgument):
            await ctx.send(f"선생님, 입력하신 인자가 올바르지 않아요: {str(error)[:100]}", delete_after=12)
        elif isinstance(error, commands.MissingPermissions):
            await ctx.send("앗, 죄송해요 선생님. 이 명령어는 특별한 권한이 필요해요.", delete_after=12)
        elif isinstance(error, commands.CommandOnCooldown):
            await ctx.send(f"선생님, 조금만 기다려주세요! {error.retry_after:.1f}초 후에 다시 시도해주세요.", delete_after=12)
        else:
            logger.error(f"명령어 에러 ({ctx.command}): {error}", exc_info=error)
            await ctx.send(f"선생님, 명령어 실행 중 오류가 발생했어요: {str(error)[:100]}", delete_after=12)

    async def _resync_slash_commands(self, reason: str):
        """연결 복구 시 Slash Commands를 재동기화합니다."""
        try:
            synced = await self.tree.sync()
            logger.info(f"Slash Commands {len(synced)}개가 동기화되었습니다. (사유: {reason})")
        except Exception as e:
            logger.error(f"Slash Commands 동기화 실패 (사유: {reason}): {e}")

    async def _set_offline(self, reason: str):
        """오프라인 상태를 기록하고 알림을 시도합니다."""
        if self._offline_since is None:
            self._offline_since = time.monotonic()
            logger.warning(f"디스코드 연결이 끊어졌습니다. (사유: {reason})")
            if OFFLINE_ALERT_WEBHOOK_URL:
                asyncio.create_task(self._send_webhook_alert(is_online=False, reason=reason))

    async def _set_online(self, reason: str):
        """온라인 상태로 전환합니다."""
        if self._offline_since is not None:
            offline_duration = time.monotonic() - self._offline_since
            logger.info(f"디스코드 연결이 복구되었습니다. (중단 {offline_duration:.1f}초, 사유: {reason})")
            self._offline_since = None
            if OFFLINE_ALERT_WEBHOOK_URL:
                asyncio.create_task(self._send_webhook_alert(is_online=True, reason=reason))

    async def _send_webhook_alert(self, *, is_online: bool, reason: str):
        """오프라인/복구 상태를 웹훅으로 알립니다."""
        if not OFFLINE_ALERT_WEBHOOK_URL:
            return
        try:
            import aiohttp
        except Exception as e:
            logger.warning(f"웹훅 알림 실패 (aiohttp 없음): {e}")
            return

        status = "복구" if is_online else "오프라인"
        hostname = socket.gethostname()
        message = f"아리스 봇 {status} 감지\n호스트: {hostname}\n사유: {reason}"

        try:
            async with aiohttp.ClientSession() as session:
                webhook = discord.Webhook.from_url(OFFLINE_ALERT_WEBHOOK_URL, session=session)
                await webhook.send(message)
        except Exception as e:
            logger.warning(f"웹훅 알림 전송 실패: {e}")

    async def _connection_watchdog(self):
        """오프라인 지속 시간 감지 및 자동 재시작 워치독."""
        while not self.is_closed():
            await asyncio.sleep(5)

            if self._restart_requested:
                return

            now = time.monotonic()

            if self._offline_since is None:
                if not self._ready_once:
                    if OFFLINE_STARTUP_GRACE_SECONDS > 0 and now - self._startup_monotonic >= OFFLINE_STARTUP_GRACE_SECONDS:
                        await self._handle_offline_timeout("startup_not_ready")
                continue

            if OFFLINE_RESTART_SECONDS > 0:
                offline_duration = now - self._offline_since
                if offline_duration >= OFFLINE_RESTART_SECONDS:
                    await self._handle_offline_timeout("offline_timeout")

    async def _handle_offline_timeout(self, reason: str):
        if self._restart_requested:
            return
        self._restart_requested = True
        logger.error(f"오프라인 지속 시간 초과로 재시작을 시도합니다. (사유: {reason})")
        if AUTO_RESTART_ON_OFFLINE:
            await self._restart_process()
        else:
            logger.error("AUTO_RESTART_ON_OFFLINE이 비활성화되어 있어 재시작하지 않습니다.")

    async def _restart_process(self):
        """봇 프로세스를 재시작합니다."""
        try:
            await self.close()
        except Exception as e:
            logger.warning(f"재시작 전 종료 중 오류 (무시됨): {e}")

        try:
            os.execv(sys.executable, [sys.executable] + sys.argv)
        except Exception as e:
            logger.error(f"프로세스 재시작 실패: {e}")

