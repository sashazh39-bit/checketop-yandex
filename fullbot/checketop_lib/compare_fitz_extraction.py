#!/usr/bin/env python3
"""
Comprehensive PyMuPDF (fitz) text extraction comparison between original and generated PDFs.
"""

import fitz
import json
import sys
from collections import defaultdict

ORIG = "/Users/aleksandrzerebatav/Downloads/СБП.pdf"
GEN  = "/Users/aleksandrzerebatav/Desktop/чекетоп/tmp_alfa_scratch/gen_real_sbp_20260327_000024.pdf"

SEPARATOR = "=" * 100
SUBSEP = "-" * 80

def char_repr(c):
    cp = ord(c)
    if cp == 0x20:
        return "SPACE(0x20)"
    elif cp == 0xA0:
        return "NBSP(0xA0)"
    elif cp == 0x0A:
        return "LF(0x0A)"
    elif cp == 0x0D:
        return "CR(0x0D)"
    elif cp == 0x09:
        return "TAB(0x09)"
    elif cp < 0x20:
        return f"CTRL(0x{cp:02X})"
    elif cp == 0x200B:
        return "ZWSP(0x200B)"
    elif cp == 0x200C:
        return "ZWNJ(0x200C)"
    elif cp == 0x200D:
        return "ZWJ(0x200D)"
    elif cp == 0xFEFF:
        return "BOM(0xFEFF)"
    elif cp == 0x2009:
        return "THIN_SP(0x2009)"
    elif cp == 0x2007:
        return "FIG_SP(0x2007)"
    elif cp == 0x2008:
        return "PUNCT_SP(0x2008)"
    elif cp == 0x202F:
        return "NARROW_NBSP(0x202F)"
    else:
        return c

def hex_dump_str(s, label=""):
    """Return hex dump of a string with char annotations."""
    if label:
        print(f"  {label}:")
    for i, c in enumerate(s):
        cp = ord(c)
        vis = char_repr(c)
        print(f"    [{i:3d}] U+{cp:04X}  {vis}")


# ─────────────────────────────────────────────────────────────
# 1. FULL TEXT COMPARISON (get_text())
# ─────────────────────────────────────────────────────────────
def compare_full_text(page_orig, page_gen):
    print(f"\n{SEPARATOR}")
    print("1. FULL TEXT COMPARISON — page.get_text()")
    print(SEPARATOR)

    t_orig = page_orig.get_text()
    t_gen  = page_gen.get_text()

    print(f"  Original length: {len(t_orig)} chars")
    print(f"  Generated length: {len(t_gen)} chars")

    if t_orig == t_gen:
        print("  ✅ IDENTICAL")
        return

    print("  ❌ DIFFERENT — char-by-char diff:")

    max_len = max(len(t_orig), len(t_gen))
    diff_count = 0
    for i in range(max_len):
        c_o = t_orig[i] if i < len(t_orig) else "<EOF>"
        c_g = t_gen[i] if i < len(t_gen) else "<EOF>"
        if c_o != c_g:
            diff_count += 1
            o_repr = char_repr(c_o) if c_o != "<EOF>" else "<EOF>"
            g_repr = char_repr(c_g) if c_g != "<EOF>" else "<EOF>"
            o_hex = f"U+{ord(c_o):04X}" if c_o != "<EOF>" else "---"
            g_hex = f"U+{ord(c_g):04X}" if c_g != "<EOF>" else "---"

            ctx_start = max(0, i - 10)
            ctx_end_o = min(len(t_orig), i + 11)
            ctx_end_g = min(len(t_gen), i + 11)
            ctx_o = repr(t_orig[ctx_start:ctx_end_o])
            ctx_g = repr(t_gen[ctx_start:ctx_end_g])

            print(f"    pos {i}: ORIG={o_repr} ({o_hex})  GEN={g_repr} ({g_hex})")
            print(f"           ORIG context: {ctx_o}")
            print(f"           GEN  context: {ctx_g}")

            if diff_count >= 50:
                print(f"    ... (stopping after 50 diffs, total chars: orig={len(t_orig)}, gen={len(t_gen)})")
                break

    print(f"  Total character differences found: {diff_count}{'+ (capped)' if diff_count >= 50 else ''}")

    # Line-by-line comparison
    lines_o = t_orig.split('\n')
    lines_g = t_gen.split('\n')
    print(f"\n  Line counts: orig={len(lines_o)}, gen={len(lines_g)}")
    max_lines = max(len(lines_o), len(lines_g))
    for i in range(max_lines):
        lo = lines_o[i] if i < len(lines_o) else "<MISSING>"
        lg = lines_g[i] if i < len(lines_g) else "<MISSING>"
        if lo != lg:
            print(f"  Line {i} differs:")
            print(f"    ORIG: {repr(lo)}")
            print(f"    GEN:  {repr(lg)}")


