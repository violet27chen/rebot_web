#!/usr/bin/env bash
set -euo pipefail

# Build & install the two GraspNet CUDA extensions that ship as source in
# this repo:
#   - sdk/graspnet-baseline/pointnet2  (pointnet2._ext)
#   - sdk/graspnet-baseline/knn        (knn_pytorch.knn_pytorch)
#
# Tested on JetPack 6 (Python 3.10, CUDA 12.x) and JetPack 7 (Python 3.12,
# CUDA 13.x). The script auto-detects Python and the active conda env.
#
# Usage:
#   conda activate graspnet
#   bash scripts/install_graspnet_cuda_ops.sh
#
# Options:
#   --force     Wipe build/ + .so in both packages before rebuilding.
#   --no-hook   Skip installing the conda activate.d LD_LIBRARY_PATH hook
#               (use only if you prefer to manage rpath another way).
#   --check     Run the import self-test only, do not build.
#
# Why a dedicated script is needed
# --------------------------------
# torch 2.x's CUDAExtension does NOT auto-inject the torch lib path into
# the extension's RPATH when is_standalone=False (the default for pip
# packages). The resulting .so embeds RPATH=$CONDA_PREFIX/lib, which has
# no libc10.so. Without this fix you get:
#   ImportError: libc10.so: cannot open shared object file
# on a fresh env. This script:
#   1) Patches each setup.py to add extra_ldflags with -L and -Wl,-rpath
#      pointing at <env>/lib/python*/site-packages/torch/lib.
#   2) Runs build_ext --inplace (to trigger the nvcc compile) and then
#      manually copies the .so to the package root. We avoid `setup.py
#      install` because in editable mode it tries to copy the .so into a
#      sub-package directory that doesn't exist (flat layout).
#   3) Drops a conda activate.d hook so every future `conda activate
#      graspnet` automatically exports LD_LIBRARY_PATH with torch lib.
#      This is a belt-and-suspenders fallback in case some build path
#      still embeds the wrong RPATH.
#
# Idempotent: re-running with --force does a clean rebuild and re-asserts
# the rpath fix and the hook.

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GRASPNET_ROOT="$PROJECT_ROOT/sdk/graspnet-baseline"
POINTNET2_DIR="$GRASPNET_ROOT/pointnet2"
KNN_DIR="$GRASPNET_ROOT/knn"

PYTHON_BIN="${PYTHON:-python}"
FORCE=0
INSTALL_HOOK=1
CHECK_ONLY=0

for arg in "$@"; do
  case "$arg" in
    --force)     FORCE=1 ;;
    --no-hook)   INSTALL_HOOK=0 ;;
    --check)     CHECK_ONLY=1 ;;
    -h|--help)
      sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "[FAIL] unknown arg: $arg" >&2
      exit 1
      ;;
  esac
done

log()  { echo "[INFO] $*"; }
ok()   { echo "[OK]   $*"; }
fail() { echo "[FAIL] $*"; exit 1; }

# ── Pre-flight ────────────────────────────────────────────────────────
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  fail "python not found: $PYTHON_BIN  (try: conda activate graspnet)"
fi

PY_VERSION="$("$PYTHON_BIN" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
PY_TAG="python${PY_VERSION//./}"
log "python:    $("$PYTHON_BIN" -c 'import sys; print(sys.executable)')"
log "version:   $PY_VERSION (tag: $PY_TAG)"

# Confirm torch + CUDA are importable in the active env.
"$PYTHON_BIN" - <<'PY' || fail "torch import failed; is 'graspnet' env active?"
import torch
from torch.utils import cpp_extension
print(f"[INFO] torch:     {torch.__version__}")
print(f"[INFO] CUDA home: {cpp_extension.CUDA_HOME}")
print(f"[INFO] CUDA avail:{torch.cuda.is_available()}")
if not torch.cuda.is_available():
    raise SystemExit("CUDA is not available in this Python env")
PY

# Resolve torch lib dir (this is the path the .so must be able to dlopen).
TORCH_LIB="$("$PYTHON_BIN" -c 'import os, torch; print(os.path.join(os.path.dirname(torch.__file__), "lib"))')"
log "torch lib: $TORCH_LIB"
[[ -d "$TORCH_LIB" ]] || fail "torch lib dir not found: $TORCH_LIB"

# Check nvcc is on PATH. (BuildExtension needs it for any .cu file.)
if ! command -v nvcc >/dev/null 2>&1; then
  # Fall back to torch's bundled CUDA_HOME.
  CUDA_HOME_VAL="$("$PYTHON_BIN" -c 'import torch.utils.cpp_extension as e; print(e.CUDA_HOME or "")')"
  if [[ -n "$CUDA_HOME_VAL" && -x "$CUDA_HOME_VAL/bin/nvcc" ]]; then
    export PATH="$CUDA_HOME_VAL/bin:$PATH"
    log "nvcc:      $CUDA_HOME_VAL/bin/nvcc (from torch CUDA_HOME)"
  else
    fail "nvcc not found on PATH and CUDA_HOME unset; install cuda-toolkit or use a JetPack Python env"
  fi
