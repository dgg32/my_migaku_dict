#!/usr/bin/env python3
"""
Convert Jitendex (Yomitan ZIP) into Migaku Dictionary JSON.

Jitendex styles its entries with an external styles.css, which Migaku ignores.
This script flattens each entry into either
  --format html : simple HTML with inline styles (no stylesheet needed), or
  --format text : one line of plain text.
Redirect rows (search-only / variant spellings that only say "⟶ X") are
replaced by the full definition of the entry they point to.

Usage:
  python3 build_jitendex_migaku.py jitendex-yomitan.zip --format html --outdir .
  options: --no-examples  --no-forms  --examples-field  --name NAME

Jitendex (c) Stephen Kraus, CC BY-SA 4.0 (https://jitendex.org), built on
JMdict (c) EDRDG (CC BY-SA 3.0/4.0) and Tatoeba example sentences (CC BY 2.0 FR).
"""
import argparse, html, json, os, re, sys, zipfile

KEYS = ["term", "altterm", "pronunciation", "definition", "pos", "examples", "audio"]

# colours copied from Jitendex's styles.css
TAG_STYLE = {
    "part-of-speech-info": ("#555", "#fff"),
    "misc-info": ("#a22", "#fff"),
    "field-info": ("#808", "#fff"),
    "dialect-info": ("#080", "#fff"),
}
POS_KIND = "part-of-speech-info"
# (term, reading) pairs of every Jitendex headword; used to split per-kanji
# furigana into words. Filled in main().
KNOWN = set()


def esc(s):
    return html.escape(s, quote=False)
TERM_TAG_LABELS = {"rarely used form", "irregular form", "old kanji form",
                   "obsolete reading", "ateji form", "search-only form",
                   "outdated kanji form", "outdated kana form", "special reading"}


def dc(n):
    return (n.get("data") or {}).get("content") if isinstance(n, dict) else None


def dclass(n):
    return (n.get("data") or {}).get("class") if isinstance(n, dict) else None


def children(n):
    if isinstance(n, dict):
        c = n.get("content")
        if c is None:
            return []
        return c if isinstance(c, list) else [c]
    if isinstance(n, list):
        return n
    return []


def is_ruby(x):
    return isinstance(x, dict) and x.get("tag") == "ruby"


def ruby_parts(n):
    kids = children(n)
    base = "".join(text(x) for x in kids if not (isinstance(x, dict) and x.get("tag") == "rt"))
    rt = "".join(text(x.get("content", "")) for x in kids if isinstance(x, dict) and x.get("tag") == "rt")
    return base, rt


def join_ruby(run):
    """Jitendex gives furigana one kanji at a time (以(い)上(じょう)何(なに)) with no
    word boundaries. Split the run into dictionary words, longest match first:
    以上(いじょう)何(なに). Neighbouring kanji that match no word stay together."""
    segs, i = [], 0
    while i < len(run):
        for j in range(len(run), i, -1):
            b = "".join(p[0] for p in run[i:j])
            r = "".join(p[1] for p in run[i:j])
            known = (b, r) in KNOWN
            if known or j == i + 1:
                if not known and segs and not segs[-1][2]:
                    pb, pr, _ = segs.pop()
                    b, r = pb + b, pr + r
                segs.append((b, r, known))
                i = j
                break
    return "".join(f"{b}({r})" if r else b for b, r, _ in segs)


def text(n, ruby_html=False):
    """Inline text of a node. Ruby -> base text, or with ruby_html (HTML mode)
    adjacent ruby nodes become 'word(reading)', e.g. 彼女(かのじょ)."""
    if isinstance(n, str):
        return html.escape(n, quote=False) if ruby_html else n
    if isinstance(n, list):
        if not ruby_html:
            return "".join(text(x) for x in n)
        out, run = [], []
        for x in n + [None]:
            if is_ruby(x):
                run.append(ruby_parts(x))
                continue
            if run:
                out.append(esc(join_ruby(run)))
                run = []
            if x is not None:
                out.append(text(x, True))
        return "".join(out)
    if not isinstance(n, dict):
        return ""
    tag = n.get("tag")
    if tag == "rt":
        return ""
    if tag == "img" or dc(n) == "attribution-footnote":
        return ""
    if tag == "ruby" and ruby_html:
        return text([n], True)
    if dc(n) == "example-keyword" and ruby_html:
        return "<b>" + text(children(n), True) + "</b>"
    return text(children(n), ruby_html)


