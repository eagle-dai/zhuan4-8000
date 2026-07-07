# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 仓库性质

这是一个**内容处理流水线**，不是软件项目——没有源代码、构建系统或测试。目标：把英语专业四级（TEM-4）词汇书的扫描页转换成 Anki 卡片。

## 流水线

`pages/`（扫描原件）→ OCR/解析 → `ocr/`（结构化 JSON）→ `anki/`（卡片）

1. **`pages/`** —— 词汇书的扫描原件，JPG 格式，按页码命名（如 `057.jpg`）。**内容是增量的**：当前只有部分页，后续会陆续补充；页码也不连续（部分页缺失，如 069–071）。别假设某个页码一定存在或范围连续——每次处理前先 `ls pages/` 看实际有哪些。这是唯一可信来源，难以重建——绝不原地修改，只在副本上操作。
2. **`ocr/`** —— 每页一个 `NNN.json`（页码对齐 `pages/NNN.jpg`），存 OCR 解析出的结构化词条。尚未创建。**增量、不重复解析**：OCR 前先对比 `pages/` 与 `ocr/`，只处理 `ocr/` 里还没有对应 `NNN.json` 的新页，已解析过的页直接跳过（除非明确要求重做某页）。
3. **`anki/`** —— 最终产物：从 `ocr/` 合并生成的卡片。尚未创建。

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

- **粒度**：**每义项一张卡**。一个词有几个义项（词性×序号）就出几张卡，各带自己的例句。exhaust（vt.×3 + n.×2）→ 5 张。
- **方向**：只 **英→中**（认词）。正面给词，背面给中文释义。
- **读音**：用 Anki **TTS 标签**，零音频文件。模板里 `{{tts en_US:Word}}` 读单词、`{{tts en_US:Example}}` 读例句。
- **正面**：单词 + 音标 + 读单词。
- **背面**：单词 + 音标 + 词性 + 中文释义 + 英文例句 + 中文翻译 + 读例句。（助记 mnemonic 暂不放卡面。）

### Anki 字段（一张卡 = 一个义项 = 一行 note）

`Word` `Phonetic` `POS` `DefZh` `Example` `ExampleZh`

一个 `ocr/NNN.json` 的每个 `senses[]` 元素摊平成一行 note：`word`/`phonetic` 从词条继承，`pos`/`def_zh`/`example`/`example_zh` 取自该义项。跨页词条（`complete:false`）须先与 tail 页合并补全后再出卡。

## 目录内容

- `README.md` —— 面向人的项目说明（是什么、目录、用法）。实现细节（schema、版面、接续规则）只写在本文件，README 不重复。
- `anki/sample-card.html` —— 卡片设计定稿的可视样卡（浏览器预览，用 Web Speech 模拟 TTS）。

## 在本仓库工作时

- 没有 build/lint/test 命令，不要尝试运行。
- 图片处理（裁剪、缩放、OCR、格式转换）用 `ffmpeg`、ImageMagick 或 OCR 工具，针对 `pages/` 里的文件。
- 跨阶段保持页码命名对应（某页的 OCR 文档要能映射回它的 `pages/NNN.jpg`）。页码缺口是有意为之（源缺页），不是需要"修复"的错误。
