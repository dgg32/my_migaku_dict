#!/usr/bin/env python3
"""
Build a Migaku-format dictionary (JSON + ZIP) from JMdict containing the
*search-only* spellings (JMdict tags sK / sk, optionally iK/oK/rK/ik/ok/rk)
that most Yomitan-format JMdict builds leave out as lookup keys.

Example: 思い留まる (search-only form of 思いとどまる, JMdict entry 1309250).

Usage:
    python3 build_migaku_variants.py                 # downloads JMdict_e.gz itself
    python3 build_migaku_variants.py JMdict_e.gz     # use a local file (.gz or .xml)
    python3 build_migaku_variants.py JMdict_e.gz --also iK oK rK   # include more tags

Output (in the current folder):
    JMdict-search-only-forms.json   Migaku JSON format
    JMdict-search-only-forms.zip    same file zipped (use whichever your Migaku accepts)

Data: JMdict (c) EDRDG, licensed CC BY-SA 3.0 -- https://www.edrdg.org/edrdg/licence.html
"""
import argparse, gzip, json, re, sys, urllib.request, zipfile
import xml.etree.ElementTree as ET

URL = "https://www.edrdg.org/pub/Nihongo/JMdict_e.gz"
KANJI_TAGS_DEFAULT = {"sK"}
KANA_TAGS_DEFAULT = {"sk"}
OUT = "JMdict-search-only-forms"


def load_xml_text(path):
    if path is None:
        print("Downloading", URL, file=sys.stderr)
        with urllib.request.urlopen(URL) as r:
            raw = r.read()
        raw = gzip.decompress(raw)
    elif path.endswith(".gz"):
        raw = gzip.open(path).read()
    else:
        raw = open(path, "rb").read()
    text = raw.decode("utf-8")
    # JMdict declares its tag entities (&sK; &v5r; ...) in a DTD that ElementTree
    # cannot resolve. Drop the DOCTYPE and turn each entity into its plain code name.
    text = re.sub(r"<!DOCTYPE.*?\]>", "", text, flags=re.S)
    text = re.sub(r"&(?!(?:amp|lt|gt|quot|apos);)([\w-]+);", r"\1", text)
    return text


def build(text, kanji_tags, kana_tags):
    root = ET.fromstring(text)
    out = []
    for e in root.iter("entry"):
        seq = e.findtext("ent_seq")
        kebs = []  # (text, set(tags))
        for k in e.findall("k_ele"):
            kebs.append((k.findtext("keb"), {i.text for i in k.findall("ke_inf")}))
        rebs = []  # (text, set(tags), restrictions or None)
        for r in e.findall("r_ele"):
            restr = [x.text for x in r.findall("re_restr")] or None
            rebs.append((r.findtext("reb"), {i.text for i in r.findall("re_inf")}, restr))

        # Skip entries with nothing to add
        wanted_k = [(t, tg) for t, tg in kebs if tg & kanji_tags]
        wanted_r = [(t, tg, rs) for t, tg, rs in rebs if tg & kana_tags]
        if not wanted_k and not wanted_r:
            continue

        # Definition: one block per sense, numbered when there are several
        senses = []
        for s in e.findall("sense"):
            gl = [g.text for g in s.findall("gloss") if g.text]
            if not gl:
                continue
            pos = ",".join(p.text for p in s.findall("pos"))
            senses.append((pos, "; ".join(gl)))
        if not senses:
            continue
        if len(senses) == 1:
            definition = senses[0][1]
        else:
            definition = " / ".join(f"{i}. {g}" for i, (_, g) in enumerate(senses, 1))
        pos_all = senses[0][0]

        # Main (display) spelling = first kanji form that is not itself odd, else first reading
        bad = {"sK", "rK", "iK", "oK", "ateji"}
        main = next((t for t, tg in kebs if not (tg & bad)), None)
        plain_reading = next((t for t, tg, rs in rebs if rs is None), rebs[0][0])

        for keb, tg in wanted_k:
            # reading that is valid for this spelling
            reading = next(
                (rt for rt, _, rs in rebs if rs is None or keb in rs), plain_reading
            )
            out.append({
                "term": keb,
                "altterm": main or "",
                "pronunciation": reading,
                "definition": definition,
                "pos": pos_all,
                "examples": "",
                "audio": "",
            })
        for reb, tg, rs in wanted_r:
            out.append({
                "term": reb,
                "altterm": main or "",
                "pronunciation": reb,
                "definition": definition,
                "pos": pos_all,
                "examples": "",
                "audio": "",
            })
    # an entry's altterm is ignored by Migaku when equal to term
    for o in out:
        if o["altterm"] == o["term"]:
            o["altterm"] = ""
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("jmdict", nargs="?", help="JMdict_e.gz or JMdict_e XML (default: download)")
    ap.add_argument("--also", nargs="*", default=[], help="extra tags to include, e.g. iK oK rK")
    a = ap.parse_args()

    kanji_tags = KANJI_TAGS_DEFAULT | {t for t in a.also if t[1] == "K"}
    kana_tags = KANA_TAGS_DEFAULT | {t for t in a.also if t[1] == "k"}
    entries = build(load_xml_text(a.jmdict), kanji_tags, kana_tags)

    with open(OUT + ".json", "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=1)
    with zipfile.ZipFile(OUT + ".zip", "w", zipfile.ZIP_DEFLATED) as z:
        z.write(OUT + ".json")
    print(f"{len(entries)} entries written to {OUT}.json and {OUT}.zip")
    hit = [e for e in entries if e["term"] == "思い留まる"]
    print("思い留まる present:", bool(hit))
    if hit:
        print(json.dumps(hit[0], ensure_ascii=False))


if __name__ == "__main__":
    main()
