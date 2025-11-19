"""Discord 봇 공통 유틸리티 함수"""
import asyncio
import logging
from typing import Optional, Union
import discord
from discord.ext import commands
from utils.config import COMMAND_MESSAGE_DELETE_DELAY

logger = logging.getLogger(__name__)


async def delete_command_message(ctx: commands.Context, delay: float = None):
    """
    명령어 메시지를 지연 후 삭제합니다.
    
    Args:
        ctx: Discord 명령어 컨텍스트
        delay: 삭제 전 대기 시간 (초). None이면 설정 파일의 기본값 사용
    """
    if delay is None:
        delay = COMMAND_MESSAGE_DELETE_DELAY
    
    await asyncio.sleep(delay)
    try:
        await ctx.message.delete()
    except (discord.NotFound, discord.HTTPException, discord.Forbidden):
        pass


async def ensure_voice_client(ctx: commands.Context) -> bool:
    """
    음성 채널에 연결되어 있는지 확인하고, 없으면 연결합니다.
    
    Args:
        ctx: Discord 명령어 컨텍스트
        
    Returns:
        bool: 연결 성공 여부
    """
    if not ctx.voice_client:
        if ctx.author.voice:
            try:
                await ctx.author.voice.channel.connect()
                return True
            except Exception as e:
                logger.error(f"음성 채널 연결 실패: {e}")
                try:
                    await ctx.send("선생님, 음성 채널에 연결할 수 없어요! 아리스가 어디로 가야 할지 모르겠어요~", delete_after=10)
                except Exception:
                    pass
                await delete_command_message(ctx)
                return False
        else:
            try:
                await ctx.send("선생님, 음성 채널에 먼저 입장해주세요! 아리스가 어디로 가야 할지 모르겠어요~", delete_after=10)
            except Exception:
                pass
            await delete_command_message(ctx)
            return False
    return True


async def safe_send(
    ctx: Union[commands.Context, discord.Interaction, discord.abc.Messageable],
    content: str = None,
    *,
    embed: discord.Embed = None,
    view: discord.ui.View = None,
    delete_after: float = None,
    ephemeral: bool = False,
    **kwargs
) -> Optional[discord.Message]:
    """
    안전하게 메시지를 전송합니다. 에러 발생 시 로깅만 하고 예외를 발생시키지 않습니다.
    
    Args:
        ctx: Discord 컨텍스트, Interaction, 또는 Messageable 객체
        content: 메시지 내용
        embed: Embed 객체
        view: View 객체
        delete_after: 자동 삭제 시간 (초)
        ephemeral: Interaction에만 사용, ephemeral 메시지 여부
        **kwargs: send() 메서드에 전달할 추가 인자
        
    Returns:
        discord.Message: 전송된 메시지 (실패 시 None)
    """
    try:
        if isinstance(ctx, discord.Interaction):
            if ctx.response.is_done():
                message = await ctx.followup.send(
                    content=content,
                    embed=embed,
                    view=view,
                    ephemeral=ephemeral,
                    **kwargs
                )
            else:
                await ctx.response.send_message(
                    content=content,
                    embed=embed,
                    view=view,
                    ephemeral=ephemeral,
                    **kwargs
                )
                return None  # response.send_message는 Message를 반환하지 않음
        else:
            message = await ctx.send(
                content=content,
                embed=embed,
                view=view,
                delete_after=delete_after,
                **kwargs
            )
        return message
    except discord.NotFound:
        logger.debug("메시지 전송 실패: 채널이나 메시지를 찾을 수 없음")
        return None
    except discord.Forbidden:
        logger.warning("메시지 전송 실패: 권한 없음")
        return None
    except discord.HTTPException as e:
        logger.exception(f"메시지 전송 중 HTTP 에러: {e}")
        return None
    except Exception as e:
        logger.exception(f"메시지 전송 중 예상치 못한 오류: {e}")
        return None


