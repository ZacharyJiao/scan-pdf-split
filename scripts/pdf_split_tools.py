# -*- coding: utf-8 -*-
"""扫描 PDF 分类拆分辅助工具。

渲染依赖 pymupdf（技能内置 .venv 已装）；批量 OCR 依赖 rapidocr_onnxruntime（同环境已装）。

子命令（页码一律 1 起，支持负数：-1 = 最后一页；支持区间：1-10；可混用逗号分隔）：
  probe  <pdf>                              探查：页数 / 每页尺寸与旋转 / 文本层字符数
  render <pdf> <输出前缀> [选项]             渲染整页并拼图（红色页码标注），输出 <前缀>-01.png ...
  ocr    <pdf> <输出.jsonl> [选项]           单进程批量 OCR：默认固定宽窗（顶部30%×横向30-100%）快速识别，
                                           窗口无文本且类型词未命中的页自动整页补识别；输出每页 JSONL（全文+锚点+类型词）
  split  <pdf> <输出目录> <plan.json>       按映射拆分，plan.json: {"输出名.pdf": [页码...]}（可非连续）；
                                           特殊键 "_副本": [页码...] 声明重复扫描页，不输出但计入页数校验
  verify <pdf> <页码列表> <out.png> [选项]   渲染指定页拼图供校验（如 1,-1 = 首页+末页）

通用选项：
  --pages 1,55,110 / 1-10 / -1   页码范围（render/ocr；缺省=全部）
  --clip x0,y0,x1,y1             相对坐标裁剪；render/verify 默认整页，ocr 默认 0.30,0.0,1.0,0.30
  --rot N                        统一旋转角（0/90/180/270），默认 0
  --rot-map 1:270,2:90           逐页旋转角（横竖混排时用，未列出的页用 --rot）
  --dpi 150                      渲染 dpi
  --per 4                        每张拼图容纳页数（render）
  --pattern <正则>               子类别锚点正则（ocr；默认匹配"标段N（名称）"，可用项目名称等自定义）
  --keywords 词1,词2             类型核心词表（ocr；逐页记录命中了哪些词，如 签到表,承诺书,评审表）
  --no-fallback                  ocr 不做整页补识别（默认仅对窗口无文本且类型词未命中的页自动补）
未知参数会直接报错，不会静默忽略。
"""

import json
import os
import re
import sys
import time

import pymupdf

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

DEFAULT_PATTERN = r"标段\s*(\d+)\s*[（(]([^）)]+)"
FULL_PAGE = (0.0, 0.0, 1.0, 1.0)
# ocr 第一遍的固定宽窗：居中/偏右标题都覆盖，免逐文件调参；行数少，速度快一个量级
WINDOW_CLIP = (0.30, 0.0, 1.0, 0.30)


def parse_pagespec(spec, total):
    """'1,55,110' / '1-10' / '-1' / '3--1' → 排序去重后的 1 起页码列表。"""
    pages = set()
    for tok in spec.split(","):
        tok = tok.strip()
        if not tok:
            continue
        m = re.fullmatch(r"(-?\d+)(?:-(-?\d+))?", tok)
        if not m:
            sys.exit(f"无法解析页码片段: {tok!r}")
        a = int(m.group(1))
        b = m.group(2)
        def norm(x):
            return total + x + 1 if x < 0 else x
        if b is None:
            pages.add(norm(a))
        else:
            lo, hi = norm(a), norm(int(b))
            if lo > hi:
                lo, hi = hi, lo
            pages.update(range(lo, hi + 1))
    bad = [p for p in pages if p < 1 or p > total]
    if bad:
        sys.exit(f"页码超出范围(1..{total}): {bad}")
    return sorted(pages)