# ─────────────────────────────────────────────────────────────
# 2. DICT COMPARISON (get_text("dict"))
# ─────────────────────────────────────────────────────────────
def compare_dict(page_orig, page_gen):
    print(f"\n{SEPARATOR}")
    print("2. STRUCTURED DICT COMPARISON — page.get_text('dict')")
    print(SEPARATOR)

    d_orig = page_orig.get_text("dict")
    d_gen  = page_gen.get_text("dict")

    print(f"  Original: {len(d_orig['blocks'])} blocks")
    print(f"  Generated: {len(d_gen['blocks'])} blocks")

    if len(d_orig['blocks']) != len(d_gen['blocks']):
        print("  ❌ BLOCK COUNT MISMATCH!")

    max_blocks = max(len(d_orig['blocks']), len(d_gen['blocks']))

    for bi in range(max_blocks):
        b_o = d_orig['blocks'][bi] if bi < len(d_orig['blocks']) else None
        b_g = d_gen['blocks'][bi] if bi < len(d_gen['blocks']) else None

        if b_o is None:
            print(f"\n  Block {bi}: ONLY IN GENERATED")
            print(f"    type={b_g.get('type','?')}, bbox={b_g.get('bbox')}")
            continue
        if b_g is None:
            print(f"\n  Block {bi}: ONLY IN ORIGINAL")
            print(f"    type={b_o.get('type','?')}, bbox={b_o.get('bbox')}")
            continue

        has_diff = False
        block_diffs = []

        # Compare bbox
        bbox_o = tuple(round(x, 2) for x in b_o['bbox'])
        bbox_g = tuple(round(x, 2) for x in b_g['bbox'])
        if bbox_o != bbox_g:
            has_diff = True
            block_diffs.append(f"    BBOX: orig={bbox_o} gen={bbox_g}")

        # Compare block type
        if b_o.get('type') != b_g.get('type'):
            has_diff = True
            block_diffs.append(f"    TYPE: orig={b_o.get('type')} gen={b_g.get('type')}")

        # For text blocks (type 0), compare lines/spans
        if b_o.get('type') == 0 and b_g.get('type') == 0:
            lines_o = b_o.get('lines', [])
            lines_g = b_g.get('lines', [])

            if len(lines_o) != len(lines_g):
                has_diff = True
                block_diffs.append(f"    LINE COUNT: orig={len(lines_o)} gen={len(lines_g)}")

            max_lines = max(len(lines_o), len(lines_g))
            for li in range(max_lines):
                l_o = lines_o[li] if li < len(lines_o) else None
                l_g = lines_g[li] if li < len(lines_g) else None

                if l_o is None:
                    has_diff = True
                    spans_text = " ".join(s.get('text','') for s in l_g.get('spans',[]))
                    block_diffs.append(f"    Line {li}: ONLY IN GEN: '{spans_text}'")
                    continue
                if l_g is None:
                    has_diff = True
                    spans_text = " ".join(s.get('text','') for s in l_o.get('spans',[]))
                    block_diffs.append(f"    Line {li}: ONLY IN ORIG: '{spans_text}'")
                    continue

                # Compare line bbox
                lbbox_o = tuple(round(x, 2) for x in l_o['bbox'])
                lbbox_g = tuple(round(x, 2) for x in l_g['bbox'])
                if lbbox_o != lbbox_g:
                    has_diff = True
                    block_diffs.append(f"    Line {li} BBOX: orig={lbbox_o} gen={lbbox_g}")

                # Compare line dir/wmode
                if l_o.get('dir') != l_g.get('dir'):
                    has_diff = True
                    block_diffs.append(f"    Line {li} DIR: orig={l_o.get('dir')} gen={l_g.get('dir')}")

                # Compare spans
                spans_o = l_o.get('spans', [])
                spans_g = l_g.get('spans', [])

                if len(spans_o) != len(spans_g):
                    has_diff = True
                    block_diffs.append(f"    Line {li} SPAN COUNT: orig={len(spans_o)} gen={len(spans_g)}")

                max_spans = max(len(spans_o), len(spans_g))
                for si in range(max_spans):
                    s_o = spans_o[si] if si < len(spans_o) else None
                    s_g = spans_g[si] if si < len(spans_g) else None

                    if s_o is None:
                        has_diff = True
                        block_diffs.append(f"    Line {li} Span {si}: ONLY IN GEN: text='{s_g.get('text','')}'")
                        continue
                    if s_g is None:
                        has_diff = True
                        block_diffs.append(f"    Line {li} Span {si}: ONLY IN ORIG: text='{s_o.get('text','')}'")
                        continue

                    # Compare span properties
                    props = ['text', 'font', 'size', 'color', 'flags', 'origin']
                    for prop in props:
                        v_o = s_o.get(prop)
                        v_g = s_g.get(prop)
                        if prop in ('size',):
                            v_o = round(v_o, 4) if v_o is not None else None
                            v_g = round(v_g, 4) if v_g is not None else None
                        if prop == 'origin':
                            v_o = tuple(round(x, 2) for x in v_o) if v_o else None
                            v_g = tuple(round(x, 2) for x in v_g) if v_g else None
                        if prop == 'text':
                            if v_o != v_g:
                                has_diff = True
                                block_diffs.append(
                                    f"    Line {li} Span {si} TEXT: orig={repr(v_o)} gen={repr(v_g)}")
                        elif v_o != v_g:
                            has_diff = True
                            block_diffs.append(
                                f"    Line {li} Span {si} {prop.upper()}: orig={v_o} gen={v_g}")

                    # Compare span bbox
                    sbbox_o = tuple(round(x, 2) for x in s_o['bbox'])
                    sbbox_g = tuple(round(x, 2) for x in s_g['bbox'])
                    if sbbox_o != sbbox_g:
                        has_diff = True
                        block_diffs.append(
                            f"    Line {li} Span {si} BBOX: orig={sbbox_o} gen={sbbox_g}")

        if has_diff:
            # Get text summary
            def block_text(b):
                if b.get('type') == 0:
                    texts = []
                    for l in b.get('lines', []):
                        for s in l.get('spans', []):
                            texts.append(s.get('text', ''))
                    return " ".join(texts)
                return "(image block)"

            print(f"\n  Block {bi} ❌ DIFFS (text: '{block_text(b_o)[:80]}'):")
            for d in block_diffs:
                print(d)
        else:
            # Even for matching blocks, print summary
            def block_text(b):
                if b.get('type') == 0:
                    texts = []
                    for l in b.get('lines', []):
                        for s in l.get('spans', []):
                            texts.append(s.get('text', ''))
                    return " ".join(texts)
                return "(image block)"
            # Only print first 60 chars
            txt = block_text(b_o)[:60]
            # suppress for identical blocks to keep output manageable
            pass


