"""Local Japanese reader: furigana + longest-match Jitendex lookup + DeepSeek translation/chat.

Run:  .venv/bin/python server.py      then open http://127.0.0.1:8787
"""
import json
import os
import re
import hashlib
import shutil
import sqlite3
import subprocess
import threading
import unicodedata
import urllib.parse
import uuid
from functools import lru_cache
from pathlib import Path

import fugashi
import httpx
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
DB_PATH = DATA / "jitendex.sqlite"


# ---------------------------------------------------------------- config

def load_env():
    env_file = HERE / ".env"
    """Read reader/.env. Shell variables win; within the file the last non-empty value wins."""
    if env_file.exists():
        values = {}
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line.startswith("export "):
                line = line[7:].strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                v = v.strip().strip('"').strip("'")
                if v:
                    values[k.strip()] = v
        for k, v in values.items():
            if not os.environ.get(k):
                os.environ[k] = v


load_env()
API_KEY = os.environ.get("OPENCODE_API_KEY", "")
BASE_URL = os.environ.get("OPENCODE_BASE_URL", "https://opencode.ai/inference/go/openai/v1").rstrip("/")
MODEL = os.environ.get("OPENCODE_MODEL", "glm-5.3-flash")
# "low" keeps thinking short so text starts streaming in ~1 s; "default" leaves it to the model
REASONING_EFFORT = os.environ.get("OPENCODE_REASONING_EFFORT", "low")


def best_japanese_voice() -> str:
    """The best installed ja_JP voice for `say`: Premium, then Enhanced, then Kyoko."""
    try:
        out = subprocess.run(["say", "-v", "?"], capture_output=True, text=True, timeout=15).stdout
    except (OSError, subprocess.SubprocessError):
        return "Kyoko"
    names = [m.group(1).strip() for m in re.finditer(r"^(.+?)\s+ja_JP\s", out, re.M)]
    for tier in ("Premium", "Enhanced"):
        for preferred in ("Kyoko", "Otoya", ""):
            for n in names:
                if tier in n and n.startswith(preferred):
                    return n
    return "Kyoko"


VOICE = os.environ.get("READER_VOICE") or best_japanese_voice()  # any ja_JP voice from `say -v ?`
PORT = int(os.environ.get("READER_PORT", "8787"))
SESSION_ID = str(uuid.uuid4())


# ---------------------------------------------------------------- dictionary

if not DB_PATH.exists():
    from build_db import build
    print("Dictionary database not found, building it from ../jitendex-yomitan.zip ...")
    build()

def ensure_indexes():
    con = sqlite3.connect(DB_PATH)
    con.execute("CREATE INDEX IF NOT EXISTS idx_seq ON terms(sequence)")
    con.commit()
    con.close()


ensure_indexes()


def ensure_names():
    """Add the JMnedict names table on first start after ../JMnedict.xml.gz is downloaded."""
    con = sqlite3.connect(DB_PATH)
    has = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='names'").fetchone()
    con.close()
    if not has:
        from build_db import DEFAULT_NAMES, build_names
        if DEFAULT_NAMES.exists():
            print("Adding the JMnedict names dictionary ...")
            build_names(DEFAULT_NAMES, DB_PATH)


ensure_names()

# Readings the user pinned for names (平城 -> ひらじろ), applied to every occurrence
PINS_PATH = DATA / "pinned_readings.json"
try:
    PINS: dict[str, str] = json.loads(PINS_PATH.read_text()) if PINS_PATH.exists() else {}
except (OSError, json.JSONDecodeError):
    PINS = {}


def save_pins():
    tmp = PINS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(PINS, ensure_ascii=False, indent=1, sort_keys=True))
    tmp.replace(PINS_PATH)
_local = threading.local()


def db():
    if not hasattr(_local, "con"):
        _local.con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, check_same_thread=False)
    return _local.con


# expression -> readings ordered by Jitendex score (best first)
READINGS: dict[str, list[str]] = {}
for expr, reading in db().execute("SELECT expression, reading FROM terms ORDER BY score DESC, id"):
    lst = READINGS.setdefault(expr, [])
    if reading not in lst:
        lst.append(reading)
MAX_KEY_LEN = max(len(k) for k in READINGS)
KANA_READINGS = {r for rs in READINGS.values() for r in rs if all("ぁ" <= c <= "ゖ" or c == "ー" for c in r)}
print(f"Loaded {len(READINGS)} dictionary headwords")


# ---------------------------------------------------------------- kana helpers

def to_hira(s: str) -> str:
    return "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in s)


def is_kana(c: str) -> bool:
    return "ぁ" <= c <= "ゖ" or "ァ" <= c <= "ヺ" or c in "ーゝゞヽヾ"


def is_kanji(c: str) -> bool:
    return ("一" <= c <= "鿿" or "㐀" <= c <= "䶿" or "豈" <= c <= "﫿"
            or "\U00020000" <= c <= "\U0003134f" or c in "々〆ヵヶ〇")


