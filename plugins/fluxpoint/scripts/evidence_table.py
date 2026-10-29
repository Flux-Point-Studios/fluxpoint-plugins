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


def harness_only_change(before, after):
    """Accept STATUS replacement and new rows in real existing harness tables.

    Existing rows, graph text, prose and table structure remain byte-bound to
    HEAD. The writer inserts new rows below the separator, so insertions at
    either edge of the existing row run are legitimate state updates.
    """
    import difflib
    old, new = before.splitlines(keepends=True), after.splitlines(keepends=True)
    headers = HEADERS + ('| When (UTC) | Decision | Chosen | Overturned prior | Frozen by | Rationale |',)
    table_positions, status_lines, fence = {}, set(), None
    for i, line in enumerate(old):
        bare = line.rstrip('\r\n')
        marker = re.match(r' {0,3}(`{3,}|~{3,})', bare)
        if fence:
            if re.fullmatch(r' {0,3}' + re.escape(fence[0]) + '{' + str(fence[1]) + r',}\s*', bare):
                fence = None
            continue
        if marker:
            fence = (marker.group(1)[0], len(marker.group(1)))
            continue
        if re.match(r'^STATUS:[^\r\n]*$', bare):
            status_lines.add(i)
        if bare.strip() not in headers or i + 1 >= len(old):
            continue
        count = len(bare.strip().strip('|').split('|'))
        cells = old[i + 1].strip().strip('|').split('|')
        if len(cells) != count or not all(re.fullmatch(r':?-{3,}:?', c.strip()) for c in cells):
            continue
        end = i + 2
        while end < len(old) and old[end].lstrip().startswith('|'):
            end += 1
        for position in range(i + 2, end + 1):
            table_positions[position] = count
    for op, a, b, x, y in difflib.SequenceMatcher(a=old, b=new, autojunk=False).get_opcodes():
        if op == 'equal':
            continue
        if (op == 'replace' and b == a + 1 and y == x + 1 and a in status_lines
                and re.fullmatch(r'STATUS:[^\r\n]*(?:\r?\n)?', new[x])):
            continue
        if op == 'insert' and a in table_positions:
            count = table_positions[a]
            if all(line.strip().startswith('|') and line.strip().endswith('|')
                   and len(re.split(r'(?<!\\)\|', line.strip())[1:-1]) == count for line in new[x:y]):
                continue
        return False
    return True
