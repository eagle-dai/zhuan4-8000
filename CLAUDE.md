# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 工作约定

- **使用简体中文回答**。
- **context 容易满，注意节省**：已读过的长文件别整份重读（读片段或按行/章节引用），重活（全文搜索/大范围读）派子 agent、主会话只接结论，别把同一大段内容反复塞进 context。

## 仓库性质

这是一个**内容处理流水线**，不是软件项目——没有源代码、构建系统或测试。目标：把英语专业四级（TEM-4）词汇书的扫描页转换成 Anki 卡片。

## 流水线

`pages/`（扫描原件）→ OCR/解析 → `ocr/`（结构化 JSON）→ `anki/`（卡片）

1. **`pages/`** —— 词汇书的扫描原件，JPG 格式，按页码命名（如 `057.jpg`）。**内容是增量的**：当前只有部分页，后续会陆续补充；页码也不连续（部分页缺失，如 069–071）。别假设某个页码一定存在或范围连续——每次处理前先 `ls pages/` 看实际有哪些。这是唯一可信来源，难以重建——绝不原地修改，只在副本上操作。
2. **`ocr/`** —— 每页一个 `NNN.json`（页码对齐 `pages/NNN.jpg`），存 OCR 解析出的结构化词条。
   - **增量、不重复解析**：OCR 前先对比 `pages/` 与 `ocr/`，只处理 `ocr/` 里还没有对应 `NNN.json` 的新页，已解析过的页直接跳过（除非明确要求重做某页）。
   - **核对后才入库**：OCR 结果须经人工核对确认正确后，才写入 `ocr/`。别把未核对的解析直接落地——扫描件识别易错（音标、义项、例句），错的 JSON 会传播成错的卡片。可先产出待核对稿给用户确认，通过后再写 `ocr/NNN.json`。
3. **`anki/`** —— 最终产物：从 `ocr/` 合并生成的卡片（`scripts/build_anki.py` 生成 `.apkg`）。

## 版面结构（OCR 时须知）

每页**双栏**，阅读顺序：左栏上→下，再右栏上→下。词条结构固定：

- **词头** + `/音标/`
- `【助记】`：词根拆解记忆法
- 词性块：`vt.`/`n.`/`v.` 等 + `①②③` 义项，每义项含 英文释义 + 中文释义 + 斜体英文例句 + 中文翻译
- `【派生】`：派生词（可多个，`//` 分隔）

## 接续问题（关键）

词条会被**栏边界**或**页边界**切断——一个词条可能左栏末尾起、右栏顶部续（跨栏），或本页末尾起、下一页续（跨页）。例：页 057 的 `debate` 续到 058。

存储策略：**按页存 + 接续标记**，OCR 阶段不跨页合并，留到 Anki 生成阶段合并。

- 页级字段 `continues_from`（本页首个词条续自上页某词，否则 `null`）、`continues_to`（本页末词条续到下页，否则 `null`）。
- 被切词条加 `partial`: `"head"`（起始半段）或 `"tail"`（续接半段）；完整词条 `complete: true`。
- 词头相同用于跨页拼接对齐。

### `ocr/NNN.json` schema

```json
{
  "page": 57,
  "continues_from": null,
  "continues_to": "debate",
  "entries": [
    {
      "word": "exhaust",
      "phonetic": "ɪɡˈzɔːst",
      "mnemonic": "ex(出)+haust(拉)→力气全被拉出来→使筋疲力尽",
      "senses": [
        {"pos": "vt.", "def_en": "to make extremely weary; wear out",
         "def_zh": "使筋疲力尽", "example": "We are all exhausted after a long cycle ride.",
         "example_zh": "长途骑车后我们都筋疲力尽了。"}
      ],
      "derivatives": ["exhaustive a. 消耗的;详尽的", "inexhaustible a. 用不完的;不知疲倦的"],
      "complete": true
    },
    {"word": "debate", "partial": "head", "phonetic": "dɪˈbeɪt", "mnemonic": "de(down)+bate(打)→用言语将对方打倒→辩论",
     "senses": [{"pos": "n.", "def_zh": "辩论", "example": "have a debate (with sb.)", "example_zh": "（与某人）进行辩论"}]}
  ]
}
```

## Anki 卡片设计

目标产物。设计已定稿（样卡见 `anki/sample-card.html`），遵循最小信息原则：