def has_kanji(s: str) -> bool:
    return any(is_kanji(c) for c in s)


def all_kana(s: str) -> bool:
    return bool(s) and all(is_kana(c) for c in s)


def ruby_parts(surface: str, reading: str):
    """Split surface into [base, rt] pairs so furigana sits over kanji only."""
    if not reading or not has_kanji(surface):
        return [[surface, ""]]
    reading = to_hira(reading)
    runs = []  # (text, is_kanji_run)
    for ch in surface:
        # "k": kanji/digits/latin get furigana, "h": kana matched literally, "s": symbols like ・ (silent)
        k = "h" if is_kana(ch) else "s" if unicodedata.category(ch).startswith(("P", "S", "Z")) else "k"
        if runs and runs[-1][1] == k:
            runs[-1][0] += ch
        else:
            runs.append([ch, k])
    pat = {"k": lambda t: "(.+?)", "h": lambda t: "(" + re.escape(to_hira(t)) + ")", "s": lambda t: "()"}
    m = re.fullmatch("".join(pat[k](t) for t, k in runs), reading)
    if not m:
        return [[surface, reading]]
    return [[t, m.group(i + 1) if k == "k" else ""] for i, (t, k) in enumerate(runs)]


# ---------------------------------------------------------------- analysis

def tagger():
    """MeCab taggers are not thread-safe; FastAPI runs sync handlers in a threadpool."""
    if not hasattr(_local, "tagger"):
        _local.tagger = fugashi.Tagger()
    return _local.tagger


SKIP_POS = {"補助記号", "空白"}
MAX_TOKENS = 12
# potential / e-row ichidan forms back to godan dictionary form (抱ける -> 抱く)
POTENTIAL = {"け": "く", "せ": "す", "て": "つ", "ね": "ぬ", "べ": "ぶ", "め": "む", "れ": "る", "げ": "ぐ", "え": "う"}
KO_ENDINGS = ("させ", "さす", "され", "られ", "ない", "なかっ", "なけれ", "なく", "ず", "よう")
AUX_WORDS = {"ない", "すぎる", "過ぎる", "やすい", "にくい", "づらい", "がたい"}


class Tok:
    __slots__ = ("surface", "pos1", "pos2", "pos3", "ctype", "cform", "lemma", "base", "kana", "kana_base", "s", "e")


def tokenize(text: str):
    toks, pos = [], 0
    for w in tagger()(text):
        surf = w.surface
        start = text.find(surf, pos)
        if start < 0:
            continue
        f = w.feature
        t = Tok()
        t.surface, t.s, t.e = surf, start, start + len(surf)
        t.pos1, t.pos2 = f.pos1 or "", f.pos2 or ""
        t.ctype = f.cType or ""
        t.pos3, t.cform, t.lemma = f.pos3 or "", f.cForm or "", f.lemma or ""
        t.base = f.orthBase if f.orthBase and f.orthBase != "*" else surf
        t.kana = to_hira(f.kana) if f.kana and f.kana != "*" else ""
        t.kana_base = to_hira(f.kanaBase) if f.kanaBase and f.kanaBase != "*" else ""
        toks.append(t)
        pos = t.e
    # unidic sometimes reads 来 as き before こ-stem endings (来させる, 来られる)
    for t, n in zip(toks, toks[1:]):
        if t.surface == "来" and t.base == "来る" and n.surface.startswith(KO_ENDINGS) and not n.surface.startswith("なさ"):
            t.kana = "こ"
    return toks


def pick_reading(key: str, guess: str) -> str:
    """Prefer Jitendex: use the tokenizer's reading only to choose among Jitendex's readings."""
    rs = [r for r in READINGS.get(key) or [] if not has_kanji(r)]  # redirect rows carry no reading
    if not rs:
        return guess
    if guess and guess in rs:
        return guess
    return rs[0]


def skeleton(text: str) -> str:
    """Kana pattern with each kanji run as ＊: 吞んだくれる and 飲んだくれる both give ＊んだくれる."""
    return re.sub(r"＊+", "＊", "".join("＊" if is_kanji(c) else to_hira(c) for c in text))


# (reading, kana pattern) -> headwords, for phrases written with other kanji than the dictionary uses
VARIANTS: dict[tuple, list] = {}
for _expr, _rs in READINGS.items():
    if len(_expr) >= 3 and has_kanji(_expr) and not all(is_kanji(c) for c in _expr):
        _sk = skeleton(_expr)
        for _r in _rs:
            if not has_kanji(_r):
                VARIANTS.setdefault((_r, _sk), []).append(_expr)


def variant_key(surf: str, reading: str):
    """Headword spelled with other kanji but read the same: 吞んだくれ -> 飲んだくれ."""
    hits = VARIANTS.get((to_hira(reading), skeleton(surf)))
    return hits[0] if hits and has_kanji(surf) else None


