# `ai.common.models.ocr`

Loaders and user-facing classes for the three supported OCR engines:

| Engine  | Loader            | User class | Requirements file          |
| ------- | ----------------- | ---------- | -------------------------- |
| EasyOCR | `EasyOCRLoader`   | `EasyOCR`  | `requirements_easyocr.txt` |
| DocTR   | `DocTRLoader`     | `DocTR`    | `requirements_doctr.txt`   |
| Surya   | `SuryaLoader`     | `Surya`    | `requirements_surya.txt`   |

Each loader exposes `load / preprocess / inference / postprocess` for use by the model server and local-mode connectors. Each user class auto-detects model-server mode via `get_model_server_address()` and falls back to local execution otherwise.

## OpenCV

All three engines reach `cv2`, and all four `opencv-*` PyPI distributions write that same import directory — so only one can be active at a time, and the last one installed owns it. uv sees four unrelated distributions and will never report a conflict between them, which is why this needed solving outside the resolver.

It used to be solved here: an `ai.common.opencv` shim that every loader imported first, pinning all four distributions to `4.13.0.92`. **That shim is gone.** Ownership of `cv2` belongs to the engine's shared-namespace family mechanism (`lib/pkg_families/`, designed in `packages/server/design/virtual-environments.md` §4.16): it detects the family in an environment's own resolution, aligns every member on one version, installs them subset-first so the widest build writes the directory last, and then *proves* the result by importing `cv2` inside the environment it just built.

Upstream requirements, at the versions the engine currently resolves:

| Engine  | PyPI package         | Upstream OpenCV requirement          | Resolved here |
| ------- | -------------------- | ------------------------------------ | ------------- |
| EasyOCR | `easyocr` 1.7.2      | `opencv-python-headless` (unpinned)  | 4.13.0.92     |
| DocTR   | `python-doctr` 1.0.1 | `opencv-python <5.0.0, >=4.5.0`      | 4.13.0.92     |
| Surya   | `surya-ocr` 0.16.1   | `opencv-python-headless` (unpinned)  | 4.13.0.92     |

The version is no longer chosen, it is **derived** from what these three resolve, so the right-hand column is a measurement and moves when they do. Surya is the one to watch: `surya-ocr` **0.17** hard-pins `opencv-python-headless==4.11.0.86`, but nothing here asks for 0.17 and the resolver settles on 0.16.1, which pins no OpenCV at all. Why it settles there is not an OpenCV fact — see the note in `requirements_surya.txt`.

**Loader convention: import `cv2` directly, like any other third-party module.** The old rule — "import `from ai.common.opencv import cv2` *before* the engine's own imports" — bought an install ordering that the family mechanism now guarantees, and a new loader copying it would import a module that does not exist. What replaces it is a declaration rule: **a module that imports `cv2` and has no other source for it must name an `opencv-*` distribution in its own `requirements_*.txt`.** None of the three engines here does, because each pulls one in transitively; `nodes/image_cleanup` and `nodes/embedding_video` do, because the shim was their only source.

## Upstream docs

- [EasyOCR](https://github.com/JaidedAI/EasyOCR)
- [DocTR documentation](https://mindee.github.io/doctr/)
- [Surya](https://github.com/VikParuchuri/surya)
