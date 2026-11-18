# JMdict supplements for Migaku Dictionary

Built from JMdict_e.gz (JMdict created 2026-11-17) with `build_migaku_variants.py`.

## Licence / attribution

The dictionary data is from **JMdict**, (c) the Electronic Dictionary Research and
Development Group (EDRDG), used under the Creative Commons Attribution-ShareAlike 3.0
licence (CC BY-SA 3.0). See https://www.edrdg.org/edrdg/licence.html.
Any file in this folder that contains JMdict data is under the same licence.

## Files

| File | Records | Contents |
|---|---|---|
| `JMdict-search-only-forms.json` / `.zip` | 20,903 | Only forms tagged `sK`/`sk` (search-only). Does **not** contain 思い留まる (see note). |
| `JMdict-alt-spellings.json` / `.zip` | 64,008 | Every kanji spelling except each entry's main one, plus all sK/sk/iK/ik/oK/ok/rK/rk forms. Contains 思い留まる. |
| `JMdict-full-migaku.json` / `.zip` | 500,028 | The whole of JMdict: every kanji spelling and every reading, with plain-text definitions. |
| `*-yomitan.zip` | same | The same records as Yomitan format 3, with plain-string glossaries (no structured-content). |

Migaku JSON schema: an array of `{term, altterm, pronunciation, definition, pos, examples, audio}`.
`examples` and `audio` are always `""`. `altterm` is `""` when it would equal `term`.

## Note on 思い留まる

In current JMdict, 思い留まる is an ordinary (not `sK`) spelling of entry 1309250. The
search-only form in that entry is 思い止どまる. The yomidevs `JMdict_english.zip`
already includes 思い留まる as a headword, so the Migaku lookup failure is not caused by
the data missing from that build.

## Rebuild

    python3 build_migaku_variants.py JMdict_e.gz --mode alt --yomitan --outdir .
    # modes: search-only (default, accepts --also iK oK rK ik ok rk), alt, full

## Jitendex in Migaku JSON

`build_jitendex_migaku.py` converts `jitendex-yomitan.zip` (Jitendex 2026-10-03) into Migaku JSON.
Jitendex gets its look from an external `styles.css`, which Migaku ignores. This converter
flattens each entry and resolves the 139,149 "⟶ redirect" rows (search-only/variant
spellings) to the full entry they point to.

| File | Records | Contents |
|---|---|---|
| `jitendex-migaku-html.json` / `.zip` | 436,555 | Simple HTML with inline colours: tag badges, ①② senses, example sentences with readings in brackets (私(わたし)), notes, cross-references, variant forms. |
| `jitendex-migaku-text.json` / `.zip` | 436,555 | The same content on one plain-text line: `[5-dan] [transitive] gloss ▸ example (translation) / …` |
| `jitendex-html-preview.html` | 9 | Local preview of how the HTML version should look in a browser. |

Dropped in the conversion: images (444 entries with photos and 250 rare-kanji SVGs),
clickable links, per-entry attribution links, and the forms validity table
(reduced to a list of spellings and readings).

Licence: Jitendex (c) Stephen Kraus, CC BY-SA 4.0 (https://jitendex.org). It includes
JMdict data (EDRDG, https://www.edrdg.org/edrdg/licence.html) and Tatoeba example
sentences (CC BY 2.0 FR, https://tatoeba.org/). These converted files are derivative
works and remain under CC BY-SA 4.0.

    python3 build_jitendex_migaku.py ../jitendex-yomitan.zip --format html   # or text
    # options: --no-examples  --no-forms  --outdir DIR  --name NAME