def after_number(toks, i: int) -> bool:
    """百円: a counter after a numeral is never a pinned name (円 -> まどか must not touch yen)."""
    return i > 0 and toks[i - 1].pos2 == "数詞"


def candidates(text: str, toks, i: int):
    """All dictionary matches starting at token i, longest first: (end_tok, key, inflected)."""
    out = []
    for j in range(min(len(toks), i + MAX_TOKENS), i, -1):
        span = toks[i:j]
        if any(t.pos1 in SKIP_POS for t in span):
            continue
        surf = text[toks[i].s:toks[j - 1].e]
        if len(surf) > MAX_KEY_LEN + 4:
            continue
        last = toks[j - 1]
        if surf in PINS and not after_number(toks, i):
            out.append((j, surf, False))  # pinned name, even if the tokenizer split it
            continue
        if (j - i > 1 and last.pos1 == "助詞" and j < len(toks) and toks[j].surface[0] not in SENTENCE_END
                and surf in READINGS and interjection_only(surf)):
            continue  # 今日は暑い is 今日+は, not the greeting こんにちは
        base = text[toks[i].s:last.s] + last.base
        inflects = last.pos1 in ("動詞", "形容詞") and last.base != last.surface
        if inflects and base in READINGS:  # 重 -> 重い rather than the noun 重
            out.append((j, base, True))
        elif surf in READINGS:
            out.append((j, surf, False))
        elif base != surf and base in READINGS:
            out.append((j, base, True))
        elif j == i + 1:
            key = single_fallback(last, surf)
            if key:
                out.append((j, key, key != surf))
        elif j - i >= 2 and all(t.kana for t in span):
            # other kanji for the same word (吞んだくれる for 飲んだくれる); several tokens only, so that
            # single words that merely sound alike (会う / 遭う) are never confused
            noun = variant_key(surf, "".join(t.kana for t in span))
            verb = (variant_key(base, "".join(t.kana for t in toks[i:j - 1]) + last.kana_base)
                    if base != surf else None)
            found = [(j, noun, False)] if noun else []
            if verb:
                # 吞んだくれている: the verb 飲んだくれる; ただの吞んだくれだ: the noun 飲んだくれ
                nxt = toks[j] if j < len(toks) else None
                copula = nxt is not None and nxt.base in ("だ", "です") and nxt.lemma != "た"
                verb_first = not noun or (nxt is not None and absorbs(last, nxt) and not copula)
                found.insert(0 if verb_first else len(found), (j, verb, True))
            out += found
    return out


@lru_cache(maxsize=4096)
def interjection_only(key: str) -> bool:
    """True for greetings like 今日は / 今晩は whose every entry is an interjection."""
    rows = db().execute("SELECT glossary FROM terms WHERE expression = ?", (key,)).fetchall()
    codes = set()
    for (g,) in rows:
        codes |= set(re.findall(r'"code": "([^"]+)", "content": "part-of-speech-info"', g))
    return codes == {"int"}


SENTENCE_END = "。！？!?…」』）)"
RARE_TAGS = ("obsolete", "rarely", "irregular", "search")


@lru_cache(maxsize=65536)
def usable_readings(key: str) -> tuple:
    """Jitendex readings of key that are in real use (no obsolete/rare/irregular forms), best first."""
    out = []
    for reading, tags, score in db().execute(
            "SELECT reading, def_tags, score FROM terms WHERE expression = ? ORDER BY score DESC, id", (key,)):
        if has_kanji(reading) or score < -50 or any(t in (tags or "") for t in RARE_TAGS):
            continue
        if reading not in out:
            out.append(reading)
    return tuple(out)


def single_fallback(t: Tok, surf: str):
    h = to_hira(surf)
    if h != surf and h in READINGS:  # katakana spelling of a kana word
        return h
    b = t.base
    if t.pos1 == "動詞" and t.ctype.startswith("下一段") and len(b) >= 2 and b.endswith("る") and b[-2] in POTENTIAL:
        g = b[:-2] + POTENTIAL[b[-2]]  # potential form: 抱ける -> 抱く
        if g in READINGS:
            return g
    if all_kana(b) and to_hira(b) in KANA_READINGS:  # すぎる -> 過ぎる (looked up by reading)
        return to_hira(b)
    return None


def absorbs(prev: Tok, t: Tok) -> bool:
    """Should token t be shown as part of the inflected word ending in prev?"""
    if t.pos1 == "助動詞":
        return True
    if t.pos1 == "助詞" and t.pos2 == "接続助詞" and t.surface in ("て", "で", "ちゃ", "じゃ", "ば"):
        return True
    if prev.surface in ("て", "で") and t.pos2 == "非自立可能" and t.pos1 in ("動詞", "形容詞"):
        return True
    if t.pos2 == "非自立可能" and t.base in AUX_WORDS and prev.pos1 in ("動詞", "形容詞", "助動詞"):
        return True
    return False


