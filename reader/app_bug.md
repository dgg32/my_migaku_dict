# Bug / improvement review — `reader/` (Japanese reader app)

Round: 2026-11-21. Reviewed `reader/server.py`, `reader/build_db.py`,
`reader/static/index.html`, `reader/README.md`, `requirements.txt`, `.env.example`,
`.gitignore`, plus the built `data/jitendex.sqlite` (436,555 rows) and a running
instance of the app. No code was modified.

Everything below was reproduced against the running server (`127.0.0.1:8787`) or by
querying the generated SQLite DB. The app starts cleanly, the DB builds in ~10 s
(matches the README), and the core feature set (furigana, longest-match popup,
sentence selection, translation/chat streaming) works. There are two real
correctness bugs and a handful of robustness/UX issues.

## Architecture as reviewed

- `build_db.py` converts `../jitendex-yomitan.zip` into `data/jitendex.sqlite`
  (`terms` + `tags`) and copies `styles.css` → `data/jitendex.css` (lines 18–78).
- `server.py` loads `expression → [readings]` into `READINGS`, tokenises with
  fugashi+unidic-lite, picks the longest dictionary span per token, rebuilds a
  reading, and returns per-line segments `{s,e,t,r,k,w,m,p}` (`/api/analyze`);
  `/api/lookup` resolves a key to Jitendex entries; `/api/translate` and `/api/chat`
  stream an OpenAI-compatible endpoint (lines 46–490).
- `index.html` renders segments as `<span class="w">` + `<ruby>`, handles selection →
  whole-sentence expansion, a dictionary popup, a translation popup, and a chat sidebar.

---

## Bugs

### A1 (high, wrong readings) — every inflected form of 来る gets く instead of the contextual reading

Reproduced:

| input | shown | correct |
|---|---|---|
| 来て | 来(く)て | きて |
| 来ている | 来(く)ている | きている |
| 来ます | 来(く)ます | きます |
| 来ない | 来(く)ない | こない |
| 来い | 来(く)い | こい |
| 来られる | 来(く)られる | こられる |
| 来させる | 来(く)させる | こさせる |
| 来すぎる | 来(く)すぎる | きすぎる |

(来た happens to render correctly only because Jitendex contains an explicit surface
entry 来た.)

Cause: `word_reading()` (`server.py:216-232`) prefers the **dictionary headword's
reading stem** over the tokenizer's contextual kana. For 来ます the span key is 来る,
`pick_reading()` returns くる, the shared る is stripped (`server.py:227-228`), and the
ruby is rebuilt as `くる` − `る` + `ます` → くます. The tokenizer knows the right kana
(`t.kana = き`, `t.kana_base = くる`, verified with fugashi), and the function already
computes it in `guess_surface` / `guess_base`, but only uses it to *choose* among
Jitendex readings, never to build the stem.

Suggested fix (no code written): for inflected spans (`inflected == True`), return the
tokenizer-derived reading of the matched span (`guess_surface`, e.g. きます / こい)
when it exists and contains no kanji; keep the current dictionary-stem reconstruction
as a fallback. This preserves the cases the current code handles well (食べた → たべた,
行った → いった, 抱ける → いだける) and fixes the irregular-stem class. The same defect
will apply to any future irregular verb whose stem kana changes under inflection.

Regression test candidates: 来ます, 来ない, 来い, 来て, 来ている, 来られる, 来させる,
来すぎる, plus the existing good cases above.

### A2 (high, wrong entry) — redirects are resolved by expression + score, ignoring the row's own sequence

`lookup_key()` (`server.py:331-337`) resolves a "⟶ X" row with
`rows_for("expression = ?", (target,), 5)` and takes `trows[0]` (highest score). The
row's own `sequence` is stored in the DB (`terms.sequence`, negative for redirects) and
points exactly at the intended entry — `abs(seq)` is the target sequence. Verified in
the source zip: 數 has rows `-1005580 / -1580820 / -1580825` which correspond to
数/しばしば, 数/かず, 数/すう; 機關 has `-1003010 / -1220870` → 機関/からくり,
機関/きかん.

Quantified: among 138,071 redirect rows, **1,027 resolve to a different sequence than
`abs(seq)`** when using expression+score. Concrete wrong lookups:

```
數   → app: 数 (seq 1580820, reading かず)     [its seq -1005580 means the 数/しばしば entry]
機關 → app: 機関 (seq 1220870, reading きかん)  [its -1003010 row means 機関/からくり]
鐵漿 → app: 鉄漿 (seq 2858957, reading かね)    [its -1002020 row means 鉄漿/おはぐろ]
```

Worse: the app collapses the three 數 redirect rows into a single popup entry, so the
per-reading mapping is lost even when the chosen entry happens to be plausible.

