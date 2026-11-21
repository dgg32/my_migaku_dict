"""Build the reader's SQLite dictionary from a Jitendex (Yomitan format) zip.

Usage:  python build_db.py [path/to/jitendex-yomitan.zip]
Writes: data/jitendex.sqlite and data/jitendex.css
"""
import gzip
import json
import re
import sqlite3
import sys
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
DEFAULT_ZIP = HERE.parent / "jitendex-yomitan.zip"
DEFAULT_NAMES = HERE.parent / "JMnedict.xml.gz"

# JMnedict name types (expanded DTD entities) -> short labels for the popup
NAME_TYPES = {
    "family or surname": "surname", "female given name or forename": "female given name",
    "male given name or forename": "male given name", "given name or forename, gender not specified": "given name",
    "full name of a particular person": "person", "place name": "place", "unclassified name": "name",
    "railway station": "station", "organization name": "organization", "company name": "company",
    "work of art, literature, music, etc. name": "work", "product name": "product",
}


def build(zip_path: Path = DEFAULT_ZIP, out_dir: Path = DATA) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    db_path = out_dir / "jitendex.sqlite"
    tmp_path = db_path.with_suffix(".tmp")
    tmp_path.unlink(missing_ok=True)

    con = sqlite3.connect(tmp_path)
    con.executescript(
        """
        CREATE TABLE terms (
            id INTEGER PRIMARY KEY,
            expression TEXT NOT NULL,
            reading TEXT NOT NULL,
            def_tags TEXT,
            rules TEXT,
            score INTEGER,
            glossary TEXT NOT NULL,
            sequence INTEGER,
            term_tags TEXT
        );
        CREATE TABLE tags (name TEXT PRIMARY KEY, category TEXT, notes TEXT);
        """
    )
    with zipfile.ZipFile(zip_path) as z:
        names = sorted(
            (n for n in z.namelist() if re.fullmatch(r"term_bank_\d+\.json", n)),
            key=lambda n: int(re.search(r"\d+", n).group()),
        )
        if not names:
            raise SystemExit(f"No term_bank_*.json files in {zip_path}")
        count = 0
        for n in names:
            rows = json.loads(z.read(n))
            con.executemany(
                "INSERT INTO terms (expression, reading, def_tags, rules, score, glossary, sequence, term_tags)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (
                    (r[0], r[1] or r[0], r[2], r[3], r[4],
                     json.dumps(r[5], ensure_ascii=False), r[6], r[7])
                    for r in rows
                ),
            )
            count += len(rows)
        for n in z.namelist():
            if re.fullmatch(r"tag_bank_\d+\.json", n):
                for t in json.loads(z.read(n)):
                    con.execute("INSERT OR REPLACE INTO tags VALUES (?,?,?)", (t[0], t[1], t[3]))
        if "styles.css" in z.namelist():
            (out_dir / "jitendex.css").write_bytes(z.read("styles.css"))

    con.executescript(
        """
        CREATE INDEX idx_expr ON terms(expression);
        CREATE INDEX idx_reading ON terms(reading);
        CREATE INDEX idx_seq ON terms(sequence);
        """
    )
    con.commit()
    con.close()
    tmp_path.replace(db_path)
    print(f"Wrote {count} entries to {db_path}")
    return db_path


def build_names(gz_path: Path = DEFAULT_NAMES, db_path: Path = DATA / "jitendex.sqlite") -> int:
    """Add (or replace) the JMnedict names table: one row per written form x reading."""
    con = sqlite3.connect(db_path)
    con.executescript("DROP TABLE IF EXISTS names; CREATE TABLE names "
                      "(expression TEXT NOT NULL, reading TEXT NOT NULL, types TEXT, gloss TEXT, seq INTEGER);")
    rows, count = [], 0
    with gzip.open(gz_path) as f:
        for _, el in ET.iterparse(f):
            if el.tag != "entry":
                continue
            seq = int(el.findtext("ent_seq") or 0)
            rebs = [r.text for r in el.iter("reb") if r.text]
            kebs = [k.text for k in el.iter("keb") if k.text] or rebs
            types = []
            for n in el.iter("name_type"):
                t = NAME_TYPES.get(n.text or "", n.text or "")
                if t and t not in types:
                    types.append(t)
            gloss = "; ".join(t.text for t in el.iter("trans_det") if t.text)[:300]
            for k in kebs:
                for r in rebs:
                    rows.append((k, r, ", ".join(types), gloss, seq))
            el.clear()
            if len(rows) > 50000:
                con.executemany("INSERT INTO names VALUES (?,?,?,?,?)", rows)
                count += len(rows)
                rows = []
    con.executemany("INSERT INTO names VALUES (?,?,?,?,?)", rows)
    count += len(rows)
    con.execute("CREATE INDEX idx_names_expr ON names(expression)")
    con.commit()
    con.close()
    print(f"Wrote {count} name readings from {gz_path.name}")
    return count


if __name__ == "__main__":
    build(Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_ZIP)
    if DEFAULT_NAMES.exists():
        build_names()
