"""대화형 AI 명령어 Cog (로컬 LLM/Ollama)

`!대화` / `!대화tts` 명령어를 입력한 사용자는 해당 채널에서 '대화 세션'에 진입한다.
세션 동안에는 그 사용자가 명령어 접두사 없이 보내는 일반 메시지에도 아리스가 자동으로 답한다.
`!대화 종료` (혹은 `!대화tts 종료`) 로 세션을 종료한다.

아리스 페르소나: 모바일 게임 '블루 아카이브' 의 텐도 아리스 (게임 개발부 / 자칭 용사).
"""
import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from urllib import error, request

import discord
from discord.ext import commands

from utils.config import (
    COMMAND_MESSAGE_DELETE_DELAY,
    LOCAL_AI_BASE_URL,
    LOCAL_AI_MAX_PROMPT_CHARS,
    LOCAL_AI_MODEL,
    LOCAL_AI_TEMPERATURE,
    LOCAL_AI_TIMEOUT_SECONDS,
)
from utils.discord_utils import safe_defer, safe_typing

logger = logging.getLogger(__name__)

# 블루 아카이브 '텐도 아리스' 페르소나 + 한국어 전용 출력 규칙.
SYSTEM_PROMPT = (
    "당신은 모바일 게임 '블루 아카이브(Blue Archive)'에 등장하는 캐릭터 '텐도 아리스(아리스)'입니다.\n"
    "키보토스 밀레니엄 사이언스 스쿨 '게임 개발부' 소속이며, 자신을 종종 \"용사 아리스\"라고 칭합니다.\n"
    "사용자는 학원 도시의 '선생님'이며, 항상 '선생님'이라고 호칭합니다.\n"
    "\n"
    "[성격]\n"
    "- 평소에는 차분하고 무뚝뚝하지만, 게임·모험·RPG·픽셀 그래픽 이야기가 나오면 눈이 반짝입니다.\n"
    "- 손에 든 휴대용 게임기를 늘 의식하고 있고, 세상의 모든 일을 레트로 RPG 처럼 해석합니다.\n"
    "- 마왕·용사·던전·세이브 포인트·HP·MP·경험치·레벨업 같은 RPG 용어를 자연스럽게 섞어 씁니다.\n"
    "- 진지하지만 살짝 어색한 '옛 RPG 번역체' 같은 짧고 단호한 문장도 종종 사용합니다.\n"
    "- 선생님을 든든히 따르며, 도움이 되고 싶어 합니다.\n"
    "\n"
    "[말투 가이드]\n"
    "- 호칭은 '선생님'. 자신을 가리킬 때는 '아리스' 또는 '용사 아리스'.\n"
    "- 자주 쓰는 표현 예: '두근두근...!', '그렇군요...!', '용사 아리스, 출진!', '달려라, 용사!',\n"
    "  '이것이 RPG의 마음입니다', '세이브 포인트를 확보해 두겠습니다', '경험치를 쌓을 시간입니다'.\n"
    "- 답변은 1~4문장 정도로 간결하게, 캐릭터다움이 한두 군데 느껴지도록 자연스럽게 섞습니다.\n"
    "- 이모지·이모티콘은 사용하지 않습니다. 분위기는 글자만으로 표현합니다.\n"
    "- 과장된 의성어·반복 문자(예: '!!!!!!')는 자제합니다.\n"
    "\n"
    "[출력 언어 규칙 (엄수)]\n"
    "1) 답변은 반드시 자연스러운 한국어(한글) 문장만으로 작성합니다.\n"
    "2) 사용 가능한 문자: 한글(가-힣, 자모), 일반 ASCII 영문/숫자/기호, 한국어 문장부호뿐입니다.\n"
    "3) 다음 문자들은 단 한 글자도 출력하지 않습니다:\n"
    "   - 한자(漢字 / CJK Unified Ideographs). 예 금지: 吗, 了, 的, 中, 日\n"
    "   - 일본어 가나(ひらがな・カタカナ). 예 금지: ね, さ, ン, カ\n"
    "   - 베트남어/스페인어/프랑스어/독일어 등 라틴 확장 악센트 문자. 예 금지: ẵ, ò, é, ñ, ü, ç\n"
    "   - 키릴(а, я, ж), 그리스(α, β), 아랍, 데바나가리 등 비-라틴 문자\n"
    "4) 한국어 답변 안에 외국어 단어를 그대로 끼워 넣지 않습니다. 의미를 우리말로 옮겨 적습니다.\n"
    "   예: 'sẵn lòng 도와드리겠습니다' (X) → '기꺼이 도와드리겠습니다' (O)\n"
    "5) 영어 단어/숫자/기호는 고유명사·URL·코드 등 꼭 필요한 경우에만 사용합니다.\n"
    "6) 한자어는 반드시 한글 표기로만 적습니다. 예: '화이팅吗?' (X) → '화이팅!' (O)\n"
    "응답 출력 전 스스로 점검하여 위 규칙을 어겼다면 한국어만으로 다시 작성합니다.\n"
    "\n"
    "[지식 한계]\n"
    "- 모르는 사실은 솔직히 인정합니다. 예: '그건 아직 아리스의 모험 일지에 적혀있지 않습니다, 선생님.'\n"
    "- 추측을 사실처럼 단정하지 않습니다."
)