Suggested fix: resolve with `WHERE sequence = abs(row.sequence)` first (one indexed
lookup, the DB already stores it), falling back to the expression lookup only if the
sequence row is missing. Note the Migaku converter in `migaku-supplement/` already
resolves by sequence, so this would also make the reader agree with the exported
dictionary.

### A3 (medium) — character offsets are Python code points but the client slices JS UTF-16 units

`analyze()` returns `s`/`e` computed with Python string indices (`server.py:235-289`),
which count Unicode code points. `index.html` feeds those values to
`String.prototype.slice` and to manual indexing (`state.text.slice(s,e)`,
`state.text[a]`, `server.py`-derived offsets at `index.html:344-363, 365-377, 471`).
Any astral character (emoji, rare-kanji Ext-B like 𠮷) shifts every later offset by one
per surrogate pair.

Demonstrated with `text = "𠮷野家🍣で食べた。"`:

```
server says 食べた is s=5, e=8
JS  "𠮷野家🍣で食べた。".slice(5,8) === "\udf63で食"   // broken surrogate + wrong chars
```

Consequences: selecting a sentence that contains an earlier emoji/rare kanji produces
garbled text in the translate request and in `state.sel.text`; sentence expansion can
also start/end mid-surrogate. Everything stays visually correct, so this is easy to
miss.

Fix options: return offsets in UTF-16 units from the server
(`len(s.encode("utf-16-le")) // 2` per segment/prefix), or keep code points and convert
the whole text + offsets on the client (`Array.from(text)` index maps). Option 1 is
cheaper and keeps one representation end-to-end.

### A4 (medium, concurrency risk) — one module-level `fugashi.Tagger()` is used by all FastAPI threadpool threads

`tagger = fugashi.Tagger()` (`server.py:118`) is shared by every sync endpoint call.
FastAPI runs sync handlers in a threadpool, so two concurrent `/api/analyze` requests
parse through the same `mecab_t`. MeCab's own documentation states `MeCab::Tagger` is
for single-threaded use; for multithreaded use you create one Tagger per thread
(sharing a Model) or a Lattice per thread. An 8-way parallel `/api/analyze` smoke test
passed here, so this is a latent risk rather than an observed failure, but it can
surface as rare corrupted/empty results or crashes under load.

Fix: `threading.local()` Tagger per thread (same pattern as `db()`), or a
`threading.Lock()` around tokenisation, or `anyio.to_thread` with one tagger per
worker. The DB connection already uses the thread-local pattern; the tagger was missed.

### A5 (low, UX) — a chat message is silently lost if sent while a reply is streaming

`#chat-form` submit clears the textarea *before* calling `sendChat` (`index.html:543`),
and `sendChat` returns immediately when `state.busy` is true (`index.html:517-519`).
Sending a second message while the model is replying therefore discards it. Keep the
draft when busy, or queue the message, or disable the send button while busy.

### A6 (low, UX) — selection toolbar can render off-screen

`#selbar` placement has no lower clamp and no vertical flip (`index.html:394-395`):
`left` can go negative for narrow windows/left-aligned selections, and `top` can exceed
the viewport bottom when the selection is near the bottom of the screen. Clamp
`left ≥ 8` and flip above the selection when `top + barHeight > viewportHeight`.

### A7 (low, UX) — popups/toolbar detach from the text when the reading pane scrolls

The scroll container is `#main` (`index.html:49`), while `#dictpop`, `#trpop`, and
`#selbar` are siblings of `#layout` (`index.html:175-180`). Their viewport-based
coordinates are correct at open time (`place()`, `index.html:324-334`) but nothing
listens to `#main`'s scroll, so scrolling leaves an open popup/toolbar floating over
unrelated text. Add a scroll listener on `#main` that hides (or repositions) them, as
is already done for `resize` (`index.html:574`).

### A8 (low, robustness) — fetch failures leave the UI stuck and hide real errors

- `load()` (`index.html:203-214`) does not check `r.ok` and has no `try/catch`; a 422/500
  or a stopped server leaves the status at "Analyzing…" and throws an unhandled promise
  rejection (same for `fetch("/api/config")`, `index.html:581`).
- `lookup()` (`index.html:297-322`) leaves "Looking up…" forever on failure and does not
  check `r.ok`.
- `/api/translate` validation errors return JSON (`{"detail": …}`) which is rendered as
  the translation text by `streamPost` (`index.html:421-433`).

Add `if (!r.ok) throw …` plus a small error path (status/toast/popup message), and in
`streamPost` surface non-200 bodies as an error rather than as model output.

### A9 (low) — lookup response race

Clicking word A then word B quickly issues two `/api/lookup` requests; whichever
finishes last wins (`index.html:303-320`). A request counter (or `AbortController`)
should make the newest click authoritative.

### A10 (low) — stale port in the module docstring

