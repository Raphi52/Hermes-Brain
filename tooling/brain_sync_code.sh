#!/usr/bin/env bash
# Sync a repo's graphify code graph into THIS brain. ZERO token, ZERO OAuth.
# Self-locating: BRAIN = parent of this tooling/ folder → marche sur n'importe quelle machine.
# graphify update = ré-extraction AST pure (no LLM) ; on mirror graph.json dans
# BRAIN/projects/<name>/graphify-out (là où une session IA le cherche).
#
# Usage: brain_sync_code.sh "<repo_path>" "<brain_project_name>"
set -uo pipefail

REPO="${1:?usage: brain_sync_code.sh <repo_path> <brain_project_name>}"
NAME="${2:?usage: brain_sync_code.sh <repo_path> <brain_project_name>}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRAIN="$(cd "$HERE/.." && pwd)"
PY="$HERE/.venv/Scripts/python.exe"
[ -x "$PY" ] || PY="$(command -v python)"
DST="$BRAIN/projects/$NAME/graphify-out"
SRC="$REPO/graphify-out"

echo "[sync] graphify update: $REPO"
graphify update "$REPO" || { echo "[sync] ERROR: graphify update failed" >&2; exit 1; }
[ -f "$SRC/graph.json" ] || { echo "[sync] ERROR: no graph.json at $SRC" >&2; exit 1; }

rm -rf "$DST"
mkdir -p "$DST"
cp "$SRC/graph.json" "$DST/"
for f in manifest.json GRAPH_REPORT.md .graphify_labels.json .graphify_analysis.json .graphify_root; do
  [ -f "$SRC/$f" ] && cp "$SRC/$f" "$DST/"
done

env -u PYTHONPATH "$PY" - "$DST/graph.json" "$NAME" <<'EOF'
import json, sys
g = json.load(open(sys.argv[1], encoding="utf-8"))
n = len(g.get("nodes", [])); e = len(g.get("links", g.get("edges", [])))
print(f"[sync] {sys.argv[2]}: {n} nodes / {e} edges -> brain")
EOF
echo "[sync] done: $DST"