INFLECTING = {"動詞", "形容詞", "助動詞"}


def strip_reading(key: str, dict_reading: str, surf: str):
    """Surface reading from a dictionary reading by swapping the okurigana: 抱く/だく + 抱いて -> だいて."""
    k, r = key, dict_reading
    while k and r and is_kana(k[-1]) and to_hira(k[-1]) == r[-1]:
        k, r = k[:-1], r[:-1]
    if k and surf.startswith(k) and r:
        return r + to_hira(surf[len(k):])
    return None


def word_reading(text, toks, i, j, key, inflected):
    """(reading of text[toks[i].s:toks[j-1].e], the Jitendex reading of key it came from)."""
    guess_surface = "".join(t.kana for t in toks[i:j])
    if not inflected:
        r = pick_reading(key, guess_surface)
        return r, r
    guess_base = "".join(t.kana for t in toks[i:j - 1]) + toks[j - 1].kana_base
    if not key.endswith(toks[j - 1].base) and len(guess_base) >= 2 and guess_base[-2] in POTENTIAL:
        guess_base = guess_base[:-2] + POTENTIAL[guess_base[-2]]  # いだける -> いだく
    dict_reading = pick_reading(key, guess_base)
    # The tokenizer's dictionary-form reading agrees with Jitendex, so its contextual reading of the
    # surface is trustworthy and handles irregular stems (来ます -> きます, 来ない -> こない).
    if guess_base == dict_reading and guess_surface and not has_kanji(guess_surface):
        return guess_surface, dict_reading
    stripped = strip_reading(key, dict_reading, text[toks[i].s:toks[j - 1].e])
    return (stripped or guess_surface), dict_reading


def analyze(text: str):
    paragraphs = []
    offset = 0
    for line in text.split("\n"):
        paragraphs.append(analyze_line(line, offset))
        offset += u16len(line) + 1
    return paragraphs


def u16len(s: str) -> int:
    return len(s) + sum(1 for c in s if ord(c) > 0xFFFF)


# ---------------------------------------------------------------- conjugation labels (10ten style)

AUX_LABELS = {
    "れる": "passive or respectful", "られる": "passive, potential or respectful",
    "せる": "causative", "させる": "causative", "す": "causative", "さす": "causative",
    "た": "past", "ない": "negative", "ぬ": "negative", "ず": "negative", "ん": "negative",
    "ます": "polite", "です": "polite", "たい": "want to", "う": "volitional", "よう": "volitional",
    "まい": "negative volitional", "ば": "conditional (-ba)", "ちゃ": "-te wa", "じゃ": "-te wa",
    "そう": "looks like / hearsay", "らしい": "seems", "すぎる": "too much", "過ぎる": "too much",
    "やすい": "easy to", "にくい": "hard to", "づらい": "hard to", "がたい": "hard to",
}
TE_LABELS = {"いる": "-te iru", "ある": "-te aru", "しまう": "-te shimau", "おく": "-te oku", "みる": "-te miru",
             "いく": "-te iku", "くる": "-te kuru", "くれる": "-te kureru", "あげる": "-te ageru",
             "もらう": "-te morau", "ない": "negative"}
FORM_LABELS = {"命令形": "imperative", "意志推量形": "volitional"}


def conj_chain(stem: Tok, extra: list, suru: bool, potential: bool) -> str:
    """'-suru < passive or respectful < past' for 接見された, like 10ten Reader."""
    chain = []
    if suru:
        chain.append("-suru")
    if potential:
        chain.append("potential")
    form = stem.cform.split("-")[0]
    if form in FORM_LABELS:
        chain.append(FORM_LABELS[form])
    elif not extra and stem.surface != stem.base:
        if stem.pos1 == "動詞" and form == "連用形":
            chain.append("masu stem")
        elif stem.pos1 == "形容詞" and form == "連用形":
            chain.append("adverbial (-ku)")
    for prev, t in zip([stem] + extra, extra):
        if t.surface in ("て", "で") and t.pos1 == "助詞":
            chain.append("-te")
            continue
        if chain and chain[-1] == "-te" and t.pos1 in ("動詞", "形容詞"):
            chain[-1] = TE_LABELS.get(t.base, "-te " + t.base)
        else:
            label = AUX_LABELS.get(t.base) or AUX_LABELS.get(t.lemma)
            if t.pos1 == "助動詞" and t.base == "だ" and t.lemma == "た":
                label = "past"  # 呑んだ
            if label and not (label == "polite" and label in chain):
                chain.append(label)
        form = t.cform.split("-")[0]
        if t is extra[-1] and form in FORM_LABELS:
            chain.append(FORM_LABELS[form])
    return " < ".join(chain)


IRREGULAR = ("カ行変格", "サ行変格")


