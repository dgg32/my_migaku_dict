"""Local Japanese reader: furigana + longest-match Jitendex lookup + DeepSeek translation/chat.

Run:  .venv/bin/python server.py      then open http://127.0.0.1:8787
"""
import json
import os
import re
import sqlite3
import threading
import unicodedata
import urllib.parse
import uuid
from pathlib import Path

import fugashi
import httpx
import uvicorn
from fastapi import FastAPI
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
    __slots__ = ("surface", "pos1", "pos2", "ctype", "base", "kana", "kana_base", "s", "e")


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
    return out


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


def word_reading(text, toks, i, j, key, inflected):
    """Reading for the matched surface text[toks[i].s:toks[j-1].e]."""
    guess_surface = "".join(t.kana for t in toks[i:j])
    if not inflected:
        return pick_reading(key, guess_surface)
    guess_base = "".join(t.kana for t in toks[i:j - 1]) + toks[j - 1].kana_base
    if not key.endswith(toks[j - 1].base) and len(guess_base) >= 2 and guess_base[-2] in POTENTIAL:
        guess_base = guess_base[:-2] + POTENTIAL[guess_base[-2]]  # いだける -> いだく
    dict_reading = pick_reading(key, guess_base)
    # The tokenizer's dictionary-form reading agrees with Jitendex, so its contextual reading of the
    # surface is trustworthy and handles irregular stems (来ます -> きます, 来ない -> こない).
    if guess_base == dict_reading and guess_surface and not has_kanji(guess_surface):
        return guess_surface
    # Strip the shared kana ending (okurigana) from headword and reading, keep the stem reading.
    k, r = key, dict_reading
    while k and r and is_kana(k[-1]) and to_hira(k[-1]) == r[-1]:
        k, r = k[:-1], r[:-1]
    surf = text[toks[i].s:toks[j - 1].e]
    if k and surf.startswith(k) and r:
        return r + to_hira(surf[len(k):])
    return guess_surface


def analyze(text: str):
    paragraphs = []
    offset = 0
    for line in text.split("\n"):
        paragraphs.append(analyze_line(line, offset))
        offset += u16len(line) + 1
    return paragraphs


def u16len(s: str) -> int:
    return len(s) + sum(1 for c in s if ord(c) > 0xFFFF)


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
            reading = word_reading(line, toks, i, j, key, inflected)
            keys = []
            for _, k, _ in cands:
                if k not in keys:
                    keys.append(k)
        else:
            j, key, reading = i + 1, None, t.kana
            keys = [t.base] if t.base != t.surface else []
        # extend the clickable word over auxiliaries (駆り立て+た, 射+た, し+て+いる)
        end = j
        if toks[j - 1].pos1 in INFLECTING or (cands and cands[0][2]):
            while end < len(toks) and toks[end].pos1 not in SKIP_POS and absorbs(toks[end - 1], toks[end]):
                reading += toks[end].kana or to_hira(toks[end].surface)
                end += 1
        s, e = toks[i].s, toks[end - 1].e
        surface = line[s:e]
        if key is None and not has_kanji(surface) and t.pos1 in ("助詞", "助動詞"):
            plain(s, e)  # unknown particle: no popup
        else:
            segs.append({
                "s": base + u16[s], "e": base + u16[e], "t": surface,
                "r": ruby_parts(surface, reading),
                "k": keys, "w": 1, "m": 1 if key else 0,
                "p": 1 if key and j - i > 1 and len(key) > 2 and not all_kana(key) else 0,
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


@app.get("/api/config")
def config():
    return {"model": MODEL, "has_key": bool(API_KEY)}


@app.post("/api/analyze")
def api_analyze(req: AnalyzeReq):
    text = unicodedata.normalize("NFC", req.text)
    return {"text": text, "paragraphs": analyze(text)}


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


@app.post("/api/chat")
def api_chat(req: ChatReq):
    msgs = [{"role": "system", "content": CHAT_SYSTEM.format(text=req.text[:60000])}]
    msgs += [{"role": m["role"], "content": str(m["content"])} for m in req.messages[-30:]
             if m.get("role") in ("user", "assistant")]
    return StreamingResponse(llm_stream(msgs, 0.5), media_type="text/plain; charset=utf-8")


if __name__ == "__main__":
    print(f"Model: {MODEL}  ({'API key set' if API_KEY else 'NO API KEY — set OPENCODE_API_KEY in reader/.env'})")
    print(f"Open http://127.0.0.1:{PORT}")
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
