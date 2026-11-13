-- ============================================================
-- YouTube 爆款雷達 · Supabase 建表
--
-- 這是【全新的 Supabase 專案】專用，不要和 IG 版共用同一個專案。
-- 可以安全重複跑：不會刪任何資料。
--
-- 在 Supabase → SQL Editor 整段貼上 → Run，看到 Success 即可。
-- ============================================================

-- ---------- 爆款影片 ----------
create table if not exists posts (
  id              bigint generated always as identity primary key,
  post_id         text unique not null,             -- YouTube 影片 ID（11 個字元）
  tracker         text not null default 'YT',
  competitor      text,                             -- 頻道（@名稱 或 UC... 頻道 ID）
  caption         text,                             -- 第一行 = 影片標題，其後 = 說明欄
  transcript      text,                             -- 影片口述逐字稿（自動轉錄）
  ai_breakdown    text,                             -- AI 拆解（自動生成）
  my_script       text,                             -- 我的改寫腳本（AI 按 brand_voice.md 改寫）
  post_type       text,                             -- Video / Short
  likes           integer default 0,
  comments        integer default 0,
  views           bigint  default 0,
  followers       integer default 0,                -- 頻道訂閱數
  duration_sec    integer default 0,
  engagement_rate numeric generated always as
      ((likes + comments)::numeric / nullif(views, 0)) stored,
  viral_score     numeric generated always as
      (round(views::numeric / nullif(followers, 0), 2)) stored,   -- 觀看次數 ÷ 訂閱數
  post_date       date,
  post_url        text,
  thumbnail_url   text,
  video_url       text,
  hashtags        text,
  is_video        boolean default true,
  status          text default '未处理',             -- 未处理/拍摄中/已处理/跳过
  last_synced     date,
  created_at      timestamptz default now()
);

-- ---------- 頻道名單（自助管理：加一行 = 追蹤，取消 active = 停） ----------
create table if not exists competitors (
  id         bigint generated always as identity primary key,
  username   text not null,        -- @頻道名稱，或頻道主頁連結，或 UC... 頻道 ID
  tracker    text not null default 'YT',
  active     boolean default true,
  notes      text,
  created_at timestamptz default now()
);

-- ============================================================
-- 資料清洗 + 約束
-- ============================================================

-- 頻道名規範化：去掉 @ 和前後空格
update competitors
   set username = ltrim(btrim(username), '@')
 where username <> ltrim(btrim(username), '@');

-- 去掉重複頻道
delete from competitors a
 using competitors b
 where a.id > b.id
   and lower(a.username) = lower(b.username)
   and a.tracker = b.tracker;

create unique index if not exists competitors_username_uniq
    on competitors (lower(username), tracker);

-- status 白名單：公開 key 能改這一欄，不鎖死的話任何人可以塞任意長文字
update posts set status = '未处理'
 where status is null or status not in ('未处理', '拍摄中', '已处理', '跳过');

alter table posts drop constraint if exists posts_status_chk;
alter table posts add  constraint posts_status_chk
      check (status in ('未处理', '拍摄中', '已处理', '跳过'));

create index if not exists posts_status_idx     on posts (status);
create index if not exists posts_score_idx      on posts (viral_score desc nulls last);
create index if not exists posts_competitor_idx on posts (competitor);
create index if not exists posts_synced_idx     on posts (last_synced);

-- ============================================================
-- 安全鎖（關鍵！）：瀏覽器的公開 key 只能【讀影片】+【改 status 一欄】。
-- 必須先 REVOKE ALL —— Supabase 預設給公開 key 全部權限，不撤掉的話
-- 任何拿到網址的人都能刪光你的資料。
-- ============================================================
alter table posts       enable row level security;
alter table competitors enable row level security;

revoke all on posts       from anon;
revoke all on competitors from anon;

grant select on posts to anon;
grant update (status) on posts to anon;

drop policy if exists "anon read posts"    on posts;
drop policy if exists "anon update status" on posts;
create policy "anon read posts"    on posts for select to anon using (true);
create policy "anon update status" on posts for update to anon using (true) with check (true);

-- ============================================================
-- ↓↓↓ 起始頻道名單：換成你想追蹤的 YouTube 頻道 ↓↓↓
--
--   可以填：@頻道名稱 ／ 頻道主頁連結 ／ UC 開頭的頻道 ID
--   不要填影片連結
--
--   下面 3 個是用 vidIQ 找到的香港頻道（英文內容），只是起步範例，
--   請盡快換成或加上你想對標的廣東話頻道。建議追蹤 8–15 個。
--   重複跑不會報錯，也不會產生重複頻道。
-- ============================================================
insert into competitors (username, tracker, active, notes) values
  ('UCsIxurRK8U77T3DLFosPiCQ', 'YT', true, 'The Quiet Way（香港，道家／簡單生活，起步範例）'),
  ('UChePaEEhlFI5Udm4wniGEoQ', 'YT', true, 'Casey Kwok（香港，起步範例）'),
  ('UC-nbg4TOukTSEPPihoScWXA', 'YT', true, 'Buddhistdoor Global（香港，起步範例）')
on conflict do nothing;
