# Bug hunt: `jitendex-migaku-html.zip` (built by `build_jitendex_migaku.py`)

Round: 2026-11-19. Findings verified against the generated
`jitendex-migaku-html.json` (436,555 records) and the source
`jitendex-yomitan.zip` (Jitendex 2026-10-03).

## How the zip is made (summary)

`python3 build_jitendex_migaku.py ../jitendex-yomitan.zip --format html`:

1. `load_rows()` reads `index.json` + all `term_bank_*.json` from the Yomitan zip.
2. `KNOWN` is filled with every (headword, reading) pair, plus kanji stems
   (役立つ/やくだつ → 役立/やくだ) — used later to re-group per-kanji furigana.
3. Pass 1 parses every non-redirect row's structured content into
   sense groups / senses / glosses / extras / forms / readings.
4. Pass 2 resolves the 139,149 redirect rows ("⟶ X") to the entry with the
   same `|sequence|` (term match first, any-row fallback second) and copies its
   definition under the redirect term.
5. `fmt_html()` renders each entry as `<br>`-separated lines with inline
   colours; `examples`/`audio` are always `""` unless `--examples-field`.
6. Result written as `<name>.json` + a ZIP containing exactly that file.

## Bugs

### B1 (major, data corruption) — `readings` line contains sense markers instead of readings

`parse_entry()` takes the reading from `cells[0]` of every non-header row of the
forms table:

```python
elif cells and cells[0].get("tag") == "th":
    readings.append(text(cells[0]))
```

But when a reading applies only to some senses, the row's first cell is the
`forms-row-senses` marker th (text `＊` or `①③`), and the actual reading is the
*second* th (`cells[1]`). Example, entry あそこ — output is:

    <i style="color:#888">readings</i> ＊・＊・①③・＊・①③

while the true readings (あそこ・あすこ・かしこ・アソコ・あしこ) are dropped entirely.

- Source rows affected: 6,779 (rows that carry a `forms-row-senses` marker).
- Output affected: **2,863** of 36,347 `readings` lines are garbage; also present
  in the text build.
- Entries without the marker column (e.g. ＣＤプレーヤー) work because there
  `cells[0]` really is the reading th.

Fix: before extracting, drop the marker cell:

```python
cells = [c for c in cells if dc(c) != "forms-row-senses"]
if cells and cells[0].get("tag") == "th":
    readings.append(text(cells[0]))
```

### B2 (minor, numbering inconsistency) — 15,126 single-sense groups lose their ①

Two shapes exist in Jitendex:

- multi-sense group: `li.sense-group` (unstyled) → `ol` → `li.sense` each styled
  `listStyleType: "\"①\""…` — number is picked up correctly;
