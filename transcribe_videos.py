#!/usr/bin/env python3
"""
Video Transcription (YouTube) — Supabase edition
-------------------------------------------------
Asks Gemini to transcribe recently-synced YouTube videos straight from their
public watch URL, and writes the transcript to the Supabase `posts` table.

Why Gemini and not Whisper / yt-dlp: YouTube blocks downloads and caption
scraping from cloud servers (GitHub Actions). Gemini reads the video on
Google's side, so nothing has to be downloaded. It understands Cantonese and
Mandarin; the transcript is kept in the language that is spoken.

A transcript is saved in its own step: a failed write retries next run.
Videos Gemini cannot open (private / age-gated / removed, HTTP 400) are marked
"(视频不可用)" so the queue moves on. A rate-limit (429) ends the run; the
leftovers are picked up next time.

Env: SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, GEMINI_API_KEY
     GEMINI_MODEL (gemini-2.5-flash), MAX_AGE_DAYS (30), VIDEO_LIMIT (25),
     MAX_MINUTES (25: videos longer than this are skipped to save free quota)
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta

SUPABASE_URL = (os.environ.get("SUPABASE_URL") or "").rstrip("/")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_FALLBACKS = ("gemini-2.5-flash", "gemini-3.5-flash-lite", "gemini-3.8-flash")
_gemini_models = [GEMINI_MODEL] + [m for m in GEMINI_FALLBACKS if m != GEMINI_MODEL]
def _envi(name, default):
    # GitHub passes unset variables as empty strings, so fall back on "" too
    try:
        return int(float(os.environ.get(name) or default))
    except ValueError:
        return int(default)


MAX_AGE_DAYS = _envi("MAX_AGE_DAYS", 30)
VIDEO_LIMIT = _envi("VIDEO_LIMIT", 25)
MAX_MINUTES = _envi("MAX_MINUTES", 25)
NO_SPEECH = "(无口播内容)"
UNAVAILABLE = "(视频不可用)"
MAX_CHARS = 5000
DELAY = 7  # free tier is about 10 requests a minute

PROMPT = (
    "請把這條影片裡說的話逐字抄寫出來。\n"
    "- 保留影片實際使用的語言（廣東話就寫廣東話，普通話就寫普通話，英文就寫英文），不要翻譯。\n"
    "- 中文用繁體字。\n"
    "- 只輸出逐字稿正文，不要時間戳、不要說話者標籤、不要任何解釋或標題。\n"
    "- 如果影片沒有人說話（只有音樂或字幕），只輸出：無口播內容"
)


def _request(url, method="GET", data=None, headers=None, timeout=180):
    hdrs = dict(headers or {})
    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return json.loads(raw) if raw else {}


def sb(path, method="GET", data=None, prefer=None):
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    headers = {"apikey": key}  # new-style keys are not JWTs: apikey header only
    if prefer:
        headers["Prefer"] = prefer
    return _request(f"{SUPABASE_URL}/rest/v1/{path}", method, data, headers, 60)


def fetch_pending():
    cutoff = (date.today() - timedelta(days=MAX_AGE_DAYS)).isoformat()
    rows = sb(
        "posts?select=post_id,competitor,post_url,duration_sec"
        "&tracker=eq.YT&transcript=is.null"
        f"&last_synced=gte.{cutoff}"
        "&order=viral_score.desc.nullslast"
    )
    # duration is filtered here: PostgREST cannot compare two columns/expressions
    # and rows with an unknown duration (0) are still worth a try
    return [r for r in rows
            if (r.get("duration_sec") or 0) <= MAX_MINUTES * 60 and r.get("post_url")]


def save_transcript(post_id, text):
    sb(f"posts?post_id=eq.{urllib.parse.quote(post_id)}", method="PATCH",
       data={"transcript": text[:MAX_CHARS]}, prefer="return=minimal")


def transcribe(key, url):
    while True:
        model = _gemini_models[0]
        cfg = {"maxOutputTokens": 8192, "temperature": 0.0}
        if model.startswith("gemini-2.5-flash"):
            cfg["thinkingConfig"] = {"thinkingBudget": 0}
        try:
            resp = _request(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                "POST",
                {"contents": [{"role": "user", "parts": [
                    {"file_data": {"file_uri": url}},
                    {"text": PROMPT}]}],
                 "generationConfig": cfg},
                {"x-goog-api-key": key}, 300)
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


def main():
    if not SUPABASE_URL or not os.environ.get("SUPABASE_SERVICE_ROLE_KEY"):
        print("ERROR: SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY required")
        sys.exit(1)
    key = (os.environ.get("GEMINI_API_KEY") or "").strip()
    if not key:
        print("No GEMINI_API_KEY — skipping transcription (non-fatal).")
        print("到倉庫 Settings → Secrets and variables → Actions 加 GEMINI_API_KEY"
              "（Google AI Studio 免費拿）。")
        return

    queue = fetch_pending()
    print(f"{len(queue)} videos pending transcription (<= {MAX_MINUTES} min)")
    if not queue:
        return
    if len(queue) > VIDEO_LIMIT:
        print(f"capping at {VIDEO_LIMIT} ({len(queue) - VIDEO_LIMIT} left for next run)")
        queue = queue[:VIDEO_LIMIT]

    ok = unavailable = retry = 0
    for i, it in enumerate(queue, 1):
        print(f"[{i}/{len(queue)}] {it['competitor']} {it['post_id']}")
        text, mark, stop = None, None, False
        try:
            text = transcribe(key, it["post_url"])
            if not text:
                mark = NO_SPEECH
            elif text.strip().strip("。.") in ("無口播內容", "无口播内容"):
                text, mark = None, NO_SPEECH
        except urllib.error.HTTPError as exc:
            if exc.code == 400:
                mark = UNAVAILABLE
                print("  UNAVAILABLE: Gemini 打不開這條影片（私人／限制級／已移除）")
            elif exc.code == 429:
                print("  RATE LIMITED — 今日免費額度用完，剩下的下次再轉錄")
                stop = True
            else:
                print(f"  TRANSIENT: HTTP {exc.code}")
        except Exception as exc:
            print(f"  TRANSIENT: {exc}")

        if stop:
            retry += len(queue) - i + 1
            break
        value = text if text is not None else mark
        if value is not None:
            try:
                save_transcript(it["post_id"], value)
                if text is not None:
                    ok += 1
                    print(f"  OK: {text[:70]}")
                else:
                    unavailable += 1
            except Exception as exc:
                retry += 1
                print(f"  SAVE FAILED, retry next run: {exc}")
        else:
            retry += 1
        time.sleep(DELAY)

    print(f"Done: {ok} transcribed, {unavailable} marked (no speech / unavailable), "
          f"{retry} to retry")


if __name__ == "__main__":
    main()