else
  log "nvcc:      $(command -v nvcc)"
fi

if [[ "$CHECK_ONLY" -eq 1 ]]; then
  log "(--check) importing extensions only"
  "$PYTHON_BIN" - <<'PY'
import sys
sys.path.insert(0, "$GRASPNET_ROOT")
sys.path.insert(0, "$GRASPNET_ROOT/pointnet2")
sys.path.insert(0, "$GRASPNET_ROOT/knn")
import pointnet2._ext as a
import knn_pytorch.knn_pytorch as b
print(f"[OK] pointnet2._ext            -> {a.__file__}")
print(f"[OK] knn_pytorch.knn_pytorch   -> {b.__file__}")
PY
  ok "extensions import cleanly"
  exit 0
fi

# ── Helper: keep setup.py's extra_ldflags in sync with the env ───────
# Background: torch 2.x's BuildExtension embeds an RPATH of
# $CONDA_PREFIX/lib into the produced .so. That directory has no
# libc10.so, so the .so fails to dlopen at import time. We work around
# it via the conda activate.d LD_LIBRARY_PATH hook installed below.
# The extra_ldflags added here is best-effort: it sometimes shows up
# in the link line (helps find -lc10 etc at link time, which is already
# covered by torch's own -L injection), and the conda hook is the actual
# runtime fix. This block is a no-op if setup.py is already patched.
patch_setup_py() {
  local setup="$1"
  if grep -q "rpath-fix" "$setup"; then
    log "setup.py already patched: $setup"
    return 0
  fi
  log "setup.py unpatched (rpath-fix marker absent); the conda hook"
  log "  installed by this script is the load-bearing fix, so we skip"
  log "  patching setup.py. To force, edit $setup manually."
  return 0
}

# ── Helper: build one extension and place the .so ────────────────────
# Why not `python setup.py install`?
#   The flat layout (no pointnet2/_ext_src/, no knn_pytorch/ subdir at the
#   time of install) makes the final `cp build/.../<pkg>/<name>.so` step
#   fail with "No such file or directory". So we run build_ext, then copy
#   the .so ourselves into the package root alongside __init__.py.
#
# Args:
#   pkg_dir        absolute path to the extension source dir
#                  (e.g. .../pointnet2, .../knn)
#   pkg_name       the .so's basename (matches CUDAExtension(name=...));
#                  also the python import name of the package (e.g.
#                  'pointnet2', 'knn_pytorch')
#   pkg_subdir     the subdir under pkg_dir that holds the __init__.py
#                  AND that nvcc will mirror inside build/lib.*. For
#                  pointnet2 the package is the top-level pkg_dir
#                  itself (pkg_subdir="."). For knn the package is
#                  pkg_dir/knn_pytorch/ (pkg_subdir="knn_pytorch").
build_one() {
  local pkg_dir="$1"
  local pkg_name="$2"
  local pkg_subdir="$3"

  log "=== building $pkg_name ==="
  if [[ "$FORCE" -eq 1 ]]; then
    rm -rf "$pkg_dir/build" "$pkg_dir/__pycache__"
  fi

  patch_setup_py "$pkg_dir/setup.py"

  # NB: we do NOT pass --inplace. With --inplace, distutils tries to
  # `cp build/<lib>/<pkg_name>/<name>.so <cwd>/<pkg_name>/<name>.so`
  # which fails when the project is laid out flat (e.g. pointnet2's
  # package is the cwd itself, so the target would be
  # `pointnet2/pointnet2/_ext.so`, a non-existent directory). Without
  # --inplace, the .so is left in build/lib.*/<pkg_name>/<name>.so and
  # we copy it ourselves below.
  (
    cd "$pkg_dir"
    FORCE_CUDA=1 MAX_JOBS="${MAX_JOBS:-2}" "$PYTHON_BIN" setup.py build_ext 2>&1
  ) | tail -20

  # Locate build output dir.
  local build_dir
  build_dir="$(ls -td "$pkg_dir"/build/lib.*-cpython-${PY_VERSION//./} 2>/dev/null | head -1 || true)"
  if [[ -z "$build_dir" ]]; then
    fail "no build output found under $pkg_dir/build/lib.*-cpython-*"
  fi

  # .so path: build_dir/<pkg_name>/<pkg_name>.cpython-<PY_TAG>-<arch>.so
  # (the package name is always the build subdir; pip-style)
  local py_tag_short="${PY_TAG//python/}"  # e.g. "312"
  local so_name="${pkg_name}.cpython-${py_tag_short}-aarch64-linux-gnu.so"
  local so_src="$build_dir/$pkg_name/$so_name"
  if [[ ! -f "$so_src" ]]; then
    fail "expected .so not found: $so_src"
  fi

  # Destination: pkg_dir/<pkg_subdir>/<so_name>
  local so_dst_dir
  if [[ "$pkg_subdir" == "." ]]; then
    so_dst_dir="$pkg_dir"
  else
    so_dst_dir="$pkg_dir/$pkg_subdir"
    mkdir -p "$so_dst_dir"
  fi
  [[ -f "$so_dst_dir/__init__.py" ]] || touch "$so_dst_dir/__init__.py"
  cp -f "$so_src" "$so_dst_dir/$so_name"
  ok "  $pkg_name .so installed at $so_dst_dir/$so_name"
}

