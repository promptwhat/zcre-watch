"""지크(zcre) 감시기 — 스레드 새 글·리셋/혜택 감지·리셋 D-1 알림 + 대시보드(index.html).

launchd가 15분마다 실행. 실행: venv/bin/python watch.py  (--test 는 네트워크 없이 파서 자체검사)
"""
import html
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(os.environ.get("ZCRE_DIR") or Path(__file__).resolve().parent)  # 맥 로컬은 결과를 따로 둔다
STATE = HERE / "state.json"
DASH = HERE / "index.html"
LOG = HERE / "watch.log"
KST = timezone(timedelta(hours=9))
PROFILE = "https://www.threads.com/@zcre.co.kr"
UPDATES = "https://zcre.co.kr/updates"
HOME = "https://zcre.co.kr/credits"

RESET_WORDS = re.compile(r"리셋|초기화|다시 (가득|채워)|재충전|충전해")
PERK_WORDS = re.compile(r"리셋|초기화|크레딧|무료|할인|%|이벤트|혜택|쿠폰|프로모션|추가 지급|드려요|드립니다|깜짝")
WD = r"(?:\s*\([월화수목금토일]\))?"
DATE_PATTERNS = [
    # 9/23(수) 오후 6시 · 9/23 18:00
    re.compile(r"(\d{1,2})/(\d{1,2})" + WD + r"\s*(오전|오후)?\s*(\d{1,2})(?:시|:(\d{2}))"),
    # 09.17 12:00
    re.compile(r"(\d{1,2})\.(\d{1,2})" + WD + r"\s*(오전|오후)?\s*(\d{1,2}):(\d{2})"),
    # 9월 23일(수) 오후 6시
    re.compile(r"(\d{1,2})월\s*(\d{1,2})일" + WD + r"\s*(오전|오후)?\s*(\d{1,2})(?:시|:(\d{2}))"),
]


def now():
    return datetime.now(KST)


def log(msg):
    line = f"[{now():%m-%d %H:%M}] {msg}"
    print(line)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def notify(title, msg):
    log(f"알림 {title}: {msg}")
    if sys.platform != "darwin":  # 클라우드(GitHub Actions)에선 웹페이지만 갱신
        return
    esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')
    subprocess.run(["osascript", "-e",
                    f'display notification "{esc(msg[:200])}" with title "{esc(title)}" sound name "Glass"'],
                   capture_output=True, timeout=10)


def find_dates(text, ref):
    """글에서 (시각, 리셋여부, 앞뒤 문맥) 목록. 연도는 ref 기준, 반년 넘게 지난 날짜면 내년으로."""
    out = []
    for line in text.splitlines():
        for pat in DATE_PATTERNS:
            for m in pat.finditer(line):
                mo, d, ampm, h, mi = int(m[1]), int(m[2]), m[3], int(m[4]), int(m[5] or 0)
                if not (1 <= mo <= 12 and 1 <= d <= 31 and h <= 24 and mi < 60):
                    continue
                if ampm == "오후" and h < 12:
                    h += 12
                try:
                    dt = datetime(ref.year, mo, d, h % 24, mi, tzinfo=KST)
                except ValueError:
                    continue
                if dt < ref - timedelta(days=180):
                    dt = dt.replace(year=ref.year + 1)
                out.append((dt, bool(RESET_WORDS.search(line) or RESET_WORDS.search(text[:300])), line.strip()))
    return out