`server.py:3` says `http://127.0.0.1:8765`; the default and README are `8787`
(`server.py:42`). Trivial but misleading.

### A11 (low, data model) — `terms.term_tags` is always empty and the name misleads

`build_db.py:51-58` stores `r[2]` as `def_tags` and `r[7]` as `term_tags`. In the
Jitendex source every row has `r[7] == ""` and all tags (★, priority form, old kanji
form, …) live in `r[2]`. Verified:

```
term_tags distinct: []   def_tags distinct: ['★ priority form', 'old kanji form', ...]
```

`split_tags(term_tags, def_tags)` (`server.py:347`, `297-307`) still works because the
tag names in the DB use NBSP internally and regular spaces between tags, but the schema
name and the always-empty column are confusing and could mislead future changes.

### A12 (cosmetic) — glossary images are dropped silently

`sc()` only accepts `SC_TAGS` and returns an **empty text node** for any other tag
(`index.html:254, 261`). The only tag outside that set in the source is `img`
(16 occurrences in 20k sampled glossaries; the Jitendex build has 444 images). The
content after an image is preserved, so this is just missing pictures; consider
rendering a link/placeholder or documenting the omission.

### A13 (cosmetic) — furigana is drawn over okurigana in the dictionary popup

`headwordHTML()` wraps the whole expression in `<ruby>` (`index.html:293-295`), so
食べる shows 食べる(たべる) with ruby over the kana べる as well. The reading view already
aligns ruby to kanji only (`ruby_parts`); reusing that splitter in the popup would look
consistent.

### A14 (cosmetic) — glossary bullets appear for single-sense (div-shaped) entries

`data/jitendex.css` removes glossary bullets only under `li[data-sc-content="sense"]`.
Most single-sense groups use `div[data-sc-content="sense"]` (274k of 356k senses), so
their glossaries fall through to `ul[data-sc-content="glossary"] { list-style-type: disc }`
and render as bulleted lists, unlike Jitendex (which uses ①/② headers). Minor visual
inconsistency in the popup.

### A15 (docs) — README overstates the chat context size

README: "Every request sends the whole text as context." The server truncates the
article to 60,000 characters (`server.py:481`) and translation context to 4,000
(`server.py:474`) without telling the user. Either state the limits or send a
"truncated" indicator.

---

## Verified-OK (checked, no issue found)

- **Build:** `build()` reproduces the shipped DB in **10.4 s**, 641 MB, 436,555 rows —
  README's "~10 s" is accurate. Indexes on `expression`/`reading` are used.
- **Startup:** `READINGS` load takes 0.46 s; server prints model/key state correctly;
  `has_key: false` is reflected in the sidebar.
