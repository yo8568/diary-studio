# Diary Studio

把手機拍的雙人日記影片變成有字幕的成品。Mac 本機工具，影片、語音、字型都不離開這台電腦。

一支 6 分鐘的直式影片，從丟檔案到出片大約 5 分鐘，其中大部分是等語音辨識。

## 做了什麼

1. **加速 5%** — 口語日記的節奏通常偏慢，加速後不改音高
2. **語音辨識** — Whisper large-v3 走 Apple GPU，產出字級時間戳
3. **找回被丟掉的聲音** — Whisper 會把笑聲和「嗯」當雜訊丟掉，這裡把它們找回來，同時擋掉短切片常見的幻覺（`谢谢大家` 那類）
4. **分辨說話者** — 聲紋比對，每個字附帶信心值；校正過的結果會存起來，下一支片自動沿用同一組人的身份
5. **你校對** — 邊看影片邊改說話者、切／併輪次、修錯字
6. **出片** — 逐字亮的字幕燒進畫面

## 為什麼字幕是自己畫的

Homebrew 的 ffmpeg 沒有編進 libass，`subtitles` 和 `ass` 濾鏡都不存在。所以字幕用 PIL 逐狀態畫成 RGBA 圖序列，再用 `overlay` 合成。

代價是多寫一些程式，換來的是描邊、陰影、逐字亮的顏色都在同一個地方決定，不必遷就字幕格式的方言。

## 安裝

需要 macOS（Apple Silicon）、ffmpeg、Python 3.12。

```bash
brew install ffmpeg
uv venv ~/.venvs/whisper --python 3.12
VIRTUAL_ENV=~/.venvs/whisper uv pip install -r requirements.txt
```

`resemblyzer` 依賴的 `webrtcvad` 還在用 `pkg_resources`，所以 setuptools 必須低於 81 — 這已經寫進 `requirements.txt`。

建立 app：

```bash
./scripts/make_app.sh            # 產生 ~/Applications/Diary Studio.app
```

app 是啟動器不是打包檔：mlx 和 torch 有好幾百 MB，而且 mlx 要真正的 Metal 環境，凍結進 bundle 在單機使用上沒有好處。所以它指向這個 repo 和 venv，改程式即時生效。

也可以直接跑：

```bash
~/.venvs/whisper/bin/python -m diary            # 原生視窗
~/.venvs/whisper/bin/python -m diary --browser  # 用瀏覽器
```

## 介面

左邊影片，右邊逐字稿。

- 點時間碼跳到那一句
- 點說話者色塊換人
- **切** 在游標處把一段拆成兩段（換手切錯時用）
- **併** 跟上一段合併
- 打 `⚠` 的是聲紋信心接近 0 的段落，多半是插在對方話中間、不到一秒的短句 — 那個長度聲紋模型分不出來，只能用耳朵
- 文字可以直接改：刪掉贅字、加標點都行。保留的字會沿用原本的時間戳，所以逐字亮仍然對得上嘴型，只是不顯示刪掉的部分

**字型與樣式**在底部的「字型樣式」裡：字型、字重、字級、字距、描邊粗細與顏色、
底字色、陰影三項、每行字數。字型從系統的中文字型中列出（`fc-list :lang=zh-tw`），
`.ttc` 的每個字重是檔案裡的一個 index，不是 CSS 的 weight。

**字幕位置直接拖。** 在預覽畫面上按住字幕拖曳即可，上下改高度、左右改水平位移，
放開就存起來。拖曳時會顯示基準線和座標讀數。預覽和輸出用同一組座標，拖到哪就出到哪。

改完按「套用修改」，再按「出片」。

## 已知限制

- **短插話分不出來** — 聲紋向量需要約一秒的乾淨人聲。0.4 秒的插話，1 秒的分析窗會同時吃到兩個人，判別值趨近 0。這是解析度問題，不是調參數能解決的（實測窗長 0.75/1.0/1.2 秒 × 切換代價 0.6/1.2/2.0 全部失敗）
- **斷句靠字數不靠語意** — Whisper 的中文輸出標點很少，所以字幕塊滿 11 字就斷

## 圖示

`scripts/make_icon.py` 產生 app 圖示和 favicon。圖案是一張 16×16 的點陣圖：
16 整除 macOS 需要的每個尺寸（16/32/64/128/256/512/1024），所以每次輸出
都是整數倍縮放，小尺寸也不會糊。改圖案就改那張字元陣列。

```bash
python scripts/make_icon.py && ./scripts/make_app.sh
```

## 說話者與預設值

說話者的名字和顏色不寫在程式裡。新專案從 `~/.diary-studio/settings.json` 取得預設，
一開始是「A」和「B」。在底部把名字改成實際的人、調好顏色，按「存為預設」，
之後每支新影片都會沿用。

聲紋參考是用名字當索引的，所以名字定下來之後，跨影片的身份就會穩定。

## 檔案放哪

```
~/.diary-studio/projects/<專案>/   影片、逐字稿、字幕圖、成品
~/.diary-studio/settings.json      說話者名字、顏色、樣式預設
~/.diary-studio/voices.json        學到的聲紋參考
~/Library/Logs/diary-studio.log    app 的執行紀錄
```
