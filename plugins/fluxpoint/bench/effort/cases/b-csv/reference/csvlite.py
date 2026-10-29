def parse(text):
    rows, row, i, n, line = [], [], 0, len(text), 1
    if not text:
        return []
    while True:
        if i < n and text[i] == '"':
            start_line = line
            i += 1
            buf = []
            while True:
                if i >= n:
                    raise ValueError(f"unterminated quoted field opened on line {start_line}")
                ch = text[i]
                if ch == '"':
                    if i + 1 < n and text[i + 1] == '"':
                        buf.append('"')
                        i += 2
                        continue
                    i += 1
                    break
                if ch == "\n":
                    line += 1
                buf.append(ch)
                i += 1
            field = "".join(buf)
            if i < n and text[i] != "," and text[i] != "\n" and not text.startswith("\r\n", i):
                raise ValueError(f"unexpected character after closing quote on line {line}")
        else:
            j = i
            while j < n and text[j] != "," and text[j] != "\n" and not text.startswith("\r\n", j):
                j += 1
            field = text[i:j]
            i = j
        row.append(field)
        if i >= n:
            rows.append(row)
            return rows
        if text[i] == ",":
            i += 1
            continue
        i += 2 if text[i] == "\r" else 1
        line += 1
        rows.append(row)
        row = []
        if i >= n:
            return rows
