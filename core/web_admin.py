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


LOGIN_HTML = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>아리스 봇 관리 - 로그인</title>
<style>
  :root {
    --bg:#05070f; --cyan:#22d3ee; --violet:#8b5cf6; --bad:#ff5470;
    --text:#dbe7ff; --muted:#7f92bb; --mono:Consolas,'Courier New',monospace;
  }
  * { box-sizing:border-box; }
  body { font-family:'Segoe UI','Malgun Gothic',sans-serif; color:var(--text); margin:0;
    min-height:100vh; display:flex; align-items:center; justify-content:center;
    background:
      radial-gradient(900px 480px at 20% -10%, rgba(34,211,238,.16), transparent 60%),
      radial-gradient(800px 480px at 85% 10%, rgba(139,92,246,.15), transparent 55%),
      var(--bg); }
  body::before { content:''; position:fixed; inset:0; pointer-events:none;
    background:
      linear-gradient(rgba(34,211,238,.05) 1px, transparent 1px),
      linear-gradient(90deg, rgba(34,211,238,.05) 1px, transparent 1px);
    background-size:42px 42px;
    mask-image:radial-gradient(ellipse at 50% 30%, black 0%, transparent 75%); }
  .box { position:relative; width:360px; padding:36px 38px; border-radius:16px;
    background:rgba(10,16,32,.78); backdrop-filter:blur(16px);
    border:1px solid rgba(56,189,248,.22);
    box-shadow:0 0 40px rgba(34,211,238,.14), 0 24px 60px rgba(0,0,0,.5);
    animation:rise .5s ease both; }
  .box::before { content:''; position:absolute; top:0; left:24px; right:24px; height:1px;
    background:linear-gradient(90deg, transparent, var(--cyan), var(--violet), transparent); }
  @keyframes rise { from { opacity:0; transform:translateY(12px); } to { opacity:1; transform:none; } }
  h1 { font-size:17px; margin:0 0 4px; letter-spacing:2px; font-weight:700;
    background:linear-gradient(90deg, var(--cyan), var(--violet));
    -webkit-background-clip:text; background-clip:text; color:transparent; }
  .deck { font-family:var(--mono); font-size:10px; letter-spacing:4px; color:var(--muted); margin:0 0 18px; }
  p.sub { margin:0 0 22px; font-size:13px; color:var(--muted); line-height:1.6; }
  input[type=password] { width:100%; padding:11px 14px; border-radius:10px; font-size:14px;
    border:1px solid rgba(56,189,248,.3); background:rgba(4,8,18,.85); color:var(--text);
    font-family:var(--mono); letter-spacing:2px; transition:all .2s; }
  input[type=password]:focus { outline:none; border-color:var(--cyan);
    box-shadow:0 0 14px rgba(34,211,238,.35); }
  button { width:100%; margin-top:16px; padding:11px; border:0; border-radius:10px;
    background:linear-gradient(90deg, rgba(34,211,238,.9), rgba(139,92,246,.9));
    color:#04101c; font-size:14px; font-weight:700; letter-spacing:3px; cursor:pointer;
    transition:all .2s; }
  button:hover { box-shadow:0 0 22px rgba(99,161,255,.55); filter:brightness(1.12); }
  .error { color:var(--bad); font-size:13px; margin-top:14px;
    text-shadow:0 0 10px rgba(255,84,112,.4); }
