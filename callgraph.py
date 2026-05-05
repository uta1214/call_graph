#!/usr/bin/env python3
"""
callgraph.py - GNU Global を使った C/C++言語インタラクティブコールグラフ生成
Usage: python callgraph.py <project_dir> [output.html]
"""

import sys
import os
import re
import json
import subprocess
from pathlib import Path

try:
    import networkx as nx
except ImportError:
    print("[ERROR] networkx が必要です: pip install networkx")
    sys.exit(1)

try:
    from pyvis.network import Network
except ImportError:
    print("[ERROR] pyvis が必要です: pip install pyvis")
    sys.exit(1)


# ───────────────────────────────────────────
# 対応拡張子
# ───────────────────────────────────────────

C_EXTENSIONS = {".c", ".h", ".cpp", ".cc", ".cxx", ".hpp", ".hh", ".C", ".CPP"}


# ───────────────────────────────────────────
# Phase 1: gtags 実行 & タグ収集
# ───────────────────────────────────────────

def run_gtags(project_dir: Path) -> None:
    """対象ディレクトリで gtags を実行してタグDBを構築"""
    print(f"[1/4] gtags を実行中: {project_dir}")
    result = subprocess.run(
        ["gtags", "--accept-dotfiles"],
        cwd=project_dir,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"[WARN] gtags stderr: {result.stderr.strip()}")


def is_function_def(source_line: str) -> bool:
    """
    ソース行がおそらく関数定義かどうかをヒューリスティックに判定。
    - マクロ (#define 等)        → False
    - } で始まる行 (構造体末尾)  → False
    - typedef を含む行           → False
    - ( を含まない行 (変数宣言)  → False
    - ; で終わる行 (関数宣言)    → False
    上記に当てはまらず ( を含む行 → True (関数定義)
    """
    s = source_line.strip()
    if not s:
        return False
    if s.startswith('#'):
        return False
    if s.startswith('}'):
        return False
    if 'typedef' in s:
        return False
    if '(' not in s:
        return False
    if s.endswith(';'):
        return False
    return True


def collect_all_tags(project_dir: Path) -> dict[str, dict]:
    """
    global -f <file> で C/C++ ソースファイルのタグを収集。
    関数定義と思われるものは is_func=True、それ以外は False とする。
    同名タグが複数ある場合、関数定義 (is_func=True) のエントリを優先する。

    Returns: { tag_name: { file, line, source_line, is_func, category } }
    """
    print("[2/4] タグを収集中...")

    c_files: list[Path] = []
    for ext in C_EXTENSIONS:
        c_files.extend(project_dir.rglob(f"*{ext}"))

    # --- タグ収集 ---
    raw_tags: dict[str, list[dict]] = {}  # tag_name -> [候補リスト]

    for c_file in c_files:
        result = subprocess.run(
            ["global", "-f", str(c_file)],
            cwd=project_dir,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            continue
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) < 3:
                continue
            tag_name = parts[0]
            try:
                lineno = int(parts[1])
            except ValueError:
                continue
            filepath = parts[2]
            raw_tags.setdefault(tag_name, []).append(
                {"file": filepath, "line": lineno, "source_line": "", "is_func": False}
            )

    # --- ソース行の読み込みと is_func 判定 ---
    file_lines_cache: dict[str, list[str]] = {}

    def read_lines(fp_str: str) -> list[str]:
        if fp_str in file_lines_cache:
            return file_lines_cache[fp_str]
        abs_path = Path(fp_str) if Path(fp_str).is_absolute() else project_dir / fp_str
        if not abs_path.exists():
            abs_path = Path(fp_str)
        try:
            with open(abs_path, "r", errors="replace") as f:
                lines = f.readlines()
        except OSError:
            lines = []
        file_lines_cache[fp_str] = lines
        return lines

    tags: dict[str, dict] = {}
    for tag_name, candidates in raw_tags.items():
        best: dict | None = None
        for cand in candidates:
            lines = read_lines(cand["file"])
            ln = cand["line"]
            src = lines[ln - 1].rstrip() if 0 < ln <= len(lines) else ""
            cand["source_line"] = src
            cand["is_func"] = is_function_def(src)
            # 関数定義エントリを優先
            if best is None or (cand["is_func"] and not best["is_func"]):
                best = cand
        if best:
            best["category"] = "function" if best["is_func"] else "other"
            tags[tag_name] = best

    func_count  = sum(1 for v in tags.values() if v["is_func"])
    other_count = len(tags) - func_count
    print(f"    → {func_count} 関数, {other_count} その他シンボルを検出")
    return tags