def alternatives(line, toks, i, j, end, key, inflected, reading, dict_reading, surface):
    """Other plausible readings of an ambiguous word, for the AI reading check.

    Returns [[surface reading, ruby, dictionary reading], ...] with the current choice first, or [].
    """
    if not has_kanji(surface) or any(t.ctype.startswith(IRREGULAR) for t in toks[i:j]):
        return []  # 来る/する stems change irregularly; the tokenizer handles those
    options = usable_readings(key)
    if len(options) < 2:
        return []
    matched = line[toks[i].s:toks[j - 1].e]
    # reading of the matched part, so the absorbed tail (た, される…) can be reattached to each option
    head = word_head(key, dict_reading, matched, inflected)
    if head is None or not reading.startswith(head):
        return []
    tail = reading[len(head):]
    out = [[reading, ruby_parts(surface, reading), dict_reading]]
    for d in options:
        if d == dict_reading:
            continue
        h = word_head(key, d, matched, inflected)
        if h and not (key == "行く" and h.startswith("ゆっ")):  # 行った is いった for both いく and ゆく
            r = h + tail
            if all(r != o[0] for o in out):
                out.append([r, ruby_parts(surface, r), d])
    return out if len(out) > 1 else []


def word_head(key, dict_reading, matched, inflected):
    if not inflected:
        return dict_reading
    return strip_reading(key, dict_reading, matched)


def analyze_line(line: str, base: int):
    toks = tokenize(line)
    segs, pos, i = [], 0, 0
    u16 = [0]  # code-point index -> UTF-16 offset within the line
    for c in line:
        u16.append(u16[-1] + (2 if ord(c) > 0xFFFF else 1))

    def plain(a, b):
        if b > a:
            segs.append({"s": base + u16[a], "e": base + u16[b], "t": line[a:b]})

    while i < len(toks):
        t = toks[i]
        plain(pos, t.s)
        if t.pos1 in SKIP_POS:
            plain(t.s, t.e)
            pos, i = t.e, i + 1
            continue
        cands = candidates(line, toks, i)
        if cands:
            j, key, inflected = cands[0]
            reading, dict_reading = word_reading(line, toks, i, j, key, inflected)
            pinned = key in PINS and line[toks[i].s:toks[j - 1].e] == key and not after_number(toks, i)
            if pinned:
                reading = dict_reading = PINS[key]
            keys = []
            for _, k, _ in cands:
                if k not in keys:
                    keys.append(k)
        else:
            j, key, reading, dict_reading = i + 1, None, t.kana, None
            keys = [t.base] if t.base != t.surface else []
            pinned = False
        # extend the clickable word over auxiliaries (駆り立て+た, 射+た, し+て+いる)
        end = j
        suru = False
        if (key and toks[j - 1].pos3 == "サ変可能" and end < len(toks)
                and toks[end].pos1 == "動詞" and toks[end].base == "する"):
            suru = True  # 接見 + された
            reading += toks[end].kana
            end += 1
        if suru or toks[j - 1].pos1 in INFLECTING or (cands and cands[0][2]):
            while end < len(toks) and toks[end].pos1 not in SKIP_POS and absorbs(toks[end - 1], toks[end]):
                reading += toks[end].kana or to_hira(toks[end].surface)
                end += 1
        s, e = toks[i].s, toks[end - 1].e
        surface = line[s:e]
        # the AI can only guess a fictional character's reading, so person names stay out of the check
        person = any(tk.pos2 == "固有名詞" and tk.pos3 == "人名" for tk in toks[i:j])
        alts = alternatives(line, toks, i, j, end, key, inflected if cands else False,
                            reading, dict_reading, surface) if key and not pinned and not person else []
        # conjugation chain per matched headword: 食べられる -> {"食べる": "passive, potential or respectful"}
        conj = {}
        for jc, kc, ic in cands:
            if kc in conj or (not ic and jc == j and kc != line[toks[i].s:toks[j - 1].e]):
                continue  # a noun read off a verb-shaped spelling (飲んだくれ in 吞んだくれている) has no conjugation
            if jc < j and not all(absorbs(a, b) for a, b in zip(toks[jc - 1:end - 1], toks[jc:end])):
                continue  # 的 of 的を射た: the rest is not an ending
            suru_c = suru and jc == j
            stem = toks[jc] if suru_c else toks[jc - 1]
            span_base = line[toks[i].s:toks[jc - 1].s] + toks[jc - 1].base
            potential = (kc != span_base and len(span_base) >= 2 and span_base[-2] in POTENTIAL
                         and kc == span_base[:-2] + POTENTIAL[span_base[-2]])
            chain = conj_chain(stem, toks[jc + (1 if suru_c else 0):end], suru_c, potential)
            if chain:
                conj[kc] = chain
        if key is None and not has_kanji(surface) and t.pos1 in ("助詞", "助動詞"):
            plain(s, e)  # unknown particle: no popup
        else:
            segs.append({
                "s": base + u16[s], "e": base + u16[e], "t": surface,
                "r": ruby_parts(surface, reading),
                "k": keys, "w": 1, "m": 1 if key else 0,
                "p": 1 if key and j - i > 1 and len(key) > 2 and not all_kana(key) else 0,
                **({"c": conj} if conj else {}),
                **({"a": alts} if alts else {}),
                **({"pin": 1} if pinned else {}),
                **({"nm": 1} if person else {}),
            })
        pos, i = e, end
    plain(pos, len(line))
    return segs


