# 📡 YouTube 爆款雷達

每週自動抓你指定的 YouTube 頻道的爆款影片 → 轉錄口述 → AI 拆解 → **改寫成你自己語氣的影片腳本** → 生成可拖拽管理的看板。

這是 IG 版「爆款雷達」的 YouTube 版，**和 IG 版完全分開**：另一個倉庫、另一個 Supabase 專案、另一個網址。

與 IG 版的差別：

| | IG 版 | YouTube 版 |
|---|---|---|
| 資料來源 | Apify（約 US$5／月） | **YouTube 官方 API（免費）** |
| 追蹤對象 | IG 帳號 | YouTube 頻道（@名稱、頻道連結或頻道 ID） |
| 爆款判斷 | 互動率、留言數 | **觀看次數 ÷ 訂閱數**、和頻道自己的平均比 |
| 轉錄 | Whisper | **Gemini 直接讀 YouTube 影片**（支援廣東話） |

---

## 開始前：準備 4 個帳號

- **GitHub**（已有）：存程式碼 + 每週自動運行
- **Supabase**（supabase.com）：免費資料庫。**要開新專案，不要用 IG 版那個**
- **Google AI Studio**（aistudio.google.com）：免費，拿 Gemini 鑰匙，做轉錄、拆解、改寫
- **Google Cloud**（console.cloud.google.com）：免費，拿 YouTube Data API 鑰匙

---

## 第 1 步 · 建新倉庫

在 GitHub 建一個新倉庫（例如 `youtube-radar`），選 **Public**（免費版要公開才能出網頁），再把這個資料夾的檔案上傳進去。

## 第 2 步 · 申請 YouTube API 鑰匙（免費）

1. 到 console.cloud.google.com，用 Google 帳號登入，按左上角專案選單 → **New project**，名字隨意。
2. 左邊 **APIs & Services → Library**，搜尋 **YouTube Data API v3** → **Enable**。
3. **APIs & Services → Credentials → Create credentials → API key**，複製（`AIza` 開頭）。
4. 建議按這把鑰匙旁的 **Edit → API restrictions → Restrict key**，只勾 YouTube Data API v3。

每日免費額度 10,000 單位；每個頻道每次約用 3 單位，用不完。

## 第 3 步 · 建 Supabase 資料庫

1. supabase.com → **New project**（Region 選 Singapore，資料庫密碼自己存好）。
2. 左邊 **SQL Editor → New query**，把 `schema.sql` **整段**複製貼上。
3. 最底下的 3 個頻道只是起步範例，換成你想對標的頻道。可以填 `@頻道名稱`、頻道主頁連結或 `UC` 開頭的頻道 ID；**不要填影片連結**。
4. 按 **Run**，看到 Success。到 **Table Editor** 確認 `posts` 和 `competitors` 兩張表都在。

> 建議追蹤 8–15 個頻道，看板才夠東西看。想多加就跑 `加竞对.sql` 那一小段，或到 Table Editor 直接加行。
> 「安全鎖」那段不要刪：公開鑰匙只能讀資料、改狀態，刪不掉你的資料。

## 第 4 步 · 複製 3 把 Supabase 鑰匙

Supabase 左下角 **Settings → API Keys**：Project URL、`sb_publishable_` 開頭的鑰匙、`sb_secret_` 開頭的鑰匙。
**secret key 只放進下一步的 Secrets，不要貼到任何地方。**

## 第 5 步 · 在倉庫填鑰匙和名字

倉庫 → **Settings → Secrets and variables → Actions**。

**Secrets（名字要一模一樣）：**

| Secret | 值 |
|---|---|
| `YOUTUBE_API_KEY` | 第 2 步的 YouTube API 鑰匙 |
| `SUPABASE_URL` | Project URL |
| `SUPABASE_ANON_KEY` | publishable key |
| `SUPABASE_SERVICE_ROLE_KEY` | secret key |
| `GEMINI_API_KEY` | Google AI Studio → Get API key |

可選：`ANTHROPIC_API_KEY`（付費，設了就優先用 Claude 拆解和改寫）。

**Variables：**

| Variable | 例子 |
|---|---|
| `BRAND` | `古璟檑 · YouTube 雷達` |
| `NICHE` | `生活覺察、轉念、銷售心態` |
| `SYNC_DAY` | `每週一` |

## 第 6 步 · 開網頁

倉庫 → **Settings → Pages**：Source 選 **Deploy from a branch**，Branch 選 **main**，資料夾選 **/docs**，**Save**。

## 第 7 步 · 首次運行

1. **Actions** 分頁 → 若有提示就按 **I understand my workflows, enable them**。
2. 左邊 **Sync Content → Run workflow**。之後會自動接力：轉錄 → AI 拆解與改寫 → 生成網頁。
3. 跑完後網址是 `https://<你的用戶名>.github.io/<倉庫名>/`。

---

## 調整內容多寡（不用改程式）

倉庫 → Settings → Variables，新增想改的：

| Variable | 預設 | 作用 |
|---|---|---|
| `MIN_VIEWS` | `500` | 觀看低於這個數的影片直接丟掉 |
| `OUTLIER_X` | `1.5` | 觀看達到頻道自己中位數的幾倍算爆款 |
| `MIN_RATIO` | `1.0` | 觀看 ÷ 訂閱達到多少也算爆款 |
| `TOP_N_PER_COMP` | `3` | 每個頻道至少保留幾條 |
| `MAX_PER_COMP` | `15` | 每個頻道最多幾條 |
| `DAYS_BACK` | `90` | 回看多少天 |
| `RESULTS_LIMIT` | `50` | 每個頻道讀最新多少條 |
| `VIDEO_LIMIT` | `25` | 每次最多轉錄幾條影片 |
| `MAX_MINUTES` | `25` | 超過這個長度的影片不轉錄（省 Gemini 免費額度） |

## 「我的稿」：把爆款改寫成你的影片腳本

`brand_voice.md` 已按你的 CLAUDE.md 寫好人設、語氣、固定結構和紅線。改過它之後：
Actions → **Rewrite To My Voice** → Run workflow → `rewrite_all` 填 `1`，再跑 **Deploy Dashboard** 刷新網頁。

⚠️ 腳本是 AI 改寫的，**拍之前一定要自己逐條看過**，特別是有沒有碰到紅線。

## 卡住了

| 現象 | 怎麼辦 |
|---|---|
| 抓到 0 條 | 看 Actions → Sync Content 日誌的逐個頻道表格：寫「找不到這個頻道」= 拼寫錯；「訂閱數被隱藏」= 該頻道藏了訂閱數，無法計算，已略過 |
| 日誌顯示 `quota` | 當日 YouTube 額度用完，隔天自動重設 |
| 沒有逐字稿 | 看 Transcribe Videos 日誌：`RATE LIMITED` = Gemini 免費額度用完，隔天補；`UNAVAILABLE` = 私人或限制級影片 |
| 網頁 404 | 先等首跑完整結束；再看 Actions 有沒有紅色失敗 |
| 網頁標題不對 | `BRAND` 沒設或拼錯，設好後重跑 Deploy Dashboard |

## 三條紅線

1. **secret key**（`sb_secret_` 開頭）和 **YouTube／Gemini 鑰匙**只進 Secrets，不要貼到別處或發給別人。
2. 倉庫是 Public：看板上的頻道資料和 AI 拆解，任何人拿到連結都看得到。
3. `schema.sql` 裡的安全鎖那段不要刪。