# ─────────────────────────────────────────────────────────────
# 3. RAWDICT COMPARISON
# ─────────────────────────────────────────────────────────────
def compare_rawdict(page_orig, page_gen):
    print(f"\n{SEPARATOR}")
    print("3. RAWDICT COMPARISON — page.get_text('rawdict')")
    print(SEPARATOR)

    d_orig = page_orig.get_text("rawdict")
    d_gen  = page_gen.get_text("rawdict")

    print(f"  Original blocks: {len(d_orig['blocks'])}")
    print(f"  Generated blocks: {len(d_gen['blocks'])}")

    # Focus on char-level differences not visible in dict mode
    for bi in range(min(len(d_orig['blocks']), len(d_gen['blocks']))):
        b_o = d_orig['blocks'][bi]
        b_g = d_gen['blocks'][bi]

        if b_o.get('type') != 0 or b_g.get('type') != 0:
            continue

        for li in range(min(len(b_o.get('lines',[])), len(b_g.get('lines',[])))):
            l_o = b_o['lines'][li]
            l_g = b_g['lines'][li]

            for si in range(min(len(l_o.get('spans',[])), len(l_g.get('spans',[])))):
                s_o = l_o['spans'][si]
                s_g = l_g['spans'][si]

                chars_o = s_o.get('chars', [])
                chars_g = s_g.get('chars', [])

                if len(chars_o) != len(chars_g):
                    # Get text for context
                    text_o = "".join(c.get('c','') for c in chars_o)
                    text_g = "".join(c.get('c','') for c in chars_g)
                    print(f"\n  Block {bi} Line {li} Span {si}: CHAR COUNT DIFF orig={len(chars_o)} gen={len(chars_g)}")
                    print(f"    ORIG text: {repr(text_o)}")
                    print(f"    GEN  text: {repr(text_g)}")

                max_chars = max(len(chars_o), len(chars_g))
                for ci in range(max_chars):
                    c_o = chars_o[ci] if ci < len(chars_o) else None
                    c_g = chars_g[ci] if ci < len(chars_g) else None

                    if c_o is None or c_g is None:
                        continue

                    # Compare char properties
                    if c_o.get('c') != c_g.get('c'):
                        print(f"  Block {bi} Line {li} Span {si} Char {ci}: "
                              f"orig='{c_o.get('c')}' (U+{ord(c_o.get('c',chr(0))):04X}) "
                              f"gen='{c_g.get('c')}' (U+{ord(c_g.get('c',chr(0))):04X})")

                    # Compare char bbox
                    if c_o.get('bbox') and c_g.get('bbox'):
                        cb_o = tuple(round(x, 1) for x in c_o['bbox'])
                        cb_g = tuple(round(x, 1) for x in c_g['bbox'])
                        if cb_o != cb_g:
                            # Only report significant bbox differences (>0.5pt)
                            max_delta = max(abs(a-b) for a,b in zip(cb_o, cb_g))
                            if max_delta > 0.5:
                                print(f"  Block {bi} L{li} S{si} Char {ci} ('{c_o.get('c','')}') BBOX: "
                                      f"orig={cb_o} gen={cb_g} delta={max_delta:.1f}")

    print("  (Only significant rawdict char-level differences shown above)")


