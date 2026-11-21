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
    # ら抜き potential of 来る (B1); 来れば stays くれば
    ("来れる。", "来れる", "来(こ)れる"),
    ("来れない。", "来れない", "来(こ)れない"),
    ("来れた。", "来れた", "来(こ)れた"),
    ("もう来れる。", "来れる", "来(こ)れる"),
    ("来れば分かる。", "来れば", "来(く)れば"),
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


@pytest.mark.parametrize("text,word,key,chain", [
    ("接見された", "接見された", "接見", "-suru < passive or respectful < past"),
    ("食べられる", "食べられる", "食べる", "passive, potential or respectful"),
    ("書かれていた", "書かれていた", "書く", "passive or respectful < -te iru < past"),
    ("食べさせたくない", "食べさせたくない", "食べる", "causative < want to < negative"),
    ("行きませんでした", "行きませんでした", "行く", "polite < negative < past"),
    ("呑んだ", "呑んだ", "呑む", "past"),
    ("来い", "来い", "来る", "imperative"),
    ("希望が抱ける", "抱ける", "抱く", "potential"),
    ("的を射た", "的を射た", "的を射る", "past"),
    ("もう来れる。", "来れる", "来る", "potential (ra-less)"),
    ("食べちゃった", "食べちゃった", "食べる", "-te shimau (-chau) < past"),
    ("食べなきゃ", "食べなきゃ", "食べる", "must (-nakya)"),
    ("行くべき", "行くべき", "行く", "should (beki)"),
])
def test_conjugation_chain(text, word, key, chain):
    seg = next(s for p in server.analyze(text) for s in p if s.get("w") and s["t"] == word)
    assert seg["c"][key] == chain


def test_no_chain_for_dictionary_forms():
    segs = [s for p in server.analyze("食べる家庭環境") for s in p if s.get("w")]
    assert all("c" not in s for s in segs)


@pytest.mark.parametrize("text,word,expected", [
    ("今日は暑い。", "今日", "今日(きょう)"),        # not the greeting こんにちは
    ("今日は、いい天気。", "今日", "今日(きょう)"),
    ("「今日は」と言った。", "今日は", "今日(こんにち)は"),  # a real greeting stays one word
    ("今晩は暇だ。", "今晩", "今晩(こんばん)"),        # B2: one 感動詞 token in the tokenizer
    ("今晩は、暇だ。", "今晩", "今晩(こんばん)"),
    ("「今晩は」", "今晩は", "今晩(こんばん)は"),
])
def test_greeting_only_before_sentence_end(text, word, expected):
    assert words(text)[word] == expected


@pytest.mark.parametrize("text,word,options", [
    ("赤ん坊を抱く。", "抱く", {"いだく", "だく"}),
    ("母親が子供を抱いて眠った。", "抱いて", {"いだいて", "だいて"}),
    ("父に手紙を認める。", "認める", {"みとめる", "したためる"}),
    ("その日、市場へ行った。", "市場", {"しじょう", "いちば"}),
])
def test_ambiguous_words_offer_alternatives(text, word, options):
    seg = next(s for p in server.analyze(text) for s in p if s.get("w") and s["t"] == word)
    assert options <= {a[0] for a in seg["a"]}


@pytest.mark.parametrize("text,word", [("来ます。", "来ます"), ("行った。", "行った"), ("家庭環境", "家庭環境")])
def test_no_alternatives_for_irregular_or_unambiguous(text, word):
    seg = next(s for p in server.analyze(text) for s in p if s.get("w") and s["t"] == word)
    assert "a" not in seg


@pytest.mark.parametrize("key,kept,dropped", [
    ("朝", {"あさ"}, {"あした"}),                      # poetic
    ("学生", {"がくせい"}, {"がくしょう", "がくそう"}),  # archaic
    ("彼", {"かれ"}, {"かの"}),                        # literary
    ("今日", {"きょう", "こんにち"}, {"こんち", "こんじつ"}),  # minor readings of the same entry
    ("日", {"ひ"}, {"か", "んち"}),                      # counter/suffix only
    ("認める", {"みとめる", "したためる"}, set()),         # a different word stays
    ("外", {"そと", "ほか", "よそ"}, set()),
])
def test_ai_check_offers_only_readings_in_use(key, kept, dropped):
    got = set(server.usable_readings(key))
    assert kept <= got and not (dropped & got)


def test_reading_prompt_sees_the_word_in_long_sentences():
    s = "あ" * 250 + "希望を抱く。" + "い" * 250
    w = server.context_window(s, "抱く")
    assert "希望を抱く" in w and len(w) < 300
    assert server.context_window("希望を抱く。", "抱く") == "希望を抱く。"


def test_long_single_line_streams_in_pieces():
    import json
    from fastapi.testclient import TestClient
    c = TestClient(server.app)
    text = "早くも檜葉が尊属殺人の規定を念頭に置いてはいないかと、警戒心を張り巡らせる。" * 1500 + "\n終わり。"
    full = c.post("/api/analyze", json={"text": text}).json()
    msgs = [json.loads(x) for x in c.post("/api/analyze_stream", json={"text": text}).text.splitlines()][1:]
    assert len(msgs) > 2
    paras = []
    for m in msgs:
        ps = [list(p) for p in m["paragraphs"]]
        if m.get("join"):
            paras[-1] += ps.pop(0)
        paras += ps
    assert paras == full["paragraphs"] and len(paras) == 2
    js = full["text"].encode("utf-16-le")
    assert all(js[s["s"] * 2:s["e"] * 2].decode("utf-16-le") == s["t"] for p in paras for s in p)