def find(n, pred, stop=lambda x: False):
    """All descendant nodes matching pred, not descending into matches or stop nodes."""
    out = []
    for c in children(n):
        if not isinstance(c, dict):
            continue
        if pred(c):
            out.append(c)
        elif not stop(c):
            out.extend(find(c, pred, stop))
    return out


def is_tag(n):
    return dclass(n) == "tag" and dc(n) in TAG_STYLE


def is_special(n):
    """Placeholder cells such as ∅ ("no associated kanji forms")."""
    return dclass(n) == "form-special" or bool(find(n, lambda x: dclass(x) == "form-special"))


def parse_extra(box):
    kind = dc(box)
    if kind == "example-sentence":
        ja = find(box, lambda x: dc(x) == "example-sentence-a")
        en = find(box, lambda x: dc(x) == "example-sentence-b")
        return {"kind": kind,
                "ja_html": text(ja, True), "ja": text(ja), "en": text(en)}
    if kind in ("xref", "antonym"):
        label = find(box, lambda x: dc(x) == "reference-label")
        links = find(box, lambda x: x.get("tag") == "a")
        gl = find(box, lambda x: dc(x) in ("xref-glossary", "antonym-glossary"))
        return {"kind": kind, "label": text(label), "word": "・".join(text(a) for a in links),
                "gloss": text(gl)}
    label = find(box, lambda x: dclass(x) == "extra-label")
    body = find(box, lambda x: dclass(x) == "extra-content")
    return {"kind": kind, "label": text(label), "body": text(body)}


def parse_sense(s, number):
    tags = [(dc(t), text(t), t.get("title", "")) for t in
            find(s, is_tag, stop=lambda x: dc(x) in ("extra-info", "glossary"))]
    glosses = [text(li) for g in find(s, lambda x: dc(x) == "glossary") for li in children(g)
               if isinstance(li, dict)]
    extras = [parse_extra(b) for b in find(s, lambda x: dclass(x) == "extra-box")]
    return {"num": number, "tags": tags, "glosses": [g for g in glosses if g], "extras": extras}


def parse_entry(sc):
    groups = []
    for g in find(sc, lambda x: dc(x) == "sense-group"):
        gtags = [(dc(t), text(t), t.get("title", "")) for t in
                 find(g, is_tag, stop=lambda x: dc(x) == "sense")]
        pos = [((t.get("data") or {}).get("code")) for t in
               find(g, lambda x: dc(x) == "part-of-speech-info", stop=lambda x: dc(x) == "sense")]
        senses = []
        gstyle = (g.get("style") or {}).get("listStyleType", "")
        for s in find(g, lambda x: dc(x) == "sense"):
            # multi-sense groups number each sense; a single-sense group carries
            # the number on the group itself
            m = re.match(r'^"(.*)"$', (s.get("style") or {}).get("listStyleType", "") or gstyle)
            senses.append(parse_sense(s, m.group(1) if m else ""))
        groups.append({"tags": gtags, "pos": [p for p in pos if p], "senses": senses})
    forms, readings = [], []
    for f in find(sc, lambda x: dc(x) == "forms"):
        tables = find(f, lambda x: x.get("tag") == "table")
        if tables:
            for row in children(tables[0]):
                if not isinstance(row, dict) or dc(row) == "forms-col-senses-row":
                    continue
                cells = [c for c in children(row) if isinstance(c, dict)]
                if dc(row) == "forms-header-row":
                    forms += [text(c) for c in cells if c.get("tag") == "th" and text(c)
                              and dc(c) != "forms-row-senses" and not is_special(c)]
                else:
                    # rows of readings that apply to only some senses start with a
                    # marker cell (＊, ①③); the reading is the th after it
                    cells = [c for c in cells if dc(c) != "forms-row-senses"]
                    if cells and cells[0].get("tag") == "th":
                        readings.append(text(cells[0]))
        else:
            forms += [text(li) for ul in find(f, lambda x: x.get("tag") == "ul")
                      for li in children(ul) if isinstance(li, dict) and not is_special(li)]
    forms = [re.sub(r"（★）$", "", x) for x in forms]
    return {"groups": groups, "forms": [x for x in forms if x and x not in ("＊", "∅")],
            "readings": [x for x in readings if x]}


# ---------------------------------------------------------------- formatters

def badge(kind, label):
    bg, fg = TAG_STYLE.get(kind, TAG_STYLE[POS_KIND])
    # kept short on purpose: badges appear hundreds of thousands of times
    return f'<b style="background:{bg};color:{fg}">&nbsp;{esc(label)}&nbsp;</b>'


def grey(label):
    return f'<i style="color:#888">{esc(label)}</i>'


