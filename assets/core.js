const MAX_IMPORT = 50;
const MAX_INPUT_BYTES = 20_000;
const ASSET_TYPES = new Set(['stock', 'fund', 'etf', 'lof']);
const TYPE_ALIASES = new Map([
  ['股票', 'stock'],
  ['a股', 'stock'],
  ['a 股', 'stock'],
  ['基金', 'fund'],
  ['公募基金', 'fund'],
  ['交易型开放式指数基金', 'etf'],
]);
const HEADER_ALIASES = new Map([
  ['code', 'code'], ['ticker', 'code'], ['代码', 'code'],
  ['name', 'name'], ['名称', 'name'],
  ['asset_type', 'asset_type'], ['type', 'asset_type'], ['类型', 'asset_type'],
  ['sector', 'sector'], ['板块', 'sector'],
  ['note', 'note'], ['备注', 'note'],
]);

function boundedText(text) {
  if (typeof text !== 'string') throw new TypeError('import payload must be text');
  if (new TextEncoder().encode(text).length > MAX_INPUT_BYTES) {
    throw new RangeError(`import payload exceeds ${MAX_INPUT_BYTES} bytes`);
  }
  return text.replace(/^\uFEFF/, '');
}

function clean(value) {
  if (value == null) return '';
  if (!['string', 'number'].includes(typeof value) || typeof value === 'boolean') {
    throw new TypeError('asset text fields must be scalar values');
  }
  return String(value)
    .split('')
    .filter((character) => character >= ' ' && character !== '\x7f')
    .join('')
    .trim()
    .replace(/\s+/g, ' ')
    .slice(0, 200);
}

function parseCsvLine(line) {
  const fields = [];
  let value = '';
  let quoted = false;
  let closedQuote = false;
  for (let index = 0; index < line.length; index += 1) {
    const character = line[index];
    if (quoted) {
      if (character === '"' && line[index + 1] === '"') {
        value += '"';
        index += 1;
      } else if (character === '"') {
        quoted = false;
        closedQuote = true;
      } else {
        value += character;
      }
    } else if (character === ',' && !quoted) {
      fields.push(value);
      value = '';
      closedQuote = false;
    } else if (character === '"' && value === '' && !closedQuote) {
      quoted = true;
    } else if (closedQuote && character.trim()) {
      throw new Error('malformed CSV quoting');
    } else {
      value += character;
    }
  }
  if (quoted) throw new Error('unterminated CSV quote');
  fields.push(value);
  return fields;
}

function parseCsvRecords(text) {
  const records = [];
  const invalid = [];
  let fields = [];
  let value = '';
  let quoted = false;
  let closedQuote = false;
  let physicalRow = 1;
  let recordRow = 1;

  const finishField = () => {
    fields.push(value);
    value = '';
    closedQuote = false;
  };
  const finishRecord = () => {
    finishField();
    records.push({ values: fields, row: recordRow });
    fields = [];
  };

  for (let index = 0; index < text.length; index += 1) {
    const character = text[index];
    const isCrLf = character === '\r' && text[index + 1] === '\n';
    const isNewline = character === '\n' || character === '\r';
    if (quoted) {
      if (character === '"' && text[index + 1] === '"') {
        value += '"';
        index += 1;
      } else if (character === '"') {
        quoted = false;
        closedQuote = true;
      } else if (isNewline) {
        value += '\n';
        if (isCrLf) index += 1;
        physicalRow += 1;
      } else {
        value += character;
      }
      continue;
    }

    if (closedQuote) {
      if (character === ',') {
        finishField();
      } else if (isNewline) {
        finishRecord();
        if (isCrLf) index += 1;
        physicalRow += 1;
        recordRow = physicalRow;
      } else if (!/\s/.test(character)) {
        throw new Error(`malformed CSV quoting at source row ${recordRow}`);
      }
      continue;
    }

    if (character === ',' ) {
      finishField();
    } else if (character === '"') {
      if (value) throw new Error(`malformed CSV quoting at source row ${recordRow}`);
      quoted = true;
    } else if (isNewline) {
      finishRecord();
      if (isCrLf) index += 1;
      physicalRow += 1;
      recordRow = physicalRow;
    } else {
      value += character;
    }
  }

  if (quoted) {
    invalid.push({ row: recordRow, input: [...fields, value].join(','), reason: 'unterminated CSV quote' });
  } else if (fields.length || value || closedQuote) {
    finishRecord();
  }
  return { records, invalid };
}

function expectedExchange(code) {
  if (/^(600|601|603|605|688|689|900)/.test(code)) return 'SH';
  if (/^(000|001|002|003|200|300|301)/.test(code)) return 'SZ';
  if (/^[48]/.test(code)) return 'BJ';
  return '';
}

