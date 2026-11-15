# -*- coding: utf-8 -*-
"""把 doc/使用说明.md 忠实转换为 dist 下的用户说明书 .docx。

为什么要有这个脚本
------------------
旧的 dist 说明书是「每版在上版 docx 上改几段」攒出来的：V2.9.1 的文档里还留着
V2.1.2 / V2.1.1 的原文段落，章节只有几节、表格 6 个，与 md 版（十六节、13 表）
严重脱节，中间版本完全断档。

现在的流程：**md 是唯一内容源，docx 由它整体生成** —— 内容不会再随版本劣化，
改版只需改 md 再跑本脚本。

用法：
    python build_manual.py              # 默认读 server.py 的 APP_VERSION
    python build_manual.py V2.9.2

支持的 md 语法：`#/##/###` 标题、`>` 引用、`-`/`*` 无序列表、`1.` 有序列表、
GFM 表格、``` 代码块、行内 `**粗体**` 与 `` `代码` ``、水平线 `---`。
"""
import os
import re
import sys

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor, Cm

BASE = os.path.dirname(os.path.abspath(__file__))
MD = os.path.join(BASE, 'doc', '使用说明.md')
DIST = os.path.join(BASE, 'dist')

FONT_CN = 'Microsoft YaHei'
FONT_EN = 'Segoe UI'
FONT_MONO = 'Consolas'
ACCENT = RGBColor(0x1F, 0x4E, 0x79)      # 一级标题深蓝
ACCENT2 = RGBColor(0x2E, 0x5E, 0x8C)     # 二级标题
GREY = RGBColor(0x60, 0x60, 0x60)


def ver():
    if len(sys.argv) > 1:
        return sys.argv[1].lstrip('Vv')
    src = open(os.path.join(BASE, 'server.py'), encoding='utf-8').read()
    m = re.search(r'APP_VERSION\s*=\s*["\']([\d.]+)["\']', src)
    assert m, 'server.py 里找不到 APP_VERSION'
    return m.group(1)


def set_font(run, size=10.5, bold=False, cn=FONT_CN, en=FONT_EN, color=None, mono=False):
    run.font.size = Pt(size)
    run.font.bold = bold
    if mono:
        cn = en = FONT_MONO
    run.font.name = en
    run.font.color.rgb = color if color is not None else RGBColor(0x1A, 0x1A, 0x1A)
    rpr = run._element.get_or_add_rPr()
    rf = rpr.find(qn('w:rFonts'))
    if rf is None:
        rf = rpr.makeelement(qn('w:rFonts'), {})
        rpr.append(rf)
    rf.set(qn('w:eastAsia'), cn)
    rf.set(qn('w:ascii'), en)
    rf.set(qn('w:hAnsi'), en)


INLINE = re.compile(r'(\*\*.+?\*\*|`[^`]+`|\*[^*]+?\*)')


def add_runs(par, text, size=10.5, base_bold=False, color=None):
    """解析行内 **粗体**、`代码`、*斜体*，逐段生成 run。"""
    text = text.replace('&quot;', '"').replace('&amp;', '&')
    for seg in INLINE.split(text):
        if not seg:
            continue
        if seg.startswith('**') and seg.endswith('**') and len(seg) > 4:
            inner = seg[2:-2]
            # 粗体里可以套 `代码`：正文里「**按 `R` 刷新**」这类写法很常见，
            # 旧实现整段当一个 run 输出 ⇒ 裸反引号原样留在 docx 里（2026-11-15 实测 13 处）。
            # 拆开后代码段保持粗体、只换成等宽字体。
            for sub in re.split(r'(`[^`]+`)', inner):
                if not sub:
                    continue
                if sub.startswith('`') and sub.endswith('`') and len(sub) > 2:
                    set_font(par.add_run(sub[1:-1]), size - 0.5, True, mono=True,
                             color=RGBColor(0xB0, 0x3A, 0x2B))
                else:
                    set_font(par.add_run(sub), size, True, color=color)
        elif seg.startswith('`') and seg.endswith('`') and len(seg) > 2:
            set_font(par.add_run(seg[1:-1]), size - 0.5, base_bold, mono=True,
                     color=RGBColor(0xB0, 0x3A, 0x2B))
        elif seg.startswith('*') and seg.endswith('*') and len(seg) > 2:
            r = par.add_run(seg[1:-1])
            r.font.italic = True
            set_font(r, size - 0.5, base_bold, color=color or GREY)
        else:
            set_font(par.add_run(seg), size, base_bold, color=color)


def cell_text(cell, text, size=9.5, bold=False, color=None):
    cell.text = ''
    par = cell.paragraphs[0]
    par.paragraph_format.space_before = Pt(2)
    par.paragraph_format.space_after = Pt(2)
    add_runs(par, text, size, bold, color)