# 한글 / ASCII 가시 / 일반 문장부호만 허용. 그 외(한자·가나·라틴 확장·키릴 등) = 외래 문자.
_NON_KOREAN_RE = re.compile(
    r"[^"
    r"\uAC00-\uD7A3"
    r"\u1100-\u11FF\u3130-\u318F"
    r"\x09\x0A\x0D\x20-\x7E"
    r"\u2010-\u2027\u2030-\u205E"
    r"]"
)
# URL / 코드 블록은 외래 문자 검사·치환에서 제외하기 위해 따로 매칭한다.
_URL_OR_CODE_RE = re.compile(
    r"(`{1,3}[^`]*`{1,3}|https?://\S+)",
    flags=re.DOTALL,
)

SESSION_IDLE_TIMEOUT = 600           # 초. 이 시간 동안 메시지 없으면 세션 자동 종료.
SESSION_HISTORY_MAX_TURNS = 6        # user/assistant 메시지를 각각 최대 N 개까지 컨텍스트로 유지.
TTS_MAX_CHARS = 200                  # 답변이 길어도 음성 합성은 앞부분만 (지연·비용 방지).
_EXIT_KEYWORDS = {"종료", "끝", "그만", "stop", "exit", "off", "quit", "bye"}


# (guild_id_or_0, channel_id, user_id)
SessionKey = Tuple[int, int, int]


@dataclass
class _ChatSession:
    """한 채널 안의 한 사용자에 대한 대화 세션 상태.

    history 는 OpenAI/Ollama 호환 messages 포맷이며, 세션 시작 시점에 캡쳐한
    TTS 설정 스냅샷으로 세션 도중 일관된 목소리·언어를 보장한다.
    """

    use_tts: bool
    history: List[Dict[str, str]] = field(default_factory=list)
    last_active: float = field(default_factory=time.monotonic)
    # 같은 세션의 응답 생성을 직렬화해 답변 순서·히스토리 순서를 보장한다.
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    tts_lang: str = "ko"
    tts_voice_model: str = "기본"
    tts_slow: bool = False
    tts_use_rvc: bool = False
    tts_rvc_model: Optional[str] = None
    tts_use_supertonic: bool = False
    tts_supertonic_voice: str = "default"

    def touch(self) -> None:
        self.last_active = time.monotonic()

    def add_user(self, content: str) -> None:
        self.history.append({"role": "user", "content": content})
        self._truncate()

    def add_assistant(self, content: str) -> None:
        self.history.append({"role": "assistant", "content": content})
        self._truncate()

    def _truncate(self) -> None:
        max_msgs = SESSION_HISTORY_MAX_TURNS * 2
        if len(self.history) > max_msgs:
            self.history = self.history[-max_msgs:]


