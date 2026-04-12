from __future__ import annotations

import ctypes
import importlib.util
import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType


DEFAULT_INFER_RELATIVE = Path("examples/WanVSR/infer_flashvsr_v1.1_tiny_long_video.py")
DEFAULT_WEIGHTS_RELATIVE = Path("examples/WanVSR/FlashVSR-v1.1")


class UpstreamConfigError(RuntimeError):
    pass


def preload_torch_libs() -> None:
    import torch

    libdir = Path(torch.__file__).resolve().parent / "lib"
    for name in ["libc10.so", "libtorch.so", "libtorch_cpu.so", "libtorch_cuda.so", "libc10_cuda.so"]:
        candidate = libdir / name
        if candidate.exists():
            ctypes.CDLL(str(candidate), mode=ctypes.RTLD_GLOBAL)


def resolve_infer_script(upstream_root: str | Path | None, infer_script: str | Path | None) -> Path:
    if infer_script:
        return Path(infer_script).expanduser().resolve()
    if upstream_root:
        return (Path(upstream_root).expanduser().resolve() / DEFAULT_INFER_RELATIVE).resolve()
    raise UpstreamConfigError("Need either --upstream-root or --infer-script")


def resolve_weights_dir(infer_script: str | Path, weights_dir: str | Path | None) -> Path:
    if weights_dir:
        return Path(weights_dir).expanduser().resolve()
    return (Path(infer_script).resolve().parent / "FlashVSR-v1.1").resolve()


@contextmanager
def _temporary_sys_path(path: Path):
    sys.path.insert(0, str(path))
    try:
        yield
    finally:
        if sys.path and sys.path[0] == str(path):
            sys.path.pop(0)
        elif str(path) in sys.path:
            sys.path.remove(str(path))


@contextmanager
def _temporary_cwd(path: Path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


@contextmanager
def upstream_runtime_dir(infer_script: str | Path, weights_dir: str | Path | None = None):
    infer_path = Path(infer_script).resolve()
    real_weights = resolve_weights_dir(infer_path, weights_dir)
    if not infer_path.exists():
        raise UpstreamConfigError(f"Infer script not found: {infer_path}")
    if not real_weights.exists():
        raise UpstreamConfigError(f"Weights directory not found: {real_weights}")

    expected_weights = infer_path.parent / "FlashVSR-v1.1"
    if expected_weights.resolve() == real_weights.resolve():
        with _temporary_cwd(infer_path.parent):
            yield infer_path.parent
        return

    with tempfile.TemporaryDirectory(prefix="flashvsr-upstream-runtime-") as temp_dir:
        runtime_dir = Path(temp_dir)
        os.symlink(real_weights, runtime_dir / "FlashVSR-v1.1")
        with _temporary_cwd(runtime_dir):
            yield runtime_dir


def load_upstream_module(infer_script: str | Path) -> ModuleType:
    infer_path = Path(infer_script).resolve()
    if not infer_path.exists():
        raise UpstreamConfigError(f"Infer script not found: {infer_path}")
    with _temporary_sys_path(infer_path.parent):
        spec = importlib.util.spec_from_file_location("flashvsr_upstream_infer", infer_path)
        if spec is None or spec.loader is None:
            raise UpstreamConfigError(f"Unable to load upstream module from {infer_path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
