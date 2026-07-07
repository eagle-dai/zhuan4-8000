# /// script
# requires-python = ">=3.10"
# dependencies = ["genanki>=0.13", "edge-tts>=6.1"]
# ///
"""把 ocr/*.json 构建成 Anki .apkg。

流程：读所有页 → 跨页合并被切词条 → 每义项摊平成一张 note（英→中）
     → edge-tts 生成单词/例句读音 mp3 → 打包进 .apkg。

读音用微软 edge-tts（免费、神经网络语音、美音），构建时预生成 mp3 存入卡片，
用 [sound:xxx.mp3] 播放（离线音质好）。mp3 只进 apkg，不入库。

用法：
    uv run scripts/build_anki.py            # 生成 anki/zhuan4.apkg（含音频）
    uv run scripts/build_anki.py --check    # 只校验+统计，不生成音频/apkg
    uv run scripts/build_anki.py --no-audio # 生成 apkg 但跳过音频（快速测试）
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import tempfile
from pathlib import Path

import genanki

ROOT = Path(__file__).resolve().parent.parent
OCR_DIR = ROOT / "ocr"
OUT = ROOT / "anki" / "zhuan4.apkg"

VOICE = "en-US-AriaNeural"  # 美音女声
TTS_CONCURRENCY = 8         # 并发调 edge-tts 的上限

# 稳定 ID：固定值，保证重复导入更新同一 deck/model 而非新建。
MODEL_ID = 1607392311
DECK_ID = 2059400110

MODEL = genanki.Model(
    MODEL_ID,
    "TEM4 英→中（edge-tts）",
    fields=[
        {"name": "Word"},
        {"name": "Phonetic"},
        {"name": "POS"},
        {"name": "DefZh"},
        {"name": "Example"},
        {"name": "ExampleZh"},
        {"name": "WordAudio"},      # [sound:xxx.mp3]
        {"name": "ExampleAudio"},   # [sound:xxx.mp3]
        {"name": "Key"},            # 唯一键，用于去重/更新
    ],
    templates=[
        {
            "name": "英→中",
            "qfmt": """