def scrape():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        try:  # 맥에 깔린 크롬 우선, 없으면 playwright 번들
            b = p.chromium.launch(headless=True, channel="chrome")
        except Exception:
            b = p.chromium.launch(headless=True)
        pg = b.new_page(locale="ko-KR", user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128 Safari/537.36")
        posts, upd, home, errs = [], "", "", []
        try:
            pg.goto(PROFILE, wait_until="domcontentloaded", timeout=60000)
            pg.wait_for_selector('div[data-pressable-container="true"] a[href*="/post/"]', timeout=30000)
            for _ in range(3):
                pg.mouse.wheel(0, 5000)
                pg.wait_for_timeout(1200)
            # 로그아웃 상태에선 고정글 + 최신 6개만 보인다 — 15분 주기면 새 글 감지엔 충분
            posts = pg.eval_on_selector_all('div[data-pressable-container="true"]', """cs => cs.map(c => {
                const a = c.querySelector('a[href*="/post/"]'), t = c.querySelector('time');
                return a && {url: 'https://www.threads.com' + a.getAttribute('href').split('?')[0],
                             dt: t && t.getAttribute('datetime'), text: c.innerText};
            }).filter(Boolean)""")
        except Exception as e:
            errs.append(f"스레드: {e}".splitlines()[0])
        try:
            pg.goto(UPDATES, wait_until="domcontentloaded", timeout=60000)
            pg.wait_for_selector(r"text=/\d{4}년 \d{1,2}월 \d{1,2}일/", timeout=30000)
            pg.wait_for_timeout(3000)
            upd = pg.inner_text("body")
            pg.goto(HOME, wait_until="domcontentloaded", timeout=60000)
            pg.wait_for_selector("text=AI 콘텐츠 제작", timeout=30000)
            try:
                pg.wait_for_load_state("networkidle", timeout=15000)  # 배너는 늦게 뜬다
            except Exception:
                pg.wait_for_timeout(5000)
            home = pg.inner_text("body")
        except Exception as e:
            errs.append(f"사이트: {e}".splitlines()[0])
        b.close()
    return posts, upd, home, errs


def clean_post(text):
    lines = [l for l in text.splitlines() if l.strip()]
    drop = {"고정됨", "zcre.co.kr", "AI Threads", "번역하기"}
    lines = [l for l in lines if l not in drop and not re.fullmatch(r"\d+(시간|분|일|주)?|\d{4}-\d{2}-\d{2}|[\d,.]+[천만]?", l)]
    return "\n".join(lines)


def latest_update_block(upd):
    """업데이트 페이지에서 가장 최근 'N월 N일 업데이트' 블록."""
    parts = re.split(r"\n(?=\d{4}년 \d{1,2}월 \d{1,2}일\n)", upd)
    return re.sub(r"\n업데이트\s*$", "", parts[1].strip()) if len(parts) > 1 else ""


def banners(home):
    # 사이트 상단 프로모션 문구(예: "seedance 2.5 50%", "로그인하면 40 크레딧 드려요")
    head = home.split("AI 콘텐츠 제작")[0]
    return sorted({l.strip() for l in head.splitlines() if PERK_WORDS.search(l)})


def run():
    st = json.loads(STATE.read_text()) if STATE.exists() else {}
    first = not st.get("posts")  # 첫 성공 수집 전까지는 기존 글을 알림 없이 기억만
    st.setdefault("posts", {})
    st.setdefault("events", {})
    st.setdefault("alerted", [])
    st.setdefault("feed", [])
    t = now()

    def feed(kind, title, body, url=""):
        st["feed"].insert(0, {"at": t.isoformat(), "kind": kind, "title": title, "url": url})
        del st["feed"][100:]

    try:
        posts, upd, home, errs = scrape()
    except Exception as e:
        posts, upd, home, errs = [], "", "", [f"브라우저: {e}".splitlines()[0]]
    if posts and not errs:
        st["ok"] = t.isoformat()
    if errs:
        log(f"수집 실패: {errs}")
        st["errs"] = errs
        if st.get("fail_day") != f"{t:%F}":
            st["fail_day"] = f"{t:%F}"
            notify("지크 감시 수집 실패", " / ".join(errs)[:150])
    else:
        st.pop("errs", None)

    for p in posts:
        text = clean_post(p["text"])
        for dt, is_reset, ctx in find_dates(text, t):
            key = f"{dt:%Y-%m-%d %H:%M}"
            ev = st["events"].setdefault(key, {"reset": False, "ctx": ctx, "url": p["url"]})
            ev["reset"] = ev["reset"] or is_reset
        if p["url"] in st["posts"]:
            continue
        kind = "r" if RESET_WORDS.search(text) else "o" if PERK_WORDS.search(text) else "n"
        # 공개 페이지라 원문 전체는 싣지 않고 앞 3줄 + 원문 링크만
        st["posts"][p["url"]] = {"dt": p["dt"], "text": "\n".join(text.splitlines()[:3]), "kind": kind}
        if first:
            continue
        head = text.splitlines()[0] if text else "(내용 없음)"
        if RESET_WORDS.search(text):
            notify("🔥 지크 리셋 감지", head)
            feed("리셋", head, text, p["url"])
        elif PERK_WORDS.search(text):
            notify("🎁 지크 혜택 글", head)
            feed("혜택", head, text, p["url"])
        else:
            notify("🐰 지크 새 스레드", head)
            feed("새 글", head, text, p["url"])

    if upd:
        block = latest_update_block(upd)
        if block and block != st.get("update_block"):
            if not first:
                perk = PERK_WORDS.search(block)
                notify("🎁 지크 업데이트(혜택 포함)" if perk else "🆕 지크 사이트 업데이트", block.splitlines()[0])
                feed("사이트 업데이트", block.splitlines()[0], block, UPDATES)
            st["update_block"] = block
    if home:
        bn = banners(home)
        if bn != st.get("banners"):
            if not first and bn:
                notify("🎁 지크 사이트 배너 변경", " / ".join(bn))
                feed("배너", " / ".join(bn), "", HOME)
            st["banners"] = bn

    for key, ev in st["events"].items():
        dt = datetime.strptime(key, "%Y-%m-%d %H:%M").replace(tzinfo=KST)
        if not ev["reset"] or dt < t:
            continue
        when = f"{dt:%m/%d}({'월화수목금토일'[dt.weekday()]}) {dt:%H:%M}"
        for tag, before, label in (("h1", timedelta(hours=1), "1시간 안"), ("d1", timedelta(days=1), "24시간 안")):
            if dt - before <= t and f"{key}|{tag}" not in st["alerted"]:
                st["alerted"] += [f"{key}|h1", f"{key}|d1"] if tag == "h1" else [f"{key}|d1"]
                notify(f"⏰ 지크 크레딧 리셋 {label}", f"{when} 리셋 — 남은 크레딧 지금 쓰세요")
                break

    if first:
        n = sum(e["reset"] and datetime.strptime(k, "%Y-%m-%d %H:%M").replace(tzinfo=KST) > t
                for k, e in st["events"].items())
        notify("지크 감시 시작", f"글 {len(st['posts'])}개 기억 · 다가오는 리셋 {n}건")
    st["checked"] = t.isoformat()
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1))
    render(st, t)