# ───────────────────────────────────────────
# Phase 2: 呼び出し関係の解析
# ───────────────────────────────────────────

def build_scope_map(tags: dict) -> dict[str, list[tuple]]:
    """
    ファイルごとに行番号でソートし、各タグのスコープ (start, end) を返す。
    Returns: { filepath: [(tag_name, start_line, end_line), ...] }
    """
    file_tags: dict[str, list] = {}
    for name, info in tags.items():
        file_tags.setdefault(info["file"], []).append((name, info["line"]))

    scope_map: dict[str, list[tuple]] = {}
    for fp, entries in file_tags.items():
        entries.sort(key=lambda x: x[1])
        scopes = []
        for i, (name, start) in enumerate(entries):
            end = entries[i + 1][1] - 1 if i + 1 < len(entries) else 10 ** 9
            scopes.append((name, start, end))
        scope_map[fp] = scopes
    return scope_map


def extract_calls(
    source_lines: list[str],
    start: int,
    end: int,
    known_tags: set[str],
    self_name: str,
) -> set[str]:
    """[start, end] 行範囲で既知タグへの呼び出しを抽出（自己再帰除外）"""
    pattern = re.compile(
        r'\b(' + '|'.join(re.escape(f) for f in known_tags) + r')\s*\('
    )
    callees: set[str] = set()
    for lineno in range(start - 1, min(end, len(source_lines))):
        line = source_lines[lineno]
        stripped = re.sub(r'//.*', '', line)
        stripped = re.sub(r'/\*.*?\*/', '', stripped)
        for m in pattern.finditer(stripped):
            callee = m.group(1)
            if callee != self_name:
                callees.add(callee)
    return callees


def build_call_graph(
    tags: dict,
    scope_map: dict,
    project_dir: Path,
) -> nx.DiGraph:
    """呼び出し関係を解析して有向グラフを構築。エッジは関数ノードからのみ出す。"""
    print("[3/4] コールグラフを構築中...")
    G = nx.DiGraph()
    known_tags = set(tags.keys())

    for name, info in tags.items():
        G.add_node(
            name,
            file=info["file"],
            line=info["line"],
            source_line=info.get("source_line", ""),
            is_func=info["is_func"],
            category=info["category"],
        )

    file_cache: dict[str, list[str]] = {}
    for filepath, scopes in scope_map.items():
        abs_path = (
            Path(filepath) if Path(filepath).is_absolute() else project_dir / filepath
        )
        if not abs_path.exists():
            abs_path = Path(filepath)
        if not abs_path.exists():
            continue
        if filepath not in file_cache:
            try:
                with open(abs_path, "r", errors="replace") as f:
                    file_cache[filepath] = f.readlines()
            except OSError:
                continue
        source_lines = file_cache[filepath]

        for func_name, start, end in scopes:
            # 関数ノードからのみエッジを出す
            if not tags.get(func_name, {}).get("is_func", False):
                continue
            callees = extract_calls(source_lines, start, end, known_tags, func_name)
            for callee in callees:
                G.add_edge(func_name, callee)

    func_nodes = sum(1 for n in G.nodes() if G.nodes[n].get("is_func"))
    print(
        f"    → ノード: {G.number_of_nodes()} (関数: {func_nodes}), "
        f"エッジ: {G.number_of_edges()}"
    )
    return G


def build_source_map(scope_map: dict, project_dir: Path) -> dict[str, str]:
    """各タグのソースコード本体を抽出"""
    source_map: dict[str, str] = {}
    file_cache: dict[str, list[str]] = {}

    for filepath, scopes in scope_map.items():
        abs_path = (
            Path(filepath) if Path(filepath).is_absolute() else project_dir / filepath
        )
        if not abs_path.exists():
            abs_path = Path(filepath)
        if not abs_path.exists():
            continue
        key = str(abs_path)
        if key not in file_cache:
            try:
                with open(abs_path, "r", errors="replace") as f:
                    file_cache[key] = f.readlines()
            except OSError:
                file_cache[key] = []
        lines = file_cache[key]

        for name, start, end in scopes:
            actual_end = min(end, len(lines))
            source_map[name] = "".join(lines[start - 1 : actual_end])

    return source_map


