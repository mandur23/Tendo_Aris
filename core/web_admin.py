"""웹 관리자 대시보드

봇과 같은 프로세스(이벤트 루프)에서 실행되는 로컬 관리용 aiohttp 웹 서버.
discord.py 가 aiohttp 를 의존하므로 추가 패키지 설치가 필요 없다.

보안:
- 기본값으로 127.0.0.1 에만 바인딩한다. 외부에서 접속할 일이 없다면 그대로 두는 것을 권장.
- TOKEN.env 의 WEB_ADMIN_PASSWORD 로 로그인해야 사용할 수 있으며,
  비밀번호가 비어 있으면 서버 자체를 시작하지 않는다.

기능:
- 봇 상태(지연시간, 업타임, 서버 목록, 로드된 Cog) 조회
- 아리스 대화 세션 목록 조회 / 강제 종료, Ollama 모델 런타임 전환
- 음악 재생 현황 조회, 스킵 / 정지
- 최근 로그 조회
- 봇 안전 종료
"""
import asyncio
import hmac
import json
import logging
import math
import os
import platform
import re
import secrets
import time
from typing import Any, Dict, List, Optional, Tuple

import discord
from aiohttp import web

try:
    import psutil
except ImportError:  # psutil이 없어도 대시보드는 동작해야 한다
    psutil = None

from core.shutdown_handler import get_shutdown_event
from utils import db_utils
from utils import krx_api
from utils import naver_stock
from utils.file_utils import atomic_write_text
from utils.config import (
    BASE_DIR,
    FFMPEG_PATH,
    LOCAL_AI_BASE_URL,
    LOCAL_AI_MODEL,
    LOCAL_AI_TEMPERATURE,
    MYSQL_DATABASE,
    MYSQL_HOST,
    MYSQL_PORT,
    USE_MYSQL,
    WEB_ADMIN_ENABLED,
    WEB_ADMIN_HOST,
    WEB_ADMIN_PASSWORD,
    WEB_ADMIN_PORT,
)

logger = logging.getLogger(__name__)

COOKIE_NAME = "aris_admin_session"
SESSION_TTL_SECONDS = 12 * 3600
_SAFE_MODEL_NAME_RE = re.compile(r"^[\w.\-/:]+$")


def _safe_compare_password(provided: str, expected: str) -> bool:
    """길이가 달라도 ValueError 없이 비밀번호를 비교합니다."""
    if not expected:
        return False
    try:
        return hmac.compare_digest(provided, expected)
    except (TypeError, ValueError):
        return False


def _validate_model_name(model: str) -> Optional[str]:
    """모델 이름에 개행·주입 문자가 없는지 검증합니다. 문제 시 오류 메시지를 반환."""
    if not model or not model.strip():
        return "model 값이 비어 있습니다."
    if "\n" in model or "\r" in model or "=" in model:
        return "model 값에 허용되지 않는 문자가 포함되어 있습니다."
    if not _SAFE_MODEL_NAME_RE.match(model):
        return "model 값 형식이 올바르지 않습니다."
    return None


