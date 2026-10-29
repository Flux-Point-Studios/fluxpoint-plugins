# parse(text) -> list[list[str]]

Parse CSV text into rows of fields. Do not use the standard `csv` module.

- Fields are separated by `,`. Records end at `\n` or `\r\n`. A lone `\r`
  outside quotes is an ordinary character.
- A single trailing record terminator does not create an empty last row.
  `""` (empty text) parses to `[]`. A blank line in the middle is a row
  with one empty field: `[""]`.
- A field that starts with `"` is quoted: it runs to the matching closing
  quote, `""` inside it is one literal `"`, and commas, `\r` and `\n` inside
  it are literal. After the closing quote the next character must be `,`,
  a record terminator or the end of text; anything else raises
  `ValueError` with the 1-based line number of that character in the
  message as `line N`.
- A `"` that appears inside an unquoted field is literal (`a"b` is `a"b`).
- An unterminated quoted field raises `ValueError` mentioning `line N`,
  where N is the line on which the quoted field opened.
- Rows may have different lengths; return them as parsed.