# ───────────────────────────────────────────
# Phase 3: 色の生成
# ───────────────────────────────────────────

_FILE_COLORS_BASE = [
    {"background": "#ffeaa7", "border": "#fdcb6e"},
    {"background": "#fab1a0", "border": "#e17055"},
    {"background": "#a29bfe", "border": "#6c5ce7"},
    {"background": "#81ecec", "border": "#00cec9"},
    {"background": "#55efc4", "border": "#00b894"},
    {"background": "#fd79a8", "border": "#e84393"},
    {"background": "#74b9ff", "border": "#0984e3"},
    {"background": "#dfe6e9", "border": "#b2bec3"},
]


def generate_file_colors(files: list[str]) -> dict[str, dict]:
    """
    ファイルごとに色を割り当てる。
    8色のプリセットを使い切ったら HSL で均等に自動生成。
    """
    total = len(files)
    color_map: dict[str, dict] = {}
    for i, f in enumerate(files):
        if i < len(_FILE_COLORS_BASE):
            color_map[f] = _FILE_COLORS_BASE[i]
        else:
            hue = int((i * 360 / total) % 360)
            color_map[f] = {
                "background": f"hsl({hue}, 65%, 80%)",
                "border":     f"hsl({hue}, 65%, 55%)",
            }
    return color_map


# ───────────────────────────────────────────
# Phase 4: インタラクティブHTML生成
# ───────────────────────────────────────────

_NAV_BUTTON_CSS = """
<style>
/* ナビゲーションボタンをエッジと同系のグレーに */
div.vis-network div.vis-navigation div.vis-button {
  background-color: rgba(170,170,170,0.45) !important;
  border-radius: 4px !important;
}
div.vis-network div.vis-navigation div.vis-button:hover {
  background-color: rgba(100,100,100,0.75) !important;
}
</style>
"""

