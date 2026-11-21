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