</style>
</head>
<body>
  <div class="box">
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
<style>
  :root {
    --bg:#05070f; --panel:rgba(13,20,38,.72); --line:rgba(56,189,248,.16);
    --cyan:#22d3ee; --violet:#8b5cf6; --ok:#2dd4a7; --bad:#ff5470;
    --text:#dbe7ff; --muted:#7f92bb; --mono:Consolas,'Courier New',monospace;
  }
  * { box-sizing:border-box; }
  body { margin:0; color:var(--text); font-family:'Segoe UI','Malgun Gothic',sans-serif;
    background:
      radial-gradient(1100px 520px at 15% -10%, rgba(34,211,238,.14), transparent 60%),
      radial-gradient(900px 500px at 85% 0%, rgba(139,92,246,.13), transparent 55%),
      radial-gradient(700px 700px at 50% 120%, rgba(34,211,238,.06), transparent 60%),
      var(--bg);
    min-height:100vh; }
  body::before { content:''; position:fixed; inset:0; pointer-events:none; z-index:0;
    background:
      linear-gradient(rgba(34,211,238,.045) 1px, transparent 1px),
      linear-gradient(90deg, rgba(34,211,238,.045) 1px, transparent 1px);
    background-size:42px 42px;
    mask-image:radial-gradient(ellipse at 50% 0%, black 0%, transparent 78%); }
  header { position:sticky; top:0; z-index:10; display:flex; align-items:center; justify-content:space-between;
    padding:13px 26px; background:rgba(7,11,22,.85); backdrop-filter:blur(14px);
    border-bottom:1px solid var(--line); box-shadow:0 0 24px rgba(34,211,238,.10); }
  header h1 { font-size:15px; margin:0; letter-spacing:2px; font-weight:700;
    background:linear-gradient(90deg, var(--cyan), var(--violet));
    -webkit-background-clip:text; background-clip:text; color:transparent;
    -webkit-text-fill-color:transparent; }
  header h1 .sub { font-size:9px; letter-spacing:4px; color:var(--muted);
    -webkit-text-fill-color:var(--muted); margin-left:12px; font-family:var(--mono); }
  header .right { display:flex; gap:8px; align-items:center; }
  #refresh-time { font-family:var(--mono); font-size:11px; }
  main { max-width:1120px; margin:0 auto; padding:22px 18px 70px; position:relative; z-index:1; }
  section { background:var(--panel); backdrop-filter:blur(10px); border:1px solid var(--line);
    border-radius:14px; padding:20px 22px; margin-top:20px; position:relative;
    box-shadow:0 10px 30px rgba(0,0,0,.35); animation:rise .4s ease both; }
  section::before { content:''; position:absolute; top:0; left:20px; right:20px; height:1px;
    background:linear-gradient(90deg, transparent, var(--cyan), var(--violet), transparent); opacity:.55; }
  @keyframes rise { from { opacity:0; transform:translateY(8px); } to { opacity:1; transform:none; } }
  section h2 { font-size:12px; margin:0 0 14px; letter-spacing:2.5px; text-transform:uppercase;
    color:var(--cyan); display:flex; align-items:center; gap:8px;
    text-shadow:0 0 12px rgba(34,211,238,.45); }
  section h2::before { content:'▸'; color:var(--violet); text-shadow:0 0 10px rgba(139,92,246,.7); }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  th { text-align:left; padding:8px; font-size:10px; letter-spacing:1.5px; text-transform:uppercase;
    color:var(--muted); border-bottom:1px solid var(--line); font-weight:normal; }
  td { text-align:left; padding:8px; border-bottom:1px solid rgba(56,189,248,.07); }
  tbody tr { transition:background .15s; }
  tbody tr:hover { background:rgba(34,211,238,.05); }
  .kv { display:grid; grid-template-columns:repeat(auto-fit, minmax(172px, 1fr)); gap:10px; }
  .kv .item { background:rgba(5,9,20,.65); border:1px solid rgba(56,189,248,.10);
    border-left:2px solid var(--cyan); border-radius:10px; padding:10px 12px;
    transition:border-color .2s, box-shadow .2s; }
  .kv .item:hover { border-color:rgba(34,211,238,.45); box-shadow:inset 0 0 14px rgba(34,211,238,.10); }
  .kv .label { font-size:10px; letter-spacing:1.5px; text-transform:uppercase; color:var(--muted); }
  .kv .value { font-size:13.5px; margin-top:5px; word-break:break-all; font-family:var(--mono); }
  button { border:1px solid rgba(34,211,238,.35); background:rgba(34,211,238,.06); color:var(--cyan);
    border-radius:8px; padding:6px 14px; font-size:12px; letter-spacing:1px; cursor:pointer;
    transition:all .18s; font-family:inherit; }
  button:hover { background:rgba(34,211,238,.16); box-shadow:0 0 14px rgba(34,211,238,.35); }
  button.primary { background:linear-gradient(90deg, rgba(34,211,238,.9), rgba(139,92,246,.9));
    color:#04101c; border:0; font-weight:700; }
  button.primary:hover { box-shadow:0 0 18px rgba(99,161,255,.5); filter:brightness(1.1); }
  button.danger { border-color:rgba(255,84,112,.5); background:rgba(255,84,112,.08); color:var(--bad); }
  button.danger:hover { background:rgba(255,84,112,.2); box-shadow:0 0 16px rgba(255,84,112,.4); }
  select { background:rgba(5,9,20,.85); color:var(--text); border:1px solid rgba(56,189,248,.3);
    border-radius:8px; padding:6px 10px; font-family:var(--mono); }
  select:focus { outline:none; border-color:var(--cyan); box-shadow:0 0 10px rgba(34,211,238,.3); }
  input[type=checkbox] { accent-color:var(--cyan); }
  label { user-select:none; }
  pre { background:rgba(3,6,14,.9); border:1px solid rgba(56,189,248,.14); border-radius:10px;
    padding:14px; font-size:12px; line-height:1.55; max-height:400px; overflow:auto;
    white-space:pre-wrap; word-break:break-all; font-family:var(--mono); color:#9fd8c8; }
  ::-webkit-scrollbar { width:10px; height:10px; }
  ::-webkit-scrollbar-thumb { background:rgba(34,211,238,.25); border-radius:6px; }
  ::-webkit-scrollbar-thumb:hover { background:rgba(34,211,238,.45); }
  ::-webkit-scrollbar-track { background:transparent; }
  .muted { color:var(--muted); font-size:12.5px; }
  .ok { color:var(--ok); text-shadow:0 0 10px rgba(45,212,167,.55); }
  .bad { color:var(--bad); text-shadow:0 0 10px rgba(255,84,112,.5); }
  #toast { position:fixed; bottom:24px; left:50%; transform:translateX(-50%); z-index:50;
    background:rgba(7,12,24,.92); border:1px solid rgba(34,211,238,.45); color:var(--text);
    padding:11px 20px; border-radius:10px; display:none; font-size:13px;
    box-shadow:0 0 24px rgba(34,211,238,.3); backdrop-filter:blur(8px); }
  /* ---- 라이브 모니터 (홀로그램) ---- */
  #monitor { overflow:hidden; }
  #monitor::after { content:''; position:absolute; top:0; bottom:0; width:140px; left:-160px;
    background:linear-gradient(90deg, transparent, rgba(34,211,238,.06), transparent);
    animation:scan 7s linear infinite; pointer-events:none; }
  @keyframes scan { to { left:115%; } }
  .mon-grid { display:grid; grid-template-columns:repeat(auto-fit, minmax(150px, 1fr)); gap:12px; }
  .holo-card { position:relative; background:rgba(5,9,20,.6); border:1px solid rgba(56,189,248,.14);
    border-radius:12px; padding:14px 10px 10px; text-align:center; }
  .holo-card::before, .holo-card::after { content:''; position:absolute; width:11px; height:11px; }
  .holo-card::before { top:5px; left:5px; border-top:1px solid var(--cyan); border-left:1px solid var(--cyan);
    filter:drop-shadow(0 0 3px rgba(34,211,238,.8)); }
  .holo-card::after { bottom:5px; right:5px; border-bottom:1px solid var(--cyan); border-right:1px solid var(--cyan);
    filter:drop-shadow(0 0 3px rgba(34,211,238,.8)); }
  .holo-card svg { width:96px; height:96px; }
  .g-label { font-size:10px; letter-spacing:2.5px; text-transform:uppercase; color:var(--muted);
    margin-top:2px; font-family:var(--mono); }
  .stat-num { font-size:44px; line-height:96px; font-family:var(--mono); color:var(--text);
    text-shadow:0 0 18px rgba(34,211,238,.55); }
  .radar { width:84px; height:84px; margin:6px auto; border-radius:50%; position:relative; overflow:hidden;
    border:1px solid rgba(34,211,238,.35); box-shadow:0 0 18px rgba(34,211,238,.18), inset 0 0 18px rgba(34,211,238,.08);
    background:
      repeating-radial-gradient(circle at 50% 50%, rgba(34,211,238,.14) 0 1px, transparent 1px 14px),
      linear-gradient(rgba(34,211,238,.10) 1px, transparent 1px) 50% 50% / 100% 50.5%,
      radial-gradient(circle, rgba(34,211,238,.12), transparent 70%); }
  .radar .sweep { position:absolute; inset:0; border-radius:50%;
    background:conic-gradient(from 0deg, rgba(34,211,238,.6), rgba(34,211,238,.12) 70deg, transparent 90deg);
    animation:spin 3.2s linear infinite; }
  @keyframes spin { to { transform:rotate(360deg); } }
  .radar.offline { border-color:rgba(255,84,112,.4); }
  .radar.offline .sweep { background:conic-gradient(from 0deg, rgba(255,84,112,.55), rgba(255,84,112,.1) 70deg, transparent 90deg);
    animation-duration:6.5s; }
  .chart-block { margin-top:14px; }
  .chart-head { display:flex; justify-content:space-between; align-items:center; margin-bottom:6px; }
  .chart-title { font-size:11px; letter-spacing:1.5px; text-transform:uppercase; color:var(--muted); }
  .legend { display:inline-flex; align-items:center; gap:6px; font-size:11px; color:var(--muted); }
  .chip { display:inline-block; width:10px; height:10px; border-radius:3px; margin-left:10px; }
  .chip.c1 { background:#0e9db8; } .chip.c2 { background:#8b5cf6; }
  .chart-wrap { position:relative; background:rgba(3,6,14,.55); border:1px solid rgba(56,189,248,.10);
    border-radius:10px; padding:6px 4px 2px; }
  .chart-wrap svg { display:block; width:100%; height:auto; }
  .tooltip { position:absolute; display:none; pointer-events:none; z-index:5; min-width:120px;
    background:rgba(7,12,24,.94); border:1px solid rgba(34,211,238,.4); border-radius:8px;
    padding:8px 10px; font-size:11.5px; font-family:var(--mono);
    box-shadow:0 0 16px rgba(34,211,238,.25); }
  .tooltip .tt-time { color:var(--muted); margin-bottom:4px; }
  .tooltip .tt-row { display:flex; align-items:center; gap:6px; margin-top:2px; }
</style>
</head>
<body>
<header>
  <h1>텐도 아리스 봇 관리<span class="sub">ARIS CONTROL DECK</span></h1>
  <div class="right">
    <span id="refresh-time" class="muted"></span>
    <button onclick="refreshAll()">새로고침</button>
    <button onclick="logout()">로그아웃</button>
  </div>
</header>
<main>
  <section id="monitor">
    <h2>라이브 모니터</h2>
    <div class="mon-grid">
      <div class="holo-card">
        <svg id="gauge-cpu" viewBox="0 0 120 120"></svg>
        <div class="g-label">CPU</div>
      </div>
      <div class="holo-card">
        <svg id="gauge-mem" viewBox="0 0 120 120"></svg>
        <div class="g-label">메모리</div>
      </div>
      <div class="holo-card">
        <div class="stat-num" id="stat-sessions">-</div>
        <div class="g-label">활성 대화 세션</div>
      </div>
      <div class="holo-card">
        <div class="radar" id="radar"><div class="sweep"></div></div>
        <div class="g-label" id="radar-label">SYSTEM</div>
      </div>
    </div>
    <div class="chart-block">
      <div class="chart-head">
        <span class="chart-title">지연시간 (ms) · 최근 10분</span>
        <span class="muted" id="lat-now"></span>
      </div>
      <div class="chart-wrap">
        <svg id="chart-latency"></svg>
        <div class="tooltip" id="tt-latency"></div>
      </div>
    </div>
    <div class="chart-block">
      <div class="chart-head">
        <span class="chart-title">CPU / 메모리 사용률 (%) · 최근 10분</span>
        <span class="legend"><span class="chip c1"></span>CPU<span class="chip c2"></span>메모리</span>
      </div>
      <div class="chart-wrap">
        <svg id="chart-sys"></svg>
        <div class="tooltip" id="tt-sys"></div>
      </div>
    </div>
  </section>

  <section>
    <h2>봇 상태</h2>
    <div class="kv" id="bot-kv"></div>
  </section>

  <section>
    <h2>시스템</h2>
    <div class="kv" id="sys-kv"></div>
  </section>

  <section>
    <h2>데이터베이스</h2>
    <div class="kv" id="db-kv"></div>
  </section>

  <section>
    <h2>서버 목록</h2>
    <table>
      <thead><tr><th>서버</th><th>멤버 수</th><th>음성 연결</th></tr></thead>
      <tbody id="guild-rows"></tbody>
    </table>
    <div id="guild-empty" class="muted" style="display:none">참여 중인 서버가 없습니다.</div>
  </section>

  <section>
    <h2>아리스 대화 (AI)</h2>
    <div class="kv" id="chat-kv"></div>
    <div style="margin:14px 0 6px">
      <span class="muted">모델 전환:</span>
      <select id="model-select"></select>
      <label class="muted" style="margin:0 4px">
        <input type="checkbox" id="model-persist" checked> 재시작 후에도 유지 (TOKEN.env 저장)
      </label>
      <button class="primary" onclick="applyModel()">적용</button>
    </div>
    <table>
      <thead><tr><th>서버</th><th>채널</th><th>사용자</th><th>모드</th><th>대화 수</th><th>유휴</th><th></th></tr></thead>
      <tbody id="session-rows"></tbody>
    </table>
    <div id="session-empty" class="muted" style="display:none">진행 중인 대화 세션이 없습니다.</div>
  </section>

  <section>
    <h2>TTS / 음성 모델</h2>
    <div class="kv" id="tts-kv"></div>
  </section>

  <section>
    <h2>음악</h2>
    <div class="kv" id="music-kv" style="margin-bottom:12px"></div>
    <table>
      <thead><tr><th>서버</th><th>재생 중</th><th>대기열</th><th>상태</th><th></th></tr></thead>
      <tbody id="music-rows"></tbody>
    </table>
    <div id="music-empty" class="muted" style="display:none">재생 중인 음악이 없습니다.</div>
  </section>

  <section>
    <h2>로그 <button onclick="loadLogs()" style="margin-left:8px">갱신</button></h2>
    <pre id="log-box">불러오는 중...</pre>
  </section>

  <section>
    <h2>위험 구역</h2>
    <p class="muted">봇 프로세스를 안전하게 종료합니다. 다시 시작하려면 서버에서 직접 실행해야 합니다.</p>
    <button class="danger" onclick="shutdownBot()">봇 종료</button>
  </section>
</main>
<div id="toast"></div>

<script>
function toast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg; t.style.display = 'block';
  clearTimeout(t._timer); t._timer = setTimeout(() => t.style.display = 'none', 3000);
}
async function api(path, opts) {
  const r = await fetch(path, Object.assign({headers: {'Content-Type': 'application/json'}}, opts || {}));
  if (r.status === 401) { location.href = '/login'; return null; }
  return r;
}
function el(tag, text, cls) {
  const e = document.createElement(tag);
  if (text !== undefined && text !== null) e.textContent = text;
  if (cls) e.className = cls;
  return e;
}
function kvItem(label, value, cls) {
  const item = el('div', null, 'item');
  item.appendChild(el('div', label, 'label'));
  const v = el('div', value, 'value' + (cls ? ' ' + cls : ''));
  item.appendChild(v);
  return item;
}
function fmtUptime(sec) {
  sec = Math.floor(sec);
  const d = Math.floor(sec / 86400), h = Math.floor(sec % 86400 / 3600),
        m = Math.floor(sec % 3600 / 60), s = sec % 60;
  return (d ? d + '일 ' : '') + (h ? h + '시간 ' : '') + (m ? m + '분 ' : '') + s + '초';
}