def render(st, t):
    e = html.escape
    up, past = [], []
    for k, ev in sorted(st["events"].items()):
        dt = datetime.strptime(k, "%Y-%m-%d %H:%M").replace(tzinfo=KST)
        (up if dt >= t else past).append((dt, ev))
    def ev_row(dt, ev, live):
        tag = '<b class="r">리셋</b>' if ev["reset"] else '<b class="o">일정</b>'
        cd = f'<span class="cd" data-t="{dt.isoformat()}"></span>' if live else ""
        return f'<li>{tag} <strong>{dt:%m/%d %H:%M}</strong> {cd}<br><a href="{e(ev["url"])}">{e(ev["ctx"])}</a></li>'
    posts = sorted(st["posts"].items(), key=lambda kv: kv[1]["dt"] or "", reverse=True)
    def post_row(url, p):
        txt, cls = p["text"], p.get("kind", "n")
        when = datetime.fromisoformat(p["dt"].replace("Z", "+00:00")).astimezone(KST).strftime("%m/%d %H:%M") if p["dt"] else ""
        return f'<li class="{cls}"><small>{when}</small> <a href="{e(url)}">열기</a><p>{e(txt)}</p></li>'
    feed = "".join(f'<li><b class="o">{e(f["kind"])}</b> <small>{f["at"][5:16].replace("T", " ")}</small> '
                   f'<a href="{e(f["url"])}">{e(f["title"])}</a></li>' for f in st["feed"][:30]) or "<li>아직 없음 — 감시 중</li>"
    blk = st.get("update_block", "").splitlines()
    upd_head = blk[0] if blk else "열기"
    upd_items = "\n".join(f"· {blk[i + 1]}" for i, l in enumerate(blk[:-1]) if l in ("신규", "개선", "수정"))
    warn = f" · ⚠️ 이번 확인 일부 실패(마지막 전체 성공 {st['ok'][5:16].replace('T', ' ')})" if st.get("errs") and st.get("ok") else ""
    DASH.write_text(f"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="300">
<title>지크 리셋 알리미</title><meta name="description" content="zcre(지크) 크레딧 리셋·혜택 일정 모음 (비공식)"><style>
:root{{--bg:#fbfaf7;--fg:#1d1d1f;--mut:#6e6e73;--card:#fff;--line:#e5e2dc;--r:#d8452e;--o:#b7791f}}
@media (prefers-color-scheme:dark){{:root{{--bg:#141414;--fg:#eee;--mut:#9a9a9f;--card:#1e1e1e;--line:#333;--r:#ff6b52;--o:#e0a84a}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 -apple-system,system-ui,sans-serif}}
main{{max-width:760px;margin:auto;padding:20px 16px 60px}} h1{{font-size:22px;margin:0}} h2{{font-size:16px;margin:28px 0 8px}}
.mut,small{{color:var(--mut)}} ul{{list-style:none;padding:0;margin:0}}
li{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px;margin:8px 0;overflow-wrap:anywhere}}
li.r{{border-left:4px solid var(--r)}} li.o{{border-left:4px solid var(--o)}} p{{white-space:pre-wrap;margin:6px 0 0}}
b.r,b.o{{font-size:12px;padding:2px 7px;border-radius:99px;color:#fff;background:var(--r)}} b.o{{background:var(--o)}}
.cd{{color:var(--r);font-weight:600}} a{{color:inherit}}
</style></head><body><main>
<h1>🐰 지크(zcre) 리셋·혜택 알리미</h1><p class="mut">마지막 확인 {t:%m/%d %H:%M} KST{warn}<br>사이트 배너: {e(" / ".join(st.get("banners", [])) or "없음")}</p>
<h2>다가오는 일정</h2><ul>{"".join(ev_row(d, v, True) for d, v in up) or "<li>없음</li>"}</ul>
<h2>새 소식 기록</h2><ul>{feed}</ul>
<h2>사이트 최신 업데이트</h2><ul><li><a href="{UPDATES}">{e(upd_head)}</a><p>{e(upd_items)}</p></li></ul>
<h2>스레드 글 ({len(posts)})</h2><ul>{"".join(post_row(u, p) for u, p in posts)}</ul>
<h2>지난 일정</h2><ul>{"".join(ev_row(d, v, False) for d, v in reversed(past)) or "<li>없음</li>"}</ul>
</main><script>
function tick(){{document.querySelectorAll('.cd').forEach(s=>{{let m=(new Date(s.dataset.t)-Date.now())/6e4;
s.textContent=m<0?'지남':'· '+(m>=1440?Math.floor(m/1440)+'일 ':'')+Math.floor(m%1440/60)+'시간 '+Math.floor(m%60)+'분 남음'}})}}
tick();setInterval(tick,30000);
</script></body></html>""", encoding="utf-8")


def selftest():
    ref = datetime(2026, 9, 17, 19, tzinfo=KST)
    pin = "🌕 zcre 추석 전야제 — 크레딧 초기화 이벤트\n📅 9/11(금) 오후 6시 — 1차 리셋\n📅 9/23(수) 오후 6시 — 2차 리셋"
    got = [(f"{d:%m-%d %H:%M}", r) for d, r, _ in find_dates(pin, ref)]
    assert got == [("09-11 18:00", True), ("09-23 18:00", True)], got
    got = [(f"{d:%m-%d %H:%M}", r) for d, r, _ in find_dates("09.17 12:00 KST", ref)]
    assert got == [("09-17 12:00", False)], got
    got = [f"{d:%Y-%m-%d %H}" for d, _, _ in find_dates("1/3 오전 9시 이벤트", ref)]
    assert got == ["2027-01-03 09"], got
    assert clean_post("고정됨\nzcre.co.kr\nAI Threads\n1시간\n본문\n88\n93") == "본문"
    upd = "업데이트\n2026년 9월 16일\n9월 16일 업데이트\n신규\nA\n업데이트\n2026년 9월 15일\nB"
    assert latest_update_block(upd) == "2026년 9월 16일\n9월 16일 업데이트\n신규\nA", latest_update_block(upd)
    print("자체검사 통과")


if __name__ == "__main__":
    selftest() if "--test" in sys.argv else run()
