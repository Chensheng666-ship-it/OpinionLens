// =============================================================================
// tests/js/test_log_redos.js
//
// The collection-task page parses crawler log lines on the renderer thread
// (app/desktop/renderer/tasks.js mergeLogResponse -> parseTechnicalLog ->
// pickLogField). pickLogField compiles
//
//   'field':\s*'((?:\\.|[^'])*)'
//   "field":\s*"((?:\\.|[^"])*)"
//
// Both alternatives inside the repeated group match a backslash, so a run of
// backslashes with no closing quote makes V8 backtrack exponentially and
// freezes the window.
//
// These tests build the log line the way the shipped crawler does, then call
// the real mergeLogResponse. They fail while that pattern is still ambiguous
// and pass once a poisoned line is parsed without blocking.
//
// Run:
//   node tests/js/test_log_redos.js
//
// Covered store lines (same renderer function):
//   - Xiaohongshu comment and note: full dict interpolated into the log
//     (store/xhs/__init__.py update_xhs_note_comment / update_xhs_note),
//     which is Python repr, so each backslash in the post/comment is doubled
//     and the double-quote pattern is the one that runs.
//   - Douyin comment and Bilibili title: the text is interpolated with str(),
//     not repr (store/douyin/__init__.py update_dy_aweme_comment,
//     store/bilibili/__init__.py update_bilibili_video). The single-quote
//     pattern and the double-quote pattern are each reachable this way.
//
// Same pickLogField, not given a separate case: Kuaishou's batch comment log
// and the Tieba/Zhihu full-dict logs. Weibo comments are sliced to 24
// characters before they are logged, so a long backslash run never arrives.
// =============================================================================
'use strict';

const assert = require('node:assert');
const { spawn, spawnSync } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const ROOT = path.join(__dirname, '..', '..');
const TASKS_JS = path.join(ROOT, 'app', 'desktop', 'renderer', 'tasks.js');
const LIMIT_MS = 1000;

const PYTHON = `
import json, logging, io, sys

# Same format string as MediaCrawler tools/utils.py init_loging_config
# (pinned commit b059657a872614bc0cb887af801b753ff4587d12).
def format_log(message):
    logger = logging.getLogger("MediaCrawler")
    logger.handlers.clear()
    logger.setLevel(logging.INFO)
    logger.propagate = False
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(name)s %(levelname)s (%(filename)s:%(lineno)d) - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    logger.addHandler(handler)
    logger.info(message)
    return buf.getvalue().rstrip("\\n")

def decode_comment(content):
    # httpx 0.28.1 Response.json is json.loads of the body. The crawler then
    # passes comments through to the store callback unchanged.
    raw = json.dumps({
        "success": True,
        "data": {"comments": [{
            "id": "c1",
            "content": content,
            "user_info": {"user_id": "u1", "nickname": "nick"},
        }]},
    })
    return json.loads(raw)["data"]["comments"][0]

def xhs_comment(content):
    # store/xhs/__init__.py update_xhs_note_comment. OpinionLens replaces the
    # hash/mask helpers with identity, so nickname stays the published name.
    item = decode_comment(content)
    user = item.get("user_info") or {}
    local = {
        "comment_id": item.get("id"),
        "create_time": 1,
        "note_id": "n1",
        "content": item.get("content"),
        "creator_hash": "" if user.get("user_id") is None else str(user.get("user_id")),
        "nickname": "" if user.get("nickname") is None else str(user.get("nickname")),
        "sub_comment_count": 0,
        "pictures": "",
        "parent_comment_id": "",
        "last_modify_ts": 1,
        "like_count": 0,
    }
    return format_log("[store.xhs.update_xhs_note_comment] xhs note comment:" + str(local))

def xhs_note(desc):
    # store/xhs/__init__.py update_xhs_note. desc is the note body, verbatim.
    local = {
        "note_id": "n1",
        "type": "normal",
        "title": "正常标题",
        "desc": desc,
        "video_url": "",
        "time": 1,
        "last_update_time": 0,
        "creator_hash": "u1",
        "nickname": "nick",
        "liked_count": "1",
        "collected_count": "0",
        "comment_count": "0",
        "share_count": "0",
        "image_list": "",
        "tag_list": "",
        "last_modify_ts": 1,
        "note_url": "https://www.xiaohongshu.com/explore/n1",
        "source_keyword": "brand",
        "xsec_token": "tok",
    }
    return format_log("[store.xhs.update_xhs_note] xhs note:" + str(local))

def douyin_comment(content):
    # store/douyin/__init__.py update_dy_aweme_comment interpolates with str(),
    # so backslashes are not doubled and quotes are not escaped.
    return format_log(
        "[store.douyin.update_dy_aweme_comment] douyin aweme comment: c1, content: " + str(content)
    )

def bilibili_title(title):
    # store/bilibili/__init__.py update_bilibili_video, title via str().
    return format_log(
        "[store.bilibili.update_bilibili_video] bilibili video id:v1, title:" + str(title)
    )

def main():
    spec = json.load(sys.stdin)
    out = {}
    for name, job in spec.items():
        kind = job["kind"]
        text = job["text"]
        if kind == "xhs_comment":
            line = xhs_comment(text)
        elif kind == "xhs_note":
            line = xhs_note(text)
        elif kind == "douyin_comment":
            line = douyin_comment(text)
        elif kind == "bilibili_title":
            line = bilibili_title(text)
        else:
            raise SystemExit("unknown kind " + kind)
        if job.get("prefix"):
            line = job["prefix"] + line
        out[name] = line
    json.dump(out, sys.stdout)
main()
`;