def build(md_path, out_path, version):
    # md 里统一写「ComfyMediaViewer.exe」，生成时换成带版本号的真实文件名
    # （否则每发一版都要手工改一次正文里的 exe 名）
    lines = open(md_path, encoding='utf-8').read().replace('\r\n', '\n').split('\n')
    lines = [l.replace('ComfyMediaViewer.exe', 'ComfyMediaViewer V%s.exe' % version)
             for l in lines]
    doc = Document()

    # 页面与正文样式
    st = doc.styles['Normal']
    st.font.name = FONT_EN
    st.font.size = Pt(10.5)
    st.element.rPr.rFonts.set(qn('w:eastAsia'), FONT_CN)
    pf = st.paragraph_format
    pf.space_after = Pt(4)
    pf.line_spacing = 1.35
    for s in doc.sections:
        s.left_margin = s.right_margin = Cm(2.2)
        s.top_margin = s.bottom_margin = Cm(2.0)

    i, n = 0, len(lines)
    title_done = False
    while i < n:
        line = lines[i].rstrip()

        # 空行
        if not line.strip():
            i += 1
            continue

        # 水平线
        if re.fullmatch(r'-{3,}', line.strip()):
            p = doc.add_paragraph()
            pPr = p._p.get_or_add_pPr()
            pbdr = pPr.makeelement(qn('w:pBdr'), {})
            bot = pbdr.makeelement(qn('w:bottom'), {})
            bot.set(qn('w:val'), 'single')
            bot.set(qn('w:sz'), '6')
            bot.set(qn('w:color'), 'D0D0D0')
            pbdr.append(bot)
            pPr.append(pbdr)
            i += 1
            continue

        # 代码块
        if line.strip().startswith('```'):
            i += 1
            buf = []
            while i < n and not lines[i].strip().startswith('```'):
                buf.append(lines[i])
                i += 1
            i += 1
            p = doc.add_paragraph()
            p.paragraph_format.left_indent = Cm(0.5)
            set_font(p.add_run('\n'.join(buf)), 9.5, mono=True)
            continue

        # 标题
        m = re.match(r'^(#{1,4})\s+(.*)$', line)
        if m:
            lvl, text = len(m.group(1)), m.group(2).strip()
            if lvl == 1 and not title_done:
                p = doc.add_paragraph()
                p.alignment = WD_ALIGN_PARAGRAPH.CENTER
                p.paragraph_format.space_after = Pt(10)
                set_font(p.add_run(text), 20, True, color=ACCENT)
                title_done = True
                i += 1
                continue
            lvl = min(lvl, 4)
            p = doc.add_paragraph(style='Heading %d' % lvl)
            size = {2: 15, 3: 12.5, 4: 11.5}.get(lvl, 11)
            p.paragraph_format.space_before = Pt(12 if lvl == 2 else 8)
            p.paragraph_format.space_after = Pt(5)
            for r in list(p.runs):
                r._element.getparent().remove(r._element)
            set_font(p.add_run(text), size, True,
                     color=ACCENT if lvl == 2 else ACCENT2)
            i += 1
            continue

        # 表格
        if line.strip().startswith('|') and i + 1 < n and re.match(r'^\s*\|[\s:|-]+\|\s*$', lines[i + 1]):
            rows = []
            header = [c.strip() for c in line.strip().strip('|').split('|')]
            i += 2
            while i < n and lines[i].strip().startswith('|'):
                rows.append([c.strip() for c in lines[i].strip().strip('|').split('|')])
                i += 1
            t = doc.add_table(rows=1, cols=len(header))
            t.style = 'Table Grid'
            t.alignment = WD_TABLE_ALIGNMENT.CENTER
            for j, h in enumerate(header):
                cell_text(t.rows[0].cells[j], h, 9.5, True, RGBColor(0xFF, 0xFF, 0xFF))
                shd = t.rows[0].cells[j]._tc.get_or_add_tcPr().makeelement(
                    qn('w:shd'), {qn('w:fill'): '1F4E79'})
                t.rows[0].cells[j]._tc.get_or_add_tcPr().append(shd)
            for row in rows:
                cells = t.add_row().cells
                for j, c in enumerate(row[:len(header)]):
                    cell_text(cells[j], c, 9.5)
            doc.add_paragraph().paragraph_format.space_after = Pt(2)
            continue

        # 引用
        if line.lstrip().startswith('>'):
            buf = []
            while i < n and lines[i].lstrip().startswith('>'):
                buf.append(re.sub(r'^\s*>\s?', '', lines[i]).rstrip())
                i += 1
            p = doc.add_paragraph()
            p.paragraph_format.left_indent = Cm(0.45)
            p.paragraph_format.space_after = Pt(4)
            add_runs(p, ' '.join(x for x in buf if x), 10, color=GREY)
            continue

        # 有序列表
        m = re.match(r'^\s*(\d+)\.\s+(.*)$', line)
        if m:
            p = doc.add_paragraph(style='List Number')
            p.paragraph_format.space_after = Pt(2)
            add_runs(p, m.group(2), 10.5)
            i += 1
            continue

        # 无序列表（含缩进续行合并）
        m = re.match(r'^(\s*)[-*]\s+(.*)$', line)
        if m:
            indent = len(m.group(1)) // 2
            p = doc.add_paragraph(style='List Bullet' if indent == 0 else 'List Bullet 2')
            p.paragraph_format.space_after = Pt(2)
            add_runs(p, m.group(2), 10.5)
            i += 1
            while i < n and lines[i].startswith('  ') and lines[i].strip() \
                    and not re.match(r'^\s*([-*]|\d+\.|>|#)', lines[i]):
                add_runs(p, ' ' + lines[i].strip(), 10.5)
                i += 1
            continue

        # 普通段落（合并软换行）
        buf = [line]
        i += 1
        while i < n and lines[i].strip() and not re.match(
                r'^\s*(#|>|\||[-*]\s|\d+\.\s|```|-)', lines[i]):
            buf.append(lines[i].strip())
            i += 1
        p = doc.add_paragraph()
        add_runs(p, ''.join(buf), 10.5)

    # 页脚落款
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.space_before = Pt(16)
    set_font(p.add_run('ComfyUI 媒体浏览器 · 单文件绿色版 · V%s' % version), 9, color=GREY)

    if os.path.isfile(out_path):
        os.remove(out_path)
    doc.save(out_path)
    return out_path


if __name__ == '__main__':
    v = ver()
    out = os.path.join(DIST, 'ComfyMediaViewer V%s使用说明.docx' % v)
    print('version:', v)
    print('saved  :', build(MD, out, v), os.path.getsize(out), 'bytes')
