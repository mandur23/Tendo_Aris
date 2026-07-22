"""주식 시세 조회 Cog.

- 기본: 네이버 금융 준실시간 현재가 (장중 실시간 반영)
- 기준일자(YYYYMMDD) 지정 시: KRX OPEN API 일별매매정보(과거 종가)

명령어:
    !주가 삼성전자            → 실시간 현재가 (네이버)
    !주가 005930             → 코드로 실시간
    !주가 에코프로            → 코스닥도 자동
    !주가 삼성전자 20260717   → 그 날짜 종가 (KRX, 인증키 필요)

KRX 인증키는 TOKEN.env 의 KRX_API_KEY 로 설정한다 (과거 조회용).
"""
import logging

import discord
from discord.ext import commands

from utils import krx_api, naver_stock
from utils.discord_utils import safe_typing

logger = logging.getLogger(__name__)


def _to_int(value) -> int | None:
    try:
        return int(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _fmt_int(value) -> str:
    n = _to_int(value)
    return f"{n:,}" if n is not None else str(value if value not in (None, "") else "-")


def _fmt_rate(rate) -> str:
    if rate is None:
        return "-"
    try:
        r = float(rate)
    except (TypeError, ValueError):
        return f"{rate}%"
    sign = "+" if r > 0 else ""
    return f"{sign}{r}%"


def _color_for(direction_or_rate) -> discord.Color:
    """상승 빨강 / 하락 파랑 (국내 관례)."""
    d = direction_or_rate
    if isinstance(d, str) and d in ("up", "down", "flat"):
        up, down = d == "up", d == "down"
    else:
        try:
            v = float(str(d).replace(",", ""))
        except (TypeError, ValueError):
            return discord.Color.light_grey()
        up, down = v > 0, v < 0
    if up:
        return discord.Color.from_rgb(220, 60, 60)
    if down:
        return discord.Color.from_rgb(60, 110, 220)
    return discord.Color.light_grey()


class Stock(commands.Cog):
    """주식 시세 조회 (실시간: 네이버 / 과거: KRX)."""

    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="주가", aliases=["stock", "시세"])
    async def stock(self, ctx: commands.Context, *args: str):
        """!주가 <종목명|코드> [기준일자YYYYMMDD]"""
        if not args:
            await ctx.reply("사용법: `!주가 삼성전자` (실시간) 또는 `!주가 삼성전자 20260717` (과거 종가)")
            return

        # 마지막 인자가 8자리 숫자면 기준일자(과거 조회)로 해석
        bas_dd = None
        parts = list(args)
        if len(parts) >= 2 and parts[-1].isdigit() and len(parts[-1]) == 8:
            bas_dd = parts.pop()
        query = " ".join(parts).strip()

        async with safe_typing(ctx):
            if bas_dd:
                embed = await self._realtime_or_historical(ctx, query, bas_dd)
            else:
                embed = await self._realtime(ctx, query)
        if embed is not None:
            await ctx.reply(embed=embed)

    # ------------------------------------------------------------------ 실시간(네이버)
    async def _realtime(self, ctx, query: str) -> discord.Embed | None:
        try:
            quote = await naver_stock.quote_by_query(query)
        except naver_stock.NaverStockError as exc:
            await ctx.reply(f"실시간 조회 실패: {exc}")
            return None
        except Exception as exc:  # noqa: BLE001
            logger.error(f"실시간 주가 조회 오류: {exc}", exc_info=True)
            await ctx.reply(f"조회 중 오류가 발생했어요: {type(exc).__name__}")
            return None

        if not quote:
            await ctx.reply(f"'{query}' 종목을 찾지 못했어요. 정확한 종목명이나 6자리 코드로 다시 시도해 주세요.")
            return None
        return self._build_realtime_embed(quote)

    def _build_realtime_embed(self, q: dict) -> discord.Embed:
        change = q.get("change")
        arrow = "▲" if (change or 0) > 0 else ("▼" if (change or 0) < 0 else "-")
        diff_txt = f" {arrow}{abs(change):,}" if change is not None else ""

        embed = discord.Embed(
            title=f"{q.get('name')} ({q.get('code')})",
            description=f"**{_fmt_int(q.get('price'))}원**{diff_txt}  ({_fmt_rate(q.get('rate'))})",
            color=_color_for(q.get("direction")),
        )
        embed.add_field(name="시가", value=f"{_fmt_int(q.get('open'))}원", inline=True)
        embed.add_field(name="고가", value=f"{_fmt_int(q.get('high'))}원", inline=True)
        embed.add_field(name="저가", value=f"{_fmt_int(q.get('low'))}원", inline=True)
        embed.add_field(name="거래량", value=f"{_fmt_int(q.get('volume'))}주", inline=True)
        if q.get("trade_value"):
            embed.add_field(name="거래대금", value=str(q["trade_value"]), inline=True)

        status = f"실시간 {q.get('time_text')}" if q.get("market_open") else "장마감 · 종가"
        embed.set_footer(text=f"{q.get('market_label')} · {status} · {q.get('source')}")
        return embed

    # ------------------------------------------------------------------ 과거(KRX)
    async def _realtime_or_historical(self, ctx, query: str, bas_dd: str) -> discord.Embed | None:
        if not krx_api.is_configured():
            await ctx.reply(
                "과거 종가 조회(KRX)는 인증키가 필요해요. `TOKEN.env`에 `KRX_API_KEY=<인증키>`를 추가하거나, "
                "날짜 없이 `!주가 " + query + "` 로 실시간 조회하세요."
            )
            return None
        try:
            result = await krx_api.search_all_markets(query, bas_dd=bas_dd)
        except krx_api.KrxApiError as exc:
            await ctx.reply(f"조회 실패: {exc}")
            return None
        except Exception as exc:  # noqa: BLE001
            logger.error(f"KRX 주가 조회 오류: {exc}", exc_info=True)
            await ctx.reply(f"조회 중 오류가 발생했어요: {type(exc).__name__}")
            return None

        if not result:
            await ctx.reply(f"'{query}' 종목을 {bas_dd} 데이터에서 찾지 못했어요.")
            return None
        market, row = result
        return self._build_krx_embed(market, row, bas_dd)

    def _build_krx_embed(self, market: str, row: dict, bas_dd: str) -> discord.Embed:
        name = row.get("ISU_NM", "-")
        code = row.get("ISU_CD", "-")
        rate = row.get("FLUC_RT")
        diff = _to_int(row.get("CMPPREVDD_PRC"))
        day_txt = f"{bas_dd[:4]}-{bas_dd[4:6]}-{bas_dd[6:]}" if len(str(bas_dd)) == 8 else str(bas_dd)

        diff_txt = ""
        if diff is not None:
            arrow = "▲" if diff > 0 else ("▼" if diff < 0 else "-")
            diff_txt = f" {arrow}{abs(diff):,}"

        embed = discord.Embed(
            title=f"{name} ({code})",
            description=f"**{_fmt_int(row.get('TDD_CLSPRC'))}원**{diff_txt}  ({_fmt_rate(rate)})",
            color=_color_for(rate),
        )
        embed.add_field(name="시가", value=f"{_fmt_int(row.get('TDD_OPNPRC'))}원", inline=True)
        embed.add_field(name="고가", value=f"{_fmt_int(row.get('TDD_HGPRC'))}원", inline=True)
        embed.add_field(name="저가", value=f"{_fmt_int(row.get('TDD_LWPRC'))}원", inline=True)
        embed.add_field(name="거래량", value=f"{_fmt_int(row.get('ACC_TRDVOL'))}주", inline=True)
        embed.add_field(name="거래대금", value=f"{_fmt_int(row.get('ACC_TRDVAL'))}원", inline=True)
        embed.add_field(name="시가총액", value=f"{_fmt_int(row.get('MKTCAP'))}원", inline=True)
        embed.set_footer(text=f"{krx_api.MARKET_LABELS.get(market, market)} · 종가 {day_txt} · KRX")
        return embed


async def setup(bot):
    await bot.add_cog(Stock(bot))
