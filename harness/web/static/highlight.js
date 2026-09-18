// Tiny dependency-free syntax highlighter.
// Works on RAW text: every token (highlighted or not) is HTML-escaped here, so the
// output is safe to assign to innerHTML. One combined regex per language; at any
// position the earliest-listed rule wins, so strings/comments swallow keywords.

const MAX_CHARS = 200_000;

export function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

const words = (list) => new RegExp(`\\b(?:${list.trim().split(/\s+/).join("|")})\\b`);

// Shared token rules (non-capturing groups only — group numbers map to rules).
const T = {
  dq: /"(?:[^"\\\n]|\\.)*"/,
  sq: /'(?:[^'\\\n]|\\.)*'/,
  bt: /`(?:[^`\\]|\\.)*`/,
  pyTriple: /"""[\s\S]*?"""|'''[\s\S]*?'''/,
  num: /\b(?:0[xX][\da-fA-F_]+|0[bB][01_]+|\d[\d_]*(?:\.\d[\d_]*)?(?:[eE][+-]?\d+)?)[a-zA-Z]{0,3}\b/,
  slashCom: /\/\/.*/,
  blockCom: /\/\*[\s\S]*?\*\//,
  hashCom: /#.*/,
  dashCom: /--.*/,
  fn: /\b[A-Za-z_$][\w$]*(?=\s*\()/,
  decorator: /@[\w.]+/,
};

const KW = {
  python: words(`and as assert async await break class continue def del elif else except finally for from
    global if import in is lambda nonlocal not or pass raise return try while with yield match case
    None True False self`),
  js: words(`break case catch class const continue debugger default delete do else export extends finally
    for function if import in instanceof let new of return super switch this throw try typeof var void
    while with yield async await static get set null undefined true false
    interface type enum implements private protected public readonly abstract declare namespace keyof as`),
  java: words(`abstract assert boolean break byte case catch char class const continue default do double else
    enum extends final finally float for goto if implements import instanceof int interface long native
    new package private protected public return short static strictfp super switch synchronized this
    throw throws transient try void volatile while var record null true false`),
  kotlin: words(`as break class continue do else false for fun if in interface is null object package return
    super this throw true try typealias val var when while by catch constructor data enum
    finally import init internal lateinit open override private protected public sealed suspend companion`),
  c: words(`auto break case char const continue default do double else enum extern float for goto if inline
    int long register restrict return short signed sizeof static struct switch typedef union unsigned void
    volatile while bool true false nullptr NULL class namespace template typename public private protected
    virtual override new delete this using try catch throw const_cast static_cast dynamic_cast auto constexpr`),
  rust: words(`as async await break const continue crate dyn else enum extern false fn for if impl in let loop
    match mod move mut pub ref return self Self static struct super trait true type unsafe use where while
    Some None Ok Err`),
  go: words(`break case chan const continue default defer else fallthrough for func go goto if import
    interface map package range return select struct switch type var nil true false`),
  sql: /\b(?:select|from|where|and|or|not|insert|into|values|update|set|delete|create|table|drop|alter|add|index|join|left|right|inner|outer|full|on|as|group|by|order|having|limit|offset|distinct|union|all|null|is|in|like|between|exists|case|when|then|else|end|primary|key|foreign|references|default|with|returning|asc|desc|count|sum|avg|min|max|true|false)\b/i,
  sh: words(`if then else elif fi for while until do done case esac function in select return exit
    local export readonly declare source alias set unset shift break continue`),
  lit: words(`true false null yes no on off`),
};

const LANGS = {
  python: [[T.pyTriple, "str"], [T.hashCom, "com"], [T.dq, "str"], [T.sq, "str"], [T.decorator, "fn"],
           [KW.python, "kw"], [T.fn, "fn"], [T.num, "num"]],
  js:     [[T.slashCom, "com"], [T.blockCom, "com"], [T.dq, "str"], [T.sq, "str"], [T.bt, "str"],
           [KW.js, "kw"], [T.fn, "fn"], [T.num, "num"]],
  java:   [[T.slashCom, "com"], [T.blockCom, "com"], [T.dq, "str"], [T.sq, "str"], [T.decorator, "fn"],
           [KW.java, "kw"], [T.fn, "fn"], [T.num, "num"]],
  kotlin: [[T.slashCom, "com"], [T.blockCom, "com"], [T.dq, "str"], [T.sq, "str"], [T.decorator, "fn"],
           [KW.kotlin, "kw"], [T.fn, "fn"], [T.num, "num"]],
  c:      [[T.slashCom, "com"], [T.blockCom, "com"], [/^[ \t]*#[ \t]*\w+/m, "kw"], [T.dq, "str"], [T.sq, "str"],
           [KW.c, "kw"], [T.fn, "fn"], [T.num, "num"]],
  rust:   [[T.slashCom, "com"], [T.blockCom, "com"], [T.dq, "str"], [/'(?:[^'\\\n]|\\.)'/, "str"],
           [/\b[a-z_]\w*!/, "fn"], [KW.rust, "kw"], [T.fn, "fn"], [T.num, "num"]],
  go:     [[T.slashCom, "com"], [T.blockCom, "com"], [T.dq, "str"], [T.bt, "str"], [T.sq, "str"],
           [KW.go, "kw"], [T.fn, "fn"], [T.num, "num"]],
  sql:    [[T.dashCom, "com"], [T.blockCom, "com"], [T.sq, "str"], [T.dq, "str"], [KW.sql, "kw"], [T.num, "num"]],
  sh:     [[T.hashCom, "com"], [T.dq, "str"], [T.sq, "str"], [/\$\{[^}\n]*\}|\$\w+|\$[@#?$!*0-9]/, "var"],
           [KW.sh, "kw"], [/(?<=\s)--?[\w-]+/, "attr"], [T.num, "num"]],
  json:   [[/"(?:[^"\\\n]|\\.)*"(?=\s*:)/, "key"], [T.dq, "str"], [KW.lit, "kw"], [/-?\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\b/, "num"]],
  yaml:   [[T.hashCom, "com"], [/^[ \t]*(?:- )?[\w.\-"' ]+?(?=:(?:\s|$))/m, "key"], [T.dq, "str"], [T.sq, "str"],
           [KW.lit, "kw"], [T.num, "num"]],
  toml:   [[T.hashCom, "com"], [/^[ \t]*\[[^\]\n]*\]/m, "tag"], [/^[ \t]*[\w.\-"]+(?=\s*=)/m, "key"],
           [T.dq, "str"], [T.sq, "str"], [KW.lit, "kw"], [T.num, "num"]],
  xml:    [[/<!--[\s\S]*?-->/, "com"], [/<!\[CDATA\[[\s\S]*?\]\]>/, "str"], [/<\/?[\w:.-]+|\/?>/, "tag"],
           [/\b[\w:.-]+(?==)/, "attr"], [T.dq, "str"], [T.sq, "str"]],
  css:    [[T.blockCom, "com"], [T.dq, "str"], [T.sq, "str"], [/@[\w-]+/, "kw"], [/#[\da-fA-F]{3,8}\b/, "num"],
           [/[\w-]+(?=\s*:[^:])/, "key"], [/-?\b\d+(?:\.\d+)?(?:px|em|rem|%|vh|vw|s|ms|deg|fr)?\b/, "num"]],
  diff:   [[/^@@.*$/m, "tag"], [/^\+.*$/m, "add"], [/^-.*$/m, "del"], [/^(?:diff|index) .*$/m, "com"]],
  md:     [[/^#{1,6} .*$/m, "kw"], [/`[^`\n]+`/, "str"], [/\*\*[^*\n]+\*\*/, "fn"]],
};

