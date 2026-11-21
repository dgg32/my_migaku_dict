# 読む — local Japanese reader

Paste Japanese text and read it with furigana. Click a word for its Jitendex entry. Highlight sentences for a
simplified-Chinese translation with notes. Discuss the text with an LLM (GLM-5.3-Flash via OpenCode Go by default) in a chat sidebar.

## Setup

**1. Download the two dictionaries to the repo root** (the folder above `reader/`). Both are gitignored.

| File | What | Where |
|---|---|---|
| `jitendex-yomitan.zip` | Jitendex, the word dictionary (required) | the **Yomitan** version from https://jitendex.org |
| `JMnedict.xml.gz` | JMnedict, the names dictionary for the popup's Names section (optional but recommended) | `curl -O http://ftp.edrdg.org/pub/Nihongo/JMnedict.xml.gz` (12 MB) |

**2. Install and start:**

```sh
cd reader
uv venv --python 3.13 .venv          # fugashi has no Python 3.15 wheel yet
uv pip install --python .venv/bin/python -r requirements.txt
cp .env.example .env                 # then put your OpenCode API key in .env
.venv/bin/python server.py
```

The first start builds `data/jitendex.sqlite` from the Jitendex zip (~10 s). If `JMnedict.xml.gz` is present, it
also adds the names (~5 s). A names file downloaded later is picked up on the next start. To refresh either
dictionary, download the new file, delete `data/jitendex.sqlite` and start the server.

Open http://127.0.0.1:8787.

## Use

- **Paste:** click **📋 Paste** or press ⌘V in the reading view. **🗑 Clear** empties the page, and **✎ Edit** lets you edit the text. Apple Books' "Excerpt From…" footer is removed.
- **Look up:** click a word. The popup shows the longest dictionary match first, then shorter matches
  ("Also: 一"). Words with a dotted gold underline are multi-word expressions (一にも二にも, 的を射た, 日がな一日).
- **Translate:** drag over one or more sentences, then press **译 Translate**. The selection expands to whole
  sentences. Press 💬 in the translation card to carry it into the chat.
- **Read aloud:** 🔊 in the word popup reads the word with the reading shown in its furigana (抱いた → だいた).
  **🔊 Read** in the selection toolbar, or 🔊 in the translation card, reads whole sentences. Words whose reading
  was chosen by context are spoken as kana, so the voice agrees with the furigana. The top-bar **🔊 1×**
  button switches between 0.75×, 1× and 1.25× speed. Audio comes from macOS's `say` and is cached in
  `data/tts/`.
- **Names:** the word popup has a **Names** section from JMnedict, listing every reading of a name with its type
  (surname, given name, place…). Only the book knows how a fictional character's name is read, so press
  **📌 Use** on the right reading: every occurrence changes in the furigana and the voice, and the choice is
  remembered for later pastes (`data/pinned_readings.json`). Press **📌 Pinned** to undo it. A pin never touches
  counters (円 → まどか leaves 三百円 alone). Person names stay out of the AI reading check, which could only guess.
- **Chat:** **💬 Chat** opens the sidebar. Every request sends the text as context, up to its first 60,000 characters.
  Translation requests send up to 4,000 characters of surrounding text.

## A better Japanese voice

macOS ships with a small, flat-sounding Kyoko. **Kyoko (Enhanced)** (131 MB) sounds much more natural, but the
download is hidden:

1. Open **System Settings → Accessibility → Read & Speak**.
2. Set **System speech language** to **Japanese**. Otherwise only English voices are listed.
3. Click the **ⓘ** button to the right of **System voice**. The menu itself only shows installed voices.
4. In the **Voice** list, click the download (cloud) icon next to **Kyoko (Enhanced)**. **O-Ren (Enhanced)** (179 MB)
   and **Otoya (Enhanced)** (93 MB, male) are also available.
5. Restart the reader server. Its first lines should say `Read-aloud voice: Kyoko (Enhanced)`.

The app picks the best installed Japanese voice by itself. To use another one, set `READER_VOICE`, for example
`READER_VOICE=O-Ren (Enhanced)`. The **System speech language** can go back to English afterwards: the app asks
for the voice by name. The Siri voices in that menu can't be used by apps; they produce silence.

