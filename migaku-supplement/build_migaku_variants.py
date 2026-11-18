#!/usr/bin/env python3
"""
Build Migaku-format dictionaries (Migaku JSON + ZIP, optionally a Yomitan ZIP)
from JMdict (EDRDG), aimed at spellings that Migaku fails to find.

Modes (--mode):
  search-only  only forms tagged sK / sk (plus --also tags), e.g. 思い止どまる
  alt          every kanji spelling that is NOT the entry's first/main spelling,
               plus all search-only/irregular forms, e.g. 思い留まる, 思い止まる
  full         every kanji spelling and every reading of every entry
               (a complete JMdict in Migaku JSON, plain-text definitions)

Usage:
  python3 build_migaku_variants.py [JMdict_e.gz|JMdict_e.xml] [--mode alt]
         [--also iK oK rK ik ok rk] [--outdir DIR] [--name NAME] [--yomitan]

With no input file, JMdict_e.gz is downloaded from EDRDG.

Data: JMdict (c) EDRDG, licensed CC BY-SA 3.0 -- https://www.edrdg.org/edrdg/licence.html
"""
import argparse, gzip, json, os, re, sys, urllib.request, zipfile
import xml.etree.ElementTree as ET

URL = "https://www.edrdg.org/pub/Nihongo/JMdict_e.gz"
SEARCH_ONLY = {"sK", "sk"}
ODD = {"sK", "sk", "iK", "ik", "oK", "ok", "rK", "rk"}  # not used as main/display forms
MIGAKU_KEYS = ["term", "altterm", "pronunciation", "definition", "pos", "examples", "audio"]


def load_xml_text(path):
    if path is None:
        print("Downloading", URL, file=sys.stderr)
        with urllib.request.urlopen(URL) as r:
            raw = gzip.decompress(r.read())
    elif path.endswith(".gz"):
        with gzip.open(path) as f:
            raw = f.read()
    else:
        with open(path, "rb") as f:
            raw = f.read()
    text = raw.decode("utf-8")
    # JMdict declares its tag entities (&sK; &v5r; ...) in an internal DTD that
    # ElementTree cannot expand. Drop the DOCTYPE and turn each entity reference
    # into its plain code name (keeping the 5 predefined XML entities).
    text = re.sub(r"<!DOCTYPE.*?\]>", "", text, count=1, flags=re.S)
    text = re.sub(r"&(?!(?:amp|lt|gt|quot|apos);)([\w-]+);", r"\1", text)
    return text


def parse_entry(e):
    kebs = [{"t": k.findtext("keb"),
             "inf": {i.text for i in k.findall("ke_inf")}} for k in e.findall("k_ele")]
    rebs = []
    for r in e.findall("r_ele"):
        rebs.append({"t": r.findtext("reb"),
                     "inf": {i.text for i in r.findall("re_inf")},
                     "nokanji": r.find("re_nokanji") is not None,
                     "restr": [x.text for x in r.findall("re_restr")] or None})
    senses, last_pos = [], []
    for s in e.findall("sense"):
        pos = [p.text for p in s.findall("pos")] or last_pos  # pos carries over
        last_pos = pos
        gl = [g.text.strip() for g in s.findall("gloss") if g.text and g.text.strip()]
        misc = [m.text for m in s.findall("misc")]
        senses.append({"pos": pos, "gloss": gl, "misc": misc,
                       "stagk": [x.text for x in s.findall("stagk")] or None,
                       "stagr": [x.text for x in s.findall("stagr")] or None})
    return int(e.findtext("ent_seq")), kebs, rebs, senses


def reading_ok_for(r, keb):
    return not r["nokanji"] and (r["restr"] is None or keb in r["restr"])


def best_reading(rebs, keb):
    ok = [r for r in rebs if reading_ok_for(r, keb)]
    ok.sort(key=lambda r: bool(r["inf"] & ODD))  # prefer regular readings (stable)
    return ok[0]["t"] if ok else rebs[0]["t"]


def senses_for(senses, keb=None, reb=None):
    out = [s for s in senses if s["gloss"]
           and (keb is None or s["stagk"] is None or keb in s["stagk"])
           and (reb is None or s["stagr"] is None or reb in s["stagr"])]
    if not out and keb is not None and reb is not None:  # stagr too strict -> stagk only
        out = senses_for(senses, keb=keb)
    return out


def definition_text(ss):
    parts = []
    for s in ss:
        g = "; ".join(s["gloss"])
        if "uk" in s["misc"]:
            g += " (usu. kana)"
        parts.append(g)
    if len(parts) == 1:
        return parts[0]
    return " / ".join(f"{i}. {g}" for i, g in enumerate(parts, 1))


