# /// script
# requires-python = ">=3.10"
# dependencies = ["genanki>=0.13", "requests>=2.31"]
# ///
"""把 ocr/*.json 构建成 Anki .apkg。

流程：读所有页 → 跨页合并被切词条 → 每词一张 note（英→中，背面列全部义项）
     → 有道 dictvoice 拉单词读音 mp3 → 打包进 .apkg。

单词读音用有道词典 dictvoice（免费、无需 key、美音），构建时预下载 mp3 存入卡片，
用 [sound:xxx.mp3] 播放（离线）。mp3 只进 apkg，不入库。
注意：有道 dictvoice 只可靠返回「单个词」的音频，多词短语/整句返回 null——
因此只给正面单词配音；例句暂不配音（ExampleAudio 恒空）。

用法：
    uv run scripts/build_anki.py            # 生成 anki/zhuan4.apkg（含单词音频）
    uv run scripts/build_anki.py --check    # 只校验+统计，不下载音频/apkg
    uv run scripts/build_anki.py --no-audio # 生成 apkg 但跳过音频（快速测试）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
import time
from pathlib import Path

import genanki
import requests

ROOT = Path(__file__).resolve().parent.parent
OCR_DIR = ROOT / "ocr"
OUT = ROOT / "anki" / "zhuan4.apkg"

# 有道词典 TTS：type=0 美音，type=1 英音。GET 直接返回 mp3。
YOUDAO_URL = "https://dict.youdao.com/dictvoice"
YOUDAO_TYPE = 0             # 0=美音 1=英音
TTS_DELAY = 0.3            # 每次请求间隔秒数：外部公共服务，串行+限频，别猛打
TTS_TIMEOUT = 15           # 单次请求超时秒数

# 稳定 ID：固定值，保证重复导入更新同一 deck/model 而非新建。
# MODEL_ID 换过一次（旧 1607392311 → 现值）：字段布局从「每义项 8 字段」
# 改为「每词 6 字段」，是全新 notetype。沿用旧 ID 会让 Anki 报 schema 冲突、
# 且旧 guid（word|pos|def_zh）与新 guid（word）无重叠导致旧卡残留，故换新 ID。
MODEL_ID = 1607392312
DECK_ID = 2059400110

MODEL = genanki.Model(
    MODEL_ID,
    "TEM4 英→中（有道 TTS）",
    fields=[
        {"name": "Word"},
        {"name": "Phonetic"},
        {"name": "Senses"},         # 预渲染 HTML：该词所有义项（pos+释义+例句）
        {"name": "WordAudio"},      # [sound:xxx.mp3]
        {"name": "ExampleAudio"},   # 保留字段但恒空：有道拉不到整句音频，例句暂不配音
        {"name": "Key"},            # 唯一键（= word），用于去重/更新
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
<div class="senses">{{Senses}}</div>
""",
        }
    ],
    css="""
/* 背单词卡：克制、干净，视觉为记忆服务。焦点=单词与中文释义，其余弱化。
   词头区居中（正面焦点），义项区左对齐（多条并列信息，左对齐利于纵向扫读）。 */
.card {
  font-family: -apple-system, "Segoe UI", "Noto Sans CJK SC", "PingFang SC", sans-serif;
  color: #1c1c1e; background: #f2f2f7;
  -webkit-font-smoothing: antialiased; line-height: 1.5;
}
/* 卡片本体：白底圆角，居中一栏，两侧留白 */
.card > * { max-width: 460px; margin-left: auto; margin-right: auto; }

.word { font-size: 42px; font-weight: 700; letter-spacing: -0.01em; text-align: center;
        margin-top: 8px; }
.phon { font-size: 19px; color: #8e8e93; margin-top: 4px; text-align: center; }

hr#answer { border: none; border-top: 1px solid #e5e5ea; margin: 22px auto; max-width: 460px; }

/* 义项区：左对齐，义项间细线分隔 */
.senses { text-align: left; }
.sense  { padding: 14px 0; border-top: 1px solid #f0f0f3; }
.sense:first-child { border-top: none; padding-top: 4px; }

.pos  { color: #ff9500; font-weight: 700; margin-right: 8px;
        font-size: 15px; letter-spacing: 0.02em; }
.def  { font-size: 22px; font-weight: 600; margin: 0 0 8px; }   /* 认词要记的：加粗 */
.ex   { font-size: 17px; color: #48484a; line-height: 1.55; }   /* 例句：正常，略弱 */
.ex i { font-style: italic; }
.ex-zh{ font-size: 14px; color: #8e8e93; margin-top: 3px; }

@media (prefers-color-scheme: dark) {
  .card { color: #f2f2f7; background: #1c1c1e; }
  .phon, .ex-zh { color: #8e8e93; }
  .ex { color: #c7c7cc; }
  hr#answer { border-top-color: #38383a; }
  .sense { border-top-color: #2c2c2e; }
}
""",
)