## Long texts

A 150,000-character paste shows its first words in under 0.25 s and is fully loaded in about 1.3 s:
- **Streaming:** the server sends paragraphs as it analyzes them (`/api/analyze_stream`), and the page draws each
  chunk as it arrives.
- **Off-screen paragraphs:** they skip layout and painting (`content-visibility: auto`). Each one gets a height
  estimate, so the scrollbar stays steady.
- **Selecting and highlighting:** these look only at the selection's ends, so they take under 1 ms at any length.
- **Reading check:** the AI reading check runs only on paragraphs within a screen or two of where you are reading.

## How words are matched

1. **Tokenize:** fugashi + unidic-lite split each line into tokens.
2. **Longest match:** from each token, try spans of up to 12 tokens, longest first, against all Jitendex
   headwords. A span matches if its surface text is a headword. It also matches if replacing its last token
   with that token's dictionary form gives a headword (的を射た → 的を射る). Fallbacks: katakana → hiragana,
   potential forms (抱ける → 抱く), and kana words found by their reading (すぎる → 過ぎる).
   **Other kanji:** if a phrase of several tokens has no dictionary match, it may be written with other kanji than
   Jitendex uses. It then matches a headword with the same reading and the same kana pattern: 吞んだくれている
   (吞 or 呑) → 飲んだくれる. Single words are excluded, so words that merely sound alike (会う / 遭う) are
   never confused.
3. **Absorb endings:** trailing auxiliaries (た, て, ない, れる, ている, すぎる…) join the clickable word.
   する joins a noun that takes it (接見された). The popup names the conjugation steps after the headword,
   as 10ten does: 接見 (< -suru < passive or respectful < past), 食べる (食べられる < passive, potential or respectful).
4. **Furigana:** Jitendex's reading is preferred. The tokenizer's reading only picks among Jitendex's
   readings. It is used directly only where Jitendex has no entry, as with character names, and for
   conjugated words whose dictionary-form reading agrees with Jitendex (来ない → こない).
5. **AI reading check** (the **AI 読み** button, on by default): words with several Jitendex readings
   (抱く いだく/だく, 認める みとめる/したためる, 市場 いちば/しじょう…) are sent with their sentences to the
   model, which picks one of Jitendex's readings. Furigana updates about 2 s after pasting. Readings the model
   changed show in the accent colour, and their popup lists the matching entry first. Rare and obsolete
   readings are never offered, and 来る/する are left to the tokenizer.
6. **Greetings:** a match that is only an interjection (今日は, 今晩は) counts only before a sentence end, so
   今日は暑い reads きょう.
   Readings are aligned so furigana sits over the kanji only.

## Tests

```sh
uv pip install --python .venv/bin/python pytest
.venv/bin/python -m pytest -q
```

[tests/test_reader.py](tests/test_reader.py) has golden cases for readings (来ます, 抱ける, 一にも二にも…),
redirect resolution (數, 機關, 鐵漿), and UTF-16 offsets.

## Config (`.env`)

| Variable | Default |
|---|---|
| `OPENCODE_API_KEY` | (required for translation and chat) |
| `OPENCODE_BASE_URL` | `https://opencode.ai/inference/go/openai/v1` (OpenCode Go). For pay-as-you-go Zen credits use `https://opencode.ai/inference/openai/v1` |
| `OPENCODE_MODEL` | `glm-5.3-flash`: text starts in about 1 s. `deepseek-v4.1-flash` gives similar quality but takes 20–60 s |
| `OPENCODE_REASONING_EFFORT` | `low`. Use `default` to leave thinking to the model |
| `READER_PORT` | `8787` |
| `READER_VOICE` | Picked automatically: the best installed Japanese voice (Premium, then Enhanced, then Kyoko). Set it to force one, such as `Otoya (Enhanced)`. See [A better Japanese voice](#a-better-japanese-voice) |

The prompts are `TRANSLATE_SYSTEM` and `CHAT_SYSTEM` in `server.py`.
