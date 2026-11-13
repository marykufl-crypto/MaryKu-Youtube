#!/usr/bin/env python3
"""
YouTube Viral Sync — Supabase edition
--------------------------------------
Reads the active channels in the Supabase `competitors` table, pulls their
recent videos from the official YouTube Data API v3 (free), keeps the
standout videos, and UPSERTS them into the Supabase `posts` table.

A video is kept when it clears a small noise floor (MIN_VIEWS) AND passes ANY
of two tests (OR, not AND):
  1. views >= OUTLIER_X x that channel's OWN median views
     (median over the videos fetched this run, so a 1k-subscriber channel is
     judged against itself, not against a 500k one)
  2. views / subscribers >= MIN_RATIO  (the video reached far beyond the
     channel's own audience)
On top of that every channel always contributes its TOP_N_PER_COMP best
videos, so a quiet month still fills the dashboard.

Quota: about 3 units per channel per run; the free daily quota is 10,000.

Upsert notes: existing rows get their metrics refreshed; status / transcript /
ai_breakdown / my_script are never in the payload, so worked rows are never
clobbered. engagement_rate and viral_score are generated columns — never written.

Env (required): YOUTUBE_API_KEY, SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY
Env (tuning, all optional -- set as GitHub Actions *Variables*):
     MIN_VIEWS (500)  OUTLIER_X (1.5)  MIN_RATIO (1.0)  TOP_N_PER_COMP (3)
     MAX_PER_COMP (15)  DAYS_BACK (90)  RESULTS_LIMIT (50)  MAX_ROWS (2500)
"""

import json
import os
import re
import statistics
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

TRACKER = "YT"
SUPABASE_URL = (os.environ.get("SUPABASE_URL") or "").rstrip("/")
YT_API = "https://www.googleapis.com/youtube/v3"
PROTECTED = ("拍摄中", "已处理")


def _envf(name, default):
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return float(default)


def _envi(name, default):
    try:
        return int(float(os.environ.get(name) or default))
    except ValueError:
        return int(default)


MIN_VIEWS      = _envi("MIN_VIEWS", 500)       # noise floor
OUTLIER_X      = _envf("OUTLIER_X", 1.5)       # x the channel's own median views
MIN_RATIO      = _envf("MIN_RATIO", 1.0)       # views / subscribers
TOP_N_PER_COMP = _envi("TOP_N_PER_COMP", 3)    # always keep each channel's best N
MAX_PER_COMP   = _envi("MAX_PER_COMP", 15)     # cap per channel
DAYS_BACK      = _envi("DAYS_BACK", 90)        # lookback window
RESULTS_LIMIT  = _envi("RESULTS_LIMIT", 50)    # newest videos read per channel
MAX_ROWS       = _envi("MAX_ROWS", 2500)       # dashboard payload cap


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


class QuotaExceeded(Exception):
    pass


def _request(url, method="GET", data=None, headers=None, timeout=60):
    hdrs = dict(headers or {})
    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            try:
                return json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                return raw
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace")
        # never print the full URL: it contains the API key
        print(f"  HTTP {exc.code} for {method} {url.split('?')[0]}")
        print(f"  Response: {text[:400]}")
        if exc.code == 403 and "quota" in text.lower():
            raise QuotaExceeded() from exc
        raise


def sb(path, method="GET", data=None, prefer=None):
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    headers = {"apikey": key}  # new-style keys are not JWTs: apikey header only
    if prefer:
        headers["Prefer"] = prefer
    return _request(f"{SUPABASE_URL}/rest/v1/{path}", method, data, headers, 60)


def yt(endpoint, **params):
    params["key"] = os.environ["YOUTUBE_API_KEY"].strip()
    return _request(f"{YT_API}/{endpoint}?{urllib.parse.urlencode(params)}", timeout=60)


# ---------------------------------------------------------------------------
# Competitors (self-service list in Supabase)
# ---------------------------------------------------------------------------


def clean_channel_ref(raw):
    """Turn whatever was typed into the competitors table into either a
    channel id (UC...) or a handle. Returns '' for unusable input
    (e.g. a video link)."""
    n = (raw or "").strip()
    if "youtube.com" in n or "youtu.be" in n:
        parts = [urllib.parse.unquote(p) for p in
                 urllib.parse.urlparse(n).path.split("/") if p]
        if not parts:
            return ""
        if parts[0].startswith("@"):
            n = parts[0]
        elif parts[0] == "channel" and len(parts) > 1:
            n = parts[1]
        else:
            print(f"  WARNING: skipping non-channel link: {n[:60]}")
            return ""
    return n.lstrip("@").strip()


