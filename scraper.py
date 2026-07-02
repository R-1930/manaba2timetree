"""
manaba(北海学園大学) 未提出課題 → ICS(iCalendar)生成スクリプト
==============================================================

【ログインの流れ】(実際のNetworkログから判明した4ステップ)
  1. sso.hgu.jp のログイン画面(login.cgi)をGET → sessid, back を取得
  2. username/password/op/back/sessid を login.cgi にPOST
  3. 返ってきたHTML内のSAML自動送信フォーム(hidden項目)を全部拾って
     Shibboleth.sso/SAML2/POST にPOST
  4. manaba(hgu.manaba.jp)のセッション確立 → 未提出課題ページを取得

【使い方】
  コマンドプロンプトで、このファイルがあるフォルダに移動してから:

    set MANABA_USER_ID=あなたのユーザID
    set MANABA_PASSWORD=あなたのパスワード
    py scraper.py

  ※ set で入れた値はそのウィンドウを閉じると消えます(安全)。
  ※ パスワードはこのコードには一切書かれません。

まず「ログインが通るか」だけを確認するための --debug モードを用意しています:
    py scraper.py --debug
これは各ステップでどのURLにたどり着いたかを表示します。
"""

import os
import re
import sys
import uuid
import datetime
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, parse_qs

# ===== 設定 =====
SSO_LOGIN_URL = "https://sso.hgu.jp/pub/login.cgi"
MANABA_BASE = "https://hgu.manaba.jp"
MANABA_ENTRY = f"{MANABA_BASE}/ct/home"
ASSIGNMENTS_URL = f"{MANABA_BASE}/ct/home_library_query"

USER_ID = os.environ.get("MANABA_USER_ID")
PASSWORD = os.environ.get("MANABA_PASSWORD")
OUTPUT_ICS_PATH = os.environ.get("OUTPUT_ICS_PATH", "assignments.ics")

DEBUG = "--debug" in sys.argv

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0 Safari/537.36"
    )
}


def log(*args):
    print(*args)


def dbg(*args):
    if DEBUG:
        print("[debug]", *args)


def follow_meta_refresh(session, html, current_url, max_hops=5):
    """HTML内の <meta http-equiv="refresh" content="0;URL=..."> を検出して
    そのURLへGETで移動する。requestsはmeta refreshを自動で追わないため必要。
    移動先でさらにmeta refreshがあれば繰り返す(最大max_hops回)。"""
    import re as _re
    res = None
    for _ in range(max_hops):
        m = _re.search(
            r'<meta[^>]+http-equiv=["\']?refresh["\']?[^>]+content=["\']?[^"\'>]*?url=([^"\'> ]+)',
            html, _re.IGNORECASE)
        if not m:
            break
        next_url = m.group(1).replace("&amp;", "&")
        next_url = urljoin(current_url, next_url)
        dbg("  meta refresh 追跡 ->", next_url[:70], "...")
        res = session.get(next_url, headers=HEADERS, allow_redirects=True)
        html = res.text
        current_url = res.url
    return res, html, current_url


def submit_auto_form(session, html, current_url):
    """HTML内の<form>のinputを全部拾ってaction先にPOSTする(SAML自動送信の再現)"""
    soup = BeautifulSoup(html, "html.parser")
    form = soup.find("form")
    if form is None:
        raise RuntimeError("フォームが見つかりませんでした。")

    action = form.get("action") or current_url
    action = urljoin(current_url, action)
    method = (form.get("method") or "post").lower()

    data = {}
    for inp in form.find_all("input"):
        name = inp.get("name")
        if not name:
            continue
        data[name] = inp.get("value", "")

    dbg(f"自動フォーム送信 -> {action} (項目: {list(data.keys())})")

    if method == "post":
        return session.post(action, data=data, headers=HEADERS)
    else:
        return session.get(action, params=data, headers=HEADERS)