/* ---- 라이브 모니터: 게이지 & 차트 (외부 라이브러리 없이 SVG) ---- */
const SVG_NS = 'http://www.w3.org/2000/svg';
const CHART_C = { c1: '#0e9db8', c2: '#8b5cf6' };  // 검증된 시리즈 팔레트

function svgEl(tag, attrs, parent) {
  const e = document.createElementNS(SVG_NS, tag);
  for (const k in attrs) e.setAttribute(k, attrs[k]);
  if (parent) parent.appendChild(e);
  return e;
}
function polar(cx, cy, r, deg) {
  const a = (deg - 90) * Math.PI / 180;
  return [cx + r * Math.cos(a), cy + r * Math.sin(a)];
}
function arcPath(cx, cy, r, a0, a1) {
  const [x0, y0] = polar(cx, cy, r, a0), [x1, y1] = polar(cx, cy, r, a1);
  return 'M ' + x0 + ' ' + y0 + ' A ' + r + ' ' + r + ' 0 ' + ((a1 - a0) > 180 ? 1 : 0) + ' 1 ' + x1 + ' ' + y1;
}
function renderGauge(id, pct, color) {
  const svg = document.getElementById(id);
  svg.replaceChildren();
  const has = pct !== null && pct !== undefined;
  const v = has ? Math.max(0, Math.min(100, pct)) : 0;
  svgEl('path', {d: arcPath(60, 60, 46, -120, 120), fill: 'none',
    stroke: 'rgba(56,189,248,.14)', 'stroke-width': 10, 'stroke-linecap': 'round'}, svg);
  if (has && v > 0.5) {
    const p = svgEl('path', {d: arcPath(60, 60, 46, -120, -120 + 240 * v / 100), fill: 'none',
      stroke: color, 'stroke-width': 10, 'stroke-linecap': 'round'}, svg);
    p.style.filter = 'drop-shadow(0 0 5px ' + color + ')';
    p.style.transition = 'd .5s';
  }
  const t = svgEl('text', {x: 60, y: 67, 'text-anchor': 'middle', 'font-size': 21,
    'font-family': 'Consolas,monospace', fill: '#dbe7ff'}, svg);
  t.textContent = has ? Math.round(v) + '%' : '-';
}
function fmtTime(ts) {
  return new Date(ts * 1000).toLocaleTimeString('ko-KR', {hour12: false});
}
function drawChart(svgId, ttId, pts, series, opts) {
  opts = opts || {};
  const svg = document.getElementById(svgId);
  const W = 760, H = 168, L = 46, R = 14, T = 12, B = 22;
  svg.setAttribute('viewBox', '0 0 ' + W + ' ' + H);
  svg.replaceChildren();
  if (pts.length < 2) {
    const t = svgEl('text', {x: W / 2, y: H / 2, 'text-anchor': 'middle', 'font-size': 13,
      fill: '#7f92bb'}, svg);
    t.textContent = '데이터 수집 중... (5초마다 갱신)';
    return;
  }
  let yMax = opts.yMax;
  if (!yMax) {
    yMax = 10;
    for (const p of pts) for (const s of series) {
      if (p[s.key] != null && p[s.key] > yMax) yMax = p[s.key];
    }
    yMax = Math.ceil(yMax * 1.2 / 10) * 10;
  }
  const x = i => L + (W - L - R) * i / (pts.length - 1);
  const y = v => T + (H - T - B) * (1 - v / yMax);
  // 눈금 (0 / 중간 / 최대)
  for (const gv of [0, yMax / 2, yMax]) {
    svgEl('line', {x1: L, y1: y(gv), x2: W - R, y2: y(gv),
      stroke: 'rgba(56,189,248,.12)', 'stroke-dasharray': '3 4'}, svg);
    const lb = svgEl('text', {x: L - 6, y: y(gv) + 3.5, 'text-anchor': 'end', 'font-size': 10,
      'font-family': 'Consolas,monospace', fill: '#7f92bb'}, svg);
    lb.textContent = Math.round(gv);
  }
  // 시간 라벨 (처음/끝)
  const tl = svgEl('text', {x: L, y: H - 6, 'font-size': 10, 'font-family': 'Consolas,monospace', fill: '#7f92bb'}, svg);
  tl.textContent = fmtTime(pts[0].t);
  const tr = svgEl('text', {x: W - R, y: H - 6, 'text-anchor': 'end', 'font-size': 10,
    'font-family': 'Consolas,monospace', fill: '#7f92bb'}, svg);
  tr.textContent = fmtTime(pts[pts.length - 1].t);
  // 시리즈
  series.forEach((s, si) => {
    let d = '', last = null;
    pts.forEach((p, i) => {
      const v = p[s.key];
      if (v == null) return;
      d += (d ? ' L ' : 'M ') + x(i).toFixed(1) + ' ' + y(v).toFixed(1);
      last = {i, v};
    });
    if (!d) return;
    if (opts.area && si === 0) {
      const gid = svgId + '-grad';
      const grad = svgEl('linearGradient', {id: gid, x1: 0, y1: 0, x2: 0, y2: 1},
        svgEl('defs', {}, svg));
      svgEl('stop', {offset: '0%', 'stop-color': s.color, 'stop-opacity': .28}, grad);
      svgEl('stop', {offset: '100%', 'stop-color': s.color, 'stop-opacity': 0}, grad);
      svgEl('path', {d: d + ' L ' + x(last.i).toFixed(1) + ' ' + y(0) + ' L ' + L + ' ' + y(0) + ' Z',
        fill: 'url(#' + gid + ')', stroke: 'none'}, svg);
    }
    const line = svgEl('path', {d, fill: 'none', stroke: s.color, 'stroke-width': 2,
      'stroke-linejoin': 'round', 'stroke-linecap': 'round'}, svg);
    line.style.filter = 'drop-shadow(0 0 4px ' + s.color + '99)';
    if (last) {
      svgEl('circle', {cx: x(last.i), cy: y(last.v), r: 3.5, fill: s.color,
        stroke: 'rgba(3,6,14,1)', 'stroke-width': 2}, svg);
      const vl = svgEl('text', {x: Math.min(x(last.i) + 6, W - R), y: y(last.v) - 6,
        'font-size': 11, 'font-family': 'Consolas,monospace', fill: '#dbe7ff'}, svg);
      vl.textContent = Math.round(last.v);
    }
  });
  // 크로스헤어 + 툴팁
  const cross = svgEl('line', {x1: 0, y1: T, x2: 0, y2: H - B, stroke: 'rgba(219,231,255,.35)',
    'stroke-dasharray': '3 3', visibility: 'hidden'}, svg);
  const overlay = svgEl('rect', {x: L, y: T, width: W - L - R, height: H - T - B,
    fill: 'transparent'}, svg);
  const tt = document.getElementById(ttId);
  overlay.addEventListener('mousemove', ev => {
    const rect = svg.getBoundingClientRect();
    const mx = (ev.clientX - rect.left) * W / rect.width;
    const idx = Math.round((mx - L) / (W - L - R) * (pts.length - 1));
    if (idx < 0 || idx >= pts.length) return;
    const p = pts[idx];
    cross.setAttribute('x1', x(idx)); cross.setAttribute('x2', x(idx));
    cross.setAttribute('visibility', 'visible');
    tt.replaceChildren();
    const tm = document.createElement('div'); tm.className = 'tt-time';
    tm.textContent = fmtTime(p.t); tt.appendChild(tm);
    for (const s of series) {
      if (p[s.key] == null) continue;
      const row = document.createElement('div'); row.className = 'tt-row';
      const chip = document.createElement('span'); chip.className = 'chip';
      chip.style.background = s.color; chip.style.margin = '0';
      row.appendChild(chip);
      row.appendChild(document.createTextNode(s.label + ' ' + Math.round(p[s.key]) + (opts.unit || '')));
      tt.appendChild(row);
    }
    const wrap = tt.parentElement.getBoundingClientRect();
    let left = (ev.clientX - wrap.left) + 14;
    if (left > wrap.width - 150) left -= 170;
    tt.style.left = left + 'px';
    tt.style.top = '10px';
    tt.style.display = 'block';
  });
  overlay.addEventListener('mouseleave', () => {
    tt.style.display = 'none';
    cross.setAttribute('visibility', 'hidden');
  });
}
function renderMonitor(d) {
  const hist = d.metrics_history || [];
  const sys = d.system || {};
  renderGauge('gauge-cpu', sys.cpu_percent, CHART_C.c1);
  renderGauge('gauge-mem', sys.memory ? sys.memory.percent : null, CHART_C.c2);
  document.getElementById('stat-sessions').textContent = d.chat ? String(d.chat.sessions.length) : '-';
  const radar = document.getElementById('radar');
  radar.classList.toggle('offline', !d.bot.ready);
  const rl = document.getElementById('radar-label');
  rl.textContent = d.bot.ready ? 'SYSTEM ONLINE' : 'SYSTEM OFFLINE';
  rl.className = 'g-label ' + (d.bot.ready ? 'ok' : 'bad');

  const latPts = hist.filter(p => p.latency_ms != null);
  drawChart('chart-latency', 'tt-latency', latPts,
    [{key: 'latency_ms', label: '지연시간', color: CHART_C.c1}], {area: true, unit: 'ms'});
  document.getElementById('lat-now').textContent =
    latPts.length ? '현재 ' + latPts[latPts.length - 1].latency_ms + 'ms' : '';
  const sysPts = hist.filter(p => p.cpu != null || p.mem != null);
  drawChart('chart-sys', 'tt-sys', sysPts,
    [{key: 'cpu', label: 'CPU', color: CHART_C.c1}, {key: 'mem', label: '메모리', color: CHART_C.c2}],
    {yMax: 100, unit: '%'});
}

