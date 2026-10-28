"""Locate and append real Markdown Evidence tables, excluding code examples."""
import re

ROW_HDR = '| When (UTC) | Source | Outcome | Claim | Proof |'
LEGACY_HDR = '| When (UTC) | runId | Outcome | Nodes OK/dead | Findings | Harness | Red-team |'
HEADERS = (ROW_HDR, LEGACY_HDR)


def locate(text, headers=HEADERS):
    lines = text.splitlines(keepends=True)
    visible, fence = [], None
    for line in lines:
        bare = line.rstrip('\r\n')
        if fence:
            visible.append(False)
            if re.fullmatch(r' {0,3}' + re.escape(fence[0]) + '{' + str(fence[1]) + r',}\s*', bare):
                fence = None
            continue
        match = re.match(r' {0,3}(`{3,}|~{3,})', bare)
        if match:
            fence = (match.group(1)[0], len(match.group(1)))
            visible.append(False)
        else:
            visible.append(not bare.startswith(('    ', '\t')))
    begin, end = 0, len(lines)
    for i, line in enumerate(lines):
        heading = re.match(r' {0,3}(#{1,6})\s+(.+?)\s*#*\s*$', line.rstrip('\r\n')) if visible[i] else None
        if heading and heading.group(2).strip().casefold() == 'evidence':
            begin = i + 1
            for j in range(begin, len(lines)):
                next_heading = re.match(r' {0,3}(#{1,6})\s+', lines[j]) if visible[j] else None
                if next_heading and len(next_heading.group(1)) <= len(heading.group(1)):
                    end = j
                    break
            break
    seen_header = False
    offset = 0
    offsets = []
    for line in lines:
        offsets.append(offset)
        offset += len(line)
    for i in range(begin, end):
        if not visible[i] or lines[i].strip() not in headers:
            continue
        seen_header = True
        if i + 1 >= end or not visible[i + 1]:
            continue
        cells = lines[i + 1].strip().strip('|').split('|')
        wanted = len(lines[i].strip().strip('|').split('|'))
        if len(cells) != wanted or not all(re.fullmatch(r':?-{3,}:?', c.strip()) for c in cells):
            continue
        return {'header': lines[i].strip(), 'insert': offsets[i + 1] + len(lines[i + 1])}, 'ok'
    return None, 'no-separator' if seen_header else 'no-header'


def append(text, rows):
    table, status = locate(text, tuple(rows))
    if table is None:
        return text, status, None
    pos = table['insert']
    prefix = '' if pos and text[pos - 1] == '\n' else '\n'
    return text[:pos] + prefix + rows[table['header']] + '\n' + text[pos:], 'ok', table['header']