def fetch_competitors():
    rows = sb(f"competitors?select=username&tracker=eq.{TRACKER}&active=is.true")
    refs = []
    for r in rows:
        ref = clean_channel_ref(r.get("username"))
        if ref and ref.lower() not in [x.lower() for x in refs]:
            refs.append(ref)
    return refs


def resolve_channel(ref):
    """Return the channel resource (snippet, contentDetails, statistics)."""
    part = "snippet,contentDetails,statistics"
    if re.fullmatch(r"UC[\w-]{22}", ref):
        data = yt("channels", part=part, id=ref)
    else:
        data = yt("channels", part=part, forHandle="@" + ref)
    items = data.get("items") or []
    return items[0] if items else None


# ---------------------------------------------------------------------------
# YouTube fetch
# ---------------------------------------------------------------------------


def parse_duration(iso):
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso or "")
    if not m:
        return 0
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def recent_video_ids(uploads_playlist, cutoff):
    ids, token = [], None
    while len(ids) < RESULTS_LIMIT:
        params = dict(part="contentDetails", playlistId=uploads_playlist,
                      maxResults=50)
        if token:
            params["pageToken"] = token
        data = yt("playlistItems", **params)
        stop = False
        for it in data.get("items", []):
            published = (it.get("contentDetails") or {}).get("videoPublishedAt") or ""
            if published and published < cutoff:
                stop = True  # uploads playlist is newest-first
                break
            vid = (it.get("contentDetails") or {}).get("videoId")
            if vid:
                ids.append(vid)
        token = data.get("nextPageToken")
        if stop or not token:
            break
    return ids[:RESULTS_LIMIT]


def video_details(ids):
    out = []
    for i in range(0, len(ids), 50):
        data = yt("videos", part="snippet,contentDetails,statistics",
                  id=",".join(ids[i:i + 50]))
        out.extend(data.get("items", []))
    return out