function render(d) {
  const bk = document.getElementById('bot-kv');
  bk.replaceChildren(
    kvItem('봇', d.bot.name || '(로그인 전)'),
    kvItem('상태', d.bot.ready ? '온라인' : '준비 중', d.bot.ready ? 'ok' : 'bad'),
    kvItem('지연시간', d.bot.latency_ms === null ? '-' : d.bot.latency_ms + 'ms'),
    kvItem('업타임', fmtUptime(d.bot.uptime_sec)),
    kvItem('서버 수', String(d.guilds.length)),
    kvItem('총 멤버(캐시)', String(d.bot.total_members ?? '-')),
    kvItem('명령어 수', d.bot.command_count === null ? '-' : String(d.bot.command_count)),
    kvItem('음성 연결', String(d.bot.voice_connections ?? 0)),
    kvItem('로드된 Cog', d.cogs.join(', ') || '-')
  );

  const sk = document.getElementById('sys-kv');
  const sys = d.system || {};
  sk.replaceChildren(
    kvItem('OS', sys.os || '-'),
    kvItem('Python', sys.python || '-'),
    kvItem('discord.py', sys.discord_py || '-'),
    kvItem('CPU 사용률', sys.cpu_percent == null ? '-' : sys.cpu_percent + '%'),
    kvItem('메모리', sys.memory
      ? sys.memory.percent + '% (' + sys.memory.used_mb.toLocaleString() + ' / ' + sys.memory.total_mb.toLocaleString() + ' MB)'
      : '-'),
    kvItem('봇 프로세스 메모리', sys.process_memory_mb == null ? '-' : sys.process_memory_mb.toLocaleString() + ' MB'),
    kvItem('PID', String(sys.pid ?? '-')),
    kvItem('FFmpeg', sys.ffmpeg || '없음', sys.ffmpeg ? 'ok' : 'bad')
  );

  const dbk = document.getElementById('db-kv');
  const db = d.database || {};
  const dbItems = [kvItem('백엔드', db.backend || '-')];
  if (db.use_mysql) {
    dbItems.push(kvItem('연결 상태', db.connected ? '정상' : '연결 안 됨', db.connected ? 'ok' : 'bad'));
    dbItems.push(kvItem('서버', (db.host || '-') + ':' + (db.port ?? '-')));
    dbItems.push(kvItem('데이터베이스', db.database || '-'));
    dbItems.push(kvItem('연결 풀', db.pool
      ? '사용 ' + (db.pool.size - db.pool.free) + ' · 유휴 ' + db.pool.free + ' · 최대 ' + db.pool.max
      : '-'));
    if (db.error) dbItems.push(kvItem('오류', db.error, 'bad'));
  } else {
    dbItems.push(kvItem('연결 상태', 'JSON 파일 저장 사용 중'));
  }
  dbk.replaceChildren(...dbItems);

  const tk = document.getElementById('tts-kv');
  const tts = d.tts || {};
  const rvc = tts.rvc_models || [];
  tk.replaceChildren(
    kvItem('RVC 모델 (' + rvc.length + '개)', rvc.join(', ') || '없음'),
    kvItem('Supertonic', tts.supertonic_available == null ? '-'
      : (tts.supertonic_available ? '사용 가능' : '사용 불가'),
      tts.supertonic_available ? 'ok' : '')
  );

  const mk = document.getElementById('music-kv');
  const ms = d.music_stats || {};
  mk.replaceChildren(
    kvItem('재생 히스토리', ms.history_items == null ? '-' : ms.history_items + '곡'),
    kvItem('히스토리 보유 서버', String(ms.history_guilds ?? '-')),
    kvItem('플레이리스트', ms.playlists == null ? '-' : ms.playlists + '개')
  );

  const gr = document.getElementById('guild-rows');
  gr.replaceChildren();
  d.guilds.forEach(g => {
    const tr = el('tr');
    tr.appendChild(el('td', g.name));
    tr.appendChild(el('td', g.member_count === null ? '-' : String(g.member_count)));
    tr.appendChild(el('td', g.voice_connected ? '연결됨' : '-', g.voice_connected ? 'ok' : ''));
    gr.appendChild(tr);
  });
  document.getElementById('guild-empty').style.display = d.guilds.length ? 'none' : 'block';

  const ck = document.getElementById('chat-kv');
  const ai = d.ai_server || {};
  if (d.chat) {
    const chatItems = [
      kvItem('사용 가능', d.chat.available ? '예' : '아니오', d.chat.available ? 'ok' : 'bad'),
      kvItem('Ollama 연결', ai.reachable == null ? '-' : (ai.reachable ? '정상' : '연결 안 됨'),
             ai.reachable ? 'ok' : 'bad'),
      kvItem('현재 모델', d.chat.active_model),
      kvItem('설정 모델', d.chat.configured_model),
      kvItem('설치된 모델 수', String((ai.installed_models || []).length)),
      kvItem('온도', String(d.chat.temperature)),
      kvItem('Ollama 서버', d.chat.base_url),
      kvItem('활성 세션', String(d.chat.sessions.length))
    ];
    if (!ai.reachable && ai.error) chatItems.push(kvItem('Ollama 오류', ai.error, 'bad'));
    ck.replaceChildren(...chatItems);
  } else {
    ck.replaceChildren(kvItem('상태', 'ChatAI Cog가 로드되지 않았습니다', 'bad'));
  }

  const sr = document.getElementById('session-rows');
  sr.replaceChildren();
  (d.chat ? d.chat.sessions : []).forEach(s => {
    const tr = el('tr');
    tr.appendChild(el('td', s.guild));
    tr.appendChild(el('td', s.channel));
    tr.appendChild(el('td', s.user));
    tr.appendChild(el('td', s.mode));
    tr.appendChild(el('td', String(s.history_len) + (s.summary_len ? ' (+요약)' : '')));
    tr.appendChild(el('td', s.idle_sec + '초 전'));
    const td = el('td');
    const btn = el('button', '종료', 'danger');
    btn.onclick = () => endSession(s);
    td.appendChild(btn);
    tr.appendChild(td);
    sr.appendChild(tr);
  });
  const sCount = d.chat ? d.chat.sessions.length : 0;
  document.getElementById('session-empty').style.display = sCount ? 'none' : 'block';

  const mr = document.getElementById('music-rows');
  mr.replaceChildren();
  d.music.forEach(m => {
    const tr = el('tr');
    tr.appendChild(el('td', m.guild_name));
    tr.appendChild(el('td', m.title || '-'));
    tr.appendChild(el('td', String(m.queue_size)));
    tr.appendChild(el('td', m.is_paused ? '일시정지' : (m.is_playing ? '재생 중' : '대기')));
    const td = el('td');
    const skip = el('button', '스킵'); skip.onclick = () => musicAction('skip', m.guild_id);
    const stop = el('button', '정지', 'danger'); stop.onclick = () => musicAction('stop', m.guild_id);
    td.appendChild(skip); td.appendChild(document.createTextNode(' ')); td.appendChild(stop);
    tr.appendChild(td);
    mr.appendChild(tr);
  });
  document.getElementById('music-empty').style.display = d.music.length ? 'none' : 'block';

  renderMonitor(d);
  document.getElementById('refresh-time').textContent = new Date().toLocaleTimeString('ko-KR');
}

