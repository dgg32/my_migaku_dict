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
  options: --no-examples  --no-forms  --name NAME

Jitendex (c) Stephen Kraus, CC BY-SA 4.0 (https://jitendex.org), built on
JMdict (c) EDRDG (CC BY-SA 3.0/4.0) and Tatoeba example sentences (CC BY 2.0 FR).
"""
import argparse, html, json, os, re, sys, zipfile

KEYS = ["term", "altterm", "pronunciation", "definition", "pos", "examples", "audio"]

# colours copied from Jitendex's styles.css
TAG_STYLE = {
    "part-of-speech-info": ("#565656", "#fff"),
    "misc-info": ("#a52a2a", "#fff"),
    "field-info": ("#800080", "#fff"),
    "dialect-info": ("#008000", "#fff"),
    "lang-source-wasei": ("#ffa500", "#000"),
}
TERM_TAG_LABELS = {"rarely used form", "irregular form", "old kanji form",
                   "obsolete reading", "ateji form", "search-only form",
                   "outdated kanji form", "outdated kana form"}


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


def text(n, ruby_html=False):
    """Inline text of a node. Ruby -> base text, or with ruby_html (HTML mode)
    adjacent ruby nodes are merged into 'base(reading)', e.g. 彼女(かのじょ)."""
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
                b, r = "".join(p[0] for p in run), "".join(p[1] for p in run)
                out.append(html.escape(f"{b}({r})" if r else b, quote=False))
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
        for s in find(g, lambda x: dc(x) == "sense"):
            m = re.match(r'^"(.*)"$', (s.get("style") or {}).get("listStyleType", ""))
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
                              and dc(c) != "forms-row-senses"]
                elif cells and cells[0].get("tag") == "th":
                    readings.append(text(cells[0]))
        else:
            forms += [text(li) for ul in find(f, lambda x: x.get("tag") == "ul")
                      for li in children(ul) if isinstance(li, dict)]
    forms = [re.sub(r"（★）$", "", x) for x in forms]
    return {"groups": groups, "forms": [x for x in forms if x and x != "＊"],
            "readings": [x for x in readings if x]}


# ---------------------------------------------------------------- formatters

def badge(kind, label, title=""):
    bg, fg = TAG_STYLE.get(kind, ("#565656", "#fff"))
    # kept short on purpose: this badge appears ~1.4 million times
    return f'<b style="background:{bg};color:{fg};font-size:.8em">&nbsp;{html.escape(label)}&nbsp;</b> '


def fmt_html(e, term_tags, examples=True, forms=True):
    out = []
    if term_tags:
        out.append("".join(badge("misc-info", t) for t in term_tags))
    for g in e["groups"]:
        if g["tags"]:
            out.append("".join(badge(k, l, ti) for k, l, ti in g["tags"]))
        for s in g["senses"]:
            line = (s["num"] + " " if s["num"] else "")
            line += "".join(badge(k, l, ti) for k, l, ti in s["tags"])
            line += html.escape("; ".join(s["glosses"]))
            # only <br>, <b>, <i>, <span>: block elements and <ruby> are not
            # rendered in every Migaku view (search page flattens them)
            out.append(line)
            for x in s["extras"]:
                if x["kind"] == "example-sentence":
                    if examples:
                        out.append(f'&emsp;▸ {x["ja_html"]}')
                        out.append(f'&emsp;&emsp;<span style="opacity:.75">{html.escape(x["en"])}</span>')
                elif x["kind"] in ("xref", "antonym"):
                    out.append(f'&emsp;<i>{html.escape(x["label"])}</i> {html.escape(x["word"])}'
                               + (f' — {html.escape(x["gloss"])}' if x["gloss"] else ""))
                else:
                    out.append(f'&emsp;<i>{html.escape(x["label"])}:</i> {html.escape(x["body"])}')
    if forms and len(e["forms"]) > 1:
        out.append(badge("part-of-speech-info", "forms") + html.escape("・".join(e["forms"])))
    if forms and len(e["readings"]) > 1:
        out.append(badge("part-of-speech-info", "readings") + html.escape("・".join(e["readings"])))
    return "<br>".join(out)


def fmt_text(e, term_tags, examples=True, forms=True):
    out = []
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
                    if examples:
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
    return " / ".join(out)


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
    for d in r[5]:
        if isinstance(d, list) and d and isinstance(d[0], str):
            return d[0]
    for d in r[5]:
        if isinstance(d, dict):
            links = find(d, lambda x: x.get("tag") == "a")
            if links:
                return text(links[0])
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("zip")
    ap.add_argument("--format", choices=["html", "text"], default="html")
    ap.add_argument("--no-examples", action="store_true")
    ap.add_argument("--no-forms", action="store_true")
    ap.add_argument("--outdir", default=".")
    ap.add_argument("--name")
    a = ap.parse_args()

    index, rows = load_rows(a.zip)
    fmt = fmt_html if a.format == "html" else fmt_text
    ex, fo = not a.no_examples, not a.no_forms

    def term_tags(r):
        return [t.replace("\xa0", " ") for t in re.split(r" (?=\S)", r[2] or "")
                if t.replace("\xa0", " ") in TERM_TAG_LABELS]

    # pass 1: full entries
    full, by_seq_term, by_seq = [], {}, {}
    redirects = []
    for r in rows:
        if is_redirect(r):
            redirects.append(r)
            continue
        sc = next(d for d in r[5] if isinstance(d, dict) and d.get("type") == "structured-content")
        e = parse_entry(sc)
        pos = ",".join(e["groups"][0]["pos"]) if e["groups"] else ""
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
            full.append({"term": r[0], "pronunciation": r[1] or r[0], "pos": "", "e": None,
                         "tt": term_tags(r), "main": tgt or "", "seq": r[6],
                         "plain": f"⟶ {tgt}" if tgt else ""})
            continue
        full.append({**rec, "term": r[0], "pronunciation": r[1] or rec["pronunciation"],
                     "tt": term_tags(r) or ["search-only form"], "main": tgt or rec["main"]})

    out, empty = [], 0
    for rec in full:
        d = fmt(rec["e"], rec["tt"], ex, fo) if rec.get("e") else rec.get("plain", "")
        if not d.strip():
            empty += 1
            continue
        alt = rec["main"] if rec["main"] != rec["term"] else ""
        out.append({"term": rec["term"], "altterm": alt, "pronunciation": rec["pronunciation"],
                    "definition": d, "pos": rec["pos"], "examples": "", "audio": ""})

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