# ─────────────────────────────────────────────────────────────
# 4. BLOCKS COMPARISON
# ─────────────────────────────────────────────────────────────
def compare_blocks(page_orig, page_gen):
    print(f"\n{SEPARATOR}")
    print("4. BLOCKS COMPARISON — page.get_text('blocks')")
    print(SEPARATOR)

    blocks_o = page_orig.get_text("blocks")
    blocks_g = page_gen.get_text("blocks")

    print(f"  Original: {len(blocks_o)} blocks")
    print(f"  Generated: {len(blocks_g)} blocks")

    if len(blocks_o) != len(blocks_g):
        print("  ❌ BLOCK COUNT MISMATCH!")

    max_blocks = max(len(blocks_o), len(blocks_g))
    for bi in range(max_blocks):
        bl_o = blocks_o[bi] if bi < len(blocks_o) else None
        bl_g = blocks_g[bi] if bi < len(blocks_g) else None

        if bl_o is None:
            print(f"\n  Block {bi}: ONLY IN GEN")
            print(f"    bbox=({bl_g[0]:.1f},{bl_g[1]:.1f},{bl_g[2]:.1f},{bl_g[3]:.1f})")
            print(f"    text={repr(bl_g[4][:100]) if isinstance(bl_g[4], str) else '(image)'}")
            continue
        if bl_g is None:
            print(f"\n  Block {bi}: ONLY IN ORIG")
            print(f"    bbox=({bl_o[0]:.1f},{bl_o[1]:.1f},{bl_o[2]:.1f},{bl_o[3]:.1f})")
            print(f"    text={repr(bl_o[4][:100]) if isinstance(bl_o[4], str) else '(image)'}")
            continue

        # bl format: (x0, y0, x1, y1, text_or_image, block_no, block_type)
        has_diff = False
        diffs = []

        # bbox comparison
        bbox_o = tuple(round(x, 1) for x in bl_o[:4])
        bbox_g = tuple(round(x, 1) for x in bl_g[:4])
        if bbox_o != bbox_g:
            has_diff = True
            diffs.append(f"    BBOX: orig={bbox_o} gen={bbox_g}")

        # text comparison
        text_o = bl_o[4] if isinstance(bl_o[4], str) else "(image)"
        text_g = bl_g[4] if isinstance(bl_g[4], str) else "(image)"
        if text_o != text_g:
            has_diff = True
            diffs.append(f"    TEXT ORIG: {repr(text_o[:120])}")
            diffs.append(f"    TEXT GEN:  {repr(text_g[:120])}")

        # type comparison
        if bl_o[6] != bl_g[6]:
            has_diff = True
            diffs.append(f"    TYPE: orig={bl_o[6]} gen={bl_g[6]}")

        if has_diff:
            print(f"\n  Block {bi} ❌ DIFFS:")
            for d in diffs:
                print(d)