LOGIN_HTML = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>아리스 봇 관리 - 로그인</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root{--accent:#968ae0;--accent2:#796cbf;--text:#e9e9ed;--muted:#9397ab;--bad:#ff8fa6;--mono:'JetBrains Mono',monospace}
  *{box-sizing:border-box}
  body{font-family:'Inter',system-ui,sans-serif;color:var(--text);margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
    background:radial-gradient(900px 480px at 20% -10%,rgba(145,132,217,.20),transparent 60%),radial-gradient(800px 480px at 85% 10%,rgba(120,108,191,.16),transparent 55%),#0f1119}
  body::before{content:'';position:fixed;inset:0;pointer-events:none;background:linear-gradient(rgba(145,132,217,.05) 1px,transparent 1px),linear-gradient(90deg,rgba(145,132,217,.05) 1px,transparent 1px);background-size:44px 44px;-webkit-mask-image:radial-gradient(ellipse at 50% 30%,#000,transparent 75%)}
  .box{position:relative;width:370px;padding:38px 40px;border-radius:16px;background:rgba(19,21,32,.82);backdrop-filter:blur(16px);border:1px solid rgba(145,132,217,.22);box-shadow:0 0 40px rgba(145,132,217,.16),0 24px 60px rgba(0,0,0,.55);animation:rise .5s ease both}
  .box::before{content:'';position:absolute;top:0;left:26px;right:26px;height:1px;background:linear-gradient(90deg,transparent,var(--accent),var(--accent2),transparent)}
  @keyframes rise{from{opacity:0;transform:translateY(12px)}to{opacity:1;transform:none}}
  .logo{width:46px;height:46px;border-radius:13px;background:linear-gradient(135deg,var(--accent),var(--accent2));display:flex;align-items:center;justify-content:center;box-shadow:0 0 18px rgba(145,132,217,.5);margin-bottom:18px}
  h1{font-size:17px;margin:0 0 4px;letter-spacing:.02em;font-weight:600}
  .deck{font-family:var(--mono);font-size:10px;letter-spacing:.28em;color:var(--muted);margin:0 0 20px}
  p.sub{margin:0 0 22px;font-size:13px;color:var(--muted);line-height:1.6}
  input[type=password]{width:100%;padding:12px 14px;border-radius:10px;font-size:14px;border:1px solid rgba(145,132,217,.3);background:rgba(10,12,22,.85);color:var(--text);font-family:var(--mono);letter-spacing:2px;transition:all .2s}
  input[type=password]:focus{outline:none;border-color:var(--accent);box-shadow:0 0 14px rgba(145,132,217,.4)}
  button{width:100%;margin-top:16px;padding:12px;border:0;border-radius:10px;background:linear-gradient(90deg,var(--accent),var(--accent2));color:#12101f;font-size:14px;font-weight:600;letter-spacing:2px;cursor:pointer;transition:all .2s}
  button:hover{filter:brightness(1.1);box-shadow:0 0 22px rgba(145,132,217,.55)}
  .error{color:var(--bad);font-size:13px;margin-top:14px}
</style>
</head>
<body>
  <div class="box">
    <div class="logo"><svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="#12101f" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="8" width="16" height="12" rx="3"/><path d="M12 8V4M9 4h6"/><circle cx="9" cy="14" r="1.3" fill="#12101f" stroke="none"/><circle cx="15" cy="14" r="1.3" fill="#12101f" stroke="none"/></svg></div>
    <h1>텐도 아리스 봇 관리</h1>
    <p class="deck">ARIS CONTROL DECK // ACCESS</p>
    <p class="sub">용사 아리스의 관리실입니다. 비밀번호를 입력해 주세요, 선생님.</p>
    <form method="post" action="/login">
      <input type="password" name="password" placeholder="ACCESS CODE" autofocus autocomplete="current-password">
      <button type="submit">입장</button>
    </form>
    <!--ERROR-->
  </div>
</body>
</html>
"""


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>텐도 아리스 봇 관리</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root{
    --color-bg:#161826; --color-surface:#232532; --color-text:#e9e9ed;
    --color-accent:#9184d9;
    --color-neutral-400:#b2b6ca; --color-neutral-500:#9397ab; --color-neutral-600:#75798c;
    --color-accent-200:#e7e5fe; --color-accent-300:#d2cefd; --color-accent-400:#b5abfc;
    --color-accent-500:#968ae0; --color-accent-600:#796cbf; --color-accent-700:#5d5294; --color-accent-800:#423a6a;
    --color-accent-2-300:#d2cefd; --color-accent-2-400:#b5afe8;
    --font-body:"Inter",system-ui,sans-serif; --font-heading:"Inter",system-ui,sans-serif;
    --mono:'JetBrains Mono',monospace; --ok:#4ad9a8; --bad:#ff8fa6; --warn:#ffcf6b;
  }
  *,*::before,*::after{box-sizing:border-box}
  html,body{margin:0;height:100%}
  body{background:#0f1119;color:var(--color-text);font-family:var(--font-body);font-size:15px;line-height:1.55}
  ::-webkit-scrollbar{width:10px;height:10px}
  ::-webkit-scrollbar-thumb{background:rgba(145,132,217,.25);border-radius:6px}
  ::-webkit-scrollbar-thumb:hover{background:rgba(145,132,217,.45)}
  ::-webkit-scrollbar-track{background:transparent}
  a{text-decoration:none}
  .mono{font-family:var(--mono)}
  .cc-nav{display:flex;align-items:center;gap:11px;padding:10px 12px;border-radius:10px;color:var(--color-neutral-400);font:400 13px var(--font-body);cursor:pointer;transition:background .16s,color .16s}
  .cc-nav:hover{background:rgba(145,132,217,.09);color:var(--color-accent-200)}
  .cc-nav.on{background:linear-gradient(90deg,rgba(145,132,217,.20),rgba(145,132,217,.04));border:1px solid rgba(145,132,217,.28);color:var(--color-accent-200)}
  .cc-btn{display:inline-flex;align-items:center;justify-content:center;gap:6px;cursor:pointer;font-family:var(--font-body);font-weight:500;font-size:12px;padding:7px 14px;border-radius:8px;border:1px solid rgba(145,132,217,.32);background:rgba(145,132,217,.07);color:var(--color-accent-200);transition:all .16s}
  .cc-btn:hover{background:rgba(145,132,217,.16);box-shadow:0 0 14px rgba(145,132,217,.3)}
  .cc-btn.primary{background:linear-gradient(90deg,var(--color-accent-500),var(--color-accent-600));color:#12101f;border:0;font-weight:600}
  .cc-btn.primary:hover{filter:brightness(1.1);box-shadow:0 0 18px rgba(145,132,217,.5)}
  .cc-btn.danger{border-color:rgba(255,110,140,.42);background:rgba(255,110,140,.08);color:#ff8fa6}
  .cc-btn.danger:hover{background:rgba(255,110,140,.18);box-shadow:0 0 14px rgba(255,110,140,.35)}
  .cc-btn.sm{padding:4px 10px;font-size:11px}
  .panel{border-radius:16px;background:linear-gradient(180deg,rgba(35,37,50,.7),rgba(20,22,34,.7));border:1px solid rgba(145,132,217,.14)}
  .kcard{padding:11px 13px;border-radius:10px;background:rgba(10,12,22,.55)}
  .kcard.acc{border-left:2px solid var(--color-accent-400)}
  .kl{font:500 9px var(--mono);letter-spacing:.14em;color:var(--color-neutral-500);text-transform:uppercase}
  .kvv{font:400 14px var(--mono);margin-top:5px;word-break:break-all}
  .sec{scroll-margin-top:16px}
  .sec-h{display:flex;align-items:center;gap:10px;margin-bottom:14px}
  .sec-h .k{font:500 12px var(--mono);letter-spacing:.18em;color:var(--color-accent-300);text-transform:uppercase;white-space:nowrap}
  .sec-h .ln{flex:1;height:1px;background:linear-gradient(90deg,rgba(145,132,217,.3),transparent)}
  table{width:100%;border-collapse:collapse;font-size:13px}
  .th{text-align:left;padding:11px 6px;font:500 9px var(--mono);letter-spacing:.1em;color:var(--color-neutral-500);text-transform:uppercase;border-bottom:1px solid rgba(145,132,217,.14)}
  .th.r{text-align:right}
  td{padding:10px 6px;border-bottom:1px solid rgba(145,132,217,.06)}
  td.r{text-align:right}
  tr.cc-row{transition:background .14s}
  tr.cc-row:hover{background:rgba(145,132,217,.06)}
  .tag{display:inline-flex;align-items:center;font:500 10px var(--font-body);letter-spacing:.02em;padding:3px 9px;border-radius:6px}
  .tag.accent{background:var(--color-accent-800);color:var(--color-accent-200)}
  .tag.outline{border:1px solid var(--color-accent-500);color:var(--color-accent-300)}
  .ok{color:var(--ok)} .bad{color:var(--bad)} .warn{color:var(--warn)} .muted{color:var(--color-neutral-500)}
  input[type=text]{background:rgba(10,12,22,.85);color:var(--color-text);border:1px solid rgba(145,132,217,.3);border-radius:8px;padding:9px 13px;font-family:var(--mono);font-size:13px}
  input[type=text]:focus{outline:none;border-color:var(--color-accent-400);box-shadow:0 0 10px rgba(145,132,217,.3)}
  select{appearance:none;background:rgba(10,12,22,.85);color:var(--color-text);border:1px solid rgba(145,132,217,.3);border-radius:8px;padding:7px 34px 7px 12px;font-family:var(--mono);font-size:12.5px}
  select:focus{outline:none;border-color:var(--color-accent-400)}
  input[type=checkbox]{accent-color:var(--color-accent-500);width:15px;height:15px;vertical-align:middle}
  pre{margin:0}
  @keyframes spin{to{transform:rotate(360deg)}}
  @keyframes scan{0%{transform:translateX(-140%)}100%{transform:translateX(900%)}}
  @keyframes blink{0%,60%{opacity:1}80%{opacity:.25}100%{opacity:1}}
  @keyframes drawline{from{stroke-dashoffset:1600}to{stroke-dashoffset:0}}
  .cc-scanline{position:absolute;top:0;bottom:0;width:120px;left:0;pointer-events:none;background:linear-gradient(90deg,transparent,rgba(145,132,217,.08),transparent);animation:scan 9s linear infinite}
  .cc-draw{stroke-dasharray:1600;animation:drawline 2.4s ease forwards}
  #toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%);z-index:80;background:rgba(20,22,34,.95);border:1px solid rgba(145,132,217,.45);color:var(--color-text);padding:11px 20px;border-radius:10px;display:none;font-size:13px;box-shadow:0 0 24px rgba(145,132,217,.3)}
</style>
</head>
<body>
<div style="display:flex;height:100vh;overflow:hidden;background:radial-gradient(1200px 560px at 12% -8%,rgba(145,132,217,.18),transparent 60%),radial-gradient(900px 520px at 92% 4%,rgba(120,108,191,.12),transparent 55%),#0f1119">
  <div style="position:fixed;inset:0;pointer-events:none;z-index:0;background-image:linear-gradient(rgba(145,132,217,.04) 1px,transparent 1px),linear-gradient(90deg,rgba(145,132,217,.04) 1px,transparent 1px);background-size:46px 46px;-webkit-mask-image:radial-gradient(ellipse at 50% 0%,#000,transparent 80%)"></div>

  <!-- SIDEBAR -->
  <aside style="width:236px;flex:none;position:relative;z-index:2;padding:22px 18px;background:rgba(13,15,24,.72);backdrop-filter:blur(14px);border-right:1px solid rgba(145,132,217,.14);display:flex;flex-direction:column;gap:24px">
    <div style="display:flex;align-items:center;gap:11px">
      <div style="width:40px;height:40px;flex:none;border-radius:12px;background:linear-gradient(135deg,var(--color-accent-500),var(--color-accent-700));display:flex;align-items:center;justify-content:center;box-shadow:0 0 18px rgba(145,132,217,.5)">
        <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="#12101f" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="8" width="16" height="12" rx="3"/><path d="M12 8V4M9 4h6"/><circle cx="9" cy="14" r="1.3" fill="#12101f" stroke="none"/><circle cx="15" cy="14" r="1.3" fill="#12101f" stroke="none"/></svg>
      </div>
      <div><div style="font:600 16px var(--font-heading)">아리스</div><div style="font:500 9px var(--mono);letter-spacing:.28em;color:var(--color-neutral-500)">CONTROL DECK</div></div>
    </div>
    <nav id="side-nav" style="display:flex;flex-direction:column;gap:3px">
      <a class="cc-nav on" href="#monitor" data-sec="monitor"><svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M3 13a9 9 0 0 1 18 0"/><path d="M12 13l4-3"/></svg>라이브 모니터</a>
      <a class="cc-nav" href="#bot" data-sec="bot"><svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="8" width="16" height="12" rx="3"/><path d="M12 8V4"/></svg>봇 상태</a>
      <a class="cc-nav" href="#servers" data-sec="servers"><svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="5" rx="1.5"/><rect x="3" y="11" width="18" height="5" rx="1.5"/></svg>서버 목록</a>
      <a class="cc-nav" href="#chat" data-sec="chat"><svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 5h16v11H8l-4 4z"/></svg>AI 대화 세션</a>
      <a class="cc-nav" href="#music" data-sec="music"><svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18V6l10-2v12"/><circle cx="6" cy="18" r="3"/><circle cx="16" cy="16" r="3"/></svg>음악 재생</a>
      <a class="cc-nav" href="#tts" data-sec="tts"><svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 4v16M8 8v8M4 11v2M16 8v8M20 11v2"/></svg>TTS · 음성</a>
      <a class="cc-nav" href="#data" data-sec="data"><svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v14c0 1.7 3.6 3 8 3s8-1.3 8-3V5M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/></svg>데이터베이스</a>
      <a class="cc-nav" href="#stock" data-sec="stock"><svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6l9-3 9 3v6c0 5-4 8-9 9-5-1-9-4-9-9z"/><path d="M9 12l2 2 4-4"/></svg>주식 시세</a>
      <a class="cc-nav" href="#logs" data-sec="logs"><svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 5h16v14H4z"/><path d="M8 10l3 2-3 2M13 14h3"/></svg>실시간 로그</a>
    </nav>
    <div style="margin-top:auto;display:flex;flex-direction:column;gap:10px">
      <div style="padding:13px;border-radius:12px;background:rgba(145,132,217,.07);border:1px solid rgba(145,132,217,.16)">
        <div style="display:flex;align-items:center;gap:8px"><span id="side-dot" style="width:8px;height:8px;border-radius:50%;background:#4ad9a8;box-shadow:0 0 10px #4ad9a8;animation:blink 2.4s infinite"></span><span id="side-status" style="font:500 12px var(--font-body)">연결 중…</span></div>
        <div id="side-uptime" style="font:400 11px var(--mono);color:var(--color-neutral-500);margin-top:6px">uptime -</div>
      </div>
      <a class="cc-nav" href="#danger" data-sec="danger" style="color:#ff8fa6"><svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v9M6.4 6.4a8 8 0 1 0 11.2 0"/></svg>위험 구역</a>
    </div>
  </aside>

  <!-- MAIN -->
  <div style="flex:1;position:relative;z-index:2;display:flex;flex-direction:column;min-width:0">
    <header style="flex:none;display:flex;align-items:center;justify-content:space-between;padding:16px 30px;border-bottom:1px solid rgba(145,132,217,.12);background:rgba(13,15,24,.6);backdrop-filter:blur(12px)">
      <div>
        <div style="font:500 18px var(--font-heading);letter-spacing:-.01em">텐도 아리스 봇 관리</div>
        <div style="font:400 11px var(--mono);color:var(--color-neutral-500);margin-top:3px;letter-spacing:.1em">REALTIME TELEMETRY · v2.4</div>
      </div>
      <div style="display:flex;align-items:center;gap:12px">
        <span id="hdr-time" style="font:400 12px var(--mono);color:var(--color-neutral-400)"></span>
        <span id="hdr-live" style="display:inline-flex;align-items:center;gap:6px;font:500 11px var(--mono);color:#4ad9a8;padding:5px 10px;border-radius:20px;border:1px solid rgba(74,217,168,.32);background:rgba(74,217,168,.08)"><span style="width:7px;height:7px;border-radius:50%;background:#4ad9a8;box-shadow:0 0 8px #4ad9a8;animation:blink 2s infinite"></span>LIVE</span>
        <button class="cc-btn primary" onclick="refreshAll()">새로고침</button>
        <button class="cc-btn" onclick="logout()">로그아웃</button>
      </div>
    </header>

    <div id="scroller" style="flex:1;overflow-y:auto;padding:24px 30px 60px;display:flex;flex-direction:column;gap:22px">

      <!-- LIVE MONITOR -->
      <section id="monitor" class="sec">
        <div class="sec-h"><span class="k">▸ 라이브 모니터</span><span class="ln"></span></div>
        <div style="display:grid;grid-template-columns:1fr 1fr 1fr 1.3fr;gap:16px;margin-bottom:16px">
          <div class="panel" style="position:relative;overflow:hidden;padding:18px 16px 14px;text-align:center">
            <div class="cc-scanline"></div>
            <svg id="gauge-cpu" width="118" height="118" viewBox="0 0 120 120" style="margin:0 auto;display:block"></svg>
            <div style="font:500 10px var(--mono);letter-spacing:.24em;color:var(--color-neutral-500);margin-top:6px">CPU</div>
          </div>
          <div class="panel" style="position:relative;overflow:hidden;padding:18px 16px 14px;text-align:center">
            <svg id="gauge-mem" width="118" height="118" viewBox="0 0 120 120" style="margin:0 auto;display:block"></svg>
            <div id="mem-sub" style="font:500 10px var(--mono);letter-spacing:.24em;color:var(--color-neutral-500);margin-top:6px">메모리</div>
          </div>
          <div class="panel" style="position:relative;overflow:hidden;padding:18px 16px 14px;display:flex;flex-direction:column;align-items:center;justify-content:center">
            <div id="stat-sessions" style="font:600 54px var(--mono);line-height:1;text-shadow:0 0 22px rgba(145,132,217,.55)">-</div>
            <div style="font:500 10px var(--mono);letter-spacing:.2em;color:var(--color-neutral-500);margin-top:12px;text-align:center">활성 대화 세션</div>
            <div id="stat-model" style="font:400 11px var(--font-body);color:var(--color-neutral-400);margin-top:4px">-</div>
          </div>
          <div class="panel" style="position:relative;overflow:hidden;padding:16px;display:flex;flex-direction:column;align-items:center;justify-content:center">
            <div style="width:112px;height:112px;border-radius:50%;position:relative;overflow:hidden;border:1px solid rgba(145,132,217,.4);box-shadow:0 0 20px rgba(145,132,217,.2),inset 0 0 22px rgba(145,132,217,.1);background:repeating-radial-gradient(circle at 50% 50%,rgba(145,132,217,.16) 0 1px,transparent 1px 18px),radial-gradient(circle,rgba(145,132,217,.12),transparent 70%)">
              <div style="position:absolute;inset:0;border-radius:50%;background:conic-gradient(from 0deg,rgba(145,132,217,.6),rgba(145,132,217,.1) 70deg,transparent 90deg);animation:spin 3.4s linear infinite"></div>
              <span style="position:absolute;top:26%;left:62%;width:5px;height:5px;border-radius:50%;background:#4ad9a8;box-shadow:0 0 8px #4ad9a8"></span>
              <span style="position:absolute;top:64%;left:38%;width:5px;height:5px;border-radius:50%;background:var(--color-accent-300);box-shadow:0 0 8px var(--color-accent-300)"></span>
            </div>
            <div id="radar-label" style="font:500 10px var(--mono);letter-spacing:.2em;color:#4ad9a8;margin-top:12px">SYSTEM</div>
          </div>
        </div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px">
          <div class="panel" style="position:relative;overflow:hidden;padding:16px 20px">
            <div class="cc-scanline"></div>
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px"><span style="font:500 11px var(--mono);letter-spacing:.14em;color:var(--color-neutral-400);text-transform:uppercase">지연시간 (ms) · 10분</span><span id="lat-now" style="font:500 12px var(--mono);color:var(--color-accent-300)"></span></div>
            <svg id="chart-latency" viewBox="0 0 560 150" preserveAspectRatio="none" style="width:100%;height:120px;display:block"></svg>
          </div>
          <div class="panel" style="position:relative;overflow:hidden;padding:16px 20px">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px"><span style="font:500 11px var(--mono);letter-spacing:.14em;color:var(--color-neutral-400);text-transform:uppercase">CPU / 메모리 (%) · 10분</span><span style="font:500 12px var(--mono)"><span style="color:var(--color-accent-300)">■</span> CPU <span style="color:var(--color-accent-2-300);margin-left:8px">■</span> MEM</span></div>
            <svg id="chart-sys" viewBox="0 0 560 150" preserveAspectRatio="none" style="width:100%;height:120px;display:block"></svg>
          </div>
        </div>
      </section>

      <!-- BOT STATUS -->
      <section id="bot" class="sec">
        <div class="sec-h"><span class="k">▸ 봇 상태</span><span class="ln"></span></div>
        <div class="panel" style="padding:20px 22px">
          <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:10px">
            <div class="kcard acc"><div class="kl">봇</div><div class="kvv" id="b-name">-</div></div>
            <div class="kcard acc"><div class="kl">상태</div><div class="kvv" id="b-status">-</div></div>
            <div class="kcard acc"><div class="kl">지연시간</div><div class="kvv" id="b-latency">-</div></div>
            <div class="kcard acc"><div class="kl">업타임</div><div class="kvv" id="b-uptime">-</div></div>
            <div class="kcard acc"><div class="kl">서버 수</div><div class="kvv" id="b-guilds">-</div></div>
            <div class="kcard acc"><div class="kl">총 멤버</div><div class="kvv" id="b-members">-</div></div>
            <div class="kcard acc"><div class="kl">명령어 수</div><div class="kvv" id="b-commands">-</div></div>
            <div class="kcard acc"><div class="kl">음성 연결</div><div class="kvv" id="b-voice">-</div></div>
          </div>
          <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;margin-top:10px">
            <div class="kcard"><div class="kl">OS</div><div class="kvv" id="b-os" style="font-size:12.5px">-</div></div>
            <div class="kcard"><div class="kl">Python · discord.py</div><div class="kvv" id="b-py" style="font-size:12.5px">-</div></div>
            <div class="kcard"><div class="kl">FFmpeg · PID</div><div class="kvv" id="b-ffmpeg" style="font-size:12.5px">-</div></div>
          </div>
          <div class="kcard" style="margin-top:12px"><div class="kl" style="margin-bottom:8px">로드된 Cog</div><div id="b-cogs" style="display:flex;flex-wrap:wrap;gap:6px"></div></div>
        </div>
      </section>

      <!-- SERVERS -->
      <section id="servers" class="sec">
        <div class="sec-h"><span class="k">▸ 서버 목록</span><span class="ln"></span></div>
        <div class="panel" style="padding:8px 22px 14px">
          <table><thead><tr><th class="th">서버</th><th class="th r">멤버 수</th><th class="th r">음성 연결</th></tr></thead>
          <tbody id="server-rows"></tbody></table>
          <div id="server-empty" class="muted" style="display:none;padding:14px 6px;font-size:12.5px">참여 중인 서버가 없습니다.</div>
        </div>
      </section>

      <!-- AI CHAT -->
      <section id="chat" class="sec">
        <div class="sec-h"><span class="k">▸ 아리스 대화 (AI)</span><span class="ln"></span></div>
        <div class="panel" style="padding:20px 22px">
          <div id="chat-kv" style="display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:16px"></div>
          <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:16px;padding-bottom:16px;border-bottom:1px solid rgba(145,132,217,.1)">
            <span style="font:400 12px var(--font-body);color:var(--color-neutral-400)">모델 전환</span>
            <div style="position:relative">
              <select id="model-select"></select>
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="var(--color-neutral-400)" stroke-width="2.5" style="position:absolute;right:12px;top:50%;transform:translateY(-50%);pointer-events:none"><path d="M6 9l6 6 6-6"/></svg>
            </div>
            <label style="display:inline-flex;align-items:center;gap:7px;font:400 12px var(--font-body);color:var(--color-neutral-400);cursor:pointer"><input type="checkbox" id="model-persist" checked> 재시작 후에도 유지 (TOKEN.env)</label>
            <button class="cc-btn primary" onclick="applyModel()">적용</button>
          </div>
          <table><thead><tr><th class="th">서버</th><th class="th">채널</th><th class="th">사용자</th><th class="th">모드</th><th class="th r">대화 수</th><th class="th r">유휴</th><th class="th"></th></tr></thead>
          <tbody id="session-rows"></tbody></table>
          <div id="session-empty" class="muted" style="display:none;padding:14px 6px;font-size:12.5px">진행 중인 대화 세션이 없습니다.</div>
        </div>
      </section>

      <!-- MUSIC -->
      <section id="music" class="sec">
        <div class="sec-h"><span class="k">▸ 음악</span><span class="ln"></span></div>
        <div class="panel" style="padding:20px 22px">
          <div id="music-kv" style="display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:16px"></div>
          <table><thead><tr><th class="th">서버</th><th class="th">재생 중</th><th class="th r">대기열</th><th class="th">상태</th><th class="th"></th></tr></thead>
          <tbody id="music-rows"></tbody></table>
          <div id="music-empty" class="muted" style="display:none;padding:14px 6px;font-size:12.5px">재생 중인 음악이 없습니다.</div>
        </div>
      </section>

      <!-- TTS + DATABASE -->
      <section id="tts" class="sec">
        <div class="sec-h"><span class="k">▸ TTS · 데이터베이스</span><span class="ln"></span></div>
        <div id="data" class="sec" style="display:grid;grid-template-columns:1fr 1fr;gap:16px">
          <div class="panel" style="padding:20px 22px">
            <div style="font:500 10px var(--mono);letter-spacing:.16em;color:var(--color-neutral-400);text-transform:uppercase;margin-bottom:12px">TTS · 음성 모델</div>
            <div class="kcard" style="margin-bottom:10px"><div class="kl" id="t-rvc-label" style="margin-bottom:7px">RVC 모델</div><div id="t-rvc" style="display:flex;flex-wrap:wrap;gap:5px"></div></div>
            <div class="kcard"><div class="kl">Supertonic</div><div class="kvv" id="t-super" style="font-size:13px">-</div></div>
          </div>
          <div class="panel" style="padding:20px 22px">
            <div style="font:500 10px var(--mono);letter-spacing:.16em;color:var(--color-neutral-400);text-transform:uppercase;margin-bottom:12px">데이터베이스</div>
            <div id="db-body" style="display:grid;grid-template-columns:1fr 1fr;gap:10px"></div>
          </div>
        </div>
      </section>

      <!-- STOCK -->
      <section id="stock" class="sec">
        <div class="sec-h"><span class="k">▸ 주식 시세 (실시간)</span><span class="ln"></span></div>
        <div class="panel" style="padding:20px 22px">
          <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:16px">
            <input type="text" id="stock-q" placeholder="종목명 또는 6자리 코드 (예: 삼성전자, 005930)" style="flex:1;min-width:220px" onkeydown="if(event.key==='Enter')lookupStock()">
            <input type="text" id="stock-date" placeholder="기준일 YYYYMMDD (비우면 실시간)" style="width:230px" onkeydown="if(event.key==='Enter')lookupStock()">
            <button class="cc-btn primary" onclick="lookupStock()">조회</button>
          </div>
          <div id="stock-result" class="muted" style="font-size:13px">종목명이나 6자리 코드를 입력하고 조회하세요. 날짜를 비우면 장중 실시간(네이버), 날짜를 넣으면 그날 종가(KRX)를 보여줍니다.</div>
        </div>
      </section>

      <!-- LOGS -->
      <section id="logs" class="sec">
        <div class="sec-h"><span class="k">▸ 실시간 로그</span><button class="cc-btn sm" onclick="loadLogs()">갱신</button><span class="ln"></span></div>
        <pre id="log-box" style="background:rgba(6,8,16,.85);border:1px solid rgba(145,132,217,.14);border-radius:14px;padding:16px 18px;font:400 12px/1.65 var(--mono);color:#9fd8c8;max-height:320px;overflow:auto;white-space:pre-wrap;word-break:break-all">불러오는 중...</pre>
      </section>

      <!-- DANGER -->
      <section id="danger" class="sec">
        <div class="sec-h"><span class="k" style="color:#ff8fa6">▸ 위험 구역</span><span class="ln" style="background:linear-gradient(90deg,rgba(255,110,140,.35),transparent)"></span></div>
        <div style="display:flex;align-items:center;justify-content:space-between;gap:20px;padding:20px 22px;border-radius:16px;background:linear-gradient(180deg,rgba(50,28,36,.55),rgba(28,18,26,.55));border:1px solid rgba(255,110,140,.22)">
          <div>
            <div style="font:500 14px var(--font-heading);margin-bottom:4px">봇 프로세스 종료</div>
            <div style="font:400 12.5px var(--font-body);color:var(--color-neutral-400)">봇 프로세스를 안전하게 종료합니다. 다시 시작하려면 서버에서 직접 실행해야 합니다.</div>
          </div>
          <button class="cc-btn danger" style="flex:none" onclick="shutdownBot()">봇 종료</button>
        </div>
      </section>

    </div>
  </div>
</div>
<div id="toast"></div>

<script>
const ACC='#b5abfc', ACC2='#b5afe8', OKC='#4ad9a8';
function toast(m){const t=document.getElementById('toast');t.textContent=m;t.style.display='block';clearTimeout(t._t);t._t=setTimeout(()=>t.style.display='none',3000);}
async function api(path,opts){const r=await fetch(path,Object.assign({headers:{'Content-Type':'application/json'}},opts||{}));if(r.status===401){location.href='/login';return null;}return r;}
function el(tag,text,cls){const e=document.createElement(tag);if(text!=null)e.textContent=text;if(cls)e.className=cls;return e;}
function setText(id,txt,cls){const e=document.getElementById(id);if(!e)return;e.textContent=txt;if(cls!==undefined)e.style.color=cls;}
function nf(n){return (n==null||n==='')?'-':Number(String(n).replace(/,/g,'')).toLocaleString('ko-KR');}
function fmtUptime(sec){sec=Math.floor(sec||0);const d=Math.floor(sec/86400),h=Math.floor(sec%86400/3600),m=Math.floor(sec%3600/60),s=sec%60;return (d?d+'d ':'')+String(h).padStart(2,'0')+':'+String(m).padStart(2,'0')+':'+String(s).padStart(2,'0');}
function kcard(label,value,cls,accent){const c=el('div',null,'kcard'+(accent?' acc':''));c.appendChild(el('div',label,'kl'));const v=el('div',value,'kvv');if(cls)v.classList.add(cls);c.appendChild(v);return c;}

function renderGauge(id,pct,color){
  const svg=document.getElementById(id);if(!svg)return;
  const v=(pct==null)?null:Math.max(0,Math.min(100,pct));
  let h='<circle cx="60" cy="60" r="46" fill="none" stroke="rgba(145,132,217,.13)" stroke-width="9"/>';
  if(v!=null)h+='<circle cx="60" cy="60" r="46" fill="none" stroke="'+color+'" stroke-width="9" stroke-linecap="round" pathLength="100" stroke-dasharray="'+v.toFixed(1)+' 100" transform="rotate(-90 60 60)" style="filter:drop-shadow(0 0 6px '+color+'aa)"/>';
  h+='<text x="60" y="58" text-anchor="middle" font-family="JetBrains Mono,monospace" font-size="26" fill="#e9e9ed">'+(v==null?'-':Math.round(v))+'</text>';
  h+='<text x="60" y="74" text-anchor="middle" font-family="JetBrains Mono,monospace" font-size="11" fill="#75798c">%</text>';
  svg.innerHTML=h;
}
function drawChart(svgId,pts,series,yMaxFixed){
  const svg=document.getElementById(svgId);if(!svg)return;
  const W=560,H=150;
  if(pts.length<2){svg.innerHTML='<text x="280" y="78" text-anchor="middle" font-size="12" fill="#75798c">데이터 수집 중... (5초마다)</text>';return;}
  let yMax=yMaxFixed||10;
  if(!yMaxFixed){for(const p of pts)for(const s of series){if(p[s.key]!=null&&p[s.key]>yMax)yMax=p[s.key];}yMax=Math.ceil(yMax*1.2/10)*10;}
  const n=pts.length;const X=i=>W*i/(n-1);const Y=v=>6+(H-12)*(1-v/yMax);
  let h='';
  for(const gy of [Y(yMax),Y(yMax/2)])h+='<line x1="0" y1="'+gy.toFixed(1)+'" x2="'+W+'" y2="'+gy.toFixed(1)+'" stroke="rgba(145,132,217,.1)" stroke-dasharray="4 6"/>';
  series.forEach(s=>{
    let d='',area='',last=null;
    pts.forEach((p,i)=>{const v=p[s.key];if(v==null)return;const x=X(i).toFixed(1),y=Y(v).toFixed(1);d+=(d?' L ':'M ')+x+' '+y;last={x:X(i),y:Y(v)};});
    if(!d)return;
    if(s.fill&&last){const gid=svgId+'-'+s.key+'g';h+='<defs><linearGradient id="'+gid+'" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="'+s.color+'" stop-opacity=".3"/><stop offset="100%" stop-color="'+s.color+'" stop-opacity="0"/></linearGradient></defs>';h+='<path d="'+d+' L '+last.x.toFixed(1)+' '+H+' L 0 '+H+' Z" fill="url(#'+gid+')"/>';}
    h+='<path d="'+d+'" fill="none" stroke="'+s.color+'" stroke-width="2.3" stroke-linejoin="round" style="filter:drop-shadow(0 0 4px '+s.color+'88)"/>';
    if(last)h+='<circle cx="'+last.x.toFixed(1)+'" cy="'+last.y.toFixed(1)+'" r="4" fill="'+s.color+'"/>';
  });
  svg.innerHTML=h;
}

function render(d){
  const sys=d.system||{};
  renderGauge('gauge-cpu',sys.cpu_percent,ACC);
  renderGauge('gauge-mem',sys.memory?sys.memory.percent:null,ACC2);
  if(sys.memory)setText('mem-sub','메모리 · '+(sys.memory.used_mb/1024).toFixed(1)+'/'+Math.round(sys.memory.total_mb/1024)+' GB');
  setText('stat-sessions',d.chat?String(d.chat.sessions.length):'-');
  setText('stat-model',d.chat?d.chat.active_model:'-');
  const rl=document.getElementById('radar-label');
  rl.textContent=d.bot.ready?'SYSTEM ONLINE':'SYSTEM OFFLINE';rl.style.color=d.bot.ready?OKC:'#ff8fa6';

  const hist=d.metrics_history||[];
  const latPts=hist.filter(p=>p.latency_ms!=null).map(p=>({latency_ms:p.latency_ms}));
  drawChart('chart-latency',latPts,[{key:'latency_ms',color:ACC,fill:true}]);
  setText('lat-now',latPts.length?'현재 '+latPts[latPts.length-1].latency_ms+'ms':'');
  drawChart('chart-sys',hist.map(p=>({cpu:p.cpu,mem:p.mem})),[{key:'cpu',color:ACC},{key:'mem',color:ACC2}],100);

  // 사이드바 상태
  document.getElementById('side-status').textContent=d.bot.ready?'시스템 온라인':'준비 중';
  document.getElementById('side-dot').style.background=d.bot.ready?OKC:'#ff8fa6';
  document.getElementById('side-uptime').textContent='uptime '+fmtUptime(d.bot.uptime_sec);
  document.getElementById('hdr-time').textContent=new Date().toLocaleString('ko-KR',{hour12:false});

  // 봇 상태
  setText('b-name',d.bot.name||'(로그인 전)');
  setText('b-status',d.bot.ready?'온라인':'준비 중',d.bot.ready?OKC:'#ff8fa6');
  setText('b-latency',d.bot.latency_ms==null?'-':d.bot.latency_ms+'ms');
  setText('b-uptime',fmtUptime(d.bot.uptime_sec));
  setText('b-guilds',String(d.guilds.length));
  setText('b-members',nf(d.bot.total_members));
  setText('b-commands',d.bot.command_count==null?'-':String(d.bot.command_count));
  setText('b-voice',String(d.bot.voice_connections??0));
  setText('b-os',sys.os||'-');
  setText('b-py',(sys.python||'-')+' · '+(sys.discord_py||'-'));
  const ff=document.getElementById('b-ffmpeg');ff.innerHTML=(sys.ffmpeg?'<span style="color:'+OKC+'">OK</span>':'<span style="color:#ff8fa6">없음</span>')+' · '+(sys.pid??'-');
  const cogs=document.getElementById('b-cogs');cogs.replaceChildren();(d.cogs||[]).forEach(c=>cogs.appendChild(el('span',c,'tag accent')));

  // 서버
  const sr=document.getElementById('server-rows');sr.replaceChildren();
  d.guilds.forEach(g=>{const tr=el('tr',null,'cc-row');tr.appendChild(el('td',g.name));const m=el('td',g.member_count==null?'-':nf(g.member_count));m.className='r mono';tr.appendChild(m);const v=el('td',g.voice_connected?'연결됨':'-');v.className='r';v.style.color=g.voice_connected?OKC:'var(--color-neutral-500)';tr.appendChild(v);sr.appendChild(tr);});
  document.getElementById('server-empty').style.display=d.guilds.length?'none':'block';

  // AI 대화
  const ck=document.getElementById('chat-kv');ck.replaceChildren();
  const ai=d.ai_server||{};
  if(d.chat){
    ck.appendChild(kcard('Ollama 연결',ai.reachable==null?'-':(ai.reachable?'정상':'연결 안 됨'),ai.reachable?'ok':'bad'));
    ck.appendChild(kcard('현재 모델',d.chat.active_model));
    ck.appendChild(kcard('온도',String(d.chat.temperature)));
    ck.appendChild(kcard('활성 세션',String(d.chat.sessions.length)));
  } else { ck.appendChild(kcard('상태','ChatAI Cog 미로드','bad')); }
  const sess=document.getElementById('session-rows');sess.replaceChildren();
  (d.chat?d.chat.sessions:[]).forEach(s=>{
    const tr=el('tr',null,'cc-row');
    tr.appendChild(el('td',s.guild));tr.appendChild(el('td',s.channel));tr.appendChild(el('td',s.user));
    const md=el('td');md.appendChild(el('span',s.mode,'tag '+(s.mode&&s.mode.indexOf('음성')>=0?'accent':'outline')));tr.appendChild(md);
    const hl=el('td',String(s.history_len)+(s.summary_len?' (+요약)':''));hl.className='r mono';tr.appendChild(hl);
    const idl=el('td',s.idle_sec+'초 전');idl.className='r mono';idl.style.color='var(--color-neutral-400)';tr.appendChild(idl);
    const act=el('td');act.className='r';const b=el('button','종료','cc-btn danger sm');b.onclick=()=>endSession(s);act.appendChild(b);tr.appendChild(act);
    sess.appendChild(tr);
  });
  document.getElementById('session-empty').style.display=(d.chat&&d.chat.sessions.length)?'none':'block';

  // 음악
  const mk=document.getElementById('music-kv');mk.replaceChildren();const ms=d.music_stats||{};
  mk.appendChild(kcard('재생 히스토리',ms.history_items==null?'-':ms.history_items+'곡'));
  mk.appendChild(kcard('히스토리 보유 서버',String(ms.history_guilds??'-')));
  mk.appendChild(kcard('플레이리스트',ms.playlists==null?'-':ms.playlists+'개'));
  const mr=document.getElementById('music-rows');mr.replaceChildren();
  d.music.forEach(m=>{
    const tr=el('tr',null,'cc-row');
    tr.appendChild(el('td',m.guild_name));tr.appendChild(el('td',m.title||'-'));
    const q=el('td',String(m.queue_size));q.className='r mono';tr.appendChild(q);
    const st=el('td',m.is_paused?'일시정지':(m.is_playing?'재생 중':'대기'));if(m.is_playing&&!m.is_paused)st.style.color=OKC;else st.style.color='var(--color-neutral-400)';tr.appendChild(st);
    const act=el('td');act.className='r';const sk=el('button','스킵','cc-btn sm');sk.onclick=()=>musicAction('skip',m.guild_id);const stp=el('button','정지','cc-btn danger sm');stp.onclick=()=>musicAction('stop',m.guild_id);act.appendChild(sk);act.appendChild(document.createTextNode(' '));act.appendChild(stp);tr.appendChild(act);
    mr.appendChild(tr);
  });
  document.getElementById('music-empty').style.display=d.music.length?'none':'block';

  // TTS
  const tts=d.tts||{};const rvc=tts.rvc_models||[];
  document.getElementById('t-rvc-label').textContent='RVC 모델 ('+rvc.length+'개)';
  const tr2=document.getElementById('t-rvc');tr2.replaceChildren();
  if(rvc.length)rvc.forEach(n=>tr2.appendChild(el('span',n,'tag outline')));else tr2.appendChild(el('span','없음','muted'));
  setText('t-super',tts.supertonic_available==null?'-':(tts.supertonic_available?'사용 가능':'사용 불가'),tts.supertonic_available?OKC:'');

  // DB
  const db=d.database||{};const dbb=document.getElementById('db-body');dbb.replaceChildren();
  dbb.appendChild(kcard('백엔드',db.backend||'-'));
  if(db.use_mysql){
    dbb.appendChild(kcard('연결 상태',db.connected?'정상':'연결 안 됨',db.connected?'ok':'bad'));
    dbb.appendChild(kcard('서버',(db.host||'-')+':'+(db.port??'-')));
    dbb.appendChild(kcard('연결 풀',db.pool?('사용 '+(db.pool.size-db.pool.free)+' · 유휴 '+db.pool.free+' · 최대 '+db.pool.max):'-'));
    if(db.error)dbb.appendChild(kcard('오류',db.error,'bad'));
  } else { dbb.appendChild(kcard('연결 상태','JSON 파일 저장 사용 중')); }
}

async function refreshAll(){try{const r=await api('/api/status');if(!r)return;render(await r.json());}catch(e){toast('상태를 불러오지 못했습니다: '+e);}}

async function loadModels(){const sel=document.getElementById('model-select');try{const r=await api('/api/chat/models');if(!r)return;const d=await r.json();sel.replaceChildren();(d.models&&d.models.length?d.models:[d.active_model]).forEach(m=>{const o=el('option',m);o.value=m;if(m===d.active_model)o.selected=true;sel.appendChild(o);});}catch(e){}}
async function applyModel(){const model=document.getElementById('model-select').value;if(!model)return;const persist=document.getElementById('model-persist').checked;const r=await api('/api/chat/model',{method:'POST',body:JSON.stringify({model,persist})});if(!r)return;const d=await r.json();if(r.ok&&!d.error){toast('모델을 '+model+'(으)로 전환했습니다'+(d.persisted?' (TOKEN.env 저장됨)':''));refreshAll();}else{toast(d.error||'모델 전환 실패');}}
async function endSession(s){if(!confirm(s.user+' ('+s.channel+') 대화 세션을 종료할까요?'))return;const r=await api('/api/chat/session/end',{method:'POST',body:JSON.stringify({guild_id:s.guild_id,channel_id:s.channel_id,user_id:s.user_id})});if(!r)return;const d=await r.json();toast(d.ended?'세션을 종료했습니다':(d.error||'세션이 이미 없습니다'));refreshAll();}
async function musicAction(action,guildId){if(action==='stop'&&!confirm('재생을 정지하고 대기열을 비울까요?'))return;const r=await api('/api/music/'+action,{method:'POST',body:JSON.stringify({guild_id:guildId})});if(!r)return;const d=await r.json();toast(r.ok?(action==='skip'?'스킵했습니다':'정지했습니다'):(d.error||'실패했습니다'));refreshAll();}
async function loadLogs(){try{const r=await api('/api/logs?lines=200');if(!r)return;document.getElementById('log-box').textContent=await r.text()||'(로그 없음)';}catch(e){document.getElementById('log-box').textContent='로그를 불러오지 못했습니다';}}
async function shutdownBot(){const a=prompt('봇을 정말 종료하려면 "종료" 라고 입력하세요.');if(a!=='종료')return;const r=await api('/api/shutdown',{method:'POST'});if(r&&r.ok)toast('종료 신호를 보냈습니다. 잠시 후 봇이 꺼집니다.');}
async function logout(){await fetch('/logout',{method:'POST'});location.href='/login';}

/* 주식 시세 (실시간: 네이버 / 과거: KRX) */
let _stockTimer=null;
function stockNum(v){const n=Number(String(v==null?'':v).replace(/,/g,''));return isFinite(n)?n.toLocaleString('ko-KR'):(v==null||v===''?'-':v);}
function renderStock(d){
  const box=document.getElementById('stock-result');box.className='';box.style.color='';box.replaceChildren();
  const dir=d.direction;const col=dir==='up'?'#ff6b6b':(dir==='down'?'#5aa0ff':'var(--color-neutral-300)');
  const arrow=dir==='up'?'▲':(dir==='down'?'▼':'');
  const rate=d.rate;const rateTxt=(rate==null)?'-':((rate>0?'+':'')+rate+'%');
  const title=el('div');title.style.cssText='display:flex;align-items:center;gap:8px;flex-wrap:wrap';
  const nm=el('strong',(d.name||'-')+' ('+(d.code||'-')+')');nm.style.cssText='font:600 15px var(--font-heading)';title.appendChild(nm);
  const badge=el('span',d.market_label||'');badge.style.cssText='font:500 10px var(--mono);color:var(--color-neutral-500);padding:2px 8px;border-radius:6px;border:1px solid rgba(145,132,217,.2)';title.appendChild(badge);
  if(d.realtime&&d.market_open){const live=el('span','● LIVE');live.style.cssText='font:500 10px var(--mono);color:#4ad9a8;padding:2px 8px;border-radius:6px;border:1px solid rgba(74,217,168,.4)';title.appendChild(live);}
  box.appendChild(title);
  const price=el('div');price.style.cssText='font:600 30px var(--mono);margin:10px 0 4px;color:'+col;
  const chg=d.change;price.textContent=stockNum(d.price)+'원';box.appendChild(price);
  const sub=el('div',(chg!=null&&chg!==0?arrow+' '+stockNum(Math.abs(chg))+'  ':'')+'('+rateTxt+')');sub.style.cssText='font:500 13px var(--mono);color:'+col;box.appendChild(sub);
  const grid=el('div');grid.style.cssText='display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-top:14px';
  const cells=[['시가',d.open,'원'],['고가',d.high,'원'],['저가',d.low,'원'],['거래량',d.volume,'주']];
  if(d.trade_value)cells.push(['거래대금',d.trade_value,'']);
  if(d.marketcap)cells.push(['시가총액',d.marketcap,'']);
  cells.forEach(c=>{const cell=el('div',null,'kcard');cell.style.padding='8px 11px';cell.appendChild(el('div',c[0],'kl'));const val=(typeof c[1]==='string'&&isNaN(Number(String(c[1]).replace(/,/g,''))))?c[1]:stockNum(c[1])+c[2];const vv=el('div',val);vv.style.cssText='font:400 12.5px var(--mono);margin-top:3px';cell.appendChild(vv);grid.appendChild(cell);});
  box.appendChild(grid);
  const foot=el('div',(d.stamp||'')+' · 자료: '+(d.source||''));foot.style.cssText='font:400 11px var(--mono);color:var(--color-neutral-600);margin-top:12px';box.appendChild(foot);
}
async function fetchStock(q,date,isAuto){
  let url='/api/stock?q='+encodeURIComponent(q);if(date)url+='&date='+encodeURIComponent(date);
  const box=document.getElementById('stock-result');
  try{const r=await api(url);if(!r)return;const d=await r.json();
    if(!r.ok||d.error){if(!isAuto){box.className='muted bad';box.style.color='#ff8fa6';box.textContent=d.error||'조회 실패';}return;}
    renderStock(d);
    if(_stockTimer){clearTimeout(_stockTimer);_stockTimer=null;}
    if(d.realtime&&d.market_open)_stockTimer=setTimeout(()=>fetchStock(d.code,'',true),Math.max(3000,d.polling_ms||7000));
  }catch(e){if(!isAuto){box.style.color='#ff8fa6';box.textContent='조회 중 오류: '+e;}}
}
function lookupStock(){if(_stockTimer){clearTimeout(_stockTimer);_stockTimer=null;}const q=document.getElementById('stock-q').value.trim();const date=document.getElementById('stock-date').value.trim();const box=document.getElementById('stock-result');if(!q){box.style.color='';box.className='muted';box.textContent='종목명 또는 코드를 입력하세요.';return;}box.className='muted';box.style.color='';box.textContent='조회 중...';fetchStock(q,date,false);}

/* 사이드바 스크롤 하이라이트 */
(function(){const scroller=document.getElementById('scroller');const links=[...document.querySelectorAll('#side-nav .cc-nav')];const secs=links.map(a=>document.getElementById(a.getAttribute('data-sec'))).filter(Boolean);
scroller.addEventListener('scroll',()=>{const top=scroller.scrollTop+90;let cur=secs[0];for(const s of secs){if(s.offsetTop<=top)cur=s;}links.forEach(a=>a.classList.toggle('on',a.getAttribute('data-sec')===cur.id));});})();

refreshAll();loadModels();loadLogs();setInterval(refreshAll,5000);
</script>
</body>
</html>
"""


class WebAdmin:
    """봇 관리용 로컬 웹 서버."""

    def __init__(self, bot):
        self.bot = bot
        self.started_at = time.time()
        self._tokens: Dict[str, float] = {}
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None
        # 느린 점검(DB ping, Ollama, RVC 스캔)은 캐시해서 5초 자동 새로고침에도 부담이 없도록 한다.
        self._cache: Dict[str, Tuple[float, Any]] = {}
        self._process = psutil.Process(os.getpid()) if psutil else None
        # 대시보드 차트용 지연시간/CPU/메모리 이력 (최근 ~10분, 상태 조회 시마다 수집)
        self._metrics_history: List[dict] = []

        self.app = web.Application(middlewares=[self._auth_middleware])
        self.app.add_routes([
            web.get("/", self.page_dashboard),
            web.get("/login", self.page_login),
            web.post("/login", self.do_login),
            web.post("/logout", self.do_logout),
            web.get("/api/status", self.api_status),
            web.get("/api/stock", self.api_stock),
            web.get("/api/logs", self.api_logs),
            web.get("/api/chat/models", self.api_chat_models),
            web.post("/api/chat/model", self.api_chat_set_model),
            web.post("/api/chat/session/end", self.api_chat_end_session),
            web.post("/api/music/skip", self.api_music_skip),
            web.post("/api/music/stop", self.api_music_stop),
            web.post("/api/shutdown", self.api_shutdown),
        ])

    # ------------------------------------------------------------------ 수명주기
    async def start(self) -> bool:
        """웹 서버를 시작한다. 설정이 없거나 실패하면 False (봇 실행에는 영향 없음)."""
        if not WEB_ADMIN_ENABLED:
            logger.info("웹 관리자 대시보드가 비활성화되어 있습니다 (WEB_ADMIN_ENABLED=false).")
            return False
        if not WEB_ADMIN_PASSWORD:
            logger.warning(
                "WEB_ADMIN_PASSWORD가 설정되지 않아 웹 관리자 대시보드를 시작하지 않습니다. "
                "TOKEN.env에 WEB_ADMIN_PASSWORD를 추가해주세요."
            )
            return False

        try:
            self._runner = web.AppRunner(self.app)
            await self._runner.setup()
            self._site = web.TCPSite(self._runner, WEB_ADMIN_HOST, WEB_ADMIN_PORT)
            await self._site.start()
        except OSError as e:
            logger.error(
                f"웹 관리자 대시보드를 시작하지 못했습니다 (포트 {WEB_ADMIN_PORT} 사용 중인지 확인): {e}"
            )
            await self.stop()
            return False

        port = self.bound_port or WEB_ADMIN_PORT
        logger.info(f"웹 관리자 대시보드 시작: http://{WEB_ADMIN_HOST}:{port}")
        return True

    async def stop(self) -> None:
        if self._runner:
            try:
                await self._runner.cleanup()
            except Exception as e:
                logger.debug(f"웹 관리자 종료 중 오류(무시): {e}")
        self._runner = None
        self._site = None

    @property
    def bound_port(self) -> Optional[int]:
        """실제 바인딩된 포트 (WEB_ADMIN_PORT=0 으로 임시 포트를 쓸 때 사용)."""
        try:
            return self._site._server.sockets[0].getsockname()[1]
        except Exception:
            return None

    # ------------------------------------------------------------------ 인증
    @web.middleware
    async def _auth_middleware(self, request: web.Request, handler):
        if request.path == "/login":
            return await handler(request)

        token = request.cookies.get(COOKIE_NAME, "")
        if token and self._tokens.get(token, 0) > time.time():
            return await handler(request)

        if request.path.startswith("/api/") or request.path == "/logout":
            return web.json_response({"error": "unauthorized"}, status=401)
        raise web.HTTPFound("/login")

    async def page_login(self, request: web.Request) -> web.Response:
        return web.Response(text=LOGIN_HTML, content_type="text/html")

    async def do_login(self, request: web.Request) -> web.Response:
        form = await request.post()
        password = str(form.get("password", ""))
        if not WEB_ADMIN_PASSWORD or not _safe_compare_password(password, WEB_ADMIN_PASSWORD):
            await asyncio.sleep(0.5)  # 무차별 대입 완화
            body = LOGIN_HTML.replace(
                "<!--ERROR-->", '<div class="error">비밀번호가 올바르지 않습니다.</div>'
            )
            return web.Response(text=body, content_type="text/html", status=401)

        # 만료된 토큰 정리 후 새 세션 발급
        now = time.time()
        self._tokens = {t: exp for t, exp in self._tokens.items() if exp > now}
        token = secrets.token_urlsafe(32)
        self._tokens[token] = now + SESSION_TTL_SECONDS

        resp = web.HTTPFound("/")
        resp.set_cookie(
            COOKIE_NAME, token,
            max_age=SESSION_TTL_SECONDS, httponly=True, samesite="Lax", path="/",
        )
        return resp

    async def do_logout(self, request: web.Request) -> web.Response:
        token = request.cookies.get(COOKIE_NAME, "")
        self._tokens.pop(token, None)
        resp = web.json_response({"ok": True})
        resp.del_cookie(COOKIE_NAME, path="/")
        return resp

    # ------------------------------------------------------------------ 페이지
    async def page_dashboard(self, request: web.Request) -> web.Response:
        return web.Response(text=DASHBOARD_HTML, content_type="text/html")

    # ------------------------------------------------------------------ 상태 API
    def _chat_cog(self):
        return self.bot.get_cog("ChatAI")

    def _music_cog(self):
        return self.bot.get_cog("Music")

    async def _cached(self, name: str, ttl: float, factory):
        """느린 점검 결과를 ttl 초 동안 캐시한다. factory 는 코루틴을 반환하는 callable."""
        now = time.monotonic()
        hit = self._cache.get(name)
        if hit and now - hit[0] < ttl:
            return hit[1]
        value = await factory()
        self._cache[name] = (now, value)
        return value

    async def _db_status(self) -> dict:
        info = {
            "use_mysql": USE_MYSQL,
            "backend": "MySQL" if USE_MYSQL else "JSON 파일",
            "host": MYSQL_HOST if USE_MYSQL else None,
            "port": MYSQL_PORT if USE_MYSQL else None,
            "database": MYSQL_DATABASE if USE_MYSQL else None,
            "connected": None,
            "pool": None,
            "error": None,
        }
        if not USE_MYSQL:
            return info

        pool = db_utils._pool
        if pool is None:
            info["connected"] = False
            info["error"] = "연결 풀이 초기화되지 않았습니다."
            return info

        try:
            async def _ping():
                async with pool.acquire() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute("SELECT 1")
                        await cur.fetchone()

            await asyncio.wait_for(_ping(), timeout=3)
            info["connected"] = True
            info["pool"] = {
                "size": pool.size,
                "free": pool.freesize,
                "min": pool.minsize,
                "max": pool.maxsize,
            }
        except Exception as e:
            info["connected"] = False
            info["error"] = str(e)[:200] or type(e).__name__
        return info

    def _check_ollama_sync(self) -> dict:
        """Ollama 서버 연결 상태 점검 (짧은 타임아웃으로 상태 API 지연 방지)."""
        from urllib import request as _request
        endpoint = f"{LOCAL_AI_BASE_URL.rstrip('/')}/api/tags"
        try:
            with _request.urlopen(_request.Request(endpoint, method="GET"), timeout=3) as resp:
                raw = resp.read().decode("utf-8")
            models = [
                m.get("name") for m in (json.loads(raw).get("models") or []) if m.get("name")
            ]
            return {"reachable": True, "installed_models": models, "error": None}
        except Exception as e:
            return {"reachable": False, "installed_models": [], "error": str(e)[:150]}

    def _tts_info_sync(self) -> dict:
        info = {"rvc_models": [], "supertonic_available": None}
        try:
            from utils.rvc_utils import load_rvc_models
            info["rvc_models"] = sorted((load_rvc_models() or {}).keys())
        except Exception as e:
            logger.debug(f"RVC 모델 목록 조회 실패(무시): {e}")
        try:
            from utils.supertonic_utils import check_supertonic_available
            info["supertonic_available"] = bool(check_supertonic_available())
        except Exception as e:
            logger.debug(f"Supertonic 상태 조회 실패(무시): {e}")
        return info

    def _system_info(self) -> dict:
        cpu_percent = None
        memory = None
        process_memory_mb = None
        if psutil:
            try:
                cpu_percent = psutil.cpu_percent(interval=None)
                vm = psutil.virtual_memory()
                memory = {
                    "percent": vm.percent,
                    "used_mb": vm.used // (1024 * 1024),
                    "total_mb": vm.total // (1024 * 1024),
                }
                process_memory_mb = self._process.memory_info().rss // (1024 * 1024)
            except Exception as e:
                logger.debug(f"시스템 리소스 조회 실패(무시): {e}")
        return {
            "os": platform.platform(),
            "python": platform.python_version(),
            "discord_py": discord.__version__,
            "pid": os.getpid(),
            "cpu_percent": cpu_percent,
            "memory": memory,
            "process_memory_mb": process_memory_mb,
            "ffmpeg": FFMPEG_PATH,
        }

    def _music_stats(self) -> dict:
        stats = {"history_items": None, "history_guilds": None, "playlists": None}
        music_cog = self._music_cog()
        if music_cog is None:
            return stats
        try:
            history = getattr(music_cog, "history", {}) or {}
            stats["history_items"] = sum(len(v) for v in history.values())
            stats["history_guilds"] = len(history)
            playlists = getattr(music_cog, "playlists", {}) or {}
            stats["playlists"] = sum(len(v) for v in playlists.values())
        except Exception as e:
            logger.debug(f"음악 통계 조회 실패(무시): {e}")
        return stats

    async def api_status(self, request: web.Request) -> web.Response:
        bot = self.bot

        latency_ms = None
        try:
            if math.isfinite(bot.latency):
                latency_ms = round(bot.latency * 1000)
        except Exception:
            pass

        guilds: List[dict] = []
        for g in getattr(bot, "guilds", []):
            guilds.append({
                "id": str(g.id),
                "name": g.name,
                "member_count": getattr(g, "member_count", None),
                "voice_connected": g.voice_client is not None,
            })

        chat = None
        chat_cog = self._chat_cog()
        if chat_cog is not None:
            sessions = []
            for (gid, cid, uid), s in list(chat_cog._sessions.items()):
                guild = bot.get_guild(gid) if gid else None
                channel = bot.get_channel(cid) if cid else None
                user = guild.get_member(uid) if guild else None
                if user is None:
                    user = bot.get_user(uid)
                sessions.append({
                    "guild_id": gid,
                    "channel_id": cid,
                    "user_id": uid,
                    "guild": getattr(guild, "name", None) or ("DM" if not gid else str(gid)),
                    "channel": getattr(channel, "name", None) or str(cid),
                    "user": getattr(user, "display_name", None) or str(uid),
                    "mode": "음성+텍스트" if s.use_tts else "텍스트",
                    "history_len": len(s.history),
                    "summary_len": len(getattr(s, "summary", "") or ""),
                    "idle_sec": int(time.monotonic() - s.last_active),
                })
            chat = {
                "available": chat_cog._is_ai_available(),
                "configured_model": LOCAL_AI_MODEL,
                "active_model": chat_cog.active_model,
                "base_url": LOCAL_AI_BASE_URL,
                "temperature": LOCAL_AI_TEMPERATURE,
                "sessions": sessions,
            }

        music: List[dict] = []
        music_cog = self._music_cog()
        if music_cog is not None:
            for guild_id, player in list(getattr(music_cog, "players", {}).items()):
                guild = getattr(player, "guild", None) or bot.get_guild(guild_id)
                vc = getattr(guild, "voice_client", None) if guild else None
                try:
                    queue_size = player.queue.qsize()
                except Exception:
                    queue_size = 0
                music.append({
                    "guild_id": str(guild_id),
                    "guild_name": getattr(guild, "name", None) or str(guild_id),
                    "title": getattr(player.current, "title", None) if player.current else None,
                    "queue_size": queue_size,
                    "is_playing": bool(vc and vc.is_playing()),
                    "is_paused": bool(vc and vc.is_paused()),
                })

        # 느린 점검들은 캐시를 사용해 병렬로 수집
        database, ai_server, tts = await asyncio.gather(
            self._cached("db", 15, self._db_status),
            self._cached("ollama", 15, lambda: asyncio.to_thread(self._check_ollama_sync)),
            self._cached("tts", 60, lambda: asyncio.to_thread(self._tts_info_sync)),
        )

        command_count = None
        try:
            command_count = len({c.name for c in bot.get_commands()})
        except Exception:
            pass

        total_members = sum((g.get("member_count") or 0) for g in guilds)

        # 차트용 이력 수집 (여러 뷰어가 있어도 2초 간격으로만 적재, 최근 120개 ≈ 10분 유지)
        system = self._system_info()
        now_ts = int(time.time())
        if not self._metrics_history or now_ts - self._metrics_history[-1]["t"] >= 2:
            self._metrics_history.append({
                "t": now_ts,
                "latency_ms": latency_ms,
                "cpu": system.get("cpu_percent"),
                "mem": (system.get("memory") or {}).get("percent"),
            })
            if len(self._metrics_history) > 120:
                self._metrics_history = self._metrics_history[-120:]

        return web.json_response({
            "bot": {
                "name": str(bot.user) if getattr(bot, "user", None) else None,
                "ready": bool(bot.is_ready()) if hasattr(bot, "is_ready") else False,
                "latency_ms": latency_ms,
                "uptime_sec": int(time.time() - self.started_at),
                "command_count": command_count,
                "voice_connections": len(getattr(bot, "voice_clients", []) or []),
                "total_members": total_members,
            },
            "guilds": guilds,
            "cogs": sorted(getattr(bot, "cogs", {}).keys()),
            "chat": chat,
            "music": music,
            "database": database,
            "ai_server": ai_server,
            "tts": tts,
            "system": system,
            "music_stats": self._music_stats(),
            "metrics_history": self._metrics_history,
        })

    # ------------------------------------------------------------------ 로그 API
    def _tail_log_sync(self, lines: int) -> str:
        log_root = BASE_DIR / "logs"
        if not log_root.exists():
            return ""
        try:
            candidates = sorted(
                log_root.rglob("*.log"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            candidates = []
        if not candidates:
            return ""
        latest = candidates[0]
        try:
            with open(latest, "r", encoding="utf-8", errors="replace") as f:
                content = f.readlines()
        except OSError as e:
            return f"로그 파일을 읽지 못했습니다: {e}"
        header = f"# {latest.name} (마지막 {min(lines, len(content))}줄)\n"
        return header + "".join(content[-lines:])

    async def api_logs(self, request: web.Request) -> web.Response:
        try:
            lines = int(request.query.get("lines", "200"))
        except ValueError:
            lines = 200
        lines = max(10, min(lines, 1000))
        text = await asyncio.to_thread(self._tail_log_sync, lines)
        return web.Response(text=text, content_type="text/plain", charset="utf-8")

    # ------------------------------------------------------------------ 주식 API (실시간: 네이버 / 과거: KRX)
    @staticmethod
    def _to_int(value):
        try:
            return int(str(value).replace(",", "").strip())
        except (TypeError, ValueError):
            return None

    async def api_stock(self, request: web.Request) -> web.Response:
        """?q=종목명|코드 [&date=YYYYMMDD]

        date 없으면 네이버 준실시간 현재가, date 있으면 KRX 일별 종가(과거)를 정규화해 반환.
        """
        query = (request.query.get("q") or "").strip()
        if not query:
            return web.json_response({"error": "종목명 또는 코드(q)가 필요합니다."}, status=400)
        date = (request.query.get("date") or "").strip() or None

        try:
            if date:
                result = await self._stock_historical(query, date)
            else:
                result = await self._stock_realtime(query)
        except (krx_api.KrxApiError, naver_stock.NaverStockError) as e:
            return web.json_response({"error": str(e)}, status=502)
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        except Exception as e:  # noqa: BLE001
            logger.error(f"웹 관리자 주가 조회 오류: {e}", exc_info=True)
            return web.json_response({"error": f"조회 오류: {type(e).__name__}"}, status=500)

        if result is None:
            return web.json_response({"error": f"'{query}' 종목을 찾지 못했습니다."}, status=404)
        return web.json_response(result)

    async def _stock_realtime(self, query: str):
        q = await naver_stock.quote_by_query(query)
        if not q:
            return None
        return {
            "name": q.get("name"), "code": q.get("code"),
            "market_label": q.get("market_label"),
            "price": q.get("price"), "change": q.get("change"), "rate": q.get("rate"),
            "direction": q.get("direction"),
            "open": q.get("open"), "high": q.get("high"), "low": q.get("low"),
            "volume": q.get("volume"), "trade_value": q.get("trade_value") or None,
            "marketcap": None,
            "stamp": (f"실시간 {q.get('time_text')}" if q.get("market_open") else "장마감 · 종가"),
            "realtime": True, "market_open": bool(q.get("market_open")),
            "polling_ms": q.get("polling_ms") or 7000,
            "source": q.get("source") or "네이버 금융",
        }

    async def _stock_historical(self, query: str, date: str):
        if not krx_api.is_configured():
            raise krx_api.KrxApiError(
                "과거 종가 조회(KRX)는 인증키가 필요합니다. TOKEN.env에 KRX_API_KEY를 추가하세요."
            )
        found = await krx_api.search_all_markets(query, bas_dd=date)
        if not found:
            return None
        market, row = found
        rate = row.get("FLUC_RT")
        try:
            rate_val = float(str(rate).replace(",", ""))
        except (TypeError, ValueError):
            rate_val = None
        day = str(date)
        stamp = f"종가 {day[:4]}-{day[4:6]}-{day[6:]}" if len(day) == 8 else f"종가 {day}"
        return {
            "name": row.get("ISU_NM"), "code": row.get("ISU_CD"),
            "market_label": krx_api.MARKET_LABELS.get(market, market),
            "price": self._to_int(row.get("TDD_CLSPRC")),
            "change": self._to_int(row.get("CMPPREVDD_PRC")),
            "rate": rate_val,
            "direction": ("up" if (rate_val or 0) > 0 else "down" if (rate_val or 0) < 0 else "flat"),
            "open": self._to_int(row.get("TDD_OPNPRC")),
            "high": self._to_int(row.get("TDD_HGPRC")),
            "low": self._to_int(row.get("TDD_LWPRC")),
            "volume": self._to_int(row.get("ACC_TRDVOL")),
            "trade_value": (f"{self._to_int(row.get('ACC_TRDVAL')):,}원"
                            if self._to_int(row.get("ACC_TRDVAL")) is not None else None),
            "marketcap": (f"{self._to_int(row.get('MKTCAP')):,}원"
                          if self._to_int(row.get("MKTCAP")) is not None else None),
            "stamp": stamp,
            "realtime": False, "market_open": False, "polling_ms": 0,
            "source": "KRX",
        }

    # ------------------------------------------------------------------ 대화(AI) API
    async def api_chat_models(self, request: web.Request) -> web.Response:
        chat_cog = self._chat_cog()
        if chat_cog is None:
            return web.json_response({"error": "ChatAI Cog가 로드되지 않았습니다."}, status=404)
        models = await asyncio.to_thread(chat_cog._fetch_installed_models_sync)
        return web.json_response({"models": models, "active_model": chat_cog.active_model})

    def _persist_model_to_env_sync(self, model: str, env_path=None) -> None:
        """TOKEN.env 의 LOCAL_AI_MODEL 값을 갱신한다 (없으면 추가). 다른 줄은 건드리지 않는다."""
        env_path = env_path or (BASE_DIR / "TOKEN.env")
        lines: List[str] = []
        if env_path.exists():
            lines = env_path.read_text(encoding="utf-8").splitlines()

        replaced = False
        for i, line in enumerate(lines):
            if line.strip().startswith("LOCAL_AI_MODEL="):
                lines[i] = f"LOCAL_AI_MODEL={model}"
                replaced = True
                break
        if not replaced:
            if lines and lines[-1].strip():
                lines.append("")
            lines.append(f"LOCAL_AI_MODEL={model}")

        atomic_write_text(str(env_path), "\n".join(lines) + "\n")

    async def api_chat_set_model(self, request: web.Request) -> web.Response:
        chat_cog = self._chat_cog()
        if chat_cog is None:
            return web.json_response({"error": "ChatAI Cog가 로드되지 않았습니다."}, status=404)

        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "잘못된 요청 본문입니다."}, status=400)
        model = str(data.get("model", "")).strip()
        persist = bool(data.get("persist", False))
        model_err = _validate_model_name(model)
        if model_err:
            return web.json_response({"error": model_err}, status=400)

        installed = await asyncio.to_thread(chat_cog._fetch_installed_models_sync)
        if installed and model not in installed:
            return web.json_response(
                {"error": f"설치되지 않은 모델입니다: {model}", "installed": installed},
                status=400,
            )
        if persist and not installed:
            return web.json_response(
                {"error": "설치된 모델 목록을 확인할 수 없어 TOKEN.env 저장을 거부했습니다."},
                status=400,
            )

        old = chat_cog.active_model
        chat_cog.active_model = model

        persisted = False
        if persist:
            try:
                await asyncio.to_thread(self._persist_model_to_env_sync, model)
                persisted = True
            except Exception as e:
                logger.error(f"TOKEN.env 모델 저장 실패: {e}", exc_info=True)
                return web.json_response(
                    {"ok": True, "active_model": model, "persisted": False,
                     "error": f"런타임 전환은 됐지만 TOKEN.env 저장에 실패했습니다: {str(e)[:150]}"},
                    status=200,
                )

        logger.info(f"웹 관리자에서 AI 모델 전환: {old} -> {model} (영구 저장: {persisted})")
        return web.json_response({"ok": True, "active_model": model, "persisted": persisted})

    async def api_chat_end_session(self, request: web.Request) -> web.Response:
        chat_cog = self._chat_cog()
        if chat_cog is None:
            return web.json_response({"error": "ChatAI Cog가 로드되지 않았습니다."}, status=404)

        try:
            data = await request.json()
            key = (int(data["guild_id"]), int(data["channel_id"]), int(data["user_id"]))
        except (KeyError, TypeError, ValueError):
            return web.json_response({"error": "guild_id/channel_id/user_id가 필요합니다."}, status=400)

        channel = self.bot.get_channel(key[1]) if key[1] else None
        ended = await chat_cog._end_session(key, channel, reason="manual")
        if ended:
            logger.info(f"웹 관리자에서 대화 세션 종료: {key}")
        return web.json_response({"ended": ended})

    # ------------------------------------------------------------------ 음악 API
    def _get_player(self, data: dict):
        music_cog = self._music_cog()
        if music_cog is None:
            return None, web.json_response({"error": "Music Cog가 로드되지 않았습니다."}, status=404)
        try:
            guild_id = int(data.get("guild_id"))
        except (TypeError, ValueError):
            return None, web.json_response({"error": "guild_id가 필요합니다."}, status=400)
        player = getattr(music_cog, "players", {}).get(guild_id)
        if player is None:
            return None, web.json_response({"error": "해당 서버에 재생 중인 음악이 없습니다."}, status=404)
        return player, None

    async def api_music_skip(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
        except Exception:
            data = {}
        player, err = self._get_player(data)
        if err:
            return err
        guild = getattr(player, "guild", None)
        vc = getattr(guild, "voice_client", None) if guild else None
        if not vc or not (vc.is_playing() or vc.is_paused()):
            return web.json_response({"error": "재생 중인 곡이 없습니다."}, status=409)
        vc.stop()  # player_loop 가 다음 곡으로 진행
        logger.info(f"웹 관리자에서 음악 스킵: guild={getattr(guild, 'id', '?')}")
        return web.json_response({"ok": True})

    async def api_music_stop(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
        except Exception:
            data = {}
        player, err = self._get_player(data)
        if err:
            return err
        try:
            await player.stop()
        except Exception as e:
            logger.error(f"웹 관리자 음악 정지 실패: {e}", exc_info=True)
            return web.json_response({"error": f"정지 실패: {e}"}, status=500)
        logger.info("웹 관리자에서 음악 정지")
        return web.json_response({"ok": True})

    # ------------------------------------------------------------------ 봇 제어 API
    async def api_shutdown(self, request: web.Request) -> web.Response:
        logger.warning("웹 관리자에서 봇 종료 요청을 받았습니다.")
        get_shutdown_event().set()
        return web.json_response({"ok": True})