_CUSTOM_JS = r"""
<script>
// ── デフォルト色を記憶 ──────────────────────────────────────────────────────
var defaultNodeColors = {};
nodes.forEach(function(n) {
  defaultNodeColors[n.id] = {
    color:  JSON.parse(JSON.stringify(n.color   || {})),
    font:   JSON.parse(JSON.stringify(n.font    || {})),
    hidden: n.hidden || false,
  };
});

var currentNode = null;
var currentHop  = null;

// ── BFS: N ホップ以内のノード集合 ──────────────────────────────────────────
function getNodesWithinHops(startId, maxHops) {
  var visited  = new Set([startId]);
  var frontier = [startId];
  for (var hop = 0; hop < maxHops; hop++) {
    var next = [];
    frontier.forEach(function(id) {
      network.getConnectedNodes(id).forEach(function(nid) {
        if (!visited.has(nid)) { visited.add(nid); next.push(nid); }
      });
    });
    frontier = next;
    if (!frontier.length) break;
  }
  return visited;
}

// ── ホップフィルタ ─────────────────────────────────────────────────────────
function applyHopFilter(maxHops) {
  currentHop = maxHops;
  if (currentNode === null) return;

  var showOther = document.getElementById('other-toggle').checked;
  var visible = (maxHops === null)
    ? new Set(nodes.getIds())
    : getNodesWithinHops(currentNode, maxHops);

  var outgoing = new Set();
  var incoming = new Set();
  edges.forEach(function(e) {
    if (e.from === currentNode) outgoing.add(e.to);
    if (e.to   === currentNode) incoming.add(e.from);
  });

  var nodeUpdates = nodes.getIds().map(function(id) {
    var d = defaultNodeColors[id] || {};
    // 非表示ノードは触らない
    if (d.hidden && !showOther) return { id: id };
    if (!visible.has(id))
      return { id: id, color: { background:'#f0f0f0', border:'#e0e0e0' }, font:{ color:'#e0e0e0' } };
    if (id === currentNode)
      return { id: id, color: { background:'#00b894', border:'#00695c' }, font:{ color:'#003d33' } };
    if (outgoing.has(id))
      return { id: id, color: { background:'#fab1a0', border:'#e17055' }, font:{ color:'#6d2b1a' } };
    if (incoming.has(id))
      return { id: id, color: { background:'#74b9ff', border:'#0984e3' }, font:{ color:'#003580' } };
    return { id: id, color: d.color, font: d.font };
  });
  nodes.update(nodeUpdates);

  var connectedEdges = new Set(network.getConnectedEdges(currentNode));
  var edgeUpdates = edges.getIds().map(function(id) {
    var e   = edges.get(id);
    var vis = visible.has(e.from) && visible.has(e.to);
    if (!vis)
      return { id: id, color:{ color:'#eeeeee', opacity:0.2 }, width:1 };
    if (connectedEdges.has(id)) {
      var col = (e.from === currentNode) ? '#e17055' : '#0984e3';
      return { id: id, color:{ color:col, opacity:1.0 }, width:2.5 };
    }
    return { id: id, color:{ color:'#aaaaaa', opacity:0.6 }, width:1 };
  });
  edges.update(edgeUpdates);

  document.querySelectorAll('.hop-btn').forEach(function(btn) {
    var active = (btn.dataset.hop === String(maxHops));
    btn.style.background = active ? '#636e72' : '#dfe6e9';
    btn.style.color      = active ? '#fff'    : '#2d3436';
  });
}

// ── 選択ノードの強調 ───────────────────────────────────────────────────────
function highlightNode(clickedId) {
  currentNode = clickedId;
  currentHop  = null;

  if (document.getElementById('src-toggle').checked) {
    showSource(clickedId);
  }

  document.getElementById('hop-panel').style.display = 'block';
  document.querySelectorAll('.hop-btn').forEach(function(btn) {
    btn.style.background = '#dfe6e9'; btn.style.color = '#2d3436';
  });

  var outgoing = new Set();
  var incoming = new Set();
  edges.forEach(function(e) {
    if (e.from === clickedId) outgoing.add(e.to);
    if (e.to   === clickedId) incoming.add(e.from);
  });
  var connectedEdges = new Set(network.getConnectedEdges(clickedId));
  var showOther = document.getElementById('other-toggle').checked;

  var nodeUpdates = nodes.getIds().map(function(id) {
    var d = defaultNodeColors[id] || {};
    if (d.hidden && !showOther) return { id: id };
    return { id: id, color:{ background:'#ececec', border:'#cccccc' }, font:{ color:'#bbbbbb' } };
  });
  nodeUpdates.push(
    { id: clickedId, color:{ background:'#00b894', border:'#00695c' }, font:{ color:'#003d33' } }
  );
  outgoing.forEach(function(id) {
    nodeUpdates.push({ id: id, color:{ background:'#fab1a0', border:'#e17055' }, font:{ color:'#6d2b1a' } });
  });
  incoming.forEach(function(id) {
    nodeUpdates.push({ id: id, color:{ background:'#74b9ff', border:'#0984e3' }, font:{ color:'#003580' } });
  });
  nodes.update(nodeUpdates);

  var edgeUpdates = edges.getIds().map(function(id) {
    return { id: id, color:{ color:'#e8e8e8', opacity:0.3 }, width:1 };
  });
  connectedEdges.forEach(function(id) {
    var e   = edges.get(id);
    var col = (e.from === clickedId) ? '#e17055' : '#0984e3';
    edgeUpdates.push({ id: id, color:{ color:col, opacity:1.0 }, width:2.5 });
  });
  edges.update(edgeUpdates);
}

// ── ノードクリック ────────────────────────────────────────────────────────
network.on("click", function(params) {
  if (params.nodes.length === 0) { resetAll(); return; }
  var clickedId = params.nodes[0];
  if (clickedId === currentNode) { resetAll(); return; }
  highlightNode(clickedId);
});

// ── ソースパネル ──────────────────────────────────────────────────────────
function showSource(funcName) {
  var panel       = document.getElementById('source-panel');
  var placeholder = document.getElementById('source-placeholder');
  var content     = document.getElementById('source-content');
  panel.style.display = 'flex';

  if (!funcName) {
    placeholder.style.display = 'flex';
    content.style.display     = 'none';
    return;
  }

  var src  = (typeof SOURCE_MAP !== 'undefined') ? (SOURCE_MAP[funcName] || '') : '';
  var info = (typeof NODE_INFO  !== 'undefined') ? (NODE_INFO[funcName]  || {}) : {};
  document.getElementById('source-func-name').textContent = funcName;
  var fileBase = info.file ? info.file.split('/').pop() : '';
  document.getElementById('source-file-info').textContent =
    fileBase ? fileBase + ' : ' + info.line + '行目' : '';
  document.getElementById('source-code').textContent = src || '(ソースが見つかりません)';
  placeholder.style.display = 'none';
  content.style.display     = 'flex';
}

// ── ファイル凡例 ──────────────────────────────────────────────────────────
(function renderLegend() {
  if (typeof FILE_LEGEND === 'undefined') return;
  var container = document.getElementById('legend-items');
  FILE_LEGEND.forEach(function(item) {
    var name = item.file.split('/').pop();
    var row  = document.createElement('div');
    row.style.cssText = 'display:flex;align-items:center;gap:6px;margin-bottom:3px;'
                      + 'font-size:11px;cursor:default;';
    row.title = item.file;
    var dot = document.createElement('span');
    dot.style.cssText = 'width:10px;height:10px;border-radius:50%;flex-shrink:0;'
      + 'background:' + item.color + ';border:1.5px solid ' + item.border + ';';
    var label = document.createElement('span');
    label.style.cssText = 'color:#2d3436;overflow:hidden;text-overflow:ellipsis;'
                        + 'white-space:nowrap;max-width:160px;';
    label.textContent = name;
    row.appendChild(dot);
    row.appendChild(label);
    container.appendChild(row);
  });
})();

// ── リセット ──────────────────────────────────────────────────────────────
function resetAll() {
  currentNode = null;
  currentHop  = null;
  network.unselectAll();

  var showOther = document.getElementById('other-toggle').checked;
  var nodeUpdates = nodes.getIds().map(function(id) {
    var d = defaultNodeColors[id] || {};
    var upd = { id: id, color: d.color, font: d.font };
    if (d.hidden && !showOther) upd.hidden = true;
    else                         upd.hidden = false;
    return upd;
  });
  nodes.update(nodeUpdates);

  edges.update(edges.getIds().map(function(id) {
    return { id: id, color:{ color:'#aaaaaa', opacity:0.8 }, width:1 };
  }));

  document.getElementById('hop-panel').style.display = 'none';
  document.getElementById('search-box').value = '';
  document.querySelectorAll('.hop-btn').forEach(function(btn) {
    btn.style.background = '#dfe6e9'; btn.style.color = '#2d3436';
  });

  if (document.getElementById('src-toggle').checked) {
    showSource(null);
  } else {
    document.getElementById('source-panel').style.display = 'none';
  }
}

network.on("doubleClick", function() { resetAll(); });

// Escape キー
document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape') resetAll();
});

// ── 検索ボックス ──────────────────────────────────────────────────────────
var searchBox = document.getElementById('search-box');
searchBox.addEventListener('input', function() {
  var q = this.value.trim().toLowerCase();
  if (!q) { resetAll(); return; }
  var matchSet = new Set();
  nodes.forEach(function(n) { if (n.id.toLowerCase().includes(q)) matchSet.add(n.id); });
  var updates = nodes.getIds().map(function(id) {
    if (matchSet.has(id)) {
      var d = defaultNodeColors[id] || {};
      return { id: id, color: d.color, font: Object.assign({}, d.font, { color:'#2d3436' }) };
    }
    return { id: id, color:{ background:'#f0f0f0', border:'#e0e0e0' }, font:{ color:'#dddddd' } };
  });
  nodes.update(updates);
});
searchBox.addEventListener('keydown', function(e) {
  if (e.key === 'Enter') {
    var q    = this.value.trim().toLowerCase();
    var hits = nodes.get({ filter: function(n) { return n.id.toLowerCase().includes(q); } });
    if (hits.length) network.focus(hits[0].id, { scale:1.5, animation:{ duration:400 } });
  }
  if (e.key === 'Escape') resetAll();
});

// ── ソースチェックボックス ────────────────────────────────────────────────
document.getElementById('src-toggle').addEventListener('change', function() {
  if (!this.checked) {
    document.getElementById('source-panel').style.display = 'none';
  } else {
    showSource(currentNode);
  }
});

// ── 構造体・変数チェックボックス ──────────────────────────────────────────
document.getElementById('other-toggle').addEventListener('change', function() {
  var show    = this.checked;
  var otherIds = (typeof OTHER_NODES !== 'undefined') ? OTHER_NODES : [];
  nodes.update(otherIds.map(function(id) { return { id: id, hidden: !show }; }));
  if (currentNode) highlightNode(currentNode);
});

// ── スクロール制御: 上下移動 / Shift+左右 / Ctrl+拡大縮小 ─────────────────
network.body.container.addEventListener('wheel', function(e) {
  e.preventDefault();
  e.stopPropagation();
  var scale = network.getScale();
  var pos   = network.getViewPosition();
  var speed = 120 / scale;            // ズームレベルに応じた移動量

  if (e.ctrlKey) {
    // 拡大縮小
    var factor = e.deltaY > 0 ? 0.85 : 1.15;
    network.moveTo({ scale: scale * factor, animation: false });
  } else if (e.shiftKey) {
    // 左右移動
    network.moveTo({ position: { x: pos.x + e.deltaY * speed / 100, y: pos.y }, animation: false });
  } else {
    // 上下移動
    network.moveTo({ position: { x: pos.x, y: pos.y + e.deltaY * speed / 100 }, animation: false });
  }
}, { passive: false, capture: true });
</script>
"""

