export function parseTimestamp(value) {
  if (typeof value !== "string" || value.trim().length === 0) return null;
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,3}))?(Z|([+-])(\d{2}):(\d{2}))$/.exec(value);
  if (!match) return null;
  const [, y, mo, d, h, mi, s, , zone, , oh, om] = match;
  const year = Number(y);
  const month = Number(mo);
  const day = Number(d);
  const hour = Number(h);
  const minute = Number(mi);
  const second = Number(s);
  if (year < 1 || month < 1 || month > 12 || hour > 23 || minute > 59 || second > 59) return null;
  if (day < 1 || day > new Date(Date.UTC(year, month, 0)).getUTCDate()) return null;
  if (zone !== "Z") {
    const offsetHour = Number(oh);
    const offsetMinute = Number(om);
    if (offsetHour > 14 || offsetMinute > 59 || (offsetHour === 14 && offsetMinute !== 0)) return null;
  }
  const milliseconds = Date.parse(value);
  return Number.isNaN(milliseconds) ? null : milliseconds;
}

export function indexDeliverables(value) {
  if (!Array.isArray(value)) throw new Error('needs a "deliverables" array');
  const indexed = new Map();
  for (const item of value) {
    if (!item || typeof item !== "object" || Array.isArray(item) || typeof item.id !== "string" || !item.id.trim()) {
      throw new Error('each deliverable needs a non-empty string "id"');
    }
    if (indexed.has(item.id)) throw new Error(`repeats deliverable id ${item.id}`);
    indexed.set(item.id, item);
  }
  return indexed;
}

export function parseJsonNoDuplicateKeys(raw, label = "JSON") {
  let index = 0;
  const cleanKey = (value) => String(value).replace(/[\u0000-\u001f\u007f-\u009f\u2028\u2029]/g, " ").slice(0, 220);
  const skipWhitespace = () => {
    while (/\s/.test(raw[index] ?? "")) index += 1;
  };
  const parseString = () => {
    if (raw[index] !== '"') throw new Error(`${label} is not valid JSON`);
    const start = index;
    index += 1;
    while (index < raw.length) {
      const character = raw[index];
      if (character === '"') {
        index += 1;
        return JSON.parse(raw.slice(start, index));
      }
      if (character === "\\") index += 2;
      else index += 1;
    }
    throw new Error(`${label} is not valid JSON`);
  };
  const parseValue = () => {
    skipWhitespace();
    if (raw[index] === "{") {
      index += 1;
      skipWhitespace();
      const keys = new Set();
      if (raw[index] === "}") {
        index += 1;
        return;
      }
      while (index < raw.length) {
        skipWhitespace();
        const key = parseString();
        if (keys.has(key)) throw new Error(`${label} contains duplicate JSON key ${cleanKey(key)}`);
        keys.add(key);
        skipWhitespace();
        if (raw[index] !== ":") throw new Error(`${label} is not valid JSON`);
        index += 1;
        parseValue();
        skipWhitespace();
        if (raw[index] === "}") {
          index += 1;
          return;
        }
        if (raw[index] !== ",") throw new Error(`${label} is not valid JSON`);
        index += 1;
      }
      throw new Error(`${label} is not valid JSON`);
    }
    if (raw[index] === "[") {
      index += 1;
      skipWhitespace();
      if (raw[index] === "]") {
        index += 1;
        return;
      }
      while (index < raw.length) {
        parseValue();
        skipWhitespace();
        if (raw[index] === "]") {
          index += 1;
          return;
        }
        if (raw[index] !== ",") throw new Error(`${label} is not valid JSON`);
        index += 1;
      }
      throw new Error(`${label} is not valid JSON`);
    }
    if (raw[index] === '"') {
      parseString();
      return;
    }
    const token = /^(?:true|false|null|-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?)/.exec(raw.slice(index));
    if (!token) throw new Error(`${label} is not valid JSON`);
    index += token[0].length;
  };
  parseValue();
  skipWhitespace();
  if (index !== raw.length) throw new Error(`${label} is not valid JSON`);
  return JSON.parse(raw);
}