async def safe_edit_message(
    message: discord.Message,
    content: str = None,
    *,
    embed: discord.Embed = None,
    view: discord.ui.View = None,
    **kwargs
) -> bool:
    """
    안전하게 메시지를 편집합니다.
    
    Args:
        message: 편집할 메시지
        content: 새 메시지 내용
        embed: 새 Embed 객체
        view: 새 View 객체
        **kwargs: edit() 메서드에 전달할 추가 인자
        
    Returns:
        bool: 편집 성공 여부
    """
    try:
        await message.edit(content=content, embed=embed, view=view, **kwargs)
        return True
    except discord.NotFound:
        logger.debug("메시지 편집 실패: 메시지를 찾을 수 없음")
        return False
    except discord.Forbidden:
        logger.warning("메시지 편집 실패: 권한 없음")
        return False
    except discord.HTTPException as e:
        logger.exception(f"메시지 편집 중 HTTP 에러: {e}")
        return False
    except Exception as e:
        logger.exception(f"메시지 편집 중 예상치 못한 오류: {e}")
        return False


async def safe_delete_message(message: discord.Message) -> bool:
    """
    안전하게 메시지를 삭제합니다.
    
    Args:
        message: 삭제할 메시지
        
    Returns:
        bool: 삭제 성공 여부
    """
    try:
        await message.delete()
        return True
    except discord.NotFound:
        logger.debug("메시지 삭제 실패: 메시지를 찾을 수 없음")
        return False
    except discord.Forbidden:
        logger.warning("메시지 삭제 실패: 권한 없음")
        return False
    except discord.HTTPException as e:
        logger.exception(f"메시지 삭제 중 HTTP 에러: {e}")
        return False
    except Exception as e:
        logger.exception(f"메시지 삭제 중 예상치 못한 오류: {e}")
        return False


async def retry_on_403_error(func, *args, max_retries: int = 3, base_delay: float = 2.0, **kwargs):
    """
    403 에러 발생 시 재시도하는 헬퍼 함수.
    
    Args:
        func: 실행할 함수 (비동기 또는 동기)
        *args: 함수에 전달할 위치 인자
        max_retries: 최대 재시도 횟수
        base_delay: 기본 지연 시간 (초)
        **kwargs: 함수에 전달할 키워드 인자
        
    Returns:
        함수 실행 결과
        
    Raises:
        Exception: 모든 재시도 실패 시 마지막 예외
    """
    import random
    last_exception = None
    
    for attempt in range(max_retries):
        try:
            if asyncio.iscoroutinefunction(func):
                result = await func(*args, **kwargs)
            else:
                result = func(*args, **kwargs)
                # run_in_executor처럼 Future 객체를 반환하는 경우 await
                if isinstance(result, asyncio.Future):
                    result = await result
            return result
        except Exception as e:
            last_exception = e
            error_msg = str(e).lower()
            
            if '403' in error_msg or 'forbidden' in error_msg:
                if attempt < max_retries - 1:
                    delay = base_delay * (2 ** attempt) + random.uniform(0, 1)
                    logger.warning(f"403 오류 감지 - 재시도 {attempt + 1}/{max_retries}, {delay:.1f}초 대기")
                    await asyncio.sleep(delay)
                    continue
                else:
                    logger.error(f"403 오류로 인한 최종 실패: {e}")
                    raise
            elif 'private' in error_msg or 'unavailable' in error_msg:
                logger.warning(f"비공개 또는 삭제된 콘텐츠: {e}")
                raise
            else:
                # 403이 아닌 다른 오류는 즉시 재발생
                raise
    
    # 모든 재시도 실패
    if last_exception:
        raise last_exception