_INSTRUCTION_HTML = """
<div id="controls" style="
  position:fixed; top:12px; left:12px; z-index:999;
  background:rgba(255,255,255,0.95); border:1px solid #ddd;
  border-radius:8px; padding:12px 14px; font-family:monospace;
  font-size:12px; box-shadow:0 2px 8px rgba(0,0,0,0.12);
  min-width:230px; line-height:1.8;">

  <b style="font-size:13px;">📞 Call Graph</b>
  <div style="color:#636e72; font-size:11px; margin:4px 0 8px; line-height:1.7;">
    <b style="color:#00b894">●</b> 選択中 &nbsp;
    <b style="color:#e17055">●</b> callee &nbsp;
    <b style="color:#0984e3">●</b> caller<br>
    再クリック / ダブルクリック / Esc → リセット<br>
    スクロール: 上下移動 &nbsp;|&nbsp; Shift+スクロール: 左右<br>
    Ctrl+スクロール: 拡大縮小
  </div>

  <input id="search-box" type="text" placeholder="🔍 関数名を検索 (Enter でフォーカス)" style="
    width:100%; box-sizing:border-box; padding:5px 8px;
    border:1px solid #b2bec3; border-radius:5px;
    font-family:monospace; font-size:12px; outline:none; margin-bottom:8px;">

  <label style="cursor:pointer;display:flex;align-items:center;gap:6px;font-size:11px;color:#2d3436;margin-bottom:4px;">
    <input id="src-toggle" type="checkbox" style="cursor:pointer;">
    ソースコードパネルを表示
  </label>

  <label style="cursor:pointer;display:flex;align-items:center;gap:6px;font-size:11px;color:#2d3436;">
    <input id="other-toggle" type="checkbox" style="cursor:pointer;">
    構造体・変数も表示
  </label>

  <div id="hop-panel" style="display:none; margin-top:10px;">
    <div style="color:#636e72; font-size:11px; margin-bottom:4px;">表示範囲（ホップ数）:</div>
    <div style="display:flex; gap:5px;">
      <button class="hop-btn" data-hop="1"    onclick="applyHopFilter(1)"
        style="flex:1;padding:4px 0;border:1px solid #b2bec3;border-radius:4px;cursor:pointer;background:#dfe6e9;font-family:monospace;font-size:12px;">1</button>
      <button class="hop-btn" data-hop="2"    onclick="applyHopFilter(2)"
        style="flex:1;padding:4px 0;border:1px solid #b2bec3;border-radius:4px;cursor:pointer;background:#dfe6e9;font-family:monospace;font-size:12px;">2</button>
      <button class="hop-btn" data-hop="3"    onclick="applyHopFilter(3)"
        style="flex:1;padding:4px 0;border:1px solid #b2bec3;border-radius:4px;cursor:pointer;background:#dfe6e9;font-family:monospace;font-size:12px;">3</button>
      <button class="hop-btn" data-hop="null" onclick="applyHopFilter(null)"
        style="flex:1;padding:4px 0;border:1px solid #b2bec3;border-radius:4px;cursor:pointer;background:#dfe6e9;font-family:monospace;font-size:12px;">All</button>
    </div>
  </div>

  <div id="file-legend" style="margin-top:10px; border-top:1px solid #ddd; padding-top:8px;">
    <div style="color:#636e72; font-size:11px; margin-bottom:5px;">ファイル凡例:</div>
    <div id="legend-items"></div>
  </div>
</div>

<!-- ソースコードパネル -->
<div id="source-panel" style="
  display:none; position:fixed; top:0; right:0; bottom:0;
  width:40%; max-width:600px; z-index:998;
  background:#1e1e2e; color:#cdd6f4; font-family:monospace;
  font-size:13px; flex-direction:column;
  border-left:2px solid #45475a; box-shadow:-4px 0 16px rgba(0,0,0,0.2);">

  <!-- ノード未選択時のプレースホルダー -->
  <div id="source-placeholder" style="
    display:flex; flex:1; align-items:center; justify-content:center;
    flex-direction:column; gap:10px; color:#6c7086;">
    <span style="font-size:28px;">←</span>
    <span style="font-size:13px;">ノードをクリックしてください</span>
  </div>

  <!-- ノード選択時のコンテンツ -->
  <div id="source-content" style="display:none; flex-direction:column; flex:1; overflow:hidden;">
    <div style="padding:10px 16px; background:#181825; border-bottom:1px solid #45475a;
                display:flex; justify-content:space-between; align-items:flex-start; flex-shrink:0;">
      <div>
        <span style="color:#89b4fa; font-weight:bold; font-size:14px;" id="source-func-name"></span><br>
        <span style="color:#6c7086; font-size:11px;" id="source-file-info"></span>
      </div>
      <button
        onclick="document.getElementById('src-toggle').checked=false;
                 document.getElementById('source-panel').style.display='none';"
        style="background:none;border:none;color:#6c7086;cursor:pointer;font-size:16px;margin-left:8px;">✕</button>
    </div>
    <pre id="source-code" style="
      margin:0; padding:16px; overflow:auto; flex:1;
      line-height:1.6; white-space:pre; color:#cdd6f4; background:#1e1e2e;"></pre>
  </div>
</div>
"""