# ---------------------------------------------------------------- lookup

TAG_NAMES = sorted((r[0] for r in db().execute("SELECT name FROM tags")), key=len, reverse=True)


def split_tags(*strings):
    """Jitendex tag names contain spaces ("priority form"), so match known names greedily."""
    out = []
    for s in strings:
        s = (s or "").strip()
        while s:
            name = next((n for n in TAG_NAMES if s == n or s.startswith(n + " ")), s.split(" ")[0])
            if name not in out:
                out.append(name)
            s = s[len(name):].strip()
    return out


def redirect_target(glossary):
    """(target expression, primary reading) of a "⟶ X" redirect row, else None."""
    for g in glossary:
        if isinstance(g, list) and g and isinstance(g[0], str):
            m = re.search(r"primary_reading=([^&\"]+)", json.dumps(glossary, ensure_ascii=False))
            return g[0], (urllib.parse.unquote(m.group(1)) if m else None)
    return None


def resolve_redirect(seq, target, primary_reading):
    """A redirect row's negative sequence names the exact entry it points to (數 -1005580 -> 数/しばしば)."""
    rows = rows_for("sequence = ?", (abs(seq),), 40) if seq and seq < 0 else []
    if not rows:
        rows = rows_for("expression = ?", (target,), 5)
    if not rows:
        return None
    return max(rows, key=lambda r: (r[0] == target, r[1] == primary_reading))


def rows_for(where, args, limit=30):
    return db().execute(
        f"SELECT expression, reading, def_tags, rules, score, glossary, sequence, term_tags FROM terms"
        f" WHERE {where} ORDER BY score DESC, id LIMIT {limit}", args).fetchall()


def lookup_key(key: str, seen_seq: set):
    rows = list(rows_for("expression = ?", (key,)))
    if all_kana(key):
        rows += rows_for("reading = ? AND expression != ?", (key, key), 12)
    entries, by_seq = [], {}
    for expr, reading, def_tags, rules, score, gloss, seq, term_tags in rows:
        glossary = json.loads(gloss)
        redirected = None
        target = redirect_target(glossary)
        if target:
            row = resolve_redirect(seq, *target)
            if not row:
                continue
            redirected = expr
            expr, reading, def_tags, rules, score, gloss, seq, term_tags = row
            glossary = json.loads(gloss)
        ident = (expr, seq)
        if ident in by_seq:
            e = by_seq[ident]
            if reading not in e["readings"]:
                e["readings"].append(reading)
            continue
        if seq in seen_seq and ident not in by_seq and redirected:
            continue
        e = {"expression": expr, "readings": [reading], "tags": split_tags(term_tags, def_tags), "sequence": seq, "glossary": glossary, "redirected": redirected, "key": key}
        by_seq[ident] = e
        entries.append(e)
    for e in entries:
        seen_seq.add(e["sequence"])
        e["ruby"] = ruby_parts(e["expression"], e["readings"][0])
    return entries


# ---------------------------------------------------------------- LLM

TRANSLATE_SYSTEM = """你是一位精通日语和中文的翻译与日语老师。用户正在阅读日语小说。
请把用户选中的日语句子翻译成自然、准确的简体中文，然后用简体中文写“Notes:”讲解句中的关键词语、惯用表达和语法点。

严格使用以下格式：第一行直接写译文本身，不要写“译文”“Translation”等标题或任何前言，也不要用尖括号或其它标记包裹。
译文之后空一行，再写 Notes。示例：

驱使他犯罪的，首要原因就是家庭环境。

Notes:
- 一にも二にも（いちにもににも）：惯用表达，意为“首先、最重要的是”，本句强调家庭环境是首要原因。
- 駆り立てる（かりたてる）：他动词，驱使、逼迫，本句为过去式「駆り立てた」。

要求：
- 译文要结合上下文（会提供上下文，但只翻译选中的部分）。
- Notes 选 3–8 个对学习者最有价值的词语或语法，惯用句、四字熟语、复合词优先，常见简单词不讲。
- 读音用平假名，务必准确（例：訥々＝とつとつ）。不确定读音时宁可不写读音。"""

CHAT_SYSTEM = """你是一位耐心的日语老师，帮助用户精读一段日语文章。
回答用户关于文章的提问：词义、语法、语感、文化背景、人物与情节理解、翻译推敲等。
默认用简体中文回答；如果用户用其它语言提问，就用该语言回答。引用日语时可以给出读音。
回答简洁、具体，紧扣原文。

以下是用户正在阅读的文章全文：
<<<
{text}
>>>"""


