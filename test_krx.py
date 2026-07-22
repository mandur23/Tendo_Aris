"""KRX OPEN API 인증키 동작 확인용 스크립트.

사용법:
    1) TOKEN.env에 인증키 추가:  KRX_API_KEY=발급받은_인증키
    2) python test_krx.py                 # 어제 날짜 KOSPI 상위 5종목
       python test_krx.py 20260717        # 특정 기준일자
       python test_krx.py 20260717 삼성전자  # 특정 종목 조회
"""
import asyncio
import sys

from utils.krx_api import (
    KrxApiError,
    get_daily_trade,
    get_latest_daily_trade,
    search_all_markets,
)


async def main() -> None:
    bas_dd = sys.argv[1] if len(sys.argv) > 1 else None
    keyword = sys.argv[2] if len(sys.argv) > 2 else None

    try:
        if keyword:
            found = await search_all_markets(keyword, bas_dd=bas_dd)
            if not found:
                print(f"'{keyword}' 종목을 찾지 못했어요. (기준일: {bas_dd or '최근 영업일'})")
                return
            market, row = found
            print(f"[{row['ISU_NM']} / {row['ISU_CD']}] ({market})")
            print(f"  종가   : {row['TDD_CLSPRC']}")
            print(f"  등락률 : {row['FLUC_RT']}%")
            print(f"  거래량 : {row['ACC_TRDVOL']}")
            print(f"  시가총액: {row['MKTCAP']}")
            return

        if bas_dd:
            day, rows = bas_dd, await get_daily_trade(market="kospi", bas_dd=bas_dd)
        else:
            day, rows = await get_latest_daily_trade(market="kospi")
        print(f"조회 성공! 총 {len(rows)}개 종목 (기준일: {day or '데이터 없음'})\n")
        for row in rows[:5]:
            print(f"  {row['ISU_NM']:<12} 종가 {row['TDD_CLSPRC']:>10}  등락률 {row['FLUC_RT']:>7}%")
        if not rows:
            print("  (빈 목록 — 최근 며칠이 주말/공휴일이거나 서비스 미신청일 수 있어요)")
    except KrxApiError as exc:
        print(f"[실패] {exc}")
    except Exception as exc:  # noqa: BLE001
        print(f"[오류] {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    asyncio.run(main())