async function refreshAll() {
  try {
    const r = await api('/api/status');
    if (!r) return;
    render(await r.json());
  } catch (e) { toast('상태를 불러오지 못했습니다: ' + e); }
}

async function loadModels() {
  const sel = document.getElementById('model-select');
  try {
    const r = await api('/api/chat/models');
    if (!r) return;
    const d = await r.json();
    sel.replaceChildren();
    (d.models.length ? d.models : [d.active_model]).forEach(m => {
      const o = el('option', m); o.value = m;
      if (m === d.active_model) o.selected = true;
      sel.appendChild(o);
    });
  } catch (e) { toast('모델 목록을 불러오지 못했습니다'); }
}

async function applyModel() {
  const model = document.getElementById('model-select').value;
  if (!model) return;
  const persist = document.getElementById('model-persist').checked;
  const r = await api('/api/chat/model', {method: 'POST', body: JSON.stringify({model, persist})});
  if (!r) return;
  const d = await r.json();
  if (r.ok && !d.error) {
    toast('모델을 ' + model + ' (으)로 전환했습니다' + (d.persisted ? ' (TOKEN.env 저장됨)' : ''));
    refreshAll();
  } else {
    toast(d.error || '모델 전환 실패');
  }
}

async function endSession(s) {
  if (!confirm(s.user + ' (' + s.channel + ') 대화 세션을 종료할까요?')) return;
  const r = await api('/api/chat/session/end', {method: 'POST',
    body: JSON.stringify({guild_id: s.guild_id, channel_id: s.channel_id, user_id: s.user_id})});
  if (!r) return;
  const d = await r.json();
  toast(d.ended ? '세션을 종료했습니다' : (d.error || '세션이 이미 없습니다'));
  refreshAll();
}