function normalizeType(value, code) {
  let assetType = clean(value).toLowerCase();
  assetType = TYPE_ALIASES.get(assetType) || assetType;
  if (!assetType) {
    if (/^\d{6}(?:\.(?:SH|SZ|BJ))?$/i.test(code)) {
      throw new Error('six-digit CN codes are ambiguous; asset_type is required');
    }
    assetType = 'stock';
  }
  if (!ASSET_TYPES.has(assetType)) throw new Error(`unsupported asset type: ${assetType}`);
  return assetType;
}

function normalizeCode(value, assetType) {
  const code = clean(value).toUpperCase();
  if (!code) throw new Error('asset code is required');
  if (/\s/.test(code)) throw new Error('invalid code: whitespace is not allowed');
  if (assetType !== 'stock') {
    if (!/^\d{6}$/.test(code)) throw new Error(`invalid ${assetType} code: ${code}`);
    return code;
  }
  const cnMatch = code.match(/^(\d{6})(?:\.(SH|SZ|BJ))?$/);
  if (cnMatch) {
    const exchange = expectedExchange(cnMatch[1]);
    if (!exchange) throw new Error(`invalid code: ${code}`);
    if (cnMatch[2] && cnMatch[2] !== exchange) {
      throw new Error(`${code} does not match its CN equity exchange ${exchange}`);
    }
    return `${cnMatch[1]}.${exchange}`;
  }
  if (/^\d{5}\.HK$/.test(code) || /^[A-Z][A-Z0-9.-]{0,9}$/.test(code)) return code;
  throw new Error(`invalid code: ${code}`);
}

function normalizeEnabled(value) {
  if (value == null || value === '') return true;
  if (typeof value === 'boolean') return value;
  const normalized = clean(value).toLowerCase();
  if (['true', '1', 'yes'].includes(normalized)) return true;
  if (['false', '0', 'no'].includes(normalized)) return false;
  throw new Error('enabled must be a boolean');
}

function normalizeAsset(raw) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) throw new TypeError('asset must be an object');
  const rawCode = raw.code ?? raw.ticker ?? raw['代码'];
  const preliminaryCode = clean(rawCode).toUpperCase();
  const assetType = normalizeType(raw.asset_type ?? raw.type ?? raw['类型'], preliminaryCode);
  const code = normalizeCode(preliminaryCode, assetType);
  const market = assetType !== 'stock' || /\.(SH|SZ|BJ)$/.test(code) ? 'CN' : code.endsWith('.HK') ? 'HK' : 'US';
  const slug = code.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
  return {
    id: `${assetType}-${market.toLowerCase()}-${slug}`,
    code,
    name: clean(raw.name ?? raw['名称']),
    asset_type: assetType,
    market,
    sector: clean(raw.sector ?? raw['板块']),
    note: clean(raw.note ?? raw['备注']),
    enabled: normalizeEnabled(raw.enabled),
  };
}

function resultWithReport(assets, invalid, duplicates, totalRows) {
  Object.defineProperties(assets, {
    invalid: { value: invalid, enumerable: false, configurable: true },
    duplicates: { value: duplicates, enumerable: false, configurable: true },
    totalRows: { value: totalRows, enumerable: false, configurable: true },
  });
  return assets;
}

function normalizeRows(rows) {
  if (rows.length > MAX_IMPORT) throw new RangeError(`an import may contain at most ${MAX_IMPORT} assets`);
  const assets = [];
  const invalid = [];
  const duplicates = [];
  const seen = new Set();
  for (const entry of rows) {
    try {
      const asset = normalizeAsset(entry.value);
      if (seen.has(asset.id)) {
        duplicates.push({ row: entry.row, code: asset.code, id: asset.id });
      } else {
        seen.add(asset.id);
        Object.defineProperty(asset, 'sourceRow', { value: entry.row, enumerable: false });
        assets.push(asset);
      }
    } catch (error) {
      invalid.push({ row: entry.row, input: entry.input, reason: error instanceof Error ? error.message : String(error) });
    }
  }
  return resultWithReport(assets, invalid, duplicates, rows.length);
}