def login(session):
    if not USER_ID or not PASSWORD:
        sys.exit(
            "環境変数 MANABA_USER_ID / MANABA_PASSWORD が設定されていません。\n"
            "  set MANABA_USER_ID=あなたのID\n"
            "  set MANABA_PASSWORD=あなたのパスワード\n"
            "を実行してから、もう一度 py scraper.py を実行してください。"
        )

    dbg("Step0: manaba入口にアクセス", MANABA_ENTRY)
    res = session.get(MANABA_ENTRY, headers=HEADERS, allow_redirects=True)
    dbg("  -> たどり着いた先:", res.url)

    soup = BeautifulSoup(res.text, "html.parser")
    form = soup.find("form")
    payload = {"username": USER_ID, "password": PASSWORD, "op": "login"}

    # hidden項目を拾う。ただし back は HTMLのvalue属性だと途中で壊れて
    # 読み取られることがあるため、ログイン画面のURLのクエリから正しい値を取り直す。
    if form:
        for inp in form.find_all("input"):
            name = inp.get("name")
            if name and name not in ("username", "password"):
                payload[name] = inp.get("value", "")

    # URL(res.url)の back= パラメータから完全な値を取得して上書き
    parsed = urlparse(res.url)
    qs = parse_qs(parsed.query)
    if "back" in qs:
        payload["back"] = qs["back"][0]
        dbg("  back をURLから取得:", payload["back"][:60], "...")

    dbg("Step1-2: login.cgi にPOST (項目:", list(payload.keys()), ")")

    login_action = SSO_LOGIN_URL
    if form and form.get("action"):
        login_action = urljoin(res.url, form.get("action"))

    res = session.post(login_action, data=payload, headers=HEADERS, allow_redirects=True)
    dbg("  -> login.cgi 応答後のURL:", res.url)

    # login.cgi の応答が meta refresh ページなら、その転送を追う(認証成功後のSSO中継)
    r2, html2, url2 = follow_meta_refresh(session, res.text, res.url)
    if r2 is not None:
        res = r2

    # SAML自動送信フォームを繰り返し処理(中継が複数段あっても対応)
    for i in range(5):
        # 各段でも meta refresh が挟まる場合があるので追う
        r3, html3, url3 = follow_meta_refresh(session, res.text, res.url)
        if r3 is not None:
            res = r3
        if "manaba" in res.url and "/ct/" in res.url:
            break
        if "SAMLResponse" in res.text or "<form" in res.text.lower():
            try:
                res = submit_auto_form(session, res.text, res.url)
                dbg(f"  -> SAML中継{i+1}段目 送信後のURL:", res.url)
            except RuntimeError:
                break
        else:
            break

    if "ログアウト" in res.text or "logout" in res.text.lower():
        log("[OK] ログイン成功！ manabaに到達しました:", res.url)
    else:
        log("[??] ログイン成功か確認できませんでした。到達URL:", res.url)
        log("     debugモード(py scraper.py --debug)で流れを確認してください。")
        if DEBUG:
            with open("last_page.html", "w", encoding="utf-8") as f:
                f.write(res.text)
            log("     最終ページを last_page.html に保存しました。")


def parse_assignments(session):
    res = session.get(ASSIGNMENTS_URL, headers=HEADERS)
    if DEBUG:
        with open("assignments_page.html", "w", encoding="utf-8") as f:
            f.write(res.text)
        dbg("未提出課題ページを assignments_page.html に保存しました。")

    soup = BeautifulSoup(res.text, "html.parser")
    assignments = []

    # 未提出の課題一覧テーブル。各行の列は:
    #   タイプ / タイトル / コース / 受付開始日時 / 受付終了日時
    # 「受付終了日時(締切)」を予定の日時として使う。
    for row in soup.select("table tr"):
        cells = row.find_all("td")
        if len(cells) < 5:
            continue

        assign_type = cells[0].get_text(strip=True)
        title = cells[1].get_text(strip=True)
        course = cells[2].get_text(strip=True)
        end_text = cells[4].get_text(strip=True)

        due = parse_datetime(end_text)
        if due is None:
            continue
        if not title:
            continue

        assignments.append({
            "title": title,
            "course": course,
            "type": assign_type,
            "due": due,
        })
    return assignments


def parse_datetime(text):
    m = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})\s+(\d{1,2}):(\d{2})", text)
    if m:
        y, mo, d, h, mi = map(int, m.groups())
        try:
            return datetime.datetime(y, mo, d, h, mi)
        except ValueError:
            return None
    return None


def build_ics(assignments):
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//manaba2timetree//JP",
        "CALSCALE:GREGORIAN",
    ]
    now_utc = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    for a in assignments:
        start = a["due"] - datetime.timedelta(minutes=30)
        end = a["due"]
        uid = uuid.uuid5(uuid.NAMESPACE_URL, f"{a['course']}-{a['title']}-{a['due']}")
        summary = f"{a['course']} - {a['title']}".strip(" -")
        lines += [
            "BEGIN:VEVENT",
            f"UID:{uid}@manaba2timetree",
            f"DTSTAMP:{now_utc}",
            f"DTSTART:{start.strftime('%Y%m%dT%H%M%S')}",
            f"DTEND:{end.strftime('%Y%m%dT%H%M%S')}",
            f"SUMMARY:{summary}",
            "DESCRIPTION:manabaの未提出課題です。",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines)


def main():
    session = requests.Session()
    login(session)
    assignments = parse_assignments(session)
    log(f"\n未提出課題 {len(assignments)} 件を検出しました。")
    for a in assignments:
        log(f"  - [{a['course']}] {a['title']} 締切: {a['due']}")
    if assignments:
        ics_text = build_ics(assignments)
        with open(OUTPUT_ICS_PATH, "w", encoding="utf-8") as f:
            f.write(ics_text)
        log(f"\nICSファイルを書き出しました: {OUTPUT_ICS_PATH}")
    else:
        log("\n課題が0件でした。ページ構造の調整が必要かもしれません。")
        log("debugモード(py scraper.py --debug)で assignments_page.html を確認してください。")


if __name__ == "__main__":
    main()