# 同一词性下多义项的圆圈序号（纯展示，不进 Key）。
CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫"


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


def _esc(s: str) -> str:
    """HTML 转义，避免释义/例句里的 < & 等破坏卡面。"""
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _render_senses(senses: list[dict]) -> str:
    """把该词所有义项渲染成一段背面 HTML。

    同一词性内多义项加圆圈序号（①②③，纯展示）；单义项不加。有例句才渲染例句块。
    """
    # 先按词性分组统计，决定是否加序号
    pos_count: dict[str, int] = {}
    for s in senses:
        pos = s.get("pos", "") or ""
        pos_count[pos] = pos_count.get(pos, 0) + 1

    blocks: list[str] = []
    pos_idx: dict[str, int] = {}
    for s in senses:
        pos = s.get("pos", "") or ""
        def_zh = s.get("def_zh", "") or ""
        ex = s.get("example", "") or ""
        ex_zh = s.get("example_zh", "") or ""
        i = pos_idx.get(pos, 0)
        pos_idx[pos] = i + 1
        num = CIRCLED[i] if pos_count[pos] > 1 and i < len(CIRCLED) else ""
        parts = [f'<div class="def"><span class="pos">{_esc(pos)}</span>{num}{_esc(def_zh)}</div>']
        if ex:
            parts.append(f'<div class="ex"><i>{_esc(ex)}</i></div>')
        if ex_zh:
            parts.append(f'<div class="ex-zh">{_esc(ex_zh)}</div>')
        blocks.append(f'<div class="sense">{"".join(parts)}</div>')
    return "\n".join(blocks)


def flatten_notes(entries: list[dict]) -> tuple[list[dict], list[str]]:
    """每词 → 一条 note 数据（背面含全部义项）。返回 (rows, 警告)。"""
    rows = []
    warnings: list[str] = []
    for e in entries:
        word = e["word"]
        phon = e.get("phonetic", "") or ""
        senses = e.get("senses", [])
        if not senses:
            warnings.append(f"词条 '{word}' 没有 senses，不出卡（检查 OCR/合并）。")
            continue
        rows.append({
            "word": word,
            "phon": phon,
            "senses_html": _render_senses(senses),
            "examples": [s["example"] for s in senses if s.get("example")],
            "key": word,  # 一词一卡，词本身即唯一键
        })
    # 重复检测：同一个 word 出现多个词条（跨页 head/tail 未合并、或 OCR 重复解析同词）
    seen: dict[str, int] = {}
    for r in rows:
        seen[r["key"]] = seen.get(r["key"], 0) + 1
    for k, cnt in seen.items():
        if cnt > 1:
            warnings.append(f"同词多条目 '{k}' 出现 {cnt} 次，卡片会互相覆盖（查跨页合并/OCR 重复）。")
    return rows, warnings


