# translate-book

[English](README.md) · [简体中文](README.zh-CN.md)

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.8+](https://img.shields.io/badge/python-3.8%2B-blue.svg)](https://www.python.org/)
[![Agent Skill](https://img.shields.io/badge/Claude%20Code-Skill-8a63d2)](https://code.claude.com/docs/en/skills)

把一整本 **EPUB 或 PDF 技术书**翻译成另一种语言，产出仍然是一个合法的 EPUB：章节、代码块、链接、插图、表格、代码旁注，全都还在作者原来的位置上。

难点从来不是翻译本身——让大模型翻一段话并不难。难点在于一本书里有成千上万个结构依赖，而任何偷懒的流水线都会静默地破坏它们：代码被"翻译"了、锚点失效了、插图跑到了别的章节，等你翻了四万字之后才发现。

## 总原则

> **永远不要用纯文本来重新生成一本书。** 把书解析成可翻译的单元，只替换文字，再写回原有结构，最后打包。

这个仓库里的一切，都是为了让这条原则可执行、可校验。

## 工作方式

每一章会被解析成若干**单元（unit）**：一个块级元素，内容由文字加行内标记组成——段落、标题、列表项、表格单元格、图注。单元里的行内标记分两类，把这两类混为一谈是最常见的翻车方式：

| | 记法 | 译者可以 | 例子 |
|---|---|---|---|
| **Token** | `⟧n⟦…⟧/n⟦` | 翻译中间的文字，两端标记保留 | 链接、`<em>`、`<strong>` |
| **Atom** | `⟨n⟩` | 整体移动位置，绝不修改或复制 | `<code>json.loads</code>`、行内图片 |

于是：行内代码外面的散文会被翻译，代码本身不会；链接翻译完还指向原来的地方。写回之前，每个单元都要过校验：不许出现源文里没有的 token、开合标记必须配对、atom 必须与源文构成完全相同的多重集。校验不通过的单元**保留原文**——漏译会在验证阶段被列出来，而损坏的标记不会。

## 流水线

| 步骤 | 命令 | 作用 |
|---|---|---|
| 1. 提取 | `units.py extract` | 每章一个 JSON，装的是单元 |
| 2. 自检 | `units.py write --identity` | 把原文原样写回再 diff——几分钟内抓出所有索引错位 |
| 3. 分批 | `units.py batch` | 切成约 1600 词的批次，正好交给一个子代理 |
| 4. 翻译 | [`AGENTS.md`](skills/translate-book/AGENTS.md) 提示词 | 每批一个子代理，输出只增加一个 `out` 字段 |
| 5. 合并 | `units.py merge` | 把已完成的批次折叠成每章一个文件 |
| 6. 写回 | `units.py write` | 把译文写回结构，逐单元校验 |
| 7. 打包 | `epub_build.py` | 复制一份再覆盖，打成合规 EPUB |
| 8. 验证 | `epub_verify.py` | 结构对比、覆盖率、未翻译清单 |

第 6–8 步可以反复重跑。重跑第 1 步会让已有译文全部失效，所以才有 `units.py rekey`：它按原文文本重新匹配，通常能救回 95% 以上的旧译文。

## 安装

### 作为 Claude Code 插件（推荐）

```bash
/plugin marketplace add Abirdcfly/translate-book-skill
/plugin install translate-book@translate-book-skill
```

### 手动装成个人技能

注意要复制的是**技能目录**，不是仓库根目录——`SKILL.md` 必须位于安装目标的第一层：

```bash
git clone https://github.com/Abirdcfly/translate-book-skill.git
cp -r translate-book-skill/skills/translate-book ~/.claude/skills/
```

或者用软链接，这样 `git pull` 之后装好的技能就是最新的：

```bash
ln -s "$PWD/translate-book-skill/skills/translate-book" ~/.claude/skills/translate-book
```

### 其他 Agent

`SKILL.md` 是纯流程说明，[`AGENTS.md`](skills/translate-book/AGENTS.md) 是纯提示词模板，两者都可移植；四个 Python 脚本就是普通命令行工具，不依赖任何特定 Agent。任何能执行 `bash` 的 Agent 或人，都能驱动这条流水线。

## 环境要求

- Python 3.8+
- [`lxml`](https://lxml.de/) —— `pip install lxml`
- 只有走 PDF 路线才需要 poppler-utils 里的 `pdftohtml`：
  `brew install poppler`（macOS）/ `apt-get install poppler-utils`（Debian、Ubuntu）

## 快速开始 —— EPUB

```bash
mkdir -p work/src && unzip -q book.epub -d work/src

# SRC 是存放这本书 XHTML 的目录——通常是 OEBPS/Text/，
# 但每本书不一样：也可能叫 EPUB/text/、Text/，或者就在树根。
SRC=work/src/OEBPS/Text

# 1. 提取：每章一个单元 JSON
python3 scripts/units.py extract $SRC work/units

# 2. 翻译之前先自检：把原文原样写回再 diff
python3 scripts/units.py write $SRC work/units work/trans work/out --identity
diff -r $SRC work/out

# 3. 分批交给子代理
python3 scripts/units.py batch work/units work/batches

# 4. 翻译：按 AGENTS.md 派发子代理，每个批次一个输出文件，
#    写进 work/done/，文件名不变，只增加 "out" 字段

# 5. 把完成的批次折叠成每章译文
python3 scripts/units.py merge work/units work/done work/trans

# 6. 写回结构（逐单元校验）
python3 scripts/units.py write $SRC work/units work/trans work/out

# 7. 打包并验证
python3 scripts/epub_build.py book.zh.epub --from work/src --overlay work/out
python3 scripts/epub_verify.py book.zh.epub --source book.epub
```

第 2 步是最值钱的一步。diff 里应该只出现序列化层面的规范化差异——折行被重新拼接、XML 声明里的 `"` 变成 `'`、DOCTYPE 被丢掉、文件末尾没换行。**只要正文用词出现了差异，就是你的环境有问题**，你宁可在翻译之前发现它。

`epub_verify.py` 会列出每一个仍未翻译的单元，供你逐个确认它们确实是「故意不翻」的——专有名词、标识符、英文的模型输出——而不是被悄悄丢掉的。

## 快速开始 —— PDF

PDF 里没有结构，只有一堆带坐标的字形。结构得你自己重建，而且**排版规则是每本书一套**，所以先勘察再写代码：

```bash
# 1. 摸清这本书的字体角色：family × size 计数、左边距直方图
python3 scripts/pdf_layout.py survey book.pdf --probe 40 --out-config layout.json

# 2. 编辑 layout.json：把你刚看到的字体映射到角色上
#    （body / heading / code / callout / caption / table / furniture）
#    并补一份 "chapters" 章节表

# 3. 重建每章的类型化块，并抽出插图
python3 scripts/pdf_layout.py extract book.pdf work/pdf \
    --config layout.json --xml work/book.xml --images work/images

# 4. 把块渲染成 OEBPS/Text/*.xhtml，然后建骨架、填内容、打包
python3 scripts/epub_build.py book.epub --init work/tree --title "T" --author "A"
python3 scripts/epub_build.py book.epub --stage work/tree --finalize

# 5. 接着走上面的 EPUB 流水线，此时 SRC=work/tree/OEBPS/Text
python3 scripts/units.py extract work/tree/OEBPS/Text work/units
```

接着用同一套 EPUB 流水线处理刚建好的目录树。[`REFERENCE.md`](skills/translate-book/REFERENCE.md) 里写了真正管用的提取顺序（先按角色分流再拼行、跨页断开的段落要合并、边栏旁注按垂直距离挂靠代码块），以及漏掉某一步时会出现什么症状。

## 它做不到什么

写清楚这些，是因为一份悄悄漏掉内容的译文，比一份老实说明缺什么的译文更糟：

- **图片里的文字不会被翻译。** 那需要重画插图。
- **行内公式往往没有文本层。** 单元会保留可见的空缺，而不是编造内容去填。
- **从 PDF 重建的表格是近似的。** 多行表头和合并单元格会错位，脚本会直说，而不是假装准确。
- **代码永远不翻译**，包括 `<pre>` 块里的代码注释。
- 正式出版的书里本来就有排版缺陷——双重转义的实体、词中间混进来的带样式的字符。流水线翻译其本意并记录例外，既不复制缺陷，也不静默丢弃。

## 仓库结构

```
.
├── .claude-plugin/           # 插件与 marketplace 清单
├── .github/workflows/ci.yml  # 字节码编译脚本、校验 SKILL.md
├── skills/
│   └── translate-book/
│       ├── SKILL.md          # 流程本身（技能被调用时载入）
│       ├── REFERENCE.md      # 单元模型、失败目录、PDF 结构恢复
│       ├── AGENTS.md         # 子代理提示词模板，填掉里面的 <槽位>
│       └── scripts/
│           ├── units.py          # extract / write / batch / merge / rekey
│           ├── pdf_layout.py     # PDF → 结构化块（坐标 + 字体）
│           ├── epub_build.py     # 暂存 + 打包成合法 EPUB
│           └── epub_verify.py    # 完整性、覆盖率、结构对比
├── LICENSE
└── README.md
```

## 适用范围

它针对的是一本书，不是一篇文章。单篇网页或文章的翻译，用更轻量的翻译技能就好——这一套是为篇幅、可续跑和结构完整性设计的，只有面对整份手稿时这些开销才划算。

## 维护者备注

- `.claude-plugin/plugin.json` 里的 `version` 是钉住版本的：这个字符串不变，插件用户就不会收到更新，所以每次发版都要改它。
- `skills/translate-book/SKILL.md` 的 YAML frontmatter 必须从第 1 行开始——只有这时清单才会被解析；技能的命令名由所在目录名决定。
- CI 会对脚本做字节码编译，并检查 `SKILL.md` 里提到的每个 `scripts/*.py` 是否真实存在。

## 许可证

[MIT](LICENSE) © 2026 Abirdcfly