def tags_html(tags):
    """Part of speech as grey italics, every other tag as a coloured badge."""
    pos = [l for k, l, _ in tags if k == POS_KIND]
    return " ".join(([grey(", ".join(pos))] if pos else [])
                    + [badge(k, l) for k, l, _ in tags if k != POS_KIND])


def fmt_html(e, term_tags, examples=True, forms=True, examples_field=False):
    """Returns (definition, examples). Example sentences go into the definition,
    or into the separate examples string when examples_field is set."""
    out, exs = [], []
    if term_tags:
        out.append(" ".join(badge("misc-info", t) for t in term_tags))
    for g in e["groups"]:
        if g["tags"]:
            out.append(tags_html(g["tags"]))
        for s in g["senses"]:
            # only <br>, <b>, <i>, <span>: block elements and <ruby> are not
            # rendered in every Migaku view (search page flattens them)
            out.append(" ".join(x for x in (s["num"], tags_html(s["tags"]),
                                            esc("; ".join(s["glosses"]))) if x))
            for x in s["extras"]:
                if x["kind"] == "example-sentence":
                    if not examples:
                        continue
                    ja, en = x["ja_html"], f'<span style="opacity:.75">{esc(x["en"])}</span>'
                    if examples_field:
                        exs.append(f"{s['num']} ▸ {ja}<br>&emsp;{en}".strip())
                    else:
                        out.append(f"&emsp;▸ {ja}")
                        out.append(f"&emsp;&emsp;{en}")
                elif x["kind"] in ("xref", "antonym"):
                    out.append(f'&emsp;<i>{esc(x["label"])}</i> {esc(x["word"])}'
                               + (f' — {esc(x["gloss"])}' if x["gloss"] else ""))
                else:
                    out.append(f'&emsp;<i>{esc(x["label"])}:</i> {esc(x["body"])}')
    if forms and len(e["forms"]) > 1:
        out.append(grey("forms") + " " + esc("・".join(e["forms"])))
    if forms and len(e["readings"]) > 1:
        out.append(grey("readings") + " " + esc("・".join(e["readings"])))
    return "<br>".join(out), "<br>".join(exs)


def fmt_text(e, term_tags, examples=True, forms=True, examples_field=False):
    out, exs = [], []
    if term_tags:
        out.append(" ".join(f"[{t}]" for t in term_tags))
    for g in e["groups"]:
        gt = " ".join(f"[{l}]" for _, l, _ in g["tags"])
        for i, s in enumerate(g["senses"]):
            line = (gt + " " if gt and i == 0 else "")
            line += (s["num"] + " " if s["num"] else "")
            line += "".join(f"[{l}] " for _, l, _ in s["tags"])
            line += "; ".join(s["glosses"])
            for x in s["extras"]:
                if x["kind"] == "example-sentence":
                    if examples and examples_field:
                        exs.append(f"▸ {x['ja']} ({x['en']})")
                    elif examples:
                        line += f" ▸ {x['ja']} ({x['en']})"
                elif x["kind"] in ("xref", "antonym"):
                    line += f" ({x['label']}: {x['word']}" + (f" — {x['gloss']}" if x["gloss"] else "") + ")"
                else:
                    line += f" ({x['label']}: {x['body']})"
            out.append(line.strip())
    if forms and len(e["forms"]) > 1:
        out.append("[forms] " + "・".join(e["forms"]))
    if forms and len(e["readings"]) > 1:
        out.append("[readings] " + "・".join(e["readings"]))
    return " / ".join(out), " / ".join(exs)


# ---------------------------------------------------------------- main

def load_rows(zpath):
    rows = []
    with zipfile.ZipFile(zpath) as z:
        names = sorted((n for n in z.namelist() if re.fullmatch(r"term_bank_\d+\.json", n)),
                       key=lambda n: int(re.search(r"\d+", n).group()))
        index = json.loads(z.read("index.json"))
        for n in names:
            rows += json.loads(z.read(n))
    return index, rows


def is_redirect(r):
    return any(isinstance(d, dict) and dc(d.get("content")) == "redirect-glossary"
               or isinstance(d, dict) and any(dc(c) == "redirect-glossary" for c in children(d))
               for d in r[5])