- single-sense group: `li.sense-group` itself carries
  `listStyleType: "\"①\""` and directly contains an unstyled `div.sense`
  (e.g. あそこ's first sense). `parse_entry()` reads the style from the inner
  node, gets none, and emits the sense **without its number** — Jitendex renders
  ①. 15,126 senses affected; safe to fix because a styled sense-group always
  contains exactly one sense (verified: 15,126 × (styled, 1), zero styled > 1).

Fix: fall back to the parent sense-group's `listStyleType` when the sense node
has none.

### B3 (minor, silent data loss) — term tags `★` and `special reading` dropped

`term_tags()` keeps only labels in `TERM_TAG_LABELS`. Source tag strings dropped:

| dropped tag | rows |
|---|---|
| `★` | 21,955 |
| `★ priority form` | 7,550 |
| `special reading` (alone / combined) | ~470 |

`★` is Jitendex's own priority marking (e.g. ＣＤプレイヤー "★ priority form");
`special reading` is meaningful (e.g. 刑事 read でか). The labels appear silently
nowhere in the output. Either add them to `TERM_TAG_LABELS`/`TAG_STYLE` (a grey
or gold badge), or document the omission in the README.

### B4 (latent) — unresolved redirects would be silently dropped or double-arrowed

In pass 2, an unresolved redirect gets `plain = f"⟶ {tgt}"`. Two issues:

- if `redirect_target()` returns `None`, `plain` is `""` and the record is
  silently discarded by the `empty` check — no warning anywhere;
- if the first branch of `redirect_target()` ever matches a list whose `d[0]`
  already starts with `⟶` (the arrow string sits inside the structured content),
  the output would read `⟶ ⟶ target`.

Currently `unresolved == 0` (verified: no `⟶` in any definition), so this is
latent, but worth a guard + a printed list of failed targets.

### B5 (cosmetic) — dead style entry

`TAG_STYLE["lang-source-wasei"]` is never used: `is_tag()` only matches
`class == "tag"`, and the inventory of source tag contents is exactly
`part-of-speech-info / misc-info / field-info / dialect-info`. Language-of-origin
info arrives as a `lang-source` *extra-box* and is rendered via the generic
`label: body` path (works fine, 11,318 entries).

## Verified-OK areas (checked, no issue found)

- HTML tag inventory of the output is exactly `{i, br, b, span}` — no leaked
  `<a>`, `<img>`, `<ruby>`, `<table>` etc.; 444 photos / 250 SVGs dropped as
  documented.
- Escaping: `&amp;` (262) and `&lt;` (5) occurrences are legitimate content
  (math glosses like `less-than sign (&lt;)`); no double-escaping.
- Furigana re-grouping: samples all correct (打(う)ち砕(くだ)かれて,
  置手紙(おきてがみ), 規則正(きそくただ)しく, ＣＤ(シーディー)プレイヤー);
  no empty or punctuation-bearing readings found.
- Redirect resolution: all 139,149 redirects resolved (0 unresolved), no
  `⟶` residue.
- Duplicate records: 0 exact (term+pronunciation+definition) duplicates;
  1,351 same term+pronunciation pairs are genuinely different JMdict entries.
- Record count 436,555 = source rows; 0 empty-skipped; zip contains exactly
  `jitendex-migaku-html.json`; JSON schema keys match Migaku's order.
- `listStyleType` inventory: only quoted circled numbers ①…⑩+, so the
  `re.match(r'^"(.*)"$', …)` number extraction is safe.
- Forms extraction (∅/＊ filtering, （★） suffix strip, single-table assumption)
  matches the source structure (every `forms` section has 0 or 1 table).

## Improvement suggestions

1. **Fix B1** (the only user-visible corruption) and regenerate both builds.
2. Fix B2 (restore ① for single-sense groups) — keeps numbering consistent
   with Jitendex and with the circled numbers already used inside xref glosses
   ("See also どこ — ① where").
3. B3: show `★` as a small badge (e.g. gold) and keep `special reading`;
   or at least list dropped tags in the README's "Dropped in the conversion"
   section, which currently omits them.
4. `--examples-field`: the per-line prefix is `f"{s['num']} ▸ …"`; when `num`
   is empty (B2) this yields lines starting with a space. Regenerate after B2.
5. B4: assert/print failed redirect targets and guard against `tgt is None`;
   make `redirect_target()` strip a leading `⟶` defensively.
6. Robustness: `next(d for d in r[5] if isinstance(d, dict) and …)` raises a
   bare `StopIteration` if any non-redirect row lacked structured content —
   a `default=None` + explicit error message would make a future Jitendex
   format change fail loudly instead of cryptically.
7. README wording: "①② senses" and "example sentences with readings in
   brackets per word" overstate the current output (see B2) — mention that
   single-sense groups currently print unnumbered until B2 is fixed.
8. Optional: `readings` currently keeps old-character glyphs
   (に対して → readings に對して). Consider normalising to the modern glyph
   (or keep and note it), so readings match what Migaku users type.
9. Optional CLI nicety: `--name` already exists; a `--limit N` flag for quick
   smoke-testing on a subset would speed up future bug-hunt rounds (a full run
   parses ~437k rows / writes 156 MB).
