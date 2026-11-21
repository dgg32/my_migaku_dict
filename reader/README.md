# 読む — local Japanese reader

Paste Japanese text and read it with furigana. Click a word for its Jitendex entry. Highlight sentences for a
simplified-Chinese translation with notes. Discuss the text with an LLM (GLM-5.3-Flash via OpenCode Go by default) in a chat sidebar.

## Setup

```sh
cd reader
uv venv --python 3.13 .venv          # fugashi has no Python 3.15 wheel yet
uv pip install --python .venv/bin/python -r requirements.txt
cp .env.example .env                 # then put your OpenCode API key in .env
.venv/bin/python server.py           # first run builds data/jitendex.sqlite from ../jitendex-yomitan.zip (~10 s)
```

Open http://127.0.0.1:8787.

## Use

- **Paste:** click **📋 Paste** or press ⌘V in the reading view. **🗑 Clear** empties the page, and **✎ Edit** lets you edit the text. Apple Books' "Excerpt From…" footer is removed.
- **Look up:** click a word. The popup shows the longest dictionary match first, then shorter matches
  ("Also: 一"). Words with a dotted gold underline are multi-word expressions (一にも二にも, 的を射た, 日がな一日).
- **Translate:** drag over one or more sentences, then press **译 Translate**. The selection expands to whole
  sentences. Press 💬 in the translation card to carry it into the chat.
- **Chat:** **💬 Chat** opens the sidebar. Every request sends the text as context, up to its first 60,000 characters.
  Translation requests send up to 4,000 characters of surrounding text.

## How words are matched

1. **Tokenize:** fugashi + unidic-lite split each line into tokens.
2. **Longest match:** from each token, try spans of up to 12 tokens, longest first, against all Jitendex
   headwords. A span matches if its surface text is a headword. It also matches if replacing its last token
   with that token's dictionary form gives a headword (的を射た → 的を射る). Fallbacks: katakana → hiragana,
   potential forms (抱ける → 抱く), and kana words found by their reading (すぎる → 過ぎる).
3. **Absorb endings:** trailing auxiliaries (た, て, ない, れる, ている, すぎる…) join the clickable word.
4. **Furigana:** Jitendex's reading is preferred. The tokenizer's reading only picks among Jitendex's
   readings. It is used directly only where Jitendex has no entry, as with character names, and for
   conjugated words whose dictionary-form reading agrees with Jitendex (来ない → こない).
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

The prompts are `TRANSLATE_SYSTEM` and `CHAT_SYSTEM` in `server.py`.