def generate_html(G: nx.DiGraph, output_path: Path, source_map: dict[str, str]) -> None:
    """pyvis でインタラクティブHTMLを生成"""
    print("[4/4] HTMLを生成中...")

    net = Network(
        height="100vh",
        width="100%",
        directed=True,
        bgcolor="#f8f9fa",
        font_color="#2d3436",
    )

    files = sorted(set(G.nodes[n].get("file", "") for n in G.nodes()))
    file_color_map = generate_file_colors(files)

    in_deg      = dict(G.in_degree())
    other_nodes = []

    for node in G.nodes():
        filepath    = G.nodes[node].get("file", "")
        lineno      = G.nodes[node].get("line", "")
        source_line = G.nodes[node].get("source_line", "").strip()
        is_func     = G.nodes[node].get("is_func", False)
        color       = file_color_map.get(filepath, _FILE_COLORS_BASE[-1])

        title = (
            f"{node} : {lineno}行\n{source_line}"
            if source_line else f"{node} : {lineno}行"
        )
        size  = 12 + in_deg.get(node, 0) * 3

        # 関数以外はグレーで初期非表示
        node_color = color if is_func else {"background": "#dfe6e9", "border": "#b2bec3"}
        net.add_node(
            node,
            label=node,           # 関数名のみ（行番号なし）
            title=title,
            size=min(size, 40),
            color=node_color,
            font={"size": 11, "face": "monospace", "color": "#2d3436"},
            hidden=not is_func,   # 関数以外は初期非表示
        )
        if not is_func:
            other_nodes.append(node)

    for src, dst in G.edges():
        net.add_edge(
            src, dst,
            arrows="to",
            color={"color": "#aaaaaa", "hover": "#aaaaaa", "highlight": "#aaaaaa"},
            width=1,
        )

    # 階層レイアウト + エッジホバー無効化
    net.set_options("""
    {
      "layout": {
        "hierarchical": {
          "enabled": true,
          "direction": "LR",
          "sortMethod": "directed",
          "levelSeparation": 220,
          "nodeSpacing": 70,
          "treeSpacing": 130,
          "blockShifting": true,
          "edgeMinimization": true,
          "parentCentralization": true
        }
      },
      "nodes": {
        "shape": "dot",
        "borderWidth": 2,
        "shadow": { "enabled": true, "size": 4, "x": 2, "y": 2, "color": "rgba(0,0,0,0.08)" },
        "font": { "size": 11, "face": "monospace" }
      },
      "edges": {
        "smooth": {
          "enabled": true,
          "type": "cubicBezier",
          "forceDirection": "horizontal",
          "roundness": 0.5
        },
        "arrows": { "to": { "scaleFactor": 0.6 } },
        "color": { "color": "#aaaaaa", "hover": "#aaaaaa", "highlight": "#aaaaaa" },
        "hoverWidth": 0,
        "selectionWidth": 0,
        "width": 1
      },
      "interaction": {
        "hover": true,
        "tooltipDelay": 80,
        "navigationButtons": true,
        "keyboard": false,
        "zoomView": false
      },
      "physics": { "enabled": false }
    }
    """)

    html_path = str(output_path)
    net.save_graph(html_path)

    with open(html_path, "r", encoding="utf-8") as f:
        html = f.read()

    # JS データ変数の埋め込み
    node_info = {
        n: {"file": G.nodes[n].get("file", ""), "line": G.nodes[n].get("line", "")}
        for n in G.nodes()
    }
    file_legend = [
        {
            "file":   f,
            "color":  file_color_map[f]["background"],
            "border": file_color_map[f]["border"],
        }
        for f in files
    ]
    data_js = (
        "<script>"
        f"var SOURCE_MAP  = {json.dumps(source_map,  ensure_ascii=False)};"
        f"var NODE_INFO   = {json.dumps(node_info,   ensure_ascii=False)};"
        f"var FILE_LEGEND = {json.dumps(file_legend, ensure_ascii=False)};"
        f"var OTHER_NODES = {json.dumps(other_nodes, ensure_ascii=False)};"
        "</script>"
    )

    html = html.replace("</head>", _NAV_BUTTON_CSS + "</head>")
    html = html.replace("</body>", data_js + _INSTRUCTION_HTML + _CUSTOM_JS + "</body>")

    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"    → 出力: {output_path}")


