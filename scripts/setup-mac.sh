#!/bin/sh
set -eu
BENCHMARK_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$BENCHMARK_ROOT"
brew install uv colima docker docker-compose docker-buildx
colima start --profile benchmark --vm-type vz --vz-rosetta --cpu 2 --memory 4 --disk 25 --runtime docker
if ! test -x .venv/bin/python; then
  uv venv --python 3.12 .venv
fi
uv pip install --python .venv/bin/python -r requirements.txt
./bench prepare "$@"
printf '\nPreparation complete. Run ./bench preflight to test both harnesses.\n'