export function parseImportText(text) {
  const lines = boundedText(text).split(/\r?\n/);
  const rows = [];
  lines.forEach((line, index) => {
    if (!line.trim()) return;
    try {
      let values;
      if (line.includes(',')) values = parseCsvLine(line);
      else if (line.includes('\t')) values = line.split('\t');
      else values = line.trim().split(/\s+/);
      if (values.length > 5) throw new Error('text rows may contain at most five columns');
      const [code = '', name = '', assetType = '', sector = '', note = ''] = values;
      rows.push({ row: index + 1, input: line, value: { code, name, asset_type: assetType, sector, note } });
    } catch (error) {
      rows.push({ row: index + 1, input: line, value: { code: '', asset_type: '' }, parseError: error });
    }
  });
  if (rows.length > MAX_IMPORT) throw new RangeError(`an import may contain at most ${MAX_IMPORT} assets`);
  const parsed = normalizeRows(rows.filter((row) => !row.parseError));
  const parseInvalid = rows.filter((row) => row.parseError).map((row) => ({ row: row.row, input: row.input, reason: row.parseError.message }));
  return resultWithReport(parsed, [...parsed.invalid, ...parseInvalid], parsed.duplicates, rows.length);
}

export function parseImportCsv(text) {
  const parsedCsv = parseCsvRecords(boundedText(text));
  const headerIndex = parsedCsv.records.findIndex((record) => record.values.some((value) => value.trim()));
  if (headerIndex < 0) throw new Error('CSV import requires a header row');
  const rawHeaders = parsedCsv.records[headerIndex].values;
  const headers = rawHeaders.map((header) => HEADER_ALIASES.get(clean(header).toLowerCase()) || '');
  if (!headers.includes('code')) throw new Error('CSV import requires a code header');
  const rows = [];
  for (const record of parsedCsv.records.slice(headerIndex + 1)) {
    if (!record.values.some((value) => value.trim())) continue;
    try {
      const values = record.values;
      if (values.length !== headers.length) throw new Error('CSV row does not match header column count');
      const value = {};
      headers.forEach((header, column) => { if (header) value[header] = values[column]; });
      rows.push({ row: record.row, input: values.join(','), value });
    } catch (error) {
      rows.push({ row: record.row, input: record.values.join(','), value: { code: '', asset_type: '' }, parseError: error });
    }
  }
  parsedCsv.invalid.forEach((item) => rows.push({ ...item, value: { code: '', asset_type: '' }, parseError: new Error(item.reason) }));
  if (rows.length > MAX_IMPORT) throw new RangeError(`an import may contain at most ${MAX_IMPORT} assets`);
  const parsed = normalizeRows(rows.filter((row) => !row.parseError));
  const parseInvalid = rows.filter((row) => row.parseError).map((row) => ({ row: row.row, input: row.input, reason: row.parseError.message }));
  return resultWithReport(parsed, [...parsed.invalid, ...parseInvalid], parsed.duplicates, rows.length);
}

export function buildIssueUrl(assets) {
  if (!Array.isArray(assets) || !assets.length) throw new Error('import contains no assets');
  if (assets.length > MAX_IMPORT) throw new RangeError(`an import may contain at most ${MAX_IMPORT} assets`);
  const normalized = assets.map(normalizeAsset);
  const ids = new Set();
  for (const asset of normalized) {
    if (ids.has(asset.id)) throw new Error(`duplicate asset: ${asset.code}`);
    ids.add(asset.id);
  }
  const payload = JSON.stringify({ version: 1, mode: 'merge', assets: normalized });
  const body = `请确认以下批量导入。\n\n<!-- WATCHLIST_IMPORT_V1\n${payload}\n-->`;
  const url = new URL('https://github.com/NORIX521/fund-monitor-025209/issues/new');
  url.searchParams.set('title', `[watchlist-import] ${normalized.length} assets`);
  url.searchParams.set('body', body);
  return url.toString();
}

export function filterAssets(assets, state = {}) {
  if (!Array.isArray(assets)) return [];
  const filters = typeof state === 'string' ? { recommendation: state } : state || {};
  const query = clean(filters.query).toLocaleLowerCase();
  const assetType = clean(filters.assetType ?? filters.asset_type ?? filters.type).toLowerCase();
  const recommendation = clean(filters.recommendation ?? filters.state ?? filters.status);
  const freshness = clean(filters.freshness).toLowerCase();
  return assets.filter((asset) => {
    const haystack = [asset?.code, asset?.name, asset?.sector, asset?.market].map((value) => clean(value).toLocaleLowerCase()).join(' ');
    if (query && !haystack.includes(query)) return false;
    if (assetType && assetType !== 'all' && clean(asset?.asset_type).toLowerCase() !== assetType) return false;
    if (recommendation && recommendation !== 'all' && clean(asset?.state) !== recommendation) return false;
    if (freshness === 'stale' && asset?.stale !== true) return false;
    if (freshness === 'fresh' && asset?.stale === true) return false;
    if (typeof filters.stale === 'boolean' && asset?.stale !== filters.stale) return false;
    return true;
  });
}

export function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>'"]/g, (character) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;',
  })[character]);
}