# ───────────────────────────────────────────
# 出力パスの決定
# ───────────────────────────────────────────

def resolve_output_path(project_dir: Path, explicit_output: Path | None) -> Path:
    """
    出力パスの決定ルール:
    - 明示指定あり             → その指定に従う
    - カレントディレクトリ指定 → ./callgraph.html
    - その他のディレクトリ指定 → ./callgraph_<フォルダ名>.html
    """
    if explicit_output:
        return explicit_output
    cwd = Path.cwd()
    if project_dir == cwd:
        return cwd / "callgraph.html"
    return cwd / f"callgraph_{project_dir.name}.html"


# ───────────────────────────────────────────
# メイン
# ───────────────────────────────────────────

def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python callgraph.py <project_dir> [output.html]")
        sys.exit(1)

    project_dir = Path(sys.argv[1]).resolve()
    if not project_dir.is_dir():
        print(f"[ERROR] ディレクトリが見つかりません: {project_dir}")
        sys.exit(1)

    explicit_output = Path(sys.argv[2]) if len(sys.argv) >= 3 else None
    output_html     = resolve_output_path(project_dir, explicit_output)

    # 既存 GTAGS があれば再利用
    if not (project_dir / "GTAGS").exists():
        run_gtags(project_dir)
    else:
        print("[1/4] 既存の GTAGS を利用します")

    tags = collect_all_tags(project_dir)
    if not tags:
        print("[ERROR] タグが見つかりませんでした。gtags が正しく動作しているか確認してください。")
        sys.exit(1)

    scope_map  = build_scope_map(tags)
    G          = build_call_graph(tags, scope_map, project_dir)
    source_map = build_source_map(scope_map, project_dir)
    generate_html(G, output_html, source_map)

    print(f"\n✅ 完了！ → {output_html}")


if __name__ == "__main__":
    main()