const ALIASES = {
  py: "python", python3: "python", py3: "python",
  javascript: "js", jsx: "js", mjs: "js", cjs: "js", ts: "js", typescript: "js", tsx: "js",
  node: "js", vue: "xml",
  bash: "sh", zsh: "sh", shell: "sh", console: "sh", shellsession: "sh", ksh: "sh",
  jsonc: "json", json5: "json", geojson: "json", ipynb: "json",
  yml: "yaml", ini: "toml", cfg: "toml", conf: "toml",
  html: "xml", htm: "xml", svg: "xml", xhtml: "xml", plist: "xml",
  h: "c", cpp: "c", cc: "c", cxx: "c", hpp: "c", hh: "c", "c++": "c", objc: "c", m: "c", cs: "java", csharp: "java",
  kt: "kotlin", kts: "kotlin", scala: "java", groovy: "java", gradle: "java", dart: "java",
  rs: "rust", golang: "go", scss: "css", less: "css", sass: "css",
  patch: "diff", markdown: "md",
  psql: "sql", mysql: "sql", sqlite: "sql", plsql: "sql",
};

const compiled = {};
function compile(name) {
  if (compiled[name]) return compiled[name];
  const rules = LANGS[name];
  const flags = new Set(["g"]);
  for (const [re] of rules) for (const f of re.flags) if ("mi".includes(f)) flags.add(f);
  const re = new RegExp(rules.map(([r]) => `(${r.source})`).join("|"), [...flags].join(""));
  return (compiled[name] = { re, classes: rules.map(([, c]) => c) });
}

export function resolveLang(lang) {
  const l = String(lang || "").toLowerCase().trim();
  if (LANGS[l]) return l;
  return ALIASES[l] || null;
}

/** Language for a file path, from its extension or well-known name. */
export function langFromPath(path) {
  const name = String(path).split("/").pop().toLowerCase();
  if (/^(dockerfile|makefile|\.?bashrc|\.?zshrc|\.?profile)$/.test(name)) return "sh";
  const ext = name.includes(".") ? name.split(".").pop() : "";
  return resolveLang(ext);
}

function guess(code) {
  const t = code.trim();
  if (/^[[{]/.test(t)) {
    try { JSON.parse(t); return "json"; } catch { /* not JSON */ }
  }
  if (/^\$ \S/m.test(t)) return "sh";
  return null;
}

/** Highlight `code` as `lang` (name, alias or empty). Returns escaped HTML. */
export function highlight(code, lang) {
  code = String(code ?? "");
  const name = resolveLang(lang) || (lang ? null : guess(code));
  if (!name || code.length > MAX_CHARS) return esc(code);
  const { re, classes } = compile(name);
  let out = "", last = 0;
  re.lastIndex = 0;
  for (const m of code.matchAll(re)) {
    if (!m[0]) continue;
    let g = 1;
    while (m[g] === undefined) g++;
    out += esc(code.slice(last, m.index)) + `<span class="tk-${classes[g - 1]}">${esc(m[0])}</span>`;
    last = m.index + m[0].length;
  }
  return out + esc(code.slice(last));
}