def probe(pdf_path):
    doc = pymupdf.open(pdf_path)
    print(f"{pdf_path}: {len(doc)} 页")
    for i, page in enumerate(doc):
        chars = len(page.get_text().strip())
        r = page.rect
        print(f"  page {i+1}: {r.width:.0f}x{r.height:.0f} rot={page.rotation} text_chars={chars}")
    doc.close()


def render_page(page, clip, rot, dpi):
    """按相对坐标 clip 渲染一页（默认整页），返回 PNG bytes。rot 仅内存生效。"""
    page.set_rotation(rot % 360)
    r = page.rect
    rect = pymupdf.Rect(
        r.x0 + r.width * clip[0],
        r.y0 + r.height * clip[1],
        r.x0 + r.width * clip[2],
        r.y0 + r.height * clip[3],
    )
    return page.get_pixmap(dpi=dpi, clip=rect).tobytes("png")


def pack_sheet(items, out_path):
    """把 [(label, png_bytes), ...] 竖向拼成一张图，红色标签在上。"""
    label_h, gap = 30, 10
    pixmaps = [(label, pymupdf.Pixmap(data)) for label, data in items]
    W = max(p.width for _, p in pixmaps)
    H = sum(p.height for _, p in pixmaps) + (label_h + gap) * len(pixmaps)
    out = pymupdf.open()
    pg = out.new_page(width=W, height=H)
    y = 0
    for label, p in pixmaps:
        pg.insert_text((8, y + 20), label, fontsize=16, color=(1, 0, 0))
        y += label_h
        pg.insert_image(pymupdf.Rect(0, y, p.width, y + p.height), stream=p.tobytes("png"))
        y += p.height + gap
    pg.get_pixmap(dpi=72).save(out_path)
    out.close()


def render_sheets(pdf_path, out_prefix, pages=None, clip=FULL_PAGE, rot=0,
                  rot_map=None, dpi=150, per=4):
    """渲染指定页（默认全部）并每 per 页拼一张图。返回输出文件列表（<前缀>-01.png ...）。"""
    rot_map = rot_map or {}
    doc = pymupdf.open(pdf_path)
    sel = pages or list(range(1, len(doc) + 1))
    items = []
    for p in sel:
        data = render_page(doc[p - 1], clip, rot_map.get(p, rot), dpi)
        items.append((f"PDF page {p}", data))
    doc.close()
    paths = []
    for s in range(0, len(items), per):
        path = f"{out_prefix}-{s // per + 1:02d}.png"
        pack_sheet(items[s:s + per], path)
        paths.append(path)
    print(f"渲染 {len(sel)} 页，生成 {len(paths)} 张拼图（{out_prefix}-01.png 起）")
    return paths


def _ocr_one(engine, page, clip, rot, dpi):
    """渲染+识别一页，返回 (文本, 行数)。"""
    png = render_page(page, clip, rot, dpi)
    result, _ = engine(png)
    text = "\n".join(l[1] for l in result) if result else ""
    return text, (len(result) if result else 0)


def _make_rec(p, text, n_lines, rx, kw, pass_no):
    m = rx.search(text)
    return {
        "page": p,
        "match": m.group(0) if m else None,
        "groups": list(m.groups()) if m else None,
        "kw_hits": [k for k in kw if k in text],
        "n_lines": n_lines,
        "pass": pass_no,
        "text": text,
    }


