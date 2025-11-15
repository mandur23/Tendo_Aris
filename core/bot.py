import logging
from discord.ext import commands
from fuzzywuzzy import process

logger = logging.getLogger(__name__)


class FuzzyBot(commands.Bot):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.remove_command('help')

    def get_commands(self):
        return list(self.all_commands.values())

    async def get_context(self, message, *, cls=commands.Context):
        ctx = await super().get_context(message, cls=cls)

        if ctx.command is None:
            command_name = ctx.invoked_with
            commands = self.get_commands()
            matches = process.extractBests(command_name, [cmd.name for cmd in commands], score_cutoff=80, limit=1)
            if matches:
                ctx.command = self.all_commands.get(matches[0][0])
            else:
                similar_commands = process.extractBests(command_name, [cmd.name for cmd in commands], score_cutoff=60)
                if similar_commands:
                    suggestions = ', '.join([match[0] for match in similar_commands])
                    await message.channel.send(f"어머나, '{command_name}' 명령어를 찾을 수 없어요: 비슷한 명령어: {suggestions}")

        return ctx

    async def on_ready(self):
        logger.info(f'아리스가 준비 완료했어요! {self.user}로 로그인했답니다~')