def build(text, mode, extra_tags):
    root = ET.fromstring(text)
    wanted_tags = SEARCH_ONLY | set(extra_tags)
    if mode != "search-only":
        wanted_tags |= ODD
    out, stats = [], {"entries": 0, "skipped_no_gloss": 0}
    for e in root.iter("entry"):
        stats["entries"] += 1
        seq, kebs, rebs, senses = parse_entry(e)
        main = next((k["t"] for k in kebs if not (k["inf"] & ODD)), None)

        terms = []  # (term, pronunciation, keb-or-None, reb-or-None)
        for i, k in enumerate(kebs):
            take = (mode == "full"
                    or bool(k["inf"] & wanted_tags)
                    or (mode == "alt" and k["t"] != main))
            if take:
                rd = best_reading(rebs, k["t"])
                terms.append((k["t"], rd, k["t"], rd))
        for r in rebs:
            if mode == "full" or (r["inf"] & wanted_tags):
                terms.append((r["t"], r["t"], None, r["t"]))

        for term, pron, keb, reb in terms:
            ss = senses_for(senses, keb=keb, reb=reb)
            if not ss:
                stats["skipped_no_gloss"] += 1
                continue
            # altterm: main spelling usable with this reading
            alt = ""
            if keb is not None:
                alt = main or ""
            else:
                r = next(x for x in rebs if x["t"] == term)
                if not r["nokanji"]:
                    alt = next((k["t"] for k in kebs if not (k["inf"] & ODD)
                                and reading_ok_for(r, k["t"])), "")
            if alt == term:
                alt = ""
            out.append({"term": term, "altterm": alt, "pronunciation": pron,
                        "definition": definition_text(ss),
                        "pos": ",".join(ss[0]["pos"]),
                        "examples": "", "audio": "",
                        "_seq": seq})
    return out, stats


def write_yomitan(entries, path, title, revision):
    """Yomitan format-3 ZIP with plain-string glossaries (no structured-content)."""
    bank = []
    for i, o in enumerate(entries):
        gl = [g for g in re.split(r" / (?=\d+\. )", o["definition"])]
        bank.append([o["term"], o["pronunciation"], "", "", 0, gl, o["_seq"], ""])
    index = {"title": title, "format": 3, "revision": revision, "sequenced": True,
             "author": "JMdict (EDRDG)",
             "attribution": "JMdict (c) Electronic Dictionary Research and Development Group, "
                            "CC BY-SA 3.0, https://www.edrdg.org/edrdg/licence.html",
             "description": "Supplement generated from JMdict_e"}
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("index.json", json.dumps(index, ensure_ascii=False))
        for n in range(0, len(bank), 10000):
            z.writestr(f"term_bank_{n // 10000 + 1}.json",
                       json.dumps(bank[n:n + 10000], ensure_ascii=False))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("jmdict", nargs="?", help="JMdict_e.gz or XML (default: download)")
    ap.add_argument("--mode", choices=["search-only", "alt", "full"], default="search-only")
    ap.add_argument("--also", nargs="*", default=[],
                    help="extra tags for search-only mode, e.g. iK oK rK ik ok rk")
    ap.add_argument("--outdir", default=".")
    ap.add_argument("--name", default=None)
    ap.add_argument("--yomitan", action="store_true", help="also write a Yomitan format-3 ZIP")
    a = ap.parse_args()

    text = load_xml_text(a.jmdict)
    created = re.search(r"JMdict created: (\S+)", text)
    revision = created.group(1) if created else "unknown"
    entries, stats = build(text, a.mode, a.also)

    name = a.name or {"search-only": "JMdict-search-only-forms",
                      "alt": "JMdict-alt-spellings", "full": "JMdict-full-migaku"}[a.mode]
    os.makedirs(a.outdir, exist_ok=True)
    base = os.path.join(a.outdir, name)
    migaku = [{k: o[k] for k in MIGAKU_KEYS} for o in entries]
    with open(base + ".json", "w", encoding="utf-8") as f:
        json.dump(migaku, f, ensure_ascii=False, indent=1)
    with zipfile.ZipFile(base + ".zip", "w", zipfile.ZIP_DEFLATED) as z:
        z.write(base + ".json", arcname=name + ".json")
    if a.yomitan:
        write_yomitan(entries, base + "-yomitan.zip", f"{name} [{revision}]", f"{name}.{revision}")

    print(f"JMdict {revision}: {stats['entries']} entries parsed, "
          f"{stats['skipped_no_gloss']} terms skipped (no applicable gloss)")
    print(f"{len(migaku)} records written to {base}.json / .zip"
          + (" / -yomitan.zip" if a.yomitan else ""))
    for t in ("思い留まる", "思い止どまる"):
        hit = [o for o in migaku if o["term"] == t]
        print(t, "present:", bool(hit), json.dumps(hit[0], ensure_ascii=False) if hit else "")


if __name__ == "__main__":
    main()
