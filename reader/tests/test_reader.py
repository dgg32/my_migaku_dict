"""Regression tests for the reader's analysis and lookup.  Run: .venv/bin/python -m pytest -q"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server  # noqa: E402  (loads the dictionary once)


def words(text):
    """{surface: furigana-annotated surface} for the clickable words in text."""
    out = {}
    for para in server.analyze(text):
        for s in para:
            if s.get("w"):
                out[s["t"]] = "".join(f"{b}({r})" if r else b for b, r in s["r"])
    return out


@pytest.mark.parametrize("text,word,expected", [
    # irregular 来る stems (A1)
    ("来ます。", "来ます", "来(き)ます"),
    ("来ない。", "来ない", "来(こ)ない"),
    ("来い。", "来い", "来(こ)い"),
    ("来て。", "来て", "来(き)て"),
    ("来ている。", "来ている", "来(き)ている"),
    ("来られる。", "来られる", "来(こ)られる"),
    ("来させる。", "来させる", "来(こ)させる"),
    ("来すぎる。", "来すぎる", "来(き)すぎる"),
    ("来なさい。", "来", "来(き)"),
    # cases that already worked
    ("食べた。", "食べた", "食(た)べた"),
    ("行った。", "行った", "行(い)った"),
    ("希望が抱ける。", "抱ける", "抱(いだ)ける"),
    ("食べさせられた。", "食べさせられた", "食(た)べさせられた"),
    ("刑罰として重すぎる。", "重すぎる", "重(おも)すぎる"),
    ("一にも二にも家庭環境です。", "一にも二にも", "一(いち)にも二(に)にも"),
    ("公判での質疑も的を射たものになる。", "的を射た", "的(まと)を射(い)た"),
    ("日がな一日呑んだ。", "日がな一日", "日(ひ)がな一日(いちにち)"),
    ("行けなかった。", "行けなかった", "行(い)けなかった"),
])
def test_readings(text, word, expected):
    assert words(text)[word] == expected


@pytest.mark.parametrize("key,expression,reading", [
    ("數", "数", "しばしば"),   # A2: resolved by the redirect row's own sequence
    ("機關", "機関", "からくり"),
    ("鐵漿", "鉄漿", "おはぐろ"),
])
def test_redirects_follow_sequence(key, expression, reading):
    entries = server.lookup_key(key, set())
    assert all(e["redirected"] == key and e["expression"] == expression for e in entries)
    assert any(reading in e["readings"] for e in entries)


def test_redirect_rows_stay_separate():
    seqs = [e["sequence"] for e in server.lookup_key("數", set())]
    assert len(seqs) == len(set(seqs)) >= 3


def test_offsets_are_utf16():
    # A3: JS slices strings in UTF-16 units; astral characters count twice
    text = "𠮷野家🍣で食べた。\n次の行で来た。"
    js = text.encode("utf-16-le")
    for para in server.analyze(text):
        for s in para:
            assert js[s["s"] * 2:s["e"] * 2].decode("utf-16-le") == s["t"]


def test_popup_ruby_skips_okurigana():
    e = server.lookup_key("食べる", set())[0]
    assert e["ruby"] == [["食", "た"], ["べる", ""]]


def test_api_smoke():
    from fastapi.testclient import TestClient
    c = TestClient(server.app)
    assert c.get("/api/config").status_code == 200
    r = c.post("/api/analyze", json={"text": "一にも二にも"})
    assert r.json()["paragraphs"][0][0]["k"][0] == "一にも二にも"
    assert c.post("/api/lookup", json={"keys": ["一にも二にも"]}).json()[0]["readings"] == ["いちにもににも"]
    assert c.post("/api/analyze", json={}).status_code == 422