def _download_word(word: str, out_path: Path, session: requests.Session) -> None:
    """从有道 dictvoice 下载单个词的 mp3，写到 out_path。

    有道返回 500 + JSON（`returned null audio`）表示拿不到音频（多词/异常输入）。
    此处只下单个词，正常都能拿到；拿不到就抛错，让上层告警而非静默写坏文件。

    词头可能含拼写变体（如 `civilize/-ise`、`specialty/speciality`），斜杠会让有道
    500——只取斜杠前的主拼写发音即可。
    """
    query = word.split("/")[0].strip()
    resp = session.get(
        YOUDAO_URL,
        params={"type": YOUDAO_TYPE, "audio": query},
        timeout=TTS_TIMEOUT,
        stream=True,
    )
    resp.raise_for_status()
    ctype = resp.headers.get("Content-Type", "")
    if "audio" not in ctype:  # 有道失败时返回 application/json
        raise RuntimeError(f"有道未返回音频（Content-Type={ctype!r}）：{resp.text[:120]}")
    try:
        with out_path.open("wb") as fh:
            for chunk in resp.iter_content(1024):
                fh.write(chunk)
    except Exception:
        # 中途断连会留下截断的 mp3，会被 glob 打包进 apkg（半损音频）——删掉再抛
        out_path.unlink(missing_ok=True)
        raise


def generate_audio(rows: list[dict], media_dir: Path) -> tuple[int, list[str]]:
    """为所有单词从有道下载 mp3，写进 media_dir，并在 rows 里填 [sound:] 标签。

    只给单词配音（有道拉不到整句），例句音频恒空。按 word 去重，每个唯一词只下一次。
    串行 + TTS_DELAY 限频，避免猛打公共服务。返回 (成功文件数, 警告列表)。
    """
    # 收集唯一 (词 → 文件名) + (词 → 该词所有 row)，同时给每行填 [sound:] 标签
    uniq: dict[str, str] = {}
    rows_by_word: dict[str, list[dict]] = {}
    for r in rows:
        wname = _audio_name(r["word"])
        uniq[r["word"]] = wname
        rows_by_word.setdefault(r["word"], []).append(r)
        r["word_audio"] = f"[sound:{wname}]"
        r["example_audio"] = ""  # 例句暂不配音

    warnings: list[str] = []
    session = requests.Session()
    session.verify = False  # 有道证书链偶发问题，沿用原方法关校验
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    ok = 0
    total = len(uniq)
    for i, (word, name) in enumerate(uniq.items(), 1):
        print(f"  [{i}/{total}] 下载单词音频: {word}")
        try:
            _download_word(word, media_dir / name, session)
            ok += 1
        except Exception as exc:  # 单词失败不中断整批，记警告
            warnings.append(f"单词 '{word}' 音频下载失败：{exc}")
            # 清空该词 [sound:] 标签，否则卡面会引用不存在的 mp3（坏播放按钮）
            for r in rows_by_word.get(word, []):
                r["word_audio"] = ""
        time.sleep(TTS_DELAY)
    return ok, warnings


def build_notes(rows: list[dict]) -> list[genanki.Note]:
    notes = []
    for r in rows:
        notes.append(genanki.Note(
            model=MODEL,
            fields=[
                r["word"], r["phon"], r["senses_html"],
                r.get("word_audio", ""), r.get("example_audio", ""),
                r["key"],
            ],
            guid=genanki.guid_for(r["key"]),  # 稳定 guid（按 word）→ 重导入更新而非重复
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

    # 音频去重后的实际文件数：只有唯一单词（例句不配音）
    uniq_audio = {r["word"] for r in rows}
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
            tone = "美音" if YOUDAO_TYPE == 0 else "英音"
            print(f"下载单词音频（有道 dictvoice, {tone}）…")
            ok, audio_warnings = generate_audio(rows, media_dir)
            for w in audio_warnings:
                print(f"⚠ {w}", file=sys.stderr)
            media_files = [str(p) for p in sorted(media_dir.glob("*.mp3"))]
            print(f"音频文件 {len(media_files)} 个（成功 {ok}）")
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