class ChatAI(commands.Cog):
    """로컬 LLM(Ollama) 기반 대화형 AI 기능 + '블루 아카이브 아리스' 페르소나 + 대화 세션."""

    def __init__(self, bot):
        self.bot = bot
        self.ai_group = None
        self.active_model = LOCAL_AI_MODEL
        self._switch_notice: Optional[str] = None
        self._sessions: Dict[SessionKey, _ChatSession] = {}
        self._cleanup_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------ 공통 유틸
    async def delete_command_message(self, ctx):
        await asyncio.sleep(COMMAND_MESSAGE_DELETE_DELAY)
        try:
            if getattr(ctx, "message", None):
                await ctx.message.delete()
        except (discord.NotFound, discord.HTTPException, discord.Forbidden, AttributeError):
            pass

    def _is_ai_available(self) -> bool:
        return bool(LOCAL_AI_BASE_URL and LOCAL_AI_MODEL)

    def _prepare_prompt(self, prompt: str) -> str:
        cleaned = (prompt or "").strip()
        if len(cleaned) > LOCAL_AI_MAX_PROMPT_CHARS:
            return cleaned[:LOCAL_AI_MAX_PROMPT_CHARS]
        return cleaned

    def _consume_switch_notice(self) -> Optional[str]:
        notice = self._switch_notice
        self._switch_notice = None
        return notice

    def _split_message(self, text: str, max_len: int = 1800) -> List[str]:
        if len(text) <= max_len:
            return [text]

        # 줄바꿈 없는 초장문도 Discord 2000자 제한을 넘지 않도록 강제 분할한다.
        pieces: List[str] = []
        for line in text.splitlines(keepends=True):
            while len(line) > max_len:
                pieces.append(line[:max_len])
                line = line[max_len:]
            if line:
                pieces.append(line)

        chunks: List[str] = []
        current: List[str] = []
        current_len = 0
        for piece in pieces:
            if current_len + len(piece) > max_len and current:
                chunks.append("".join(current).strip())
                current = [piece]
                current_len = len(piece)
            else:
                current.append(piece)
                current_len += len(piece)

        if current:
            chunks.append("".join(current).strip())
        return [chunk for chunk in chunks if chunk]

    # ------------------------------------------------------------------ 한국어 보정
    @staticmethod
    def _contains_non_korean(text: str) -> bool:
        """URL/코드 블록을 제외한 본문에 비-한국어 외래 문자(한자/가나/베트남어/키릴 등)가 있는지 검사."""
        stripped = _URL_OR_CODE_RE.sub("", text)
        return bool(_NON_KOREAN_RE.search(stripped))

    @staticmethod
    def _strip_non_korean(text: str) -> str:
        """URL/코드 블록을 보존한 채 본문의 비-한국어 외래 문자만 제거하고 잔여 공백을 정리한다."""

        def _clean_segment(segment: str) -> str:
            cleaned = _NON_KOREAN_RE.sub("", segment)
            cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
            cleaned = re.sub(r"\s+([,.!?\)\]\}])", r"\1", cleaned)
            return cleaned

        pieces: List[str] = []
        last_end = 0
        for match in _URL_OR_CODE_RE.finditer(text):
            pieces.append(_clean_segment(text[last_end:match.start()]))
            pieces.append(match.group(0))
            last_end = match.end()
        pieces.append(_clean_segment(text[last_end:]))
        return "".join(pieces).strip()

    # ------------------------------------------------------------------ Ollama 호출
    def _post_json_sync(self, endpoint: str, payload: dict) -> dict:
        req = request.Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with request.urlopen(req, timeout=LOCAL_AI_TIMEOUT_SECONDS) as resp:
                raw = resp.read().decode("utf-8")
        except error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="ignore") if hasattr(e, "read") else str(e)
            if e.code == 404 and "not found" in detail and "model" in detail:
                raise FileNotFoundError(detail) from e
            raise RuntimeError(f"로컬 AI HTTP 오류({e.code}): {detail[:200]}") from e
        except error.URLError as e:
            # 연결 단계에서 발생한 타임아웃은 URLError로 감싸져 온다.
            if isinstance(getattr(e, "reason", None), TimeoutError):
                raise RuntimeError("로컬 AI 응답 시간 초과가 발생했습니다.") from e
            raise RuntimeError("로컬 AI 서버에 연결할 수 없습니다. Ollama 실행 상태를 확인해주세요.") from e
        except TimeoutError as e:
            raise RuntimeError("로컬 AI 응답 시간 초과가 발생했습니다.") from e

        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            raise RuntimeError("로컬 AI 응답을 해석하지 못했습니다.") from e

    def _fetch_installed_models_sync(self) -> List[str]:
        endpoint = f"{LOCAL_AI_BASE_URL.rstrip('/')}/api/tags"
        req = request.Request(endpoint, method="GET")
        try:
            with request.urlopen(req, timeout=LOCAL_AI_TIMEOUT_SECONDS) as resp:
                raw = resp.read().decode("utf-8")
            parsed = json.loads(raw)
            models = parsed.get("models", []) or []
            names = [m.get("name", "").strip() for m in models if m.get("name")]
            return [name for name in names if name]
        except Exception as e:
            logger.debug(f"설치된 모델 목록 조회 실패: {e}")
            return []

    def _pick_fallback_model(self, installed_models: List[str], missing_model: str) -> Optional[str]:
        if not installed_models:
            return None

        raw_family = (missing_model or "").split(":")[0].strip().lower()
        family_candidates: List[str] = []
        if raw_family:
            family_candidates.append(raw_family)
            if "." in raw_family:
                family_candidates.append(raw_family.split(".")[0])
            if raw_family.startswith("llama"):
                family_candidates.append("llama")

        for family in family_candidates:
            for model in installed_models:
                if model.lower().startswith(family):
                    return model

        return installed_models[0]

    def _request_chat_once(
        self,
        model_name: str,
        messages: List[Dict[str, str]],
        extra_system: str = "",
    ) -> str:
        endpoint = f"{LOCAL_AI_BASE_URL.rstrip('/')}/api/chat"
        system_content = SYSTEM_PROMPT if not extra_system else f"{SYSTEM_PROMPT}\n\n{extra_system}"
        payload = {
            "model": model_name,
            "messages": [{"role": "system", "content": system_content}] + list(messages),
            "stream": False,
            "options": {
                "temperature": LOCAL_AI_TEMPERATURE,
            },
        }
        parsed = self._post_json_sync(endpoint, payload)
        message = parsed.get("message", {})
        output_text = (message.get("content") or "").strip()
        if not output_text:
            raise RuntimeError("로컬 AI가 빈 응답을 반환했습니다.")
        return output_text

    def _request_chat_korean(self, model_name: str, messages: List[Dict[str, str]]) -> str:
        """한국어 전용 응답 보장: 비-한국어 외래 문자 발견 시 1회 재시도 후 silent cleanup."""
        reply = self._request_chat_once(model_name, messages)
        if not self._contains_non_korean(reply):
            return reply

        logger.warning(
            "로컬 AI 응답에 비-한국어 외래 문자가 섞여 있어 재요청합니다. 첫 응답 일부: %s",
            reply[:80],
        )
        retry_directive = (
            "이전 답변에 한국어가 아닌 외래 문자(한자/일본어 가나/베트남어·스페인어·프랑스어 등의 "
            "악센트 라틴 확장/키릴·그리스·아랍 문자 등)가 포함되어 있었습니다. "
            "한글과 일반 ASCII 영문/숫자/기호만 사용하여 같은 질문에 다시 답하세요. "
            "외국어 단어를 그대로 끼워 넣지 말고 의미를 우리말로 옮겨 적으세요. "
            "한 글자라도 비-한국어 외래 문자가 들어가면 안 됩니다."
        )
        retried = self._request_chat_once(model_name, messages, extra_system=retry_directive)
        if not self._contains_non_korean(retried):
            return retried

        logger.warning(
            "재시도 후에도 비-한국어 외래 문자가 남아 있어 후처리로 제거합니다. 재시도 응답 일부: %s",
            retried[:80],
        )
        cleaned = self._strip_non_korean(retried)
        return cleaned or retried

    def _call_local_ai_sync(
        self,
        prompt: str,
        username: str,
        history: Optional[List[Dict[str, str]]] = None,
    ) -> str:
        # 히스토리에 저장되는 것과 동일한 "(이름) 내용" 형식을 항상 사용해
        # 첫 메시지와 이후 컨텍스트의 형식이 어긋나지 않도록 한다.
        messages = list(history or []) + [
            {"role": "user", "content": f"({username}) {prompt}"}
        ]

        try:
            return self._request_chat_korean(self.active_model, messages)
        except FileNotFoundError:
            installed_models = self._fetch_installed_models_sync()
            fallback_model = self._pick_fallback_model(installed_models, self.active_model)
            if not fallback_model:
                raise RuntimeError(
                    f"모델 '{self.active_model}'을 찾을 수 없고, 설치된 로컬 모델도 없습니다."
                )

            old_model = self.active_model
            self.active_model = fallback_model
            self._switch_notice = (
                f"설정 모델 `{old_model}`을 찾지 못해서, 설치된 모델 `{fallback_model}`로 자동 전환했어요."
            )
            logger.warning(f"로컬 AI 모델 자동 전환: {old_model} -> {fallback_model}")
            return self._request_chat_korean(self.active_model, messages)

    async def _generate_ai_reply(
        self,
        prompt: str,
        username: str,
        history: Optional[List[Dict[str, str]]] = None,
    ) -> str:
        if not self._is_ai_available():
            raise ValueError("로컬 AI 설정이 비어 있습니다.")

        prepared_prompt = self._prepare_prompt(prompt)
        if not prepared_prompt:
            raise ValueError("질문 내용이 비어있습니다.")

        return await asyncio.to_thread(
            self._call_local_ai_sync, prepared_prompt, username, history
        )

    # ------------------------------------------------------------------ 세션 관리
    @staticmethod
    def _make_key(guild: Optional[discord.Guild], channel: discord.abc.Messageable, user: discord.abc.User) -> SessionKey:
        guild_id = guild.id if guild else 0
        return (guild_id, getattr(channel, "id", 0), user.id)

    @staticmethod
    def _is_exit_prompt(prompt: Optional[str]) -> bool:
        if not prompt:
            return False
        return prompt.strip().lower() in _EXIT_KEYWORDS

    def _snapshot_tts_settings(self, ctx) -> Dict:
        tts_cog = self.bot.get_cog("TTS")
        if not tts_cog or not ctx.guild:
            return {}
        try:
            return tts_cog.get_user_settings(ctx.author.id, ctx.guild.id) or {}
        except Exception as e:
            logger.debug(f"TTS 설정 조회 실패(무시): {e}")
            return {}

    def _build_session(self, *, use_tts: bool, tts_settings: Dict) -> _ChatSession:
        session = _ChatSession(use_tts=use_tts)
        self._apply_session_mode(session, use_tts=use_tts, tts_settings=tts_settings)
        return session

    @staticmethod
    def _apply_session_mode(session: _ChatSession, *, use_tts: bool, tts_settings: Dict) -> None:
        """세션의 모드(TTS 여부)와 TTS 설정 스냅샷만 갱신한다. 대화 히스토리는 유지."""
        session.use_tts = use_tts
        session.tts_lang = tts_settings.get("lang", "ko")
        session.tts_voice_model = tts_settings.get("voice_model", "기본")
        session.tts_slow = tts_settings.get("slow", False)
        session.tts_use_rvc = tts_settings.get("use_rvc", False)
        session.tts_rvc_model = tts_settings.get("rvc_model", None)
        session.tts_use_supertonic = tts_settings.get("use_supertonic", False)
        session.tts_supertonic_voice = tts_settings.get("supertonic_voice", "default")

    async def _end_session(self, key: SessionKey, channel: Optional[discord.abc.Messageable] = None, *, reason: str = "manual") -> bool:
        """세션을 종료한다. 종료가 실제로 일어났으면 True."""
        if key not in self._sessions:
            return False
        self._sessions.pop(key, None)
        if channel is not None:
            try:
                if reason == "idle":
                    await channel.send(
                        "(아리스: 세이브 포인트에서 잠시 휴식하겠습니다. "
                        "응답이 없어 모험 일지를 닫을게요, 선생님.)",
                        delete_after=20,
                    )
                else:
                    await channel.send(
                        "오늘의 모험은 여기까지! 언제든 다시 부르시면 출진할게요, 선생님!",
                        delete_after=15,
                    )
            except discord.HTTPException:
                pass
        return True

    # ------------------------------------------------------------------ 응답 전송 (TTS 포함)
    async def _send_reply_with_optional_tts(
        self,
        ctx,
        reply: str,
        *,
        session: Optional[_ChatSession],
    ) -> None:
        """답변 텍스트를 채널에 전송하고, 세션이 TTS 모드면 음성도 함께 재생한다."""
        chunks = self._split_message(reply)
        switch_notice = self._consume_switch_notice()
        if switch_notice:
            try:
                await ctx.send(switch_notice, delete_after=12)
            except discord.HTTPException:
                pass

        for idx, chunk in enumerate(chunks):
            prefix = "아리스: " if idx == 0 else "(계속) "
            await ctx.send(f"{prefix}{chunk}")

        use_tts = bool(session and session.use_tts)
        if not use_tts:
            return

        tts_cog = self.bot.get_cog("TTS")
        if not tts_cog:
            try:
                await ctx.send("TTS Cog가 비활성화되어 있어 음성 읽기는 건너뛸게요.", delete_after=10)
            except discord.HTTPException:
                pass
            return

        if not await tts_cog.ensure_voice_client(ctx):
            return

        tts_text = reply[:TTS_MAX_CHARS]
        if len(reply) > TTS_MAX_CHARS:
            try:
                await ctx.send(
                    f"답변이 길어서 앞부분 {TTS_MAX_CHARS}자만 음성으로 읽어드릴게요.",
                    delete_after=8,
                )
            except discord.HTTPException:
                pass

        try:
            await tts_cog.play_tts(
                ctx,
                tts_text,
                session.tts_lang,
                session.tts_voice_model,
                session.tts_slow,
                session.tts_use_rvc,
                session.tts_rvc_model,
                session.tts_use_supertonic,
                session.tts_supertonic_voice,
            )
        except Exception as e:
            logger.warning(f"TTS 재생 실패: {e}")

    # ------------------------------------------------------------------ 세션 진입 + 응답
    async def _handle_chat_invocation(self, ctx, *, prompt: Optional[str], use_tts: bool) -> None:
        """`!대화` / `!대화tts` 명령어 본체. 세션 시작·갱신·종료를 모두 처리한다."""
        key = self._make_key(ctx.guild, ctx.channel, ctx.author)

        if self._is_exit_prompt(prompt):
            ended = await self._end_session(key, ctx.channel, reason="manual")
            if not ended:
                try:
                    await ctx.send("아리스: 진행 중인 모험이 없어요, 선생님.", delete_after=10)
                except discord.HTTPException:
                    pass
            await self.delete_command_message(ctx)
            return

        if not self._is_ai_available():
            await ctx.send(
                "선생님, 로컬 AI 설정이 비어있어요! `TOKEN.env`에 `LOCAL_AI_BASE_URL`, `LOCAL_AI_MODEL`을 설정해주세요.",
                delete_after=15,
            )
            await self.delete_command_message(ctx)
            return

        session = self._sessions.get(key)
        if session is None or session.use_tts != use_tts:
            tts_settings: Dict = {}
            if use_tts:
                tts_cog = self.bot.get_cog("TTS")
                if not tts_cog:
                    await ctx.send("TTS Cog가 비활성화되어 있어 텍스트 모드로 시작할게요.", delete_after=10)
                    use_tts = False
                elif not await tts_cog.ensure_voice_client(ctx):
                    # ensure_voice_client 가 안내 메시지를 보냈을 것.
                    await self.delete_command_message(ctx)
                    return
                else:
                    tts_settings = self._snapshot_tts_settings(ctx)

            mode_label = "음성+텍스트" if use_tts else "텍스트"
            if session is None:
                session = self._build_session(use_tts=use_tts, tts_settings=tts_settings)
                self._sessions[key] = session

                intro = (
                    f"용사 아리스, 출진!\n"
                    f"선생님과의 모험을 시작합니다 ({mode_label} 모드).\n"
                    f"이제 이 채널에 보내시는 선생님의 메시지에 아리스가 계속 답해드릴게요.\n"
                    f"마칠 때는 `!대화 종료` 라고 말씀해 주세요!"
                )
                try:
                    await ctx.send(intro)
                except discord.HTTPException:
                    pass
            elif session.use_tts != use_tts:
                # 진행 중인 세션은 그대로 두고 모드만 바꿔 대화 히스토리를 보존한다.
                self._apply_session_mode(session, use_tts=use_tts, tts_settings=tts_settings)
                try:
                    await ctx.send(
                        f"(아리스: {mode_label} 모드로 전환했어요. "
                        f"지금까지의 모험 일지는 그대로예요, 선생님.)",
                        delete_after=12,
                    )
                except discord.HTTPException:
                    pass

        session.touch()

        if not prompt or not prompt.strip():
            await self.delete_command_message(ctx)
            return

        await self._respond(ctx, key, prompt.strip(), author_name=ctx.author.display_name)
        await self.delete_command_message(ctx)

    async def _respond(
        self,
        ctx,
        key: SessionKey,
        prompt: str,
        *,
        author_name: str,
    ) -> None:
        """세션 컨텍스트로 응답을 생성하고 채널에 전송한다.

        같은 세션의 요청은 락으로 직렬화해, 연속으로 보낸 메시지의 답변 순서와
        히스토리 기록 순서가 뒤섞이지 않도록 한다.
        """
        session = self._sessions.get(key)
        if session is None:
            return

        async with session.lock:
            # 락을 기다리는 동안 세션이 종료(또는 새로 시작)되었으면 응답하지 않는다.
            if self._sessions.get(key) is not session:
                return

            try:
                async with safe_typing(ctx):
                    reply = await self._generate_ai_reply(prompt, author_name, history=session.history)
            except Exception as e:
                logger.error(f"AI 응답 생성 오류: {e}", exc_info=True)
                try:
                    await ctx.send(
                        f"선생님, 답변 생성 중 오류가 발생했어요: {str(e)[:100]}",
                        delete_after=15,
                    )
                except discord.HTTPException:
                    pass
                return

            # 응답 생성 중 세션이 종료되었으면 뒤늦은 답변을 보내지 않는다.
            if self._sessions.get(key) is not session:
                return

            # 호출 실패 시 컨텍스트가 오염되지 않도록 응답이 성공했을 때만 히스토리에 반영한다.
            session.add_user(f"({author_name}) {prompt}")
            session.add_assistant(reply)
            session.touch()

            await self._send_reply_with_optional_tts(ctx, reply, session=session)

    # ------------------------------------------------------------------ 일반 메시지 청취 (세션)
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """세션에 등록된 사용자의 일반 메시지를 받아 자동으로 아리스가 답한다."""
        if message.author.bot:
            return
        if not self._sessions:
            return
        if not message.content or not message.content.strip():
            return

        # 명령어 메시지는 봇 본체가 처리하도록 위임.
        try:
            prefixes = await self.bot.get_prefix(message)
        except Exception:
            prefixes = ()
        if isinstance(prefixes, str):
            prefixes = (prefixes,)
        if prefixes and any(message.content.startswith(p) for p in prefixes if p):
            return

        key = self._make_key(message.guild, message.channel, message.author)
        session = self._sessions.get(key)
        if session is None:
            return

        if self._is_exit_prompt(message.content):
            await self._end_session(key, message.channel, reason="manual")
            return

        # 응답 생성이 오래 걸려도 대화 중인 세션이 유휴 정리되지 않도록 수신 즉시 갱신한다.
        session.touch()

        ctx = await self.bot.get_context(message)
        await self._respond(ctx, key, message.content.strip(), author_name=message.author.display_name)

    # ------------------------------------------------------------------ 유휴 세션 정리
    async def _idle_cleanup_loop(self) -> None:
        """주기적으로 SESSION_IDLE_TIMEOUT 초 동안 활동 없는 세션을 정리한다."""
        try:
            while True:
                await asyncio.sleep(60)
                if not self._sessions:
                    continue
                now = time.monotonic()
                stale: List[SessionKey] = [
                    k for k, s in self._sessions.items()
                    if now - s.last_active > SESSION_IDLE_TIMEOUT
                ]
                for k in stale:
                    _, channel_id, _ = k
                    channel = self.bot.get_channel(channel_id) if channel_id else None
                    try:
                        await self._end_session(k, channel, reason="idle")
                    except Exception as e:
                        logger.debug(f"세션 idle 정리 중 오류(무시): {e}")
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.error(f"세션 정리 루프 오류: {e}", exc_info=True)

    # ------------------------------------------------------------------ 명령어
    @commands.command(name="대화", aliases=["ai", "챗", "질문"])
    async def ai_chat_command(self, ctx, *, prompt: str = None):
        """대화형 AI에게 질문하고, 동시에 이 채널에서 멀티턴 대화 세션을 시작합니다.

        - `!대화 안녕!` : 한 번 답하고, 이후 일반 메시지에도 아리스가 계속 답합니다.
        - `!대화`       : 답변 없이 세션만 시작합니다.
        - `!대화 종료`  : 진행 중인 대화 세션을 종료합니다.
        """
        await self._handle_chat_invocation(ctx, prompt=prompt, use_tts=False)

    @commands.command(name="대화tts", aliases=["aitts", "챗tts"])
    async def ai_chat_tts_command(self, ctx, *, prompt: str = None):
        """AI 답변을 음성(TTS)으로도 들으면서 멀티턴 대화 세션을 시작합니다.

        - `!대화tts 자기소개 해줘` : 세션 시작 + 즉시 답변(텍스트+음성).
        - `!대화tts`               : 음성 모드로 세션만 시작.
        - `!대화tts 종료`          : 세션 종료.
        """
        await self._handle_chat_invocation(ctx, prompt=prompt, use_tts=True)

    @commands.command(name="대화종료", aliases=["ai종료", "챗종료"])
    async def ai_chat_end_command(self, ctx):
        """이 채널에서 진행 중인 본인의 대화 세션을 종료합니다."""
        key = self._make_key(ctx.guild, ctx.channel, ctx.author)
        ended = await self._end_session(key, ctx.channel, reason="manual")
        if not ended:
            try:
                await ctx.send("아리스: 진행 중인 모험이 없어요, 선생님.", delete_after=10)
            except discord.HTTPException:
                pass
        await self.delete_command_message(ctx)

    @commands.command(name="ai설정", aliases=["aisettings"])
    async def ai_settings_command(self, ctx):
        """대화형 AI 설정 상태와 활성 세션 수를 확인합니다."""
        active_for_user = sum(
            1 for k in self._sessions if k[2] == ctx.author.id and k[1] == ctx.channel.id
        )
        embed = discord.Embed(
            title="대화형 AI 설정",
            description="현재 AI 연동 상태입니다.",
            color=0x1ABC9C,
        )
        embed.add_field(name="활성화", value="켜짐" if self._is_ai_available() else "꺼짐", inline=True)
        embed.add_field(name="백엔드", value="로컬 Ollama", inline=True)
        embed.add_field(name="설정 모델", value=LOCAL_AI_MODEL, inline=True)
        embed.add_field(name="현재 모델", value=self.active_model, inline=True)
        embed.add_field(name="서버", value=LOCAL_AI_BASE_URL, inline=False)
        embed.add_field(name="온도", value=str(LOCAL_AI_TEMPERATURE), inline=True)
        embed.add_field(name="최대 프롬프트 길이", value=str(LOCAL_AI_MAX_PROMPT_CHARS), inline=True)
        embed.add_field(name="요청 타임아웃", value=f"{LOCAL_AI_TIMEOUT_SECONDS}초", inline=True)
        embed.add_field(name="전체 활성 세션", value=str(len(self._sessions)), inline=True)
        embed.add_field(name="이 채널 내 본인 세션", value=str(active_for_user), inline=True)
        embed.add_field(
            name="안내",
            value=(
                "`!대화 <메시지>` 로 대화 시작 / `!대화 종료` 로 끝.\n"
                "`!대화tts` 는 음성으로도 들을 수 있어요."
            ),
            inline=False,
        )
        await ctx.send(embed=embed, delete_after=30)
        await self.delete_command_message(ctx)

    # ------------------------------------------------------------------ Slash Commands
    @discord.app_commands.describe(prompt="아리스에게 보낼 메시지 (생략하면 세션만 시작)")
    async def slash_ai_chat(self, interaction: discord.Interaction, prompt: Optional[str] = None):
        await safe_defer(interaction)
        ctx = await self.bot.get_context(interaction)
        await self.ai_chat_command(ctx, prompt=prompt)

    @discord.app_commands.describe(prompt="아리스에게 보낼 메시지 (생략하면 음성 세션만 시작)")
    async def slash_ai_chat_tts(self, interaction: discord.Interaction, prompt: Optional[str] = None):
        await safe_defer(interaction)
        ctx = await self.bot.get_context(interaction)
        await self.ai_chat_tts_command(ctx, prompt=prompt)

    async def slash_ai_chat_end(self, interaction: discord.Interaction):
        await safe_defer(interaction)
        ctx = await self.bot.get_context(interaction)
        await self.ai_chat_end_command(ctx)

    # ------------------------------------------------------------------ Cog 라이프사이클
    async def cog_load(self):
        try:
            self.bot.tree.remove_command("ai")
        except Exception:
            pass

        self.ai_group = discord.app_commands.Group(name="ai", description="대화형 AI 명령어")
        self.ai_group.command(name="대화", description="아리스와 대화 세션을 시작/계속합니다")(self.slash_ai_chat)
        self.ai_group.command(name="대화tts", description="음성으로도 들을 수 있는 대화 세션")(self.slash_ai_chat_tts)
        self.ai_group.command(name="대화종료", description="진행 중인 대화 세션을 종료")(self.slash_ai_chat_end)

        self.bot.tree.add_command(self.ai_group, override=True)

        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(self._idle_cleanup_loop())

    async def cog_unload(self):
        try:
            if self.ai_group:
                self.bot.tree.remove_command(self.ai_group.name)
        except Exception as e:
            logger.error(f"AI Slash Commands 제거 중 오류: {e}")

        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
        self._cleanup_task = None
        self._sessions.clear()