def to_records(videos, key, subs, today):
    recs = []
    for v in videos:
        sn, st = v.get("snippet") or {}, v.get("statistics") or {}
        if sn.get("liveBroadcastContent") in ("live", "upcoming"):
            continue
        vid = v.get("id")
        if not vid or "viewCount" not in st:
            continue
        views = int(st.get("viewCount") or 0)
        likes = int(st.get("likeCount") or 0)
        comments = int(st.get("commentCount") or 0)
        dur = parse_duration((v.get("contentDetails") or {}).get("duration"))
        title = (sn.get("title") or "").strip()
        desc = (sn.get("description") or "").strip()
        thumbs = sn.get("thumbnails") or {}
        thumb = ((thumbs.get("high") or thumbs.get("medium") or {}).get("url")
                 or f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg")
        tags = sn.get("tags") or []
        recs.append({
            "row": {
                "post_id": vid,
                "tracker": TRACKER,
                "competitor": key,
                "caption": (title + "\n" + desc)[:5000],
                "post_type": "Short" if 0 < dur <= 60 else "Video",
                "likes": likes,
                "comments": comments,
                "views": views,
                "followers": subs,
                "duration_sec": dur,
                "post_date": (sn.get("publishedAt") or "")[:10] or None,
                "post_url": f"https://www.youtube.com/watch?v={vid}",
                "thumbnail_url": thumb,
                "hashtags": ", ".join(tags[:15]),
                "is_video": True,
                "last_synced": today,
            },
            "views": views,
            "ratio": views / subs if subs else 0,
        })
    return recs


def select_keepers(key, recs):
    """Judge each video against its own channel, then guarantee a minimum."""
    views_list = [r["views"] for r in recs]
    med = statistics.median(views_list) if len(views_list) >= 3 else 0
    kept, keep_ids = [], set()
    for r in recs:
        if r["views"] < MIN_VIEWS:
            continue
        if (med > 0 and r["views"] >= OUTLIER_X * med) or r["ratio"] >= MIN_RATIO:
            keep_ids.add(r["row"]["post_id"])
            kept.append(r)
    passed = len(kept)
    rescued = 0
    if passed < TOP_N_PER_COMP:
        for r in sorted(recs, key=lambda r: -r["ratio"]):
            if r["row"]["post_id"] in keep_ids:
                continue
            keep_ids.add(r["row"]["post_id"])
            kept.append(r)
            rescued += 1
            if len(kept) >= TOP_N_PER_COMP:
                break
    capped = 0
    if len(kept) > MAX_PER_COMP:
        kept.sort(key=lambda r: -r["ratio"])
        capped = len(kept) - MAX_PER_COMP
        kept = kept[:MAX_PER_COMP]
    return kept, passed, rescued, capped, med


# ---------------------------------------------------------------------------
# Cleanup (dashboard weight, not Supabase limits — free tier holds 500MB)
# ---------------------------------------------------------------------------


def cleanup():
    rows = sb("posts?select=post_id,status,last_synced&order=last_synced.asc.nullsfirst")
    if len(rows) <= MAX_ROWS:
        print(f"[Cleanup] {len(rows)} rows <= {MAX_ROWS}, nothing removed")
        return
    overflow = len(rows) - MAX_ROWS
    victims = [r["post_id"] for r in rows if r.get("status") not in PROTECTED][:overflow]
    if not victims:
        print(f"[Cleanup] WARNING: {overflow} over cap but all rows carry work")
        return
    for i in range(0, len(victims), 100):
        chunk = ",".join(victims[i:i + 100])  # video ids are URL-safe
        sb(f"posts?post_id=in.({chunk})", method="DELETE", prefer="return=minimal")
    print(f"[Cleanup] removed {len(victims)} oldest unworked rows")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    if (not (os.environ.get("YOUTUBE_API_KEY") or "").strip() or not SUPABASE_URL
            or not os.environ.get("SUPABASE_SERVICE_ROLE_KEY")):
        print("ERROR: YOUTUBE_API_KEY / SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY required")
        sys.exit(1)

    refs = fetch_competitors()
    if not refs:
        print(f"No active {TRACKER} channels in Supabase — nothing to do.")
        return
    print(f"[{TRACKER}] {len(refs)} channels: {', '.join(refs[:8])}"
          + (" ..." if len(refs) > 8 else ""))
    print(f"  filter: views>={OUTLIER_X}x own median OR views/subs>={MIN_RATIO} | "
          f"floor {MIN_VIEWS} views | keep {TOP_N_PER_COMP}-{MAX_PER_COMP} per channel | "
          f"last {DAYS_BACK} days")

    now = datetime.now(timezone.utc)
    today = now.date().isoformat()
    cutoff = (now - timedelta(days=DAYS_BACK)).strftime("%Y-%m-%dT%H:%M:%SZ")

    rows = []
    print(f"  {'頻道':<26}{'抓到':>5}{'入選':>5}{'補位':>5}  基準")
    try:
        for ref in refs:
            ch = resolve_channel(ref)
            if not ch:
                print(f"  {ref:<26}  找不到這個頻道 — 請檢查 competitors 表的拼寫")
                continue
            stats = ch.get("statistics") or {}
            if stats.get("hiddenSubscriberCount"):
                print(f"  {ref:<26}  訂閱數被隱藏 — 無法計算觀看／訂閱比，已略過")
                continue
            subs = int(stats.get("subscriberCount") or 0)
            if subs <= 0:
                print(f"  {ref:<26}  訂閱數為 0 — 已略過")
                continue
            uploads = ((ch.get("contentDetails") or {}).get("relatedPlaylists") or {}).get("uploads")
            if not uploads:
                print(f"  {ref:<26}  沒有上傳清單 — 已略過")
                continue
            ids = recent_video_ids(uploads, cutoff)
            recs = to_records(video_details(ids), ref, subs, today) if ids else []
            kept, passed, rescued, capped, med = select_keepers(ref, recs)
            rows.extend(r["row"] for r in kept)
            note = f" · 超額截掉 {capped}" if capped else ""
            print(f"  {ref:<26}{len(recs):>5}{len(kept):>5}{rescued:>5}  "
                  f"中位 {int(med):,} 觀看 · {subs:,} 訂閱{note}")
    except QuotaExceeded:
        print("YouTube API 今日額度已用完（每日 10,000 單位，太平洋時間午夜重設）。"
              "已抓到的部分會照常寫入。")

    print(f"  ── 合計 {len(rows)} 條入庫")
    if rows:
        for i in range(0, len(rows), 100):
            sb("posts?on_conflict=post_id", method="POST", data=rows[i:i + 100],
               prefer="resolution=merge-duplicates,return=minimal")
        print(f"[Upsert] {len(rows)} rows written")
    else:
        print("[Upsert] nothing to write — 檢查上面的逐個頻道表格，"
              "或放寬 Variables（MIN_VIEWS / OUTLIER_X / MIN_RATIO）")

    try:
        cleanup()
    except Exception as exc:
        print(f"[Cleanup] skipped ({exc})")

    print("Done.")


if __name__ == "__main__":
    main()