def ocr_batch(pdf_path, out_jsonl, pages=None, clip=None, rot=0, rot_map=None,
              dpi=150, pattern=DEFAULT_PATTERN, keywords=None, fallback=True):
    """单进程批量 OCR：第一遍固定宽窗快速识别；窗口无文本且类型词未命中的页第二遍整页补识别。
    JSONL 含每页文本、锚点、类型词与识别遍数（pass=1 窗口 / 2 整页），可离线重分析。
    注意：锚点（--pattern）未命中不触发整页补——锚点只印在部分页时（如编号类锚点），
    子类别可离线从全文提取，整页补对分类无增量、纯属耗时（42 页实测多花约 5 分钟）。"""
    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError:
        sys.exit("当前环境缺少 rapidocr_onnxruntime；请用技能内置 .venv 的解释器运行，"
                 "或改用 MCP / 视觉路径。")
    clip = clip or WINDOW_CLIP
    rot_map = rot_map or {}
    rx = re.compile(pattern)
    kw = [k.strip() for k in (keywords or "").split(",") if k.strip()]
    doc = pymupdf.open(pdf_path)
    sel = pages or list(range(1, len(doc) + 1))

    t0 = time.time()
    engine = RapidOCR()  # 模型只加载一次
    print(f"模型加载 {time.time() - t0:.1f}s，第一遍（窗口）识别 {len(sel)} 页...")

    recs = {}
    for p in sel:
        t1 = time.time()
        text, n = _ocr_one(engine, doc[p - 1], clip, rot_map.get(p, rot), dpi)
        recs[p] = _make_rec(p, text, n, rx, kw, 1)
        print(f"  page {p}: {'命中 ' + recs[p]['match'] if recs[p]['match'] else '未命中'} "
              f"({time.time() - t1:.1f}s)")

    # 整页补只针对窗口遍"什么都没识别出来"的页（无文本且类型词未命中）；
    # 锚点未命中但已有文本的页，分类信息已齐，整页补对分类无增量
    unmatched = [p for p in sel
                 if not recs[p]["match"] and not recs[p]["kw_hits"] and recs[p]["n_lines"] == 0]
    if unmatched and fallback:
        print(f"第二遍（整页）补识别 {len(unmatched)} 页: {unmatched}")
        for p in unmatched:
            t1 = time.time()
            text, n = _ocr_one(engine, doc[p - 1], FULL_PAGE, rot_map.get(p, rot), dpi)
            rec2 = _make_rec(p, text, n, rx, kw, 2)
            if rec2["match"]:
                recs[p] = rec2
            elif n > recs[p]["n_lines"]:
                recs[p]["text"] = text  # 整页文本更全，留着便于离线重分析
                recs[p]["n_lines"] = n
            print(f"  page {p}: {'命中 ' + recs[p]['match'] if recs[p]['match'] else '仍未命中'} "
                  f"({time.time() - t1:.1f}s)")
    doc.close()

    with open(out_jsonl, "w", encoding="utf-8") as f:
        for p in sel:
            f.write(json.dumps(recs[p], ensure_ascii=False) + "\n")

    matched = [p for p in sel if recs[p]["match"]]
    missed = [p for p in sel if not recs[p]["match"]]
    dt = time.time() - t0
    print(f"完成：{len(matched)}/{len(sel)} 页命中锚点，总耗时 {dt:.0f}s"
          f"（{dt / max(len(sel), 1):.1f}s/页），结果写入 {out_jsonl}")
    if missed:
        print(f"未命中页（{len(missed)}）：{missed}")


def split_pdf(pdf_path, outdir, plan):
    """plan: {输出文件名: [1 起页码...]}（支持非连续）。返回 [(文件名, 页数)]。
    特殊键 "_副本": [页码...] 声明重复扫描页——不输出，但计入页数校验。"""
    os.makedirs(outdir, exist_ok=True)
    doc = pymupdf.open(pdf_path)
    total = 0
    results = []
    dups = list(plan.get("_副本", []))
    for name, pages in plan.items():
        if name == "_副本":
            continue
        out = pymupdf.open()
        for p in pages:
            out.insert_pdf(doc, from_page=p - 1, to_page=p - 1)
        out.save(os.path.join(outdir, name))
        results.append((name, len(out)))
        total += len(out)
        out.close()
    src_pages = len(doc)
    doc.close()
    accounted = total + len(dups)
    dup_note = f"（另排除副本 {len(dups)} 页）" if dups else ""
    print(f"输出 {len(results)} 个文件，合计 {total} 页{dup_note}；源文件 {src_pages} 页 -> "
          + ("一致 ✓" if accounted == src_pages else "不一致 ✗ 请检查映射！"))
    return results


