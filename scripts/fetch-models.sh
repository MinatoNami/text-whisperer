#!/usr/bin/env bash
# Downloads the speaker-diarization models into data/models.
#
# Both are ungated mirrors published by the sherpa-onnx project, so no
# HuggingFace account or token is needed. ~36 MB total. Safe to re-run.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODEL_DIR="${MODEL_DIR:-$APP_DIR/data/models}"
BASE=https://github.com/k2-fsa/sherpa-onnx/releases/download

SEGMENTATION_DIR="$MODEL_DIR/sherpa-onnx-pyannote-segmentation-3-0"
EMBEDDING="$MODEL_DIR/wespeaker_en_voxceleb_CAM++.onnx"

mkdir -p "$MODEL_DIR"

if [[ -f "$SEGMENTATION_DIR/model.onnx" ]]; then
  echo "==> segmentation model already present"
else
  echo "==> fetching pyannote segmentation model"
  curl -fsSL "$BASE/speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2" \
    | tar xj -C "$MODEL_DIR"
fi

if [[ -f "$EMBEDDING" ]]; then
  echo "==> speaker embedding model already present"
else
  echo "==> fetching speaker embedding model"
  curl -fsSL -o "$EMBEDDING.part" \
    "$BASE/speaker-recongition-models/wespeaker_en_voxceleb_CAM++.onnx"
  mv "$EMBEDDING.part" "$EMBEDDING"
fi

echo "==> models ready in $MODEL_DIR"
du -sh "$SEGMENTATION_DIR" "$EMBEDDING" 2>/dev/null || true