async function musicAction(action, guildId) {
  if (action === 'stop' && !confirm('재생을 정지하고 대기열을 비울까요?')) return;
  const r = await api('/api/music/' + action, {method: 'POST', body: JSON.stringify({guild_id: guildId})});
  if (!r) return;
  const d = await r.json();
  toast(r.ok ? (action === 'skip' ? '스킵했습니다' : '정지했습니다') : (d.error || '실패했습니다'));
  refreshAll();
}

async function loadLogs() {
  try {
    const r = await api('/api/logs?lines=200');
    if (!r) return;
    document.getElementById('log-box').textContent = await r.text() || '(로그 없음)';
  } catch (e) { document.getElementById('log-box').textContent = '로그를 불러오지 못했습니다'; }
}

async function shutdownBot() {
  const answer = prompt('봇을 정말 종료하려면 "종료" 라고 입력하세요.');
  if (answer !== '종료') return;
  const r = await api('/api/shutdown', {method: 'POST'});
  if (r && r.ok) toast('종료 신호를 보냈습니다. 잠시 후 봇이 꺼집니다.');
}

async function logout() {
  await fetch('/logout', {method: 'POST'});
  location.href = '/login';
}

refreshAll();
loadModels();
loadLogs();
setInterval(refreshAll, 5000);
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
        if not WEB_ADMIN_PASSWORD or not hmac.compare_digest(password, WEB_ADMIN_PASSWORD):
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

        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

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
        if not model:
            return web.json_response({"error": "model 값이 비어 있습니다."}, status=400)

        installed = await asyncio.to_thread(chat_cog._fetch_installed_models_sync)
        if installed and model not in installed:
            return web.json_response(
                {"error": f"설치되지 않은 모델입니다: {model}", "installed": installed},
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
