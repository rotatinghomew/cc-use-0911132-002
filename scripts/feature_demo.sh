set -eu
workspace=$(mktemp -d)
trap 'rm -rf "$workspace"' EXIT
export PYTHONDONTWRITEBYTECODE=1
bash scripts/demo.sh
python3 -m brand_ladder_observatory.operator.cli fixtures/demo.json --database "$workspace/analysis.sqlite3" > "$workspace/baseline.json"
python3 -m brand_ladder_observatory.transition_study --input fixtures/analysis/input.json --database "$workspace/analysis.sqlite3" --output "$workspace/first.json"
python3 -m brand_ladder_observatory.transition_study --input fixtures/analysis/input.json --database "$workspace/analysis.sqlite3" --output "$workspace/replay.json"
python3 scripts/check_analysis.py "$workspace/first.json" "$workspace/replay.json"
python3 -m json.tool "$workspace/first.json"