async def llm_stream(messages, temperature=0.3):
    if not API_KEY:
        yield "⚠️ 未设置 OPENCODE_API_KEY。请在 reader/.env 中添加 OPENCODE_API_KEY=… 后重启服务器。"
        return
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
        "User-Agent": "jp-reader/0.1",
        "x-opencode-session": SESSION_ID,
    }
    body = {"model": MODEL, "messages": messages, "stream": True, "temperature": temperature}
    if REASONING_EFFORT and REASONING_EFFORT != "default":
        body["reasoning_effort"] = REASONING_EFFORT
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=15)) as client:
            async with client.stream("POST", f"{BASE_URL}/chat/completions", headers=headers, json=body) as r:
                if r.status_code != 200:
                    detail = (await r.aread()).decode("utf-8", "replace")[:800]
                    yield f"⚠️ API error {r.status_code}: {detail}"
                    return
                async for line in r.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    for ch in chunk.get("choices") or []:
                        piece = (ch.get("delta") or {}).get("content")
                        if piece:
                            yield piece
    except httpx.HTTPError as ex:
        yield f"\n⚠️ Network error: {ex!r}"


# ---------------------------------------------------------------- API

app = FastAPI()


class AnalyzeReq(BaseModel):
    text: str


class LookupReq(BaseModel):
    keys: list[str]


class TranslateReq(BaseModel):
    selection: str
    context: str = ""


class ReadingItem(BaseModel):
    id: str
    word: str
    sentence: str
    options: list[str]


class ReadingsReq(BaseModel):
    items: list[ReadingItem]


class ChatReq(BaseModel):
    text: str
    messages: list[dict]