function buildLines(spec) {
  const run = spawnSync('python3', ['-c', PYTHON], {
    input: JSON.stringify(spec),
    encoding: 'utf8',
    maxBuffer: 8 * 1024 * 1024,
  });
  if (run.status !== 0) {
    throw new Error('python log builder failed: ' + (run.stderr || run.stdout));
  }
  return JSON.parse(run.stdout);
}

function loadTasks(context) {
  const src = fs.readFileSync(TASKS_JS, 'utf8');
  if (!/\}\)\(\);\s*$/.test(src)) {
    throw new Error('tasks.js no longer ends in the page IIFE');
  }
  const hooked = src.replace(
    /\}\)\(\);\s*$/,
    '  window.__logParse = { mergeLogResponse: mergeLogResponse, entries: function () { return state.logEntries; } };\n})();\n'
  );
  vm.runInContext(hooked, context, { filename: TASKS_JS });
}

function parseLine(line) {
  const context = {
    console,
    setTimeout,
    clearTimeout,
  };
  context.window = context;
  context.localStorage = {
    getItem: function () { return null; },
    setItem: function () {},
  };
  context.FS = {
    PLATFORMS: [
      { id: 'xhs', name: '小红书' },
      { id: 'dy', name: '抖音' },
      { id: 'ks', name: '快手' },
      { id: 'bili', name: 'B站' },
      { id: 'wb', name: '微博' },
      { id: 'tieba', name: '贴吧' },
      { id: 'zhihu', name: '知乎' },
    ],
    parseTime: function (value) {
      const date = new Date(value);
      return Number.isNaN(date.getTime()) ? null : date;
    },
    platformLabel: function (id) { return id; },
    timers: { after: function () { return 0; }, clear: function () {} },
    views: {},
  };
  vm.createContext(context);
  loadTasks(context);
  const started = process.hrtime.bigint();
  const accepted = context.__logParse.mergeLogResponse(
    { lines: [{ ts: 0, level: 'info', text: line }], next_line: 1 },
    false
  );
  const ms = Number(process.hrtime.bigint() - started) / 1e6;
  const entries = context.__logParse.entries() || [];
  return {
    ms,
    accepted,
    entries: entries.map(function (entry) {
      return { kind: entry.kind, text: entry.text, level: entry.level };
    }),
  };
}

function runWorker(line) {
  return new Promise((resolve) => {
    const child = spawn(process.execPath, [__filename, '--worker'], { stdio: ['pipe', 'pipe', 'pipe'] });
    let stdout = '';
    let stderr = '';
    let settled = false;
    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      child.kill('SIGKILL');
      resolve({ timedOut: true, stdout, stderr });
    }, LIMIT_MS);
    child.stdout.on('data', (chunk) => { stdout += chunk; });
    child.stderr.on('data', (chunk) => { stderr += chunk; });
    child.on('close', (code) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve({ timedOut: false, code, stdout, stderr });
    });
    child.stdin.end(JSON.stringify({ line }));
  });
}

