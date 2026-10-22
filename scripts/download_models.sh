#!/usr/bin/env bash
# Download the offline detection models (~23 MB) into ./models
set -euo pipefail

DIR="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/models}"
mkdir -p "$DIR"
cd "$DIR"

fetch() {
  local url="$1" out="$2" min="$3"
  if [[ -f "$out" && $(stat -c%s "$out") -ge $min ]]; then
    echo "  ok      $out"
    return
  fi
  echo "  fetch   $out"
  curl -fsSL --retry 3 -o "$out.part" "$url"
  if [[ $(stat -c%s "$out.part") -lt $min ]]; then
    rm -f "$out.part"; echo "  FAILED  $out (too small)"; return 1
  fi
  mv "$out.part" "$out"
}

fetch "https://raw.githubusercontent.com/chuanqi305/MobileNet-SSD/master/deploy.prototxt" \
      "MobileNetSSD_deploy.prototxt" 40000
fetch "https://raw.githubusercontent.com/chuanqi305/MobileNet-SSD/master/mobilenet_iter_73000.caffemodel" \
      "mobilenet_iter_73000.caffemodel" 20000000
fetch "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx" \
      "face_detection_yunet_2023mar.onnx" 200000

echo "models ready in $DIR"