def redirect_target(r):
    def clean(t):
        return t.lstrip("⟶").strip() or None
    for d in r[5]:
        if isinstance(d, list) and d and isinstance(d[0], str):
            return clean(d[0])
    for d in r[5]:
        if isinstance(d, dict):
            links = find(d, lambda x: x.get("tag") == "a")
            if links:
                return clean(text(links[0]))
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("zip")
    ap.add_argument("--format", choices=["html", "text"], default="html")
    ap.add_argument("--no-examples", action="store_true")
    ap.add_argument("--no-forms", action="store_true")
    ap.add_argument("--examples-field", action="store_true",
                    help="put example sentences in Migaku's separate 'examples' field "
                         "instead of inside the definition")
    ap.add_argument("--outdir", default=".")
    ap.add_argument("--name")
    a = ap.parse_args()

    index, rows = load_rows(a.zip)
    for r in rows:
        if r[1]:
            KNOWN.add((r[0], r[1]))
            # also the kanji stem of words with trailing kana: 役立つ/やくだつ -> 役立/やくだ
            m = re.fullmatch(r"(.*[^\u3040-\u30ff])([\u3040-\u30ff]+)", r[0])
            if m and r[1].endswith(m.group(2)) and len(r[1]) > len(m.group(2)):
                KNOWN.add((m.group(1), r[1][:-len(m.group(2))]))
    fmt = fmt_html if a.format == "html" else fmt_text
    ex, fo = not a.no_examples, not a.no_forms

    def term_tags(r):
        # Jitendex's "★" / "priority form" (common word) is left out on purpose:
        # Migaku shows its own frequency rating next to the word
        tags = [t.replace("\xa0", " ") for t in re.split(r" (?=\S)", r[2] or "")]
        return [t for t in tags if t in TERM_TAG_LABELS]

    # pass 1: full entries
    full, by_seq_term, by_seq = [], {}, {}
    redirects = []
    for r in rows:
        if is_redirect(r):
            redirects.append(r)
            continue
        sc = next((d for d in r[5] if isinstance(d, dict) and d.get("type") == "structured-content"),
                  None)
        if sc is None:
            sys.exit(f"row {r[0]} [{r[1]}] seq {r[6]}: no structured-content glossary "
                     "(has the Jitendex format changed?)")
        e = parse_entry(sc)
        # part-of-speech codes of every sense group, in order, without repeats
        pos = ",".join(dict.fromkeys(p for g in e["groups"] for p in g["pos"]))
        rec = {"term": r[0], "pronunciation": r[1] or r[0], "pos": pos, "e": e, "tt": term_tags(r),
               "main": e["forms"][0] if e["forms"] else "", "seq": r[6]}
        full.append(rec)
        by_seq_term.setdefault((r[6], r[0]), rec)
        by_seq.setdefault(r[6], rec)

    # pass 2: resolve redirects to the target entry (same |sequence|)
    unresolved = 0
    for r in redirects:
        tgt = redirect_target(r)
        seq = abs(r[6]) if r[6] else None
        rec = by_seq_term.get((seq, tgt)) or by_seq.get(seq)
        if rec is None:
            unresolved += 1
            print(f"warning: redirect {r[0]} [{r[1]}] -> {tgt!r} (seq {r[6]}) not resolved",
                  file=sys.stderr)
            full.append({"term": r[0], "pronunciation": r[1] or r[0], "pos": "", "e": None,
                         "tt": term_tags(r), "main": tgt or "", "seq": r[6],
                         "plain": f"⟶ {tgt}" if tgt else ""})
            continue
        full.append({**rec, "term": r[0], "pronunciation": r[1] or rec["pronunciation"],
                     "tt": term_tags(r) or ["search-only form"], "main": tgt or rec["main"]})

    out, empty = [], 0
    for rec in full:
        d, exs = (fmt(rec["e"], rec["tt"], ex, fo, a.examples_field) if rec.get("e")
                  else (rec.get("plain", ""), ""))
        if not d.strip():
            empty += 1
            continue
        alt = rec["main"] if rec["main"] != rec["term"] else ""
        out.append({"term": rec["term"], "altterm": alt, "pronunciation": rec["pronunciation"],
                    "definition": d, "pos": rec["pos"], "examples": exs, "audio": ""})

    name = a.name or f"jitendex-migaku-{a.format}"
    os.makedirs(a.outdir, exist_ok=True)
    base = os.path.join(a.outdir, name)
    with open(base + ".json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    with zipfile.ZipFile(base + ".zip", "w", zipfile.ZIP_DEFLATED) as z:
        z.write(base + ".json", arcname=name + ".json")
    print(f"{index.get('title')}: {len(rows)} rows, {len(redirects)} redirects "
          f"({unresolved} unresolved), {empty} skipped empty, {len(out)} written to {base}.json/.zip")


if __name__ == "__main__":
    main()
