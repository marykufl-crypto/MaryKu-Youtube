#!/usr/bin/env python3
"""
AI Viral-Post Breakdown — Supabase edition
-------------------------------------------
Feeds each viral post (stats + caption + transcript) to an LLM and writes a
structured breakdown to the `ai_breakdown` column. Highest-scoring posts
first so the free model budget goes to the best material; a
permanently-rejected post is marked (HTTP 400) so the queue always moves on.

Provider: Gemini (free tier, GEMINI_API_KEY from Google AI Studio) by default;
Anthropic API when ANTHROPIC_API_KEY is set. (GitHub Models was retired on
2026-07-30 and is no longer supported.)

Env: SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, GEMINI_API_KEY | ANTHROPIC_API_KEY
     NICHE (default: 自媒体内容), GEMINI_MODEL (gemini-2.5-flash), POST_LIMIT (80)
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

SUPABASE_URL = (os.environ.get("SUPABASE_URL") or "").rstrip("/")
NICHE = os.environ.get("NICHE") or "生活覺察與身心靈"
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
# Google 会陆续下线旧的免费档模型(gemini-2.0-flash 已经没了)。
# 遇到 404 就自动换下一个,不用改代码;想指定就设 GEMINI_MODEL 变量。
GEMINI_FALLBACKS = ("gemini-2.5-flash", "gemini-3.5-flash-lite", "gemini-3.8-flash")
_gemini_models = [GEMINI_MODEL] + [m for m in GEMINI_FALLBACKS if m != GEMINI_MODEL]
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001")
POST_LIMIT = int(os.environ.get("POST_LIMIT", "80"))
FAIL_MARK = "(拆解失败)"
SKIP_TR = ("(转录失败)", "(视频不可用)", "(无口播内容)")

SYSTEM = (
    f"你是 YouTube 爆款影片拆解專家,服務一個做「{NICHE}」內容的華語頻道(香港市場)。"
    "給你一條同類頻道的爆款影片資料(標題、說明欄、口述逐字稿),輸出精煉拆解,"
    "嚴格按以下四行格式,用繁體中文,每行不超過60字,不要任何其他文字:\n"
    "標題與Hook類型: <標題用了什麼手法,如 反常識/痛點直擊/懸念/數字/身份共鳴/日常場景>\n"
    "結構: <A → B → C 形式概括>\n"
    "為什麼火: <一句話,只講資料裡看得到的原因,不要猜測>\n"
    f"改編角度: <給{NICHE}領域的具體改編建議,一句話,只借結構,不抄原片字句>"
)


def _request(url, method="GET", data=None, headers=None, timeout=120):
    hdrs = headers or {}
    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def sb(path, method="GET", data=None, prefer=None):
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    headers = {"apikey": key}  # new-style keys are not JWTs: apikey header only
    if prefer:
        headers["Prefer"] = prefer
    return _request(f"{SUPABASE_URL}/rest/v1/{path}", method, data, headers, 60)


def fetch_pending():
    rows = sb(
        "posts?select=post_id,competitor,caption,transcript,post_type,"
        "likes,comments,views,followers"
        "&ai_breakdown=is.null"
        "&order=viral_score.desc.nullslast"
    )
    # enough material to analyse: a real transcript or a decent caption
    # (length filters live here — PostgREST cannot filter on expressions)
    return [p for p in rows
            if len((p.get("transcript") or "") if (p.get("transcript") or "") not in SKIP_TR else "") > 30
            or len(p.get("caption") or "") > 50]


def build_prompt(p):
    tr = p.get("transcript") or ""
    if tr in SKIP_TR:
        tr = ""
    return (
        f"頻道: {p.get('competitor', '?')}(訂閱 {p.get('followers', '?')})\n"
        f"類型: {p.get('post_type', '?')} | 觀看 {p.get('views', '?')} | 讚好 {p.get('likes', '?')} | 留言 {p.get('comments', '?')}\n"
        f"標題與說明欄:\n{(p.get('caption') or '')[:1200]}\n"
        + (f"\n口述逐字稿:\n{tr[:2500]}" if tr else "")
    )


def analyze_gemini(key, p):
    while True:
        model = _gemini_models[0]
        cfg = {"maxOutputTokens": 1200, "temperature": 0.6}
        if model.startswith("gemini-2.5-flash"):
            cfg["thinkingConfig"] = {"thinkingBudget": 0}  # 2.5 默认会"思考",会把输出额度吃光
        try:
            resp = _request(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                "POST",
                {"system_instruction": {"parts": [{"text": SYSTEM}]},
                 "contents": [{"role": "user", "parts": [{"text": build_prompt(p)}]}],
                 "generationConfig": cfg},
                {"x-goog-api-key": key}, 90)
        except urllib.error.HTTPError as exc:
            if exc.code == 404 and len(_gemini_models) > 1:
                _gemini_models.pop(0)
                print(f"  model {model} not found (retired?) -> switching to {_gemini_models[0]}")
                continue
            raise
        cands = resp.get("candidates") or []
        if not cands:
            return ""
        parts = (cands[0].get("content") or {}).get("parts") or []
        return "".join(x.get("text", "") for x in parts if not x.get("thought")).strip()


def analyze_anthropic(key, p):
    resp = _request(
        "https://api.anthropic.com/v1/messages", "POST",
        {"model": CLAUDE_MODEL, "max_tokens": 400, "system": SYSTEM,
         "messages": [{"role": "user", "content": build_prompt(p)}]},
        {"x-api-key": key, "anthropic-version": "2023-06-01"}, 90)
    parts = resp.get("content") or []
    return "".join(x.get("text", "") for x in parts if x.get("type") == "text").strip()


def save(post_id, text):
    sb(f"posts?post_id=eq.{urllib.parse.quote(post_id)}", method="PATCH",
       data={"ai_breakdown": text[:1500]}, prefer="return=minimal")


def main():
    if not SUPABASE_URL or not os.environ.get("SUPABASE_SERVICE_ROLE_KEY"):
        print("ERROR: SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY required")
        sys.exit(1)
    anthropic_key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    gemini_key = (os.environ.get("GEMINI_API_KEY") or "").strip()
    if anthropic_key:
        provider, delay = "anthropic", 0.5
        call = lambda p: analyze_anthropic(anthropic_key, p)
    elif gemini_key:
        provider, delay = "gemini", 6.5  # 免费档约 10 次/分钟,留点余量
        call = lambda p: analyze_gemini(gemini_key, p)
    else:
        print("No ANTHROPIC_API_KEY or GEMINI_API_KEY — skipping (non-fatal).")
        print("到仓库 Settings → Secrets and variables → Actions 加 GEMINI_API_KEY(Google AI Studio 免费拿)。")
        return
    print(f"[Provider] {provider} | [Niche] {NICHE}")

    queue = fetch_pending()
    print(f"{len(queue)} posts pending breakdown")
    if not queue:
        return
    if len(queue) > POST_LIMIT:
        print(f"capping at {POST_LIMIT} ({len(queue) - POST_LIMIT} left for next run)")
        queue = queue[:POST_LIMIT]

    ok = failed = 0
    for i, p in enumerate(queue, 1):
        who = p.get("competitor", "?")
        try:
            text = call(p)
            if not text:
                raise ValueError("empty response")
            save(p["post_id"], text)
            ok += 1
            print(f"[{i}/{len(queue)}] @{who} OK: {text[:50]}")
        except urllib.error.HTTPError as exc:
            failed += 1
            if exc.code == 400:
                try:
                    save(p["post_id"], FAIL_MARK)
                except Exception:
                    pass
                print(f"[{i}/{len(queue)}] @{who} REJECTED (marked)")
            elif exc.code == 429:
                print(f"[{i}/{len(queue)}] @{who} RATE LIMITED (wait 30s; leftovers next run)")
                time.sleep(30)
            else:
                print(f"[{i}/{len(queue)}] @{who} FAILED: HTTP {exc.code} (retry)")
        except Exception as exc:
            failed += 1
            print(f"[{i}/{len(queue)}] @{who} FAILED: {exc} (retry)")
        time.sleep(delay)

    print(f"Done: {ok} analyzed, {failed} failed")


if __name__ == "__main__":
    main()