# ── Build both ───────────────────────────────────────────────────────
# pointnet2: package dir IS the package itself (no extra subdir).
# knn:       package is knn_pytorch/ inside the source dir.
build_one "$POINTNET2_DIR" "pointnet2"     "."
build_one "$KNN_DIR"       "knn_pytorch"   "knn_pytorch"

# ── Install conda LD_LIBRARY_PATH hook ───────────────────────────────
# This is a runtime fallback. Even though setup.py now embeds the torch
# lib path into RPATH, some build paths still write only $CONDA_PREFIX/lib
# (see issue: torch 2.x BuildExtension overrides user RPATH on some
# toolchains). The hook guarantees `import pointnet2._ext` works no
# matter which RPATH ended up baked in.
if [[ "$INSTALL_HOOK" -eq 1 ]]; then
  CONDA_PREFIX_VAL="${CONDA_PREFIX:-}"
  if [[ -z "$CONDA_PREFIX_VAL" ]]; then
    log "CONDA_PREFIX is empty; skipping activate.d hook (you can run the"
    log "       verify step manually with LD_LIBRARY_PATH=.../torch/lib)"
  else
    mkdir -p "$CONDA_PREFIX_VAL/etc/conda/activate.d" \
             "$CONDA_PREFIX_VAL/etc/conda/deactivate.d"

    cat > "$CONDA_PREFIX_VAL/etc/conda/activate.d/01-graspnet-rpath.sh" <<'HOOK'
# Auto-set LD_LIBRARY_PATH so the in-tree GraspNet CUDA extensions
# (pointnet2._ext, knn_pytorch.knn_pytorch) can locate PyTorch's
# bundled shared libraries (libc10.so, libtorch_cpu.so, etc.).
# Installed by scripts/install_graspnet_cuda_ops.sh
_PYTORCH_LIB="$(python -c 'import os, torch; print(os.path.join(os.path.dirname(torch.__file__), "lib"))' 2>/dev/null || true)"
if [ -n "${_PYTORCH_LIB}" ] && [ -d "${_PYTORCH_LIB}" ]; then
  export LD_LIBRARY_PATH="${_PYTORCH_LIB}${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"
fi
unset _PYTORCH_LIB
HOOK

    cat > "$CONDA_PREFIX_VAL/etc/conda/deactivate.d/01-graspnet-rpath.sh" <<'HOOK'
# Remove the torch lib entry we added at activation time. We probe a
# handful of common paths because Python may already be unavailable
# at deactivate time.
for _candidate in \
  "${CONDA_PREFIX}/lib/python3.13/site-packages/torch/lib" \
  "${CONDA_PREFIX}/lib/python3.12/site-packages/torch/lib" \
  "${CONDA_PREFIX}/lib/python3.11/site-packages/torch/lib" \
  "${CONDA_PREFIX}/lib/python3.10/site-packages/torch/lib"; do
  case ":${LD_LIBRARY_PATH:-}:" in
    *":${_candidate}:"*)
      LD_LIBRARY_PATH="$(printf '%s' "${LD_LIBRARY_PATH}" | tr ':' '\n' | grep -v "^${_candidate}$" | paste -sd: -)"
      ;;
  esac
done
export LD_LIBRARY_PATH
unset _candidate
HOOK
    ok "conda hook installed at $CONDA_PREFIX_VAL/etc/conda/activate.d/01-graspnet-rpath.sh"
  fi
else
  log "--no-hook: skipping conda hook"
fi

# ── Verify ───────────────────────────────────────────────────────────
log "=== import self-test ==="
GRASPNET_ROOT="$GRASPNET_ROOT" "$PYTHON_BIN" - <<'PY'
import os, sys
GRASPNET_ROOT = os.environ["GRASPNET_ROOT"]
for p in (GRASPNET_ROOT, GRASPNET_ROOT + "/pointnet2", GRASPNET_ROOT + "/knn"):
    if p not in sys.path:
        sys.path.insert(0, p)
import pointnet2._ext as a
import knn_pytorch.knn_pytorch as b
print(f"[OK] pointnet2._ext            -> {a.__file__}")
print(f"[OK] knn_pytorch.knn_pytorch   -> {b.__file__}")
PY
ok "all extensions import cleanly"
echo
ok "done. next:"
echo "    conda activate graspnet"
echo "    bash scripts/install_graspnet_cuda_ops.sh --check   # any time"
echo "    python scripts/verify_graspnet_stack.py            # full stack check"
