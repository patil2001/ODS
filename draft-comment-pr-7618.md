Related edge case in the same preflight math, worth covering here or as a follow-up once this lands:

When **Include vision is ticked** and the repo's projector is **already on disk**, `_hf_preflight` still adds `projector_bytes` to `needed` for every weights artifact that isn't installed (`routers/models.py`, around `:1411`):

```python
needed = 0 if artifact.get("installed") else _hf_artifact_size(artifact) + projector_bytes
```

The projector's local filename depends only on repo, revision and remote filename (`_hf_local_filename`, around `:1457-1477`). So every quant in the same repo shares one mmproj file on disk. However, `installed` is computed per weights artifact only (around `:1130-1143`), and nothing checks whether the projector file is already present.

**Scenario:**
1. Import Q4_K_M with vision. This downloads the weights plus about 0.9 GB of mmproj.
2. Open Q8_0 on a disk whose free space is above Q8 + margin but below Q8 + margin + 0.9 GB.
3. Preflight reports `insufficient` and the import is blocked, although the download would only need the weights.

The same happens when re-importing at the same revision after deleting only the weights.

**Possible fix:**

```python
projector_present = bool(projector) and _installed_model_path(
    _hf_local_filename(details["id"], projector["filename"], details["sha"])) is not None
needed = 0 if artifact.get("installed") else _hf_artifact_size(artifact) + (0 if projector_present else projector_bytes)
```

`_hf_artifact_fit` should keep counting the projector, because the memory it needs doesn't depend on whether the file is already on disk.

A test can cover both cases. Monkeypatch `_installed_model_path` to return a path for the projector only. With 10 GB of weights, a 1 GB projector and 10.5 GB free, assert disk `ok`. With the projector absent, assert `insufficient`.