# ─────────────────────────────────────────────────────────────
# 5. WORDS COMPARISON
# ─────────────────────────────────────────────────────────────
def compare_words(page_orig, page_gen):
    print(f"\n{SEPARATOR}")
    print("5. WORDS COMPARISON — page.get_text('words')")
    print(SEPARATOR)

    words_o = page_orig.get_text("words")
    words_g = page_gen.get_text("words")

    print(f"  Original: {len(words_o)} words")
    print(f"  Generated: {len(words_g)} words")

    if len(words_o) != len(words_g):
        print("  ❌ WORD COUNT MISMATCH!")

    # Build word-text lists for alignment
    wt_o = [w[4] for w in words_o]
    wt_g = [w[4] for w in words_g]

    # Simple index-based comparison
    max_words = max(len(words_o), len(words_g))
    diff_count = 0
    for wi in range(max_words):
        w_o = words_o[wi] if wi < len(words_o) else None
        w_g = words_g[wi] if wi < len(words_g) else None

        if w_o is None:
            diff_count += 1
            print(f"  Word {wi}: ONLY IN GEN: '{w_g[4]}'")
            continue
        if w_g is None:
            diff_count += 1
            print(f"  Word {wi}: ONLY IN ORIG: '{w_o[4]}'")
            continue

        has_diff = False
        diffs = []

        # Text
        if w_o[4] != w_g[4]:
            has_diff = True
            diffs.append(f"    TEXT: orig={repr(w_o[4])} gen={repr(w_g[4])}")

        # Bbox
        bbox_o = tuple(round(x, 1) for x in w_o[:4])
        bbox_g = tuple(round(x, 1) for x in w_g[:4])
        if bbox_o != bbox_g:
            max_delta = max(abs(a-b) for a,b in zip(bbox_o, bbox_g))
            if max_delta > 0.3:
                has_diff = True
                diffs.append(f"    BBOX: orig={bbox_o} gen={bbox_g} (delta={max_delta:.1f})")

        # Block/line numbers
        if w_o[5] != w_g[5] or w_o[6] != w_g[6]:
            has_diff = True
            diffs.append(f"    BLOCK/LINE: orig=({w_o[5]},{w_o[6]}) gen=({w_g[5]},{w_g[6]})")

        if has_diff:
            diff_count += 1
            print(f"  Word {wi} ❌ '{w_o[4]}':")
            for d in diffs:
                print(d)

        if diff_count >= 80:
            print("  ... (capped at 80 diffs)")
            break

    print(f"  Total word-level differences: {diff_count}")