def handle_extract_error(error: Exception, max_retries: int = 3, attempt: int = 0) -> Optional[str]:
    """
    YouTube 정보 추출 에러를 처리하고 적절한 에러 메시지를 반환합니다.
    
    Args:
        error: 발생한 예외
        max_retries: 최대 재시도 횟수
        attempt: 현재 시도 횟수
        
    Returns:
        str: 에러 메시지 (재시도 가능하면 None)
    """
    error_msg = str(error).lower()
    
    if '403' in error_msg or 'forbidden' in error_msg:
        if attempt < max_retries - 1:
            logger.warning(f"제목 추출 403 오류 - 재시도 {attempt + 1}/{max_retries}")
            return None  # 재시도 가능
        else:
            logger.error(f"정보 추출 최종 실패: {error}")
            return "🔍 제목을 가져올 수 없는 영상"
    elif 'private' in error_msg or 'unavailable' in error_msg:
        return "❌ 비공개 또는 삭제된 영상"
    else:
        if attempt >= max_retries - 1:
            logger.error(f"정보 추출 완전 실패: {error}")
            return "❓ 알 수 없는 영상"
        return None  # 재시도 가능


class AuthorLockedView(discord.ui.View):
    """
    생성자에게만 조작을 허용하는 View 베이스 클래스.
    타임아웃 시 자동으로 버튼을 비활성화합니다.
    """
    def __init__(self, author_id: int, timeout: float = 30.0):
        super().__init__(timeout=timeout)
        self.author_id = author_id
    
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """인터랙션을 생성한 사용자만 허용합니다."""
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "선생님, 다른 사람의 명령을 조작할 수 없어요!",
                ephemeral=True,
                delete_after=5
            )
            return False
        return True
    
    async def on_timeout(self):
        """타임아웃 시 모든 버튼을 비활성화합니다."""
        for item in self.children:
            item.disabled = True
        try:
            if hasattr(self, 'message') and self.message:
                await safe_edit_message(self.message, view=self)
        except Exception as e:
            logger.debug(f"View 타임아웃 처리 중 오류: {e}")


class ConfirmView(AuthorLockedView):
    """
    2단계 확인을 위한 View 클래스.
    확인/취소 버튼을 제공하며, 타임아웃 시 자동 비활성화됩니다.
    """
    def __init__(
        self,
        author_id: int,
        confirm_label: str = "확인",
        cancel_label: str = "취소",
        confirm_style: discord.ButtonStyle = discord.ButtonStyle.danger,
        cancel_style: discord.ButtonStyle = discord.ButtonStyle.secondary,
        timeout: float = 30.0,
        on_confirm: callable = None,
        on_cancel: callable = None
    ):
        super().__init__(author_id, timeout=timeout)
        self.on_confirm_callback = on_confirm
        self.on_cancel_callback = on_cancel
        
        self.confirm_button = discord.ui.Button(
            label=confirm_label,
            style=confirm_style
        )
        self.confirm_button.callback = self._confirm_callback
        self.add_item(self.confirm_button)
        
        self.cancel_button = discord.ui.Button(
            label=cancel_label,
            style=cancel_style
        )
        self.cancel_button.callback = self._cancel_callback
        self.add_item(self.cancel_button)
    
    async def _confirm_callback(self, interaction: discord.Interaction):
        """확인 버튼 콜백"""
        if not await self.interaction_check(interaction):
            return
        
        # 모든 버튼 비활성화
        for item in self.children:
            item.disabled = True
        
        try:
            await interaction.response.edit_message(view=self)
        except Exception as e:
            logger.debug(f"확인 버튼 처리 중 오류: {e}")
        
        if self.on_confirm_callback:
            try:
                await self.on_confirm_callback(interaction)
            except Exception as e:
                logger.error(f"확인 콜백 실행 중 오류: {e}")
    
    async def _cancel_callback(self, interaction: discord.Interaction):
        """취소 버튼 콜백"""
        if not await self.interaction_check(interaction):
            return
        
        # 모든 버튼 비활성화
        for item in self.children:
            item.disabled = True
        
        try:
            await interaction.response.edit_message(view=self)
        except Exception as e:
            logger.debug(f"취소 버튼 처리 중 오류: {e}")
        
        if self.on_cancel_callback:
            try:
                await self.on_cancel_callback(interaction)
            except Exception as e:
                logger.error(f"취소 콜백 실행 중 오류: {e}")
