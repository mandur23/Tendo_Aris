import logging
import discord
from discord.ext import commands
from rapidfuzz import process

logger = logging.getLogger(__name__)


class FuzzyBot(commands.Bot):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.remove_command('help')
        self._cmd_names_cached = None

    def get_commands(self):
        return list(self.all_commands.values())

    def _get_cmd_names(self):
        """명령어 이름 리스트를 캐시하여 반환합니다."""
        if self._cmd_names_cached is None:
            self._cmd_names_cached = [cmd.name for cmd in self.get_commands()]
        return self._cmd_names_cached

    async def on_command_add(self, command):
        """명령어가 추가될 때 캐시를 무효화합니다."""
        self._cmd_names_cached = None

    async def on_command_remove(self, command):
        """명령어가 제거될 때 캐시를 무효화합니다."""
        self._cmd_names_cached = None

    async def get_context(self, message, *, cls=commands.Context):
        ctx = await super().get_context(message, cls=cls)

        if ctx.command is None:
            command_name = ctx.invoked_with or ""
            names = self._get_cmd_names()
            
            # 매우 높은 유사도(95점 이상)에서만 자동 실행
            match = process.extractOne(command_name, names, score_cutoff=95)
            if match:
                ctx.command = self.all_commands.get(match[0])
            else:
                # 후보 리스트 (UI 피드백용) - 제안만 제공
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
        logger.info(f'아리스가 준비 완료했어요! {self.user}로 로그인했답니다~')
        # Slash Commands 동기화
        try:
            synced = await self.tree.sync()
            logger.info(f"Slash Commands {len(synced)}개가 동기화되었습니다.")
        except Exception as e:
            logger.error(f"Slash Commands 동기화 실패: {e}")

    async def on_command_error(self, ctx, error):
        """전역 명령어 에러 핸들러"""
        async def _safe_error_send(message: str):
            try:
                if self.is_closed():
                    return
                await ctx.send(message, delete_after=12)
            except Exception:
                pass

        if isinstance(error, commands.CommandNotFound):
            # 이미 get_context에서 처리됨
            return
        elif isinstance(error, commands.MissingRequiredArgument):
            await _safe_error_send(f"선생님, 명령어에 필요한 인자가 빠졌어요: `{error.param.name}`")
        elif isinstance(error, commands.BadArgument):
            await _safe_error_send(f"선생님, 입력하신 인자가 올바르지 않아요: {str(error)[:100]}")
        elif isinstance(error, commands.MissingPermissions):
            await _safe_error_send("앗, 죄송해요 선생님. 이 명령어는 특별한 권한이 필요해요.")
        elif isinstance(error, commands.CommandOnCooldown):
            await _safe_error_send(f"선생님, 조금만 기다려주세요! {error.retry_after:.1f}초 후에 다시 시도해주세요.")
        else:
            logger.error(f"명령어 에러 ({ctx.command}): {error}", exc_info=error)
            await _safe_error_send(f"선생님, 명령어 실행 중 오류가 발생했어요: {str(error)[:100]}")