def test_names_without_jmnedict(monkeypatch):
    import sqlite3
    from fastapi.testclient import TestClient
    con = sqlite3.connect(":memory:", check_same_thread=False)
    monkeypatch.setattr(server, "db", lambda: con)
    r = TestClient(server.app).get("/api/names", params={"word": "平城"})
    assert r.status_code == 200 and r.json()["names"] == []


def test_katakana_name_readings_match_pins(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(server, "PINS_PATH", tmp_path / "pins.json")
    monkeypatch.setattr(server, "PINS", {})
    c = TestClient(server.app)
    rows = c.get("/api/names", params={"word": "３Ｍ"}).json()["names"]
    assert rows and all(not any("ァ" <= ch <= "ヶ" for ch in n["reading"]) for n in rows)
    c.post("/api/pins", json={"word": "３Ｍ", "reading": rows[0]["reading"]})
    assert c.get("/api/names", params={"word": "３Ｍ"}).json()["pinned"] == rows[0]["reading"]
    seg = next(s for p in server.analyze("３Ｍの製品。") for s in p if s.get("w") and s["t"] == "３Ｍ")
    assert seg["r"] == [["３Ｍ", rows[0]["reading"]]]  # shown and spoken with the pinned reading
    assert c.post("/api/pins", json={"word": "３Ｍ", "reading": "ー"}).status_code == 400


def test_speak_needs_the_page_header():
    from fastapi.testclient import TestClient
    assert TestClient(server.app).get("/api/speak", params={"text": "こんにちは"}).status_code == 403


def test_analyze_stream_matches_analyze():
    import json
    from fastapi.testclient import TestClient
    c = TestClient(server.app)
    text = ("「一にも二にも家庭環境です。」\n\n𠮷野家で食べた。\n" + "早くも檜葉が尊属殺人の規定を念頭に置いてはいないかと、警戒心を張り巡らせる。\n" * 120)
    full = c.post("/api/analyze", json={"text": text}).json()
    lines = [json.loads(x) for x in c.post("/api/analyze_stream", json={"text": text}).text.splitlines()]
    assert lines[0]["text"] == full["text"]
    assert len(lines) > 2  # actually chunked
    assert not any(x.get("join") for x in lines[1:])
    assert [p for x in lines[1:] for p in x["paragraphs"]] == full["paragraphs"]


def test_names_lists_person_readings_first():
    from fastapi.testclient import TestClient
    names = TestClient(server.app).get("/api/names", params={"word": "平城"}).json()["names"]
    readings = [n["reading"] for n in names]
    assert "ひらじろ" in readings and "へいじょう" in readings
    assert "surname" in names[0]["types"] or "place" in names[0]["types"]


def test_pins_apply_everywhere_but_not_to_counters(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(server, "PINS_PATH", tmp_path / "pins.json")
    monkeypatch.setattr(server, "PINS", {})
    c = TestClient(server.app)
    assert c.post("/api/pins", json={"word": "円", "reading": "マドカ"}).json()["pins"] == {"円": "まどか"}
    en = ["".join(r for b, r in sg["r"]) for p in server.analyze("平城よりも円だった。三百二十円で買った。円は笑った。")
          for sg in p if sg.get("w") and sg["t"] == "円"]
    assert en == ["まどか", "えん", "まどか"]  # the yen counter after 三百二十 is untouched
    seg = next(s for p in server.analyze("円は笑った。") for s in p if s.get("w") and s["t"] == "円")
    assert seg["pin"] == 1 and "a" not in seg  # pinned words skip the AI check
    assert c.post("/api/pins", json={"word": "円", "reading": None}).json()["pins"] == {}
    assert c.post("/api/pins", json={"word": "円", "reading": "madoka"}).status_code == 400
    assert (tmp_path / "pins.json").exists()


@pytest.mark.parametrize("text,word,first_key", [
    ("日がな一日吞んだくれているダメな父親。", "吞んだくれている", "飲んだくれる"),  # 吞 U+541E
    ("日がな一日呑んだくれているダメな父親。", "呑んだくれている", "飲んだくれる"),  # 呑 U+5451
    ("毎晩呑んだくれた。", "呑んだくれた", "飲んだくれる"),
    ("ただの呑んだくれだ。", "呑んだくれだ", "飲んだくれ"),
])
def test_other_kanji_for_the_same_word(text, word, first_key):
    seg = next(s for p in server.analyze(text) for s in p if s.get("w") and s["t"] == word)
    assert seg["k"][0] == first_key
    assert seg["r"][0] == [word[0], "の"]


def test_sound_alike_single_words_stay_apart():
    assert words("友達に会って、事故に遭った。")["会って"] == "会(あ)って"