# ─────────────────────────────────────────────────────────────
# 6. HTML COMPARISON
# ─────────────────────────────────────────────────────────────
def compare_html(page_orig, page_gen):
    print(f"\n{SEPARATOR}")
    print("6. HTML EXTRACTION COMPARISON — page.get_text('html')")
    print(SEPARATOR)

    html_o = page_orig.get_text("html")
    html_g = page_gen.get_text("html")

    print(f"  Original HTML length: {len(html_o)}")
    print(f"  Generated HTML length: {len(html_g)}")

    if html_o == html_g:
        print("  ✅ IDENTICAL")
        return

    print("  ❌ DIFFERENT")

    # Extract style info differences
    import re
    # Find all span style declarations
    style_o = re.findall(r'style="([^"]+)"', html_o)
    style_g = re.findall(r'style="([^"]+)"', html_g)

    print(f"  Style declarations: orig={len(style_o)}, gen={len(style_g)}")

    # Find font-family mentions
    fonts_o = set(re.findall(r'font-family:([^;"]+)', html_o))
    fonts_g = set(re.findall(r'font-family:([^;"]+)', html_g))
    if fonts_o != fonts_g:
        print(f"  ❌ FONT FAMILIES DIFFER:")
        print(f"    ORIG: {sorted(fonts_o)}")
        print(f"    GEN:  {sorted(fonts_g)}")
    else:
        print(f"  Fonts: {sorted(fonts_o)}")

    # Find font-size mentions
    sizes_o = sorted(set(re.findall(r'font-size:([^;"]+)', html_o)))
    sizes_g = sorted(set(re.findall(r'font-size:([^;"]+)', html_g)))
    if sizes_o != sizes_g:
        print(f"  ❌ FONT SIZES DIFFER:")
        print(f"    ORIG: {sizes_o}")
        print(f"    GEN:  {sizes_g}")
    else:
        print(f"  Font sizes: {sizes_o}")

    # Find color mentions
    colors_o = sorted(set(re.findall(r'color:#([0-9a-fA-F]+)', html_o)))
    colors_g = sorted(set(re.findall(r'color:#([0-9a-fA-F]+)', html_g)))
    if colors_o != colors_g:
        print(f"  ❌ COLORS DIFFER:")
        print(f"    ORIG: {colors_o}")
        print(f"    GEN:  {colors_g}")
    else:
        print(f"  Colors: {colors_o}")

    # Line-by-line diff of HTML (first 30 differing lines)
    lines_o = html_o.split('\n')
    lines_g = html_g.split('\n')
    print(f"\n  HTML line counts: orig={len(lines_o)}, gen={len(lines_g)}")

    diff_count = 0
    for i in range(max(len(lines_o), len(lines_g))):
        lo = lines_o[i] if i < len(lines_o) else "<MISSING>"
        lg = lines_g[i] if i < len(lines_g) else "<MISSING>"
        if lo != lg:
            diff_count += 1
            if diff_count <= 30:
                print(f"  HTML line {i}:")
                print(f"    ORIG: {lo[:200]}")
                print(f"    GEN:  {lg[:200]}")
    print(f"  Total differing HTML lines: {diff_count}")


# ─────────────────────────────────────────────────────────────
# 7. BLOCK GROUPING / BBOX ANALYSIS
# ─────────────────────────────────────────────────────────────
def compare_block_grouping(page_orig, page_gen):
    print(f"\n{SEPARATOR}")
    print("7. BLOCK GROUPING & BBOX ANALYSIS")
    print(SEPARATOR)

    d_orig = page_orig.get_text("dict")
    d_gen  = page_gen.get_text("dict")

    print("\n  ORIGINAL block structure:")
    for bi, b in enumerate(d_orig['blocks']):
        if b['type'] == 0:
            bbox = tuple(round(x, 1) for x in b['bbox'])
            lines_text = []
            for l in b.get('lines', []):
                span_texts = []
                for s in l.get('spans', []):
                    span_texts.append(f"[{s.get('font','?')} {s.get('size',0):.1f}pt #{s.get('color',0):06X}] '{s.get('text','')}'")
                lines_text.append(" + ".join(span_texts))
            print(f"  Block {bi} bbox={bbox}")
            for lt in lines_text:
                print(f"    {lt}")
        else:
            bbox = tuple(round(x, 1) for x in b['bbox'])
            print(f"  Block {bi} (IMAGE) bbox={bbox}")

    print("\n  GENERATED block structure:")
    for bi, b in enumerate(d_gen['blocks']):
        if b['type'] == 0:
            bbox = tuple(round(x, 1) for x in b['bbox'])
            lines_text = []
            for l in b.get('lines', []):
                span_texts = []
                for s in l.get('spans', []):
                    span_texts.append(f"[{s.get('font','?')} {s.get('size',0):.1f}pt #{s.get('color',0):06X}] '{s.get('text','')}'")
                lines_text.append(" + ".join(span_texts))
            print(f"  Block {bi} bbox={bbox}")
            for lt in lines_text:
                print(f"    {lt}")
        else:
            bbox = tuple(round(x, 1) for x in b['bbox'])
            print(f"  Block {bi} (IMAGE) bbox={bbox}")