def verify_sheet(pdf_path, pages, out_path, clip=FULL_PAGE, rot=0, dpi=150):
    """渲染指定页（1 起，支持 -1 末页）的拼图，供拆分后校验。"""
    doc = pymupdf.open(pdf_path)
    items = []
    for p in pages:
        data = render_page(doc[p - 1], clip, rot, dpi)
        items.append((f"page {p}", data))
    doc.close()
    pack_sheet(items, out_path)
    print(f"已生成 {out_path}")


def _parse_clip(s):
    parts = [float(x) for x in s.split(",")]
    if len(parts) != 4:
        sys.exit("--clip 需要 4 个相对坐标")
    return tuple(parts)


def _parse_rot_map(s):
    m = {}
    for kv in s.split(","):
        k, v = kv.split(":")
        m[int(k)] = int(v)
    return m


def main(argv):
    args = list(argv)
    opts = {"clip": None, "rot": 0, "rot_map": {}, "dpi": 150, "per": 4,
            "pages": None, "pattern": DEFAULT_PATTERN, "keywords": None,
            "no_fallback": False}
    known = {"--clip": "clip", "--rot": "rot", "--rot-map": "rot_map",
             "--dpi": "dpi", "--per": "per", "--pages": "pages",
             "--pattern": "pattern", "--keywords": "keywords"}
    i = 0
    while i < len(args):
        a = args[i]
        if a.startswith("--"):
            if a == "--no-fallback":
                opts["no_fallback"] = True
                del args[i]
                continue
            if a not in known:
                sys.exit(f"未知参数: {a}（支持 {', '.join(sorted(known))}、--no-fallback）")
            if i + 1 >= len(args):
                sys.exit(f"参数 {a} 缺少值")
            key = known[a]
            val = args[i + 1]
            if key == "clip":
                opts["clip"] = _parse_clip(val)
            elif key == "rot_map":
                opts["rot_map"] = _parse_rot_map(val)
            elif key in ("rot", "dpi", "per"):
                opts[key] = int(val)
            else:
                opts[key] = val
            del args[i:i + 2]
        else:
            i += 1

    if not args:
        sys.exit(__doc__)
    cmd = args[0]

    def pages_of(pdf, optkey="pages"):
        if opts[optkey] is None:
            return None
        d = pymupdf.open(pdf)
        n = len(d)
        d.close()
        return parse_pagespec(opts[optkey], n)

    if cmd == "probe":
        probe(args[1])
    elif cmd == "render":
        render_sheets(args[1], args[2], pages=pages_of(args[1]),
                      clip=opts["clip"] or FULL_PAGE,
                      rot=opts["rot"], rot_map=opts["rot_map"], dpi=opts["dpi"],
                      per=opts["per"])
    elif cmd == "ocr":
        ocr_batch(args[1], args[2], pages=pages_of(args[1]), clip=opts["clip"],
                  rot=opts["rot"], rot_map=opts["rot_map"], dpi=opts["dpi"],
                  pattern=opts["pattern"], keywords=opts["keywords"],
                  fallback=not opts["no_fallback"])
    elif cmd == "split":
        with open(args[3], encoding="utf-8") as f:
            plan = json.load(f)
        split_pdf(args[1], args[2], plan)
    elif cmd == "verify":
        d = pymupdf.open(args[1])
        n = len(d)
        d.close()
        pages = parse_pagespec(args[2], n)
        verify_sheet(args[1], pages, args[3], clip=opts["clip"] or FULL_PAGE,
                     rot=opts["rot"], dpi=opts["dpi"])
    else:
        sys.exit(f"未知子命令: {cmd}（可选 probe / render / ocr / split / verify）")


if __name__ == "__main__":
    main(sys.argv[1:])
