-- ============================================================
-- 加頻道專用。只跑這一段，永遠不要重跑 schema.sql 的整段。
-- 填 @頻道名稱、頻道主頁連結或 UC 開頭的頻道 ID；不要填影片連結。
-- 重複跑不會報錯。
-- ============================================================
insert into competitors (username, tracker, active) values
  ('@新頻道1', 'YT', true),
  ('@新頻道2', 'YT', true),
  ('@新頻道3', 'YT', true)
on conflict do nothing;

-- 想停掉某個頻道（保留資料，只是不再抓）：
-- update competitors set active = false where lower(username) = lower('要停的頻道名');

-- 看現在在追蹤哪些：
-- select username, tracker, active, notes from competitors order by active desc, username;