<div class="word">{{Word}}</div>
<div class="phon">{{Phonetic}}</div>
{{WordAudio}}
""",
            "afmt": """
{{FrontSide}}
<hr id="answer">
<div class="def"><span class="pos">{{POS}}</span>{{DefZh}}</div>
{{#Example}}
<div class="ex"><i>{{Example}}</i></div>
<div class="ex-zh">{{ExampleZh}}</div>
{{ExampleAudio}}
{{/Example}}
""",
        }
    ],
    css="""
.card { font-family: -apple-system, "Segoe UI", "Noto Sans CJK SC", sans-serif;
        text-align: center; color: #1c1c1e; background: #fff; }
.word { font-size: 40px; font-weight: 700; }
.phon { font-size: 20px; color: #8e8e93; font-style: italic; margin-top: 6px; }
.pos  { color: #ff9500; font-weight: 700; margin-right: 6px; }
.def  { font-size: 26px; font-weight: 600; margin: 14px 0; }
.ex   { font-size: 19px; line-height: 1.5; }
.ex-zh{ font-size: 16px; color: #8e8e93; margin-top: 8px; }
""",
)


def load_pages() -> list[dict]:
    """读所有 ocr/NNN.json，按页码升序。"""
    pages = []
    for f in sorted(OCR_DIR.glob("*.json")):
        with f.open(encoding="utf-8") as fh:
            data = json.load(fh)
        data["_file"] = f.name
        pages.append(data)
    pages.sort(key=lambda p: p["page"])
    return pages


def merge_cross_page(pages: list[dict]) -> tuple[list[dict], list[str]]:
    """合并被页边界切断的词条。

    head 词条（complete=False，末尾在某页）与下一页 continues_from 指向它的
    tail 词条按词头拼接：把 tail 的 senses 接到 head 后，缺字段以 tail 补全。
    返回 (完整词条列表, 警告列表)。
    """
    warnings: list[str] = []
    entries: list[dict] = []
    for p in pages:
        for e in p.get("entries", []):
            entries.append({**e, "_page": p["page"]})

    tails: dict[str, dict] = {}
    for e in entries:
        if e.get("partial") == "tail":
            if e["word"] in tails:
                warnings.append(
                    f"词头 '{e['word']}' 出现多个 tail（页 {tails[e['word']]['_page']} 和 "
                    f"{e['_page']}），按 word 配对会错乱；需人工确认接续关系。"
                )
            tails[e["word"]] = e
    used_tails: set[str] = set()
    merged: list[dict] = []

    for e in entries:
        role = e.get("partial")
        if role == "tail":
            continue  # tail 只在配对时消费
        if role == "head" or e.get("complete") is False:
            tail = tails.get(e["word"])
            if tail is None:
                warnings.append(
                    f"词条 '{e['word']}' (页 {e['_page']}) 被切但找不到续接 tail，"
                    f"可能下一页尚未 OCR；本词条暂跳过。"
                )
                continue
            used_tails.add(e["word"])
            merged.append(_stitch(e, tail))
        else:
            merged.append(e)

    for w, t in tails.items():
        if w not in used_tails:
            warnings.append(f"续接 tail '{w}' (页 {t['_page']}) 找不到对应 head。")

    return merged, warnings


def _stitch(head: dict, tail: dict) -> dict:
    """把 tail 的续接义项拼到 head。同一个被切义项（head 末 + tail 首）以 tail 补全。"""
    h_senses = list(head.get("senses", []))
    t_senses = list(tail.get("senses", []))
    if h_senses and t_senses:
        last = h_senses[-1]
        first = t_senses[0]
        if _is_incomplete(last) and last.get("pos") == first.get("pos"):
            h_senses[-1] = {**last, **{k: v for k, v in first.items() if v is not None}}
            t_senses = t_senses[1:]
    stitched = {**head, "senses": h_senses + t_senses}
    stitched.pop("partial", None)
    stitched["complete"] = True
    if not stitched.get("derivatives") and tail.get("derivatives"):
        stitched["derivatives"] = tail["derivatives"]
    return stitched


def _is_incomplete(sense: dict) -> bool:
    """义项是否被页边界切残。判据：缺中文释义（核心字段）。

    仅缺例句不算残缺——无例句义项合法常见（如 exhaust 的 n. 义项）。
    只有中文释义都没有，才说明这个义项在 head 末尾被切断、须由 tail 首义项补全。
    """
    return not sense.get("def_zh")


def _audio_name(text: str) -> str:
    """按文本内容生成稳定 mp3 文件名（内容相同则复用，去重）。"""
    h = hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]
    return f"tem4-{h}.mp3"


def flatten_notes(entries: list[dict]) -> tuple[list[dict], list[str]]:
    """每义项 → 一条 note 数据（dict，含待生成音频的原文）。返回 (rows, 警告)。"""
    rows = []
    warnings: list[str] = []
    for e in entries:
        word = e["word"]
        phon = e.get("phonetic", "") or ""
        senses = e.get("senses", [])
        if not senses:
            warnings.append(f"词条 '{word}' 没有 senses，不出卡（检查 OCR/合并）。")
            continue
        for s in senses:
            pos = s.get("pos", "") or ""
            def_zh = s.get("def_zh", "") or ""
            ex = s.get("example", "") or ""
            # 稳定 key：按义项内容（词+词性+中文释义）生成，而非序号。
            # 义项顺序或数量变化（如后补 tail）不会移动已有义项的 key，重导入才能正确更新。
            key = f"{word}|{pos}|{def_zh}"
            rows.append({
                "word": word,
                "phon": phon,
                "pos": pos,
                "def_zh": def_zh,
                "example": ex,
                "example_zh": s.get("example_zh", "") or "",
                "key": key,
            })
    # key 冲突检测（同词同词性同释义重复）
    seen: dict[str, int] = {}
    for r in rows:
        seen[r["key"]] = seen.get(r["key"], 0) + 1
    for k, cnt in seen.items():
        if cnt > 1:
            warnings.append(f"key 冲突 '{k}' 出现 {cnt} 次，重复卡会互相覆盖。")
    return rows, warnings


async def _gen_one(text: str, out_path: Path, sem: asyncio.Semaphore) -> None:
    import edge_tts
    async with sem:
        await edge_tts.Communicate(text, VOICE).save(str(out_path))


async def generate_audio(rows: list[dict], media_dir: Path) -> int:
    """为所有单词和例句生成 mp3，写进 media_dir，并在 rows 里填 [sound:] 标签。

    先按文本去重成唯一集合，每个唯一文本只建一个 task——既避免重复调 edge-tts，
    也避免多协程并发写同一文件的竞态。返回实际生成的文件数。
    """
    # 收集唯一 (文本 → 文件名)，同时给每行填 [sound:] 标签
    uniq: dict[str, str] = {}
    for r in rows:
        wname = _audio_name(r["word"])
        uniq[r["word"]] = wname
        r["word_audio"] = f"[sound:{wname}]"
        if r["example"]:
            ename = _audio_name(r["example"])
            uniq[r["example"]] = ename
            r["example_audio"] = f"[sound:{ename}]"
        else:
            r["example_audio"] = ""

    sem = asyncio.Semaphore(TTS_CONCURRENCY)
    tasks = [_gen_one(text, media_dir / name, sem) for text, name in uniq.items()]
    await asyncio.gather(*tasks)
    return len(uniq)


def build_notes(rows: list[dict]) -> list[genanki.Note]:
    notes = []
    for r in rows:
        notes.append(genanki.Note(
            model=MODEL,
            fields=[
                r["word"], r["phon"], r["pos"], r["def_zh"],
                r["example"], r["example_zh"],
                r.get("word_audio", ""), r.get("example_audio", ""),
                r["key"],
            ],
            guid=genanki.guid_for(r["key"]),  # 稳定 guid → 重导入更新而非重复
        ))
    return notes


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只校验统计，不生成音频/apkg")
    ap.add_argument("--no-audio", action="store_true", help="生成 apkg 但跳过音频")
    args = ap.parse_args()

    if not OCR_DIR.exists():
        print(f"错误：{OCR_DIR} 不存在", file=sys.stderr)
        return 1

    pages = load_pages()
    if not pages:
        print(f"错误：{OCR_DIR} 里没有 *.json", file=sys.stderr)
        return 1

    entries, warnings = merge_cross_page(pages)
    rows, flat_warnings = flatten_notes(entries)
    warnings += flat_warnings

    # 音频去重后的实际文件数：唯一单词 + 唯一例句
    uniq_audio = {r["word"] for r in rows} | {r["example"] for r in rows if r["example"]}
    n_audio = 0 if args.no_audio else len(uniq_audio)
    print(f"页数 {len(pages)}｜完整词条 {len(entries)}｜卡片 {len(rows)}｜音频 {n_audio} 个")
    for w in warnings:
        print(f"⚠ {w}", file=sys.stderr)

    if args.check:
        return 0

    OUT.parent.mkdir(parents=True, exist_ok=True)
    deck = genanki.Deck(DECK_ID, "TEM-4 词汇")

    with tempfile.TemporaryDirectory() as td:
        media_dir = Path(td)
        media_files: list[str] = []
        if not args.no_audio:
            print(f"生成音频（edge-tts, {VOICE}）…")
            asyncio.run(generate_audio(rows, media_dir))
            media_files = [str(p) for p in sorted(media_dir.glob("*.mp3"))]
            print(f"音频文件 {len(media_files)} 个")
        else:
            for r in rows:  # 无音频时清空标签，避免卡面出现坏引用
                r["word_audio"] = ""
                r["example_audio"] = ""

        for n in build_notes(rows):
            deck.add_note(n)

        pkg = genanki.Package(deck)
        pkg.media_files = media_files
        pkg.write_to_file(str(OUT))

    print(f"已写出 {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