# ─────────────────────────────────────────────────────────────
# 8. SPECIAL: "Сформирована" block check
# ─────────────────────────────────────────────────────────────
def check_sformirovana(page_orig, page_gen):
    print(f"\n{SEPARATOR}")
    print("8. 'Сформирована' BLOCK STRUCTURE CHECK")
    print(SEPARATOR)

    for label, page in [("ORIGINAL", page_orig), ("GENERATED", page_gen)]:
        d = page.get_text("dict")
        print(f"\n  {label}:")
        for bi, b in enumerate(d['blocks']):
            if b['type'] != 0:
                continue
            for l in b.get('lines', []):
                for s in l.get('spans', []):
                    if 'Сформирован' in s.get('text', '') or 'сформирован' in s.get('text', '').lower():
                        bbox = tuple(round(x, 1) for x in b['bbox'])
                        print(f"    Found in Block {bi}, bbox={bbox}")
                        print(f"    Block has {len(b.get('lines',[]))} lines:")
                        for li2, l2 in enumerate(b.get('lines', [])):
                            lbbox = tuple(round(x, 1) for x in l2['bbox'])
                            for si2, s2 in enumerate(l2.get('spans', [])):
                                sbbox = tuple(round(x, 1) for x in s2['bbox'])
                                print(f"      Line {li2} Span {si2}: font={s2.get('font')} "
                                      f"size={s2.get('size',0):.4f} color=#{s2.get('color',0):06X} "
                                      f"flags={s2.get('flags',0)} text={repr(s2.get('text',''))}")
                                print(f"        span_bbox={sbbox} line_bbox={lbbox}")


# ─────────────────────────────────────────────────────────────
# 9. NBSP / SPACE AUDIT
# ─────────────────────────────────────────────────────────────
def audit_spaces(page_orig, page_gen):
    print(f"\n{SEPARATOR}")
    print("9. NBSP / SPACE / WHITESPACE AUDIT")
    print(SEPARATOR)

    for label, page in [("ORIGINAL", page_orig), ("GENERATED", page_gen)]:
        d = page.get_text("rawdict")
        space_types = defaultdict(int)
        for b in d['blocks']:
            if b.get('type') != 0:
                continue
            for l in b.get('lines', []):
                for s in l.get('spans', []):
                    for c in s.get('chars', []):
                        ch = c.get('c', '')
                        if ch and ord(ch) in (0x20, 0xA0, 0x09, 0x2009, 0x2007, 0x2008, 0x202F, 0x200B):
                            space_types[f"U+{ord(ch):04X} ({char_repr(ch)})"] += 1

        print(f"\n  {label} whitespace chars:")
        for k, v in sorted(space_types.items()):
            print(f"    {k}: {v}")


# ─────────────────────────────────────────────────────────────
# 10. FONT SUMMARY
# ─────────────────────────────────────────────────────────────
def compare_fonts_summary(page_orig, page_gen):
    print(f"\n{SEPARATOR}")
    print("10. FONT SUMMARY COMPARISON")
    print(SEPARATOR)

    for label, page in [("ORIGINAL", page_orig), ("GENERATED", page_gen)]:
        d = page.get_text("dict")
        fonts = defaultdict(lambda: {"sizes": set(), "colors": set(), "flags": set(), "count": 0})
        for b in d['blocks']:
            if b.get('type') != 0:
                continue
            for l in b.get('lines', []):
                for s in l.get('spans', []):
                    fn = s.get('font', '?')
                    fonts[fn]['sizes'].add(round(s.get('size', 0), 4))
                    fonts[fn]['colors'].add(f"#{s.get('color', 0):06X}")
                    fonts[fn]['flags'].add(s.get('flags', 0))
                    fonts[fn]['count'] += 1

        print(f"\n  {label} fonts:")
        for fn in sorted(fonts.keys()):
            info = fonts[fn]
            print(f"    '{fn}': sizes={sorted(info['sizes'])}, colors={sorted(info['colors'])}, "
                  f"flags={sorted(info['flags'])}, spans={info['count']}")


# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────
def main():
    print("Opening PDFs...")
    doc_orig = fitz.open(ORIG)
    doc_gen  = fitz.open(GEN)

    print(f"  Original: {doc_orig.page_count} pages")
    print(f"  Generated: {doc_gen.page_count} pages")

    page_orig = doc_orig[0]
    page_gen  = doc_gen[0]

    print(f"  Original page size: {page_orig.rect}")
    print(f"  Generated page size: {page_gen.rect}")

    compare_full_text(page_orig, page_gen)
    compare_dict(page_orig, page_gen)
    compare_rawdict(page_orig, page_gen)
    compare_blocks(page_orig, page_gen)
    compare_words(page_orig, page_gen)
    compare_html(page_orig, page_gen)
    compare_block_grouping(page_orig, page_gen)
    check_sformirovana(page_orig, page_gen)
    audit_spaces(page_orig, page_gen)
    compare_fonts_summary(page_orig, page_gen)

    doc_orig.close()
    doc_gen.close()


if __name__ == "__main__":
    main()