async function main() {
  if (process.argv[2] === '--worker') {
    const chunks = [];
    for await (const chunk of process.stdin) chunks.push(chunk);
    const job = JSON.parse(Buffer.concat(chunks).toString('utf8'));
    const result = parseLine(job.line);
    process.stdout.write(JSON.stringify(result));
    return;
  }

  const slash = (count) => '\\'.repeat(count);
  // 30 published backslashes become 60 in the dict repr. A raw run of 47 is
  // already exponential on the V8 this app's Electron 45 renderer uses.
  const quotedRun = '"content_id": "' + slash(30);
  const rawQuotedRun = '"content_id": "' + slash(47);
  const singleRun = "'content_id': '" + slash(47);
  const spec = {
    benignComment: { kind: 'xhs_comment', text: '请问终面完多久出结果呀？', prefix: '[xhs] ' },
    benignQuote: { kind: 'xhs_comment', text: 'say "hi"', prefix: '[xhs] ' },
    benignApostrophe: { kind: 'xhs_comment', text: "it's a \\test", prefix: '[xhs] ' },
    closedBackslashes: { kind: 'xhs_comment', text: slash(40), prefix: '[xhs] ' },
    xhsComment: { kind: 'xhs_comment', text: quotedRun, prefix: '[xhs] ' },
    xhsCommentPlain: { kind: 'xhs_comment', text: quotedRun, prefix: '' },
    xhsNote: { kind: 'xhs_note', text: quotedRun, prefix: '[xhs] ' },
    douyinComment: { kind: 'douyin_comment', text: '{' + singleRun, prefix: '[dy] ' },
    bilibiliTitle: { kind: 'bilibili_title', text: '{' + rawQuotedRun, prefix: '[bili] ' },
  };
  const lines = buildLines(spec);

  const cases = [
    {
      name: 'benign xhs comment still extracts author and text',
      line: lines.benignComment,
      expect: (result) => {
        assert.strictEqual(result.entries.length, 1);
        assert.strictEqual(result.entries[0].kind, 'comment');
        assert.ok(result.entries[0].text.includes('@nick'), result.entries[0].text);
        assert.ok(result.entries[0].text.includes('请问终面完多久出结果呀'), result.entries[0].text);
      },
    },
    {
      name: 'benign double-quoted comment text is still extracted',
      line: lines.benignQuote,
      expect: (result) => {
        assert.strictEqual(result.entries[0].kind, 'comment');
        assert.ok(result.entries[0].text.includes('say "hi"'), result.entries[0].text);
      },
    },
    {
      name: 'apostrophe in a comment does not block parsing of the rest of the line',
      line: lines.benignApostrophe,
      expect: (result) => {
        assert.strictEqual(result.entries[0].kind, 'comment');
        assert.ok(result.entries[0].text.includes('@nick'), result.entries[0].text);
      },
    },
    {
      name: 'a closed run of backslashes inside a real field still finishes',
      line: lines.closedBackslashes,
      expect: (result) => {
        assert.strictEqual(result.entries[0].kind, 'comment');
        assert.ok(result.entries[0].text.includes('@nick'), result.entries[0].text);
      },
    },
    {
      name: 'xhs comment dict repr with an unclosed "content_id" run does not block',
      line: lines.xhsComment,
      expect: (result) => {
        assert.strictEqual(result.entries[0].kind, 'comment');
        // nickname is logged after the poisoned content field; parsing must
        // still reach it instead of giving up on the whole line.
        assert.ok(result.entries[0].text.includes('@nick'), result.entries[0].text);
      },
    },
    {
      name: 'single-platform xhs comment log (no [xhs] prefix) does not block',
      line: lines.xhsCommentPlain,
      expect: (result) => {
        assert.strictEqual(result.entries[0].kind, 'comment');
        assert.ok(result.entries[0].text.includes('@nick'), result.entries[0].text);
      },
    },
    {
      name: 'xhs note body with an unclosed "content_id" run does not block',
      line: lines.xhsNote,
      expect: (result) => {
        assert.strictEqual(result.entries[0].kind, 'content');
        assert.ok(result.entries[0].text.includes('正常标题'), result.entries[0].text);
        assert.ok(result.entries[0].text.includes('@nick'), result.entries[0].text);
      },
    },
    {
      name: 'douyin comment str() with an unclosed single-quoted content_id run does not block',
      line: lines.douyinComment,
      expect: (result) => {
        assert.strictEqual(result.entries[0].kind, 'comment');
      },
    },
    {
      name: 'bilibili title str() with an unclosed double-quoted content_id run does not block',
      line: lines.bilibiliTitle,
      expect: (result) => {
        assert.strictEqual(result.entries[0].kind, 'content');
      },
    },
  ];

  let passed = 0;
  const failures = [];
  for (const item of cases) {
    const outcome = await runWorker(item.line);
    if (outcome.timedOut) {
      failures.push({ name: item.name, message: 'mergeLogResponse did not return within ' + LIMIT_MS + 'ms' });
      console.log('  [FAIL] ' + item.name + '：timed out (renderer thread would be blocked)');
      continue;
    }
    let result;
    try {
      result = JSON.parse(outcome.stdout);
      assert.strictEqual(result.accepted, true);
      assert.ok(result.ms < LIMIT_MS);
      item.expect(result);
      passed += 1;
      console.log('  [PASS] ' + item.name + ' (' + Math.round(result.ms) + 'ms)');
    } catch (error) {
      failures.push({ name: item.name, message: (error && error.message) || String(error) });
      console.log('  [FAIL] ' + item.name + '：' + ((error && error.message) || error));
      if (outcome.stderr) console.log(outcome.stderr.slice(0, 500));
    }
  }
  console.log('----- 结果：PASS ' + passed + ' / FAIL ' + failures.length + ' -----');
  if (failures.length) process.exit(1);
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