- **粒度**：**每词一张卡**。一个词无论多少义项都是 1 张卡，背面按词性依次列全部义项（同词性多义项加 ①②③），各带自己的例句。exhaust（vt.×3 + n.×2）→ 1 张，背面 5 个义项俱全。
- **方向**：只 **英→中**（认词）。正面给词，背面给全部中文释义。
- **读音**：用 **edge-tts**（微软免费神经网络语音，美音 `en-US-AriaNeural`）在构建时**预生成 mp3**，打包进 apkg，卡面用 `[sound:xxx.mp3]` 播放。正面读单词，背面顺序读所有例句。mp3 按文本内容 hash 命名去重，**只进 apkg 不入库**。
- **正面**：单词 + 音标 + 读单词。
- **背面**：单词 + 音标 + 全部义项（词性 + 中文释义 + 英文例句 + 中文翻译）+ 顺序读所有例句。（助记 mnemonic 暂不放卡面。）

### Anki 字段（一张卡 = 一个词 = 一行 note）

`Word` `Phonetic` `Senses` `WordAudio` `ExampleAudio` `Key`

一个词条摊平成一行 note：`Word`/`Phonetic` 取自词条；`Senses` 是把该词所有 `senses[]` 预渲染成的一段背面 HTML（每义项一块 pos + 中文释义 + 例句，同词性多义项带 ①②③ 序号，纯展示不入 Key）；`WordAudio` 是单词 `[sound:]` 标签，`ExampleAudio` 是该词所有例句 `[sound:]` 标签的顺序拼接（Anki 依次播放）；`Key`（= `word`，一词即唯一键）稳定，脚本据此生成 note guid，重导入时更新而非重复。跨页词条（`complete:false`）须先与 tail 页合并补全后再出卡。

**重复来源**：新粒度下 Key 就是 `word`，同一个词只该出现一个词条。若 `build_anki.py --check` 报「同词多条目」告警 = 有真重复（跨页 head/tail 未合并、或 OCR 把同词解析了两遍），须查 OCR/合并，不能靠拆 Key 绕过。

**OCR 义项别拆**：一个词若某词性只有一个义项但书里给两个例句（`//` 分隔），仍是同一个 sense——两例句合进该 sense 的 `example` 字段用 ` // ` 连，别拆成两个 sense（拆了背面会多列一块重复义项）。

## 目录内容

- `README.md` —— 面向人的项目说明（是什么、目录、用法）。实现细节（schema、版面、接续规则）只写在本文件，README 不重复。
- `anki/sample-card.html` —— 卡片设计定稿的可视样卡（浏览器预览，用 Web Speech 模拟 TTS）。

## 构建 Anki

```bash
uv run scripts/build_anki.py --check    # 只校验+统计（页数/词条/卡片/音频数、跨页警告），不生成
uv run scripts/build_anki.py            # 生成 anki/zhuan4.apkg（含 edge-tts 音频，需联网）
uv run scripts/build_anki.py --no-audio # 生成 apkg 但跳过音频（快速测试，无需联网）
```

`scripts/build_anki.py`（uv PEP 723 inline 依赖：genanki + edge-tts）读 `ocr/*.json` → 跨页合并被切词条 → 每义项一张 note → edge-tts 生成 mp3 → 打包成 `.apkg`。要点：

- **跨页合并**：`complete:false` 的 head 词条须在下一页找到 `partial:tail` 的同词头 tail 才拼全出卡；配不上（如下一页还没 OCR）则**跳过并告警**，不出残缺卡。
- **稳定 ID/guid**：MODEL_ID、DECK_ID、note 的 guid（按 `word` 生成）都固定，重复导入是**更新**不是新建重复卡。
- **音频**：edge-tts 联网生成，按文本 hash 命名去重（多张卡同词只生成一个词音频）。生成的 mp3 在临时目录，打包进 apkg 后清理，不落地不入库。
- `.apkg` 是构建产物（zip 二进制），`.gitignore` 已忽略，不入库。

## 在本仓库工作时

- 图片处理（裁剪、缩放、OCR、格式转换）用 `ffmpeg`、ImageMagick 或 OCR 工具，针对 `pages/` 里的文件。
- 跨阶段保持页码命名对应（某页的 OCR 文档要能映射回它的 `pages/NNN.jpg`）。页码缺口是有意为之（源缺页），不是需要"修复"的错误。