- **Analysis performance:** 17.5k-character text analysed in 0.25 s; 8 parallel requests
  all completed in 0.13 s (see A4 for the caveat that this doesn't prove thread safety).
- **Longest-match / phrase logic:** 一にも二にも, 的を射た, 日がな一日 get the dotted
  phrase underline; 取り扱い returns 取り扱い → 取 as "Also" keys; 三人, 毎朝, 取り扱い,
  打ち合わせ, 持ち帰り, 言い訳, 出て行った, 見ている, 食べさせられた, 食べさせてあげたかった,
  行ってらっしゃい all produce correct readings/spans.
- **Absorption:** た/て/ている/ない/られる/させる/すぎる all attach correctly and extend
  the reading (食べました → たべました, 食べなかった → たべなかった, 食べすぎる → たべすぎる).
- **Potential forms / kana fallbacks:** 抱ける → 抱く with reading いだける; きれい →
  綺麗 via reading lookup; すぎる → 過ぎる.
- **Tags:** the 8 tag-bank names are loaded and `split_tags` correctly splits
  NBSP-joined multi-tag strings (★ priority form, rarely used form ateji form, …);
  chips render as expected for 思い止どまる, 一, 取る, 来る.
- **Redirect happy path:** 思い止どまる correctly shows 思いとどまる with "← 思い止どまる".
- **Structured content:** source tag inventory is exactly `SC_TAGS` + `img`; recursion,
  query links (`a.q`), attribution, examples, forms tables all render; XSS is avoided
  (text nodes via `createElement`/`esc`, `rel="noopener"` on external links).
- **Selection → sentences:** expansion back to the previous 。/newline and forward over
  closers works; multi-paragraph selections stop at newlines.
- **JS syntax:** inline script parses cleanly (`node --check`); no duplicate element IDs
  in the static DOM.
- **Config:** `.env` is loaded with `setdefault` so shell env wins; `.env.example` matches
  the README table; `.gitignore` excludes `.venv/`, `data/`, `.env`.

## Improvement suggestions (prioritised)

1. **Fix A1 and A2** — both are user-visible correctness bugs and both have small,
   local fixes (tokenizer kana for inflected spans; sequence-based redirect lookup).
2. **Fix A3** — pick one offset convention (UTF-16 is the path of least resistance for a
   JS client) and assert it in a test with an emoji and an Ext-B kanji.
3. **Fix A4** — thread-local `fugashi.Tagger()`; the `db()` thread-local is a ready-made
   pattern.
4. **Add a tiny test suite** (pytest): golden `analyze()` cases for readings/absorptions
   (including 来ます, 来い, 抱ける, 食べさせられた), a redirect-resolution test
   (數/機關/鐵漿), and API smoke tests with `TestClient`. None exist today, and the
   reading heuristics are subtle enough that refactors will silently regress them.
5. **Pin `fugashi` and `unidic-lite`** in `requirements.txt`: a dictionary/version bump
   changes tokenisation and therefore every reading. Record the versions in the README.
6. **Robustness pass on the frontend (A8/A9/A5/A6/A7)**: `r.ok` checks + error states,
   request sequencing/abort, keep-or-queue chat drafts, clamp/flip `#selbar`, hide or
   reposition popups on `#main` scroll.
7. **Large-document behaviour**: `load()` sends the whole text with no cap and
   `renderReader` creates one span per token; a 200k-character novel will freeze the
   render and can silently fail the 5 MB `localStorage` write (`store.set`, index.html:186).
   Consider chunked rendering, a text-size warning, and IndexedDB (or File System
   Access) for saved texts.
8. **`lookup_key()` N+1**: up to 30 rows × 6 keys with one redirect query each. A single
   `sequence IN (…)` / `expression IN (…)` query would cut latency and simplify A2's fix.
9. **`build_db.py`**: add `PRAGMA journal_mode=OFF/synchronous=OFF` during the build,
   optionally compress `glossary` (or store only the structured content the UI needs) to
   cut the 641 MB file, and warn if `styles.css` is absent (currently `/jitendex.css`
   silently returns an empty stylesheet when the DB exists but the CSS file does not).
10. **`.env` parser**: handle `export KEY=…` and inline `# comments` (`server.py:28-35`);
    quote/URI-escape `DB_PATH` in the SQLite URI (`server.py:58`) so paths with spaces
    are safe.
11. **Clean up A11/A13/A14** in the same pass as the popup work (schema naming, kanji-
    only ruby, div-sense glossary bullets).
12. **Nice-to-haves**: keyboard word navigation, a vocabulary/known-words list, export a
    clicked entry to Migaku/Anki, persist chat and reading position, and a "copy
    translation" affordance.

## How the findings were reproduced

```sh
# server (already running on :8787 during review)
.venv/bin/python server.py

# A1
curl -s -X POST localhost:8787/api/analyze -H 'Content-Type: application/json' \
  -d '{"text":"来ます。来ない。来い。来ている。"}'

# A2
curl -s -X POST localhost:8787/api/lookup -H 'Content-Type: application/json' \
  -d '{"keys":["數","機關","鐵漿"]}'          # compare terms.sequence with abs(redirect seq)

# A3
curl -s -X POST localhost:8787/api/analyze -H 'Content-Type: application/json' \
  -d '{"text":"𠮷野家🍣で食べた。"}'            # s/e are code points; JS slice() is UTF-16

# build timing / DB size
.venv/bin/python -c 'from build_db import build; import time,tempfile,pathlib; t=time.time(); build(pathlib.Path("../jitendex-yomitan.zip"), pathlib.Path(tempfile.mkdtemp())); print(time.time()-t)'

# JS syntax
python3 - <<'PY'
import re; src=open('static/index.html').read()
open('/tmp/app.js','w').write(re.search(r'<script>(.*)</script>', src, re.S).group(1))
PY
node --check /tmp/app.js
```

---

# Round 2 — second pass (2026-11-21, current code)

The app was substantially rewritten after round 1 (AI reading check, JMnedict names + pins, TTS,
streaming analysis, resizable chat, 987-line frontend, tests). This round re-checked the fixes and
hunted in the new code.

Method: 53-test suite run; the real `data/jitendex.sqlite`; a `/tmp` copy of the server with a local
OpenAI-compatible SSE mock (so no real tokens were spent) for `/api/readings`, `/api/translate`,
`/api/chat`; and headless Chromium probes of the actual page (clicks, selections, scrolling,
placement, AI application, TTS, pins, 60k/140k streaming). No app files were modified; the `/tmp`
copy and mock were deleted afterwards.

## Round-1 findings: status

| Item | Status |
|---|---|
| A1 来る inflections | **most fixed**; ら抜き 来れる still wrong → B1 |
| A2 redirect by sequence | fixed, verified (數 / 機關 / 鐵漿 tests) |
| A3 UTF-16 offsets | fixed, verified (tests + 20-span emoji probe) |
| A4 shared fugashi Tagger | fixed (thread-local `tagger()`, `server.py:123, 188-192`) |
| A5 chat message lost while busy | fixed (Send disabled, draft kept, `index.html:875-879`) |
| A6 selbar off-screen | fixed; headless probe: visible mid-list and flips above at the bottom |
| A7 popups detach on scroll | fixed (`#main` scroll hides popups); verified |
| A8 fetch failures stuck | fixed (`postJSON`/`streamPost`/`load` check `r.ok`) |
| A9 lookup race | fixed (`lookupSeq`, `index.html:533-554`) |
| A10 stale port in docstring | fixed |
| A11 `terms.term_tags` always empty | **still present** (schema/insert unchanged) |
| A12 glossary `img` dropped | **still present** |
| A13 popup ruby over okurigana | fixed (server sends `e.ruby`, `headwordHTML` uses it) |
| A14 div-sense glossary bullets | **still present** (`data/jitendex.css` unchanged) |
| A15 README chat-context claim | fixed (README now states the 60k limit) |

## New bugs

### B1 (high, wrong reading and wrong entry) — ら抜き 来れる is still broken

| input | shown | correct |
|---|---|---|
| 来れる | 来(く)れる, key `来る` | 来(こ)れる |
| 来れない | 来(く)れない | 来(こ)れない |
| 来れた | 来(く)れた | 来(こ)れた |
| もう来れる | 来(き)れる, keys `切れる, 来る` | 来(こ)れる |

Two different wrong paths:

1. A lone `来れる` token (fugashi: base 来れる, kana これる) is turned into the dictionary key `来る` by
   the potential-form fallback (`single_fallback`, `server.py:341-362`, because unidic classes 来れる as
   下一段). `word_reading` then rebuilds from 来る/くる → `く` + `れ` + `る`.
2. In `もう来れる` fugashi splits `来` + `れる`; the two-token span hits the "*other kanji*" variant
   branch (`server.py:297-310`) with kana きれる, which matches the dictionary word `切れる` by
   skeleton `＊れる` + reading. `切れる` is returned **first**, so clicking the word opens 切れる.

The round-1 `KO_ENDINGS` fix (`server.py:225-229`) cannot cover this: it deliberately leaves 来れ
alone because 来れば must stay くれば.

Fix: when the token itself is the potential form (`base` ends in れる/れる and kana agrees, e.g.
来れる), prefer the token's contextual kana (これる) over the dictionary-stem reconstruction; and for
the split form (`来` + `れる`) do not let the variant branch override an inflected single-token match
with a different headword. Add 来れる/来れない/来れた to the tests (which currently stop at 来られる and
来させる).

### B2 (medium, wrong popup) — 今晩は in a non-greeting sentence shows the greeting entry

`今晩は暇だ。` and `今晩は、暇だ。` both produce segment `今晩は` with key `今晩は` → the popup is the
greeting 今晩は (こんばんは), not 今晩 + は. The furigana happens to look right (今晩(こんばん)は) only
because the greeting reading aligns.

The interjection guard (`server.py:282-284`) only runs when the greeting spans more than one token
(`j - i > 1` and last token 助詞). Here fugashi emits 今晩は as a single 感動詞 token, so nothing
checks that it is followed by a sentence end. 今日は works only because it is usually split.

Fix: extend the guard to a single-token match: if `interjection_only(key)` and the following token is
not in `SENTENCE_END` (and not end-of-line/end-of-text), do not use the interjection entry; fall back
to the tokenizer reading (which gives the correct 今晩(こんばん)は) and lookup of the surface only in
greeting position. A test `("今晩は暇だ。", "今晩", "今晩(こんばん)")` would cover it.

### B3 (high, quality + cost) — the AI reading check offers rare/archaic readings the README says it never offers

`usable_readings` (`server.py:329-338`) filters only `score < -50` and the row-level `def_tags`
(`rarely`, `obsolete`, `irregular`, `search`). Rare readings that are tagged at the **sense** level
(`misc-info` codes `arch`/`poet`) and have score 0 pass through:

```
朝      usable: あさ / ちょう / あした      ← あした: score 0, sense misc "poetical"
学生    usable: がくせい / がくしょう / がくそう  ← both score 0/-1, sense misc "archaic"
彼      usable: かれ / かの                  ← かの offered on every 彼
今日    usable: きょう / こんにち / こんち / こんじつ ← こんち/こんじつ (98/97)
```

README (`README.md:98`) says "Rare and obsolete readings are never offered". That is false for the
rows above. Consequences: common words (彼, 今日, 朝, 外, 学生…) are sent to the model on **every
occurrence**, and the candidate list contains readings no reader would want. A deliberately naive mock
that always picks the second option turned 彼(かれ) into 彼(かの), 朝(あさ) into 朝(ちょう), and
学生(がくせい) into 学生(がくしょう); a real model is better but the false-option surface is the risk.

Volume measured on a prose sample: 1,250 of 13,500 segments (9.3 %) had alternatives — 32 batches of
40 items for 30.5k characters. `/api/readings` also repeats the whole sentence for every ambiguous
word in it instead of grouping by sentence, and `_reading_cache` is in-memory only.

Fix suggestions: also inspect the sense-level `misc-info` codes (`arch`, `obs`, `poet`, `rare`) and/or
require Jitendex priority (score ≥ some threshold, or both readings tagged ★); group request items by
sentence; cap per paragraph; persist the cache; and update the README to match the actual filter.

### B4 (medium, silent wrong answers) — `/api/readings` truncates the sentence to 200 characters

`server.py:921` builds the prompt with `it.sentence[:200]`. When the ambiguous word appears after the
200th character of a long sentence, the model receives text that does not contain the word at all.
Reproduced: `"あ"*250 + "希望を抱く。"` produced this prompt line:

```
1. 語：抱く　文：あああ…（200 あ）…　候補：いだく / だく
```

The model therefore cannot use context; it will guess. `expandToSentences` regularly returns sentences
longer than 200 characters in novels. Fix: send a window around the word (e.g. 100 chars either side,
or the sentence up to a larger cap), or send the full sentence and cap only the total request.

### B5 (medium, wrong UI state) — a chat reply from the old text can land in the new conversation

`pasteText` clears `state.chat` when the pasted text differs (`index.html:932-936`), but an in-flight
`sendChat` has no generation/abort guard: when the old reply finishes it pushes its assistant message
into the freshly cleared chat (`index.html:847-868`). Verified with a 2 s mock: after pasting new text,
`state.chat` was `[assistant: "这是回答。"]` — a reply about the previous text in the new conversation.

`#btn-clear` (`index.html:880`) has the same problem: clicking Clear while a reply streams leaves that
reply behind. Fix: bump a chat generation on text change/clear (or abort the chat request) and drop
stale replies; optionally disable Clear while `state.busy`.

### B6 (medium, wasted work) — TTS synthesis is prefetched on every word click and every selection

Both `lookup()` (`index.html:539`) and the selection handler (`index.html:703`) unconditionally call
`audioURL(...)`, i.e. every click on a word and every mouse selection spawns a `say` process
(~1.5 s CPU, a ~45 KB WAV on disk) even when the user never presses 🔊. Headless probe confirmed
`audioCache` grows on selection (`ttsPrefetchDelta: 1`) and after a few clicks. Rapid browsing reads
dozens of clips. Fix: prefetch on pointerover/first press of the 🔊 button, or add an audio on/off
setting; at minimum debounce.

### B7 (medium, error spam for an optional dictionary) — `/api/names` returns 500 when JMnedict is absent

`api_names` (`server.py:807-817`) queries `names` without a guard. JMnedict is optional per the README;
without it the table does not exist and the API raises `sqlite3.OperationalError: no such table:
names` → 500. The frontend calls `/api/names` for **every kanji word** (`index.html:546-549`), so
without JMnedict every click logs a failed request (the UI hides names because `r.ok` is false, so it
looks fine). Verified with an in-process DB that has `terms` but no `names`. Fix: return
`{"word": word, "pinned": ..., "names": []}` when the table is missing (catch `OperationalError` or
check `sqlite_master`), and/or expose `has_names` in `/api/config`.

### B8 (medium, pins can't be undone) — katakana name readings never show as pinned

102,573 of 749,064 name readings contain katakana (e.g. ３Ｍ → スリーエム, ＡＮＣ → エーエヌシー).
`api_pin` normalizes the reading with `to_hira` (`server.py:826-839`), but `api_names` returns the raw
JMnedict reading. `renderNames` compares strictly (`pinned === n.reading`, `index.html:587-590`), so
the row never switches to "📌 Pinned" and clicking it again just re-pins. Verified:

```
pin スリーエム -> {"pins": {"３Ｍ": "すりーえむ"}}
after pin: pinned = すりーえむ | row reading = スリーエム | UI shows Pinned: False
```

Fix: normalize name readings with `to_hira` in `/api/names` (display is unaffected) or compare
normalized in the UI; also let the server send the pinned reading per row.

### B9 (low/medium, streaming claim) — single-line texts do not stream

`api_analyze_stream` batches by lines (`server.py:853-874`), so a paste with no newlines is one giant
paragraph chunk. Measured:

```
140,000 chars, one line   → 2 chunks, first paragraphs after 2,045 ms
145,000 chars, 5,000 lines → 10 chunks, first paragraphs after    34 ms, total 1,843 ms
```

The README's "sends paragraphs as it analyzes them / first words in under 0.25 s" only holds for text
with newlines (Apple Books excerpts usually have them, but web-page copies may not). Fix: split overlong
lines at sentence boundaries or a character budget (e.g. 2–4k chars) before analyzing.

### B10 (low/medium, recovery) — an interrupted names build is never repaired

`build_names` (`build_db.py:94-127`) runs `DROP TABLE IF EXISTS names; CREATE TABLE names …` via
`executescript` (committed immediately), then bulk-inserts and commits at the end. If the process is
killed mid-build (5.7 s window on this machine), the empty `names` table remains committed. At the next
start `ensure_names` (`server.py:97-109`) only checks whether the table exists, so it never rebuilds —
names stay empty until the user deletes the whole 700 MB DB. Fix: build into `names_new`, verify the
row count, then `ALTER TABLE … RENAME` inside a transaction; make `ensure_names` check
`SELECT COUNT(*)` (and consider guarding against two servers building concurrently).

### B11 (low) — small validation/robustness gaps

- `POST /api/pins` accepts the reading `"ー"` (returns 200, stores it). Require at least one real kana.
- `save_pins` (`server.py:119-122`) has no lock. Two concurrent pin requests mutate the shared `PINS`
  dict and dump it; `json.dumps` iterating while another thread inserts can raise
  "dictionary changed size during iteration". Use a lock (and the fixed `.tmp` name makes concurrent
  writers race for the same file).
- `_reading_cache` (`server.py:898`) is unbounded, in-memory only, and a batch is not deduped before
  the request; repeated words/sentences in the same batch are sent repeatedly. Persist it and/or cap it.
- `/api/speak` silently truncates text to 3,000 characters (`server.py:779`) — a long selection is
  spoken only partially with no indication.
- TTS cache eviction is by file mtime and serving does not update mtime, so a frequently used clip can
  be evicted while one-offs stay; the in-page `audioCache` eviction can revoke the object URL that is
  currently playing (60 clips).
- `/api/speak` is a `GET` (a "simple" cross-site request): any web page can make the local server
  synthesize arbitrary text (CPU/disk churn) even though the response cannot be read. A POST or a
  same-origin token would remove this; risk is low for a localhost single-user app.
- The analyze endpoints have no size cap; a multi-MB paste ties up a worker thread and memory.

### B12 (cosmetic) — conjugation labels are sometimes wrong or missing

- 食べれる → "passive or respectful" (it is the ら抜き potential; 食べられる is the ambiguous one).
- 食べちゃった → only "past"; the 〜ちゃう/〜じゃう contraction is not labelled.
- 食べなきゃ → "negative"; the conditional/must meaning of なきゃ is missing.
- 行くべき → no chain at all (`べき` is absorbed but has no `AUX_LABELS` entry).
- 飲まされる → analysed as 飲ます + passive; defensible, but 飲む + causative-passive is the more
  common reading.

### B13 (cosmetic) — pins on kana/latin names have no effect on voice or furigana

`segKana` builds the spoken form from `seg.r` (`index.html:516`), and `ruby_parts` gives no ruby to a
surface without kanji. So pinning e.g. ３Ｍ → すりーえむ stores the pin, sets `seg.pin`, but the word
is still spoken as the surface and no reading is displayed. For kanji names (平城 → ひらじろ) it works.
Fix: for pinned segments, speak the pinned reading directly regardless of ruby.

### B14 (cosmetic) — small UI nits

- `#btn-paste`'s clipboard-denied fallback calls `showEditor()` and then **clears** `#input`
  (`index.html:947-949`), discarding an existing draft.
- `#btn-clear-text` leaves the chat conversation about the now-removed text (paste clears it, Clear does
  not), and `api_chat` then receives an empty article with old messages.
- The **AI 読み** button stays enabled when no API key is configured; the only hint is "· no API key"
  in the sidebar, and the status never says the reading check is off.
- A large text is stored in `localStorage` silently; past ~5 MB the write fails (caught) and the text is
  lost on reload. 150k characters are fine; a whole novel may not be.
- `appendParagraphs` captures `state.hasKey` once per chunk; a paste that arrives before `/api/config`
  resolves gets no AI check for its first paragraphs, and they are never re-observed.

## Verified OK in round 2

- **All 53 tests pass** (`pytest -q`, 3.2 s), including the round-1 regression tests for 来ます /
  抱ける / 一にも二にも / redirects by sequence / UTF-16 offsets / conjugation chains.
- **Round-1 fixes work in the real UI** (headless probes): popup opens 8 px under a clicked word while
  `#main` is scrolled; it flips above near the bottom and closes on scroll; the selection toolbar is
  clamped and flips above at the bottom; an AI-adjusted word's entry is listed first; pins survive a
  reload and 三百二十円 stays えん; 平城 → ひらじろ; 円高/平城京 (dictionary words) are not pinned.
- **AI pipeline with a mock**: `/api/readings` returns validated picks, caches them, ignores an invalid
  choice, tolerates fenced JSON, and turns an upstream 500 into `{"error": "⚠️ API error 500: …"}`
  (frontend marks the check failed). Sentence/batch/status plumbing works.
- **TTS**: `/api/speak` returns 22,050 Hz mono WAV, caches (second hit 12 ms), handles an empty text
  (400) and a leading `-`; the selected voice is `Kyoko (Enhanced)` on this machine.
- **Names**: `/api/names?word=平城` returns 9 readings with place/surname first; a non-name returns an
  empty list; `/api/pins` validates empty/too-long/non-kana input (except `ー`).
- **Streaming**: multi-line 145k text: first paragraphs after 34 ms, complete in 1.84 s (README's
  ~0.25 s / ~1.3 s is in the right ballpark); 60k text 79 ms / 1.27 s; a single-line 140k text is the
  exception (B9).
- **Ruby/offset integrity**: over 2,160 word segments, zero single-run ruby fallbacks and zero
  surface-reconstruction failures; all `s/e` offsets slice exactly `t` (incl. emoji/Ext-B).
- **Robustness**: empty / NUL / 10k-kana / 50-Ext-B / 5k-latin inputs return 200 without crashing.
- **Variants**: real phrases (目が覚めた, 気がついた, 腹が立つ, 雨が降る, 世話になる, 呑んだくれる…)
  resolve to the expected headwords; a battery of common noun+particle spans produced no false matches.
- **Build metrics**: `build_names` 749,064 rows in 5.7 s / 55 MB temp DB (README ~5 s); server import
  2.3 s, 206 MB RSS, 71,115 variant keys.

## Improvement suggestions (round 2)

1. Fix **B1** and **B2** — both are user-visible wrong readings/entries in common text; add the missing
   golden tests.
2. Rework the ambiguity filter for **B3** and the prompt window for **B4**. The AI reading check is the
   most expensive feature and the easiest to make unreliable: require plausible/priority readings,
   group items by sentence, dedupe in a batch, and persist the cache.
3. Add a conversation generation/abort for **B5** (and for Clear) — one counter plus a check after
   `streamPost` is enough.
4. Make TTS lazy (**B6**): a small "audio" toggle or prefetch on hover; this also removes the
   click-driven `say` process churn.
5. Guard the optional names path (**B7**), normalize pins vs names (**B8**), and make the names build
   atomic/recoverable (**B10**).
6. Split overlong lines in `api_analyze_stream` (**B9**) so the streaming promise holds for any paste.
7. Small hardening (**B11**): pin validation + lock, bounded/persisted reading cache, explicit
   truncation in `/api/speak`, POST or token for synthesis, request-size caps.
8. Conjugation labels (**B12**) and pinned non-kanji speech (**B13**) are cheap polish with tests.
9. UI nits (**B14**): keep the editor draft on a denied clipboard, clear/keep chat consistently, show
   when the AI check is unavailable, and consider IndexedDB for very large saved texts.
10. Finish the two round-1 leftovers: `terms.term_tags` (always empty; remove or fill) and glossary
    images (currently dropped silently in `sc()`), plus the div-sense glossary bullets.

## Round-2 reproduction (no real API calls)

```sh
# tests
.venv/bin/python -m pytest -q

# B1 / B2 / B3 (in-process, uses the real DB)
.venv/bin/python - <<'PY'
import server
for t in ["来れる。", "もう来れる。", "今晩は暇だ。", "朝が来た。学生だ。彼は来た。"]:
    print(t, [(s["t"], s["k"], "".join(b + (f"({r})" if r else "") for b, r in s["r"])) for s in server.analyze(t)[0] if s.get("w")])
print(server.usable_readings("朝"), server.usable_readings("学生"), server.usable_readings("彼"))
PY

# B4 (needs a server on READER_PORT; mock at OPENCODE_BASE_URL, see /tmp mock)
curl -s -X POST localhost:8787/api/readings -H 'Content-Type: application/json' \
  -d '{"items":[{"id":"1","word":"抱く","sentence":"'"$(python3 -c 'print("あ"*250+"希望を抱く。")')"'","options":["いだく","だく"]}]}'
# inspect the mock request log: the 文： field is 200 chars and does not contain 抱く

# B5 (headless probe or manual): start a chat reply, paste new text, watch the old reply appear
# B7: point a TestClient at a DB without the names table and GET /api/names -> 500
# B8: pin a katakana name reading, then GET /api/names -> pinned != row reading
# B9: single-line 140k text through /api/analyze_stream -> first paragraphs only after the whole line
```