@app.get("/")
def index():
    return FileResponse(HERE / "static" / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/jitendex.css")
def jitendex_css():
    p = DATA / "jitendex.css"
    return Response(p.read_text() if p.exists() else "", media_type="text/css")


TTS_DIR = DATA / "tts"
TTS_CACHE_FILES = 400


@app.get("/api/speak")
def api_speak(text: str):
    """Read Japanese aloud with a macOS voice (`say`). Returns a WAV file, cached by text."""
    text = text.strip()[:3000]
    if not text:
        raise HTTPException(400, "empty text")
    if not shutil.which("say"):
        raise HTTPException(501, "text-to-speech needs macOS (the `say` command)")
    TTS_DIR.mkdir(parents=True, exist_ok=True)
    path = TTS_DIR / (hashlib.sha1(f"{VOICE}\n{text}".encode()).hexdigest() + ".wav")
    if not path.exists():
        tmp = path.with_suffix(f".{uuid.uuid4().hex}.tmp.wav")
        try:
            # text goes in on stdin, so nothing in it can be read as a command-line option
            subprocess.run(["say", "-v", VOICE, "-f", "-", "-o", str(tmp),
                            "--file-format=WAVE", "--data-format=LEI16@22050"],
                           input=text.encode("utf-8"), check=True, timeout=120, capture_output=True)
            tmp.replace(path)
        except (subprocess.SubprocessError, OSError) as ex:
            tmp.unlink(missing_ok=True)
            raise HTTPException(500, f"say failed: {ex}")
        files = sorted(TTS_DIR.glob("*.wav"), key=lambda f: f.stat().st_mtime)
        for f in files[:-TTS_CACHE_FILES]:
            f.unlink(missing_ok=True)
    return FileResponse(path, media_type="audio/wav", headers={"Cache-Control": "max-age=86400"})


NAME_ORDER = ("surname", "female given name", "male given name", "given name", "person", "place")


@app.get("/api/names")
def api_names(word: str):
    """JMnedict readings of a word, person names first: [{reading, types, gloss}]."""
    merged: dict[str, dict] = {}
    for reading, types, gloss in db().execute(
            "SELECT reading, types, gloss FROM names WHERE expression = ? LIMIT 200", (word,)):
        m = merged.setdefault(reading, {"reading": reading, "types": [], "gloss": gloss})
        for t in (types or "").split(", "):
            if t and t not in m["types"]:
                m["types"].append(t)
    rank = lambda m: min((NAME_ORDER.index(t) if t in NAME_ORDER else 9) for t in m["types"] or ["?"])
    return {"word": word, "pinned": PINS.get(word), "names": sorted(merged.values(), key=rank)[:40]}


class PinReq(BaseModel):
    word: str
    reading: str | None = None


@app.post("/api/pins")
def api_pin(req: PinReq):
    """Pin (or with reading=None, unpin) the reading of a name for every occurrence."""
    word = req.word.strip()
    if not word or len(word) > 20:
        raise HTTPException(400, "bad word")
    if req.reading is None:
        PINS.pop(word, None)
    else:
        reading = to_hira(req.reading.strip())
        if not reading or not all("ぁ" <= c <= "ゖ" or c == "ー" for c in reading):
            raise HTTPException(400, "reading must be kana")
        PINS[word] = reading
    save_pins()
    return {"pins": PINS}


@app.get("/api/config")
def config():
    return {"model": MODEL, "has_key": bool(API_KEY)}


@app.post("/api/analyze")
def api_analyze(req: AnalyzeReq):
    text = unicodedata.normalize("NFC", req.text)
    return {"text": text, "paragraphs": analyze(text)}


@app.post("/api/analyze_stream")
def api_analyze_stream(req: AnalyzeReq):
    """Same result as /api/analyze, sent as NDJSON in growing chunks so the first screen renders at once.

    Line 1: {"text": ...}; then {"paragraphs": [...]} chunks (~1.5k characters first, ~20k after).
    """
    text = unicodedata.normalize("NFC", req.text)

    def chunks():
        yield json.dumps({"text": text}, ensure_ascii=False) + "\n"
        batch, size, limit, offset = [], 0, 1500, 0
        for line in text.split("\n"):
            batch.append(analyze_line(line, offset))
            offset += u16len(line) + 1
            size += len(line) + 1
            if size >= limit:
                yield json.dumps({"paragraphs": batch}, ensure_ascii=False, separators=(",", ":")) + "\n"
                batch, size, limit = [], 0, 20000
        if batch:
            yield json.dumps({"paragraphs": batch}, ensure_ascii=False, separators=(",", ":")) + "\n"

    return StreamingResponse(chunks(), media_type="application/x-ndjson")


@app.post("/api/lookup")
def api_lookup(req: LookupReq):
    seen, out = set(), []
    for k in req.keys[:6]:
        out += lookup_key(k, seen)
    return JSONResponse(out)


@app.post("/api/translate")
def api_translate(req: TranslateReq):
    user = f"上下文：\n{req.context[:4000]}\n\n需要翻译的部分：\n{req.selection}" if req.context else req.selection
    msgs = [{"role": "system", "content": TRANSLATE_SYSTEM}, {"role": "user", "content": user}]
    return StreamingResponse(llm_stream(msgs, 0.2), media_type="text/plain; charset=utf-8")


READING_SYSTEM = """あなたは日本語の読み方の専門家です。
各項目について、文中でのその語の読み方を「候補」から一つだけ選んでください。
意味と文脈（目的語、共起語、文体）で判断すること。例：「赤ん坊を抱く」はだく、「希望を抱く」はいだく、
「手紙を認める」はしたためる、「才能を認める」はみとめる、「今日の日本」はこんにち。
出力はJSONオブジェクトのみ（説明なし）：{"番号": "選んだ読み", ...}。候補にない読みは書かないこと。"""

_reading_cache: dict = {}


async def llm_complete(messages) -> str:
    out = ""
    async for piece in llm_stream(messages, 0.0):
        out += piece
    if out.startswith("⚠️"):
        raise RuntimeError(out)
    return out


@app.post("/api/readings")
async def api_readings(req: ReadingsReq):
    """AI reading check: pick each ambiguous word's reading in its sentence (抱く: いだく/だく)."""
    result, todo = {}, []
    for it in req.items[:80]:
        ck = (it.word, it.sentence, tuple(it.options))
        if ck in _reading_cache:
            result[it.id] = _reading_cache[ck]
        elif len(it.options) >= 2:
            todo.append(it)
    if todo:
        lines = [f"{n}. 語：{it.word}　文：{it.sentence[:200]}　候補：{' / '.join(it.options)}"
                 for n, it in enumerate(todo, 1)]
        msgs = [{"role": "system", "content": READING_SYSTEM}, {"role": "user", "content": "\n".join(lines)}]
        try:
            raw = await llm_complete(msgs)
        except RuntimeError as ex:
            return JSONResponse({"error": str(ex)}, status_code=502)
        m = re.search(r"\{.*\}", raw, re.S)
        try:
            picks = json.loads(m.group(0)) if m else {}
        except json.JSONDecodeError:
            picks = {}
        for n, it in enumerate(todo, 1):
            choice = str(picks.get(str(n), "")).strip()
            if choice in it.options:  # never accept a reading outside Jitendex's list
                _reading_cache[(it.word, it.sentence, tuple(it.options))] = choice
                result[it.id] = choice
    return {"readings": result}


@app.post("/api/chat")
def api_chat(req: ChatReq):
    msgs = [{"role": "system", "content": CHAT_SYSTEM.format(text=req.text[:60000])}]
    msgs += [{"role": m["role"], "content": str(m["content"])} for m in req.messages[-30:]
             if m.get("role") in ("user", "assistant")]
    return StreamingResponse(llm_stream(msgs, 0.5), media_type="text/plain; charset=utf-8")


if __name__ == "__main__":
    print(f"Read-aloud voice: {VOICE}")
    print(f"Model: {MODEL}  ({'API key set' if API_KEY else 'NO API KEY — set OPENCODE_API_KEY in reader/.env'})")
    print(f"Open http://127.0.0.1:{PORT}")
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
