"""Build the reader's SQLite dictionary from a Jitendex (Yomitan format) zip.

Usage:  python build_db.py [path/to/jitendex-yomitan.zip]
Writes: data/jitendex.sqlite and data/jitendex.css
"""
import json
import re
import sqlite3
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
DEFAULT_ZIP = HERE.parent / "jitendex-yomitan.zip"


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


if __name__ == "__main__":
    build(Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_ZIP)
