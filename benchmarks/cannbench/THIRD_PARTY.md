# CANN Bench third-party dependency

CANN Bench is included as a Git submodule, not copied into Muyuan source ownership.

- Path: `benchmarks/cannbench/third_party/cann-bench`
- Upstream: `https://gitcode.com/cann/cann-bench.git`
- Pinned commit: `1ead26c77487cd20fbccf0c6b06e4b252e5dd2d3`
- License: `third_party/cann-bench/LICENSE`

Initialize it after cloning Muyuan:

```bash
git submodule update --init --recursive benchmarks/cannbench/third_party/cann-bench
```

The upstream project is source-oriented (`tool.uv.package = false`) and its full
runtime dependency set includes `torch_npu`. The Muyuan adapter therefore adds
the submodule's `src/` directory to `PYTHONPATH`; the non-NPU control-plane
environment does not install CANN Bench as a wheel or install `torch_npu`.

Upstream source changes must be made and reviewed in the CANN Bench repository.
Muyuan only owns the pinned gitlink and the adapter under `src/muyuan_cannbench/`.
