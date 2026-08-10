# ocr

RocketRide filter nodes that extract machine-readable text — and, on the standard component, tables — from images using optical character recognition.

## What it does

Since the 2A-4 split this directory ships **two** providers out of one package. They share an icon and a docs page and nothing else: separate module paths, separate protocols, separate dependency sets.

| Component    | Protocol       | Module path          | Engines        | Lanes out       |
| ------------ | -------------- | -------------------- | -------------- | --------------- |
| **standard** | `ocr://`       | `nodes.ocr.standard` | EasyOCR, DocTR | `text`, `table` |
| **Surya**    | `ocr_surya://` | `nodes.ocr.surya`    | Surya          | `text`          |

The `ocr://` protocol is unchanged, so existing `.pipe` documents keep resolving — only the module path behind it moved down a level.

The point of the split is the dependency set. Surya's stack (`surya-ocr`, and the `transformers` floor it carries) no longer has to coexist with EasyOCR, DocTR and img2table in one environment, so the Surya component can run inside a **Virtual Environment container** with an overlay that holds none of them. The package root deliberately carries no `requirements.txt` and its `__init__.py` imports nothing: an ancestor package's requirement files are inherited by every component beneath it, and an ancestor's `__init__.py` is *executed* at import while never being walked — either one would put the standard component's dependencies back into the Surya child.

Both turn visual content (scanned documents, screenshots, photos) into text for downstream analysis. Both are GPU-capable and registered as filters. OCR reads are serialised with an internal threading lock so concurrent instances share one engine safely, and animated GIFs are handled frame by frame — each frame is OCR'd individually and the per-frame texts are joined with newlines.

**standard** supports two OCR engines via the `ai.common.models` model-server wrappers: **EasyOCR** (multi-language, the default) and **DocTR** (document-focused, language-agnostic). The wrappers auto-detect whether to call a remote model server or fall back to local inference. An unknown engine name falls back to EasyOCR silently; `surya` is the one exception and raises, naming the component to point the pipeline at.

Its table extraction uses **img2table** for OpenCV-based table structure detection, with OCR inference routed through the same model-server adapter (`ModelServerOCR`). Detected tables are emitted on the `table` lane as Markdown. Both img2table v1 and v2 plug-in APIs are supported.

**Surya** supports one engine and has **no configuration at all** — no engine picker, no script family, no table settings. Surya recognition is multilingual and auto-detecting, so there is no language list to choose. It has no table stack either: its `image` lane produces `text` only.

Its engine is held at `surya-ocr==0.16.1`, and the pin is load-bearing rather than cautious. Before the split that line was a bare name kept at 0.16.1 by an accident of the global compile — `ai/**` is resolved there beside `transformers==4.53.3`, which nothing newer admits. A scoped environment never sees that pin, so the bare name floated: unbounded it took 0.22.1, where the API this loader targets no longer exists, and `>=0.16,<0.17` took 0.16.7, whose looser bounds let `transformers` reach 5.x and break Surya from the other side. Pinning the exact release fixes both with one bound, because 0.16.1 declares `transformers >=4.51.2,<4.54.0` itself. Lifting it to `>=0.17,<0.18` needs the tree-wide `transformers` move.

## Example pipelines

**Extract and redact document text**

`webhook → parse → ocr → ner → anonymize_text → response_text`

<div align="center">

![The OCR node on the canvas extracting text from parsed documents in a redaction pipeline](example.png)

[![Download example.pipe](https://img.shields.io/badge/example.pipe-Download-41b6e6?style=for-the-badge)](example.pipe)

</div>

`parse` feeds document images to OCR and also sends text directly to NER.
Recognized and parsed text is tagged, anonymized, and returned as redacted
text.

**Invoice tables into a database**

`webhook → ocr → extract_data → db_postgres`

Invoice images come in and the `table` lane extracts their line items as
Markdown tables; `extract_data` structures the fields and PostgreSQL stores
the rows — paper documents to queryable data with no manual entry.

**Searchable archive of scanned records**

`webhook → ocr → embedding_transformer → store_qdrant`

OCR converts a scanned archive to text, the embedding node vectorizes it, and
the vector store makes it searchable by meaning rather than filename.

## Lanes

**standard** (`ocr://`):

| Lane in     | Lane out | Description                       |
| ----------- | -------- | --------------------------------- |
| `documents` | `text`   | Extract text from image documents |
| `image`     | `text`   | Extract text from a raw image     |
| `image`     | `table`  | Extract tables from a raw image   |

**Surya** (`ocr_surya://`):

| Lane in     | Lane out | Description                       |
| ----------- | -------- | --------------------------------- |
| `documents` | `text`   | Extract text from image documents |
| `image`     | `text`   | Extract text from a raw image     |

On the `documents` lane, every incoming document must be of type `Image` (both components raise a `ValueError` otherwise). Each image document is OCR'd and re-emitted as a `Document`-type copy whose `page_content` is the extracted text. The original image documents are not forwarded: if a downstream node needs the images themselves, connect it to the source node directly.

## Profiles

Profiles are preconfigured combinations of engine, script family, and table engine, and belong to the standard component. Selecting a profile sets all three at once. Default: **Latin (English)**
(`latin`).

| Profile key            | Title                         | Engine  | Script family         | Table engine |
| ---------------------- | ----------------------------- | ------- | --------------------- | ------------ |
| `latin` **(default)**  | Latin (English)               | EasyOCR | `latin`               | DocTR        |
| `latin-extended`       | Latin Extended (European)     | EasyOCR | `latin-extended`      | DocTR        |
| `cyrillic`             | Cyrillic (Russian, etc.)      | EasyOCR | `cyrillic`            | DocTR        |
| `arabic`               | Arabic/Persian/Urdu           | EasyOCR | `arabic`              | DocTR        |
| `devanagari`           | Devanagari (Hindi, etc.)      | EasyOCR | `devanagari`          | DocTR        |
| `chinese-simplified`   | Chinese (Simplified)          | EasyOCR | `chinese-simplified`  | DocTR        |
| `chinese-traditional`  | Chinese (Traditional)         | EasyOCR | `chinese-traditional` | DocTR        |
| `japanese`             | Japanese                      | EasyOCR | `japanese`            | DocTR        |
| `korean`               | Korean                        | EasyOCR | `korean`              | DocTR        |
| `doctr`                | DocTR (Language-agnostic)     | DocTR   | `latin` (unused)      | DocTR        |

## Configuration

**The Surya component has no configuration fields.** Everything below belongs to the standard component: each entry is an engine picker, an engine-specific option or a table setting, and none of them is read by a single-engine, table-free component. Surya is configured by *what it is placed beside*, not by settings of its own.

Pick a profile and you can usually ignore everything else — the remaining
fields exist to override what the profile chose. The main settings panel
exposes `ocr.profile`, `ocr.engine`, `ocr.script_family`, and
`ocr.table_engine`; the DocTR architecture fields are for table extraction
tuning.

### OCR Engine

Overrides the engine the profile selected. The trade-offs: **EasyOCR** covers
the most languages via script families and is the safe default. **DocTR** is
language-agnostic and strongest on structured documents (forms, dense
layouts). An unknown engine name silently falls back to EasyOCR; `surya` is
the one exception and raises, because Surya moved to its own component
(`ocr_surya://`).

### Script Family

Script families map to EasyOCR language code lists — this field picks which
set of language models EasyOCR loads. It applies **only when the engine is
EasyOCR**; DocTR ignores it. The Surya component has no such setting: its
recognition is multilingual and auto-detecting. A wrong script family is the
most common cause of garbage output on non-English documents: if Russian
scans come out as Latin gibberish, check this field first.

Every family except plain `latin` also loads English as a fallback:

| Family                | Languages loaded                                                                                                                       |
| --------------------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| `latin`               | `en` only (English only, for reliability)                                                                                              |
| `latin-extended`      | `en` plus ~28 Latin-script languages: fr, de, es, it, pt, nl, pl, ro, cs, sk, hu, hr, sl, sq, lt, lv, da, no, sv, id, ms, tl, vi, tr, az, uz, sw, la, oc |
| `cyrillic`            | ru, uk, be, bg, rs_cyrillic, mn, en (Macedonian not supported by EasyOCR; Serbian maps to `rs_cyrillic`)                              |
| `arabic`              | ar, fa, ur, ug, en                                                                                                                     |
| `devanagari`          | hi, mr, ne, en                                                                                                                         |
| `bengali`             | bn, as, en                                                                                                                             |
| `chinese-simplified`  | ch_sim, en                                                                                                                             |
| `chinese-traditional` | ch_tra, en                                                                                                                             |
| `japanese`            | ja, en                                                                                                                                 |
| `korean`              | ko, en                                                                                                                                 |
| `thai`                | th, en                                                                                                                                 |
| `tamil`               | ta, en                                                                                                                                 |
| `telugu`              | te, en                                                                                                                                 |

The `bengali`, `thai`, `tamil`, and `telugu` families are selectable via `ocr.script_family` but have no preconfigured profile. The Japanese EasyOCR models may misread English text; the full test suite uses a relaxed assertion (`contains: "quick"` instead of `"quick brown fox"`) for that profile.

### Detection / Recognition Architecture (DocTR)

Model choices for DocTR's two-stage table pipeline: detection finds where text
is, recognition reads it. The defaults (`db_resnet50` + `crnn_vgg16_bn`) are
right for nearly everyone; alternatives trade accuracy against speed and
memory — see the [DocTR model docs](https://mindee.github.io/doctr/latest/using_doctr/using_models.html)
before changing them.

Detection architectures: `linknet_resnet18`, `linknet_resnet34`, `linknet_resnet50`, `db_resnet50`, `db_mobilenet_v3_large`, `fast_tiny`, `fast_small`, `fast_base`.

Recognition architectures: `crnn_vgg16_bn`, `crnn_mobilenet_v3_small`, `crnn_mobilenet_v3_large`, `sar_resnet31`, `master`, `vitstr_small`, `vitstr_base`, `parseq`.

### Table OCR Engine

Engine used for table text extraction (default DocTR, which is optimized for
document tables; EasyOCR is a general-purpose alternative). This is
independent of the main `engine` field — text and tables can use different
engines.

## Requirements

GPU-capable: engines run on CUDA when available and fall back to CPU (slower,
same results). Engine models are fetched through the `ai.common.models`
model-server wrappers, which auto-detect whether to call a remote model
server or run local inference; models download on first use.

## Notes

### Composition: text plus tables

The two components compose rather than replace each other. Place the Surya component in a Virtual Environment container and the standard `ocr` node beside it in the main environment: Surya reads the text, standard reads the tables with DocTR or EasyOCR cells.

What that combination cannot give you is **Surya-recognised table cells**. Surya cells in the table stack would mean importing Surya beside img2table, which is exactly the environment being separated. The `surya` profile that shipped both in one click (`engine: surya` *and* `table_engine: surya`) is gone, and there is no replacement for its table half.

### Migrating a saved pipeline

| Old config            | What happens now                                                          |
| --------------------- | ------------------------------------------------------------------------- |
| `engine: surya`       | **Raises** at reader construction, naming `ocr_surya://`                  |
| `table_engine: surya` | **Raises** when `ModelServerOCR` is built, i.e. at node startup           |
| `profile: surya`      | The profile is gone from the picker; set the two values above by hand     |
| `engine: trocr`       | Still loads, still silently returns EasyOCR results                       |
| `profile: trocr`      | Gone from the picker; the runtime fallback above keeps the pipeline alive |

The asymmetry is deliberate. Surya has somewhere to go, so saying so is more useful than degrading; TrOCR was deleted from the tree in increment 2.5 and has nowhere to point, so its silent fallback stays and only the *offer* was retired.

`table_engine` validation happens at construction, not on first use. Every path that reaches the table engine lazily swallows its errors — `content()` warns, `of()` returns `None`, and `IInstance.extract_tables_from_image` wraps the call a third time so a table failure cannot kill text OCR — so a deferred raise would mean "node starts, text works, tables silently produce nothing". One side effect is kept on purpose: **every** unrecognised `table_engine` now fails at startup, not just `surya`.

### OpenCV

Both OCR engines and img2table reach `cv2`, and all four `opencv-*` PyPI distributions write that same import directory — only one can be active at a time, and the last one installed owns it. uv sees unrelated distributions and never reports a conflict between them.

The node used to settle this itself, importing an `ai.common.opencv` shim that pinned all four distributions to `4.13.0.92`. That shim is gone: `cv2` is now owned by the engine's shared-namespace family mechanism (`lib/pkg_families/`, `virtual-environments.md` §4.16), which aligns every member an environment resolves onto one version, installs them subset-first so the widest build writes the directory last, and verifies the result by importing `cv2` inside the finished environment. Nothing in this node pins OpenCV, and nothing should.

Upstream requirements in the **standard** component's environment, at the versions the engine currently resolves:

| Consumer  | PyPI package         | Upstream OpenCV requirement          | Resolved here |
| --------- | -------------------- | ------------------------------------ | ------------- |
| EasyOCR   | `easyocr` 1.7.2      | `opencv-python-headless` (unpinned)  | 4.14.0.94     |
| DocTR     | `python-doctr` 1.0.1 | `opencv-python <5.0.0, >=4.5.0`      | 4.14.0.94     |
| img2table | `img2table` 2.0.0    | `opencv-contrib-python`              | 4.14.0.94     |

img2table is why "widest build last" matters here: it calls `cv2.ximgproc.niBlackThreshold`, which only the `contrib` builds carry, and the family's probe asserts `cv2.ximgproc` in exactly the environments that installed one. That is a property of the resolution, not of an import order — `IGlobal.py` imports img2table like any other module, and the ordering that used to be bought by importing a shim first is now bought by the install itself.

The **Surya** component is the contrast, and it is the clearest thing the split does to this problem: its environment resolves **one** family member — `opencv-python-headless`, which `surya-ocr` 0.16.1 requests as `>=4.11.0.86,<5.0.0.0` and which lands on the same 4.14.0.94. There is nothing to align against, no subset-first ordering to impose, and no `ximgproc` for the probe to assert; the probe still fires and still proves the import. Measured on a live scoped run: a four-way same-directory contest became a three-way one plus a singleton.

### img2table version compatibility

This applies to the standard component only; the Surya component has no table stack.

img2table 2.0 (released 2026-05-10) reorganised the OCR plug-in API. The node supports both v1 and v2:

| Symbol / location                  | img2table v1      | img2table v2      |
| ---------------------------------- | ----------------- | ----------------- |
| `OCRInstance` base class           | `img2table.ocr.base` | `img2table.ocr._types` |
| Result type returned by `of()`     | `OCRDataframe` (`img2table.ocr.data`) | `OCRData` (`img2table.ocr._types`) |
| Plug-in contract                   | `content()` + `to_ocr_dataframe()` | single `of()` override |

The `_IMG2TABLE_V2` flag is set at import time and gates each code path. `standard/external_contracts.py` declares version-tagged import requirements so the `check-externals` CI framework can validate the correct symbols on whichever version is installed — it sits inside `standard/` because that directory, not the package root, is the contract-check component.

## Upstream docs

- [EasyOCR](https://github.com/JaidedAI/EasyOCR)
- [DocTR documentation](https://mindee.github.io/doctr/)
- [Surya](https://github.com/VikParuchuri/surya)

---

<!-- ROCKETRIDE:GENERATED:PARAMS START -->
<!-- Generated by nodes:docs-generate. Do not edit by hand. -->

## Schema

| Field | Type | Description | Default |
|---|---|---|---|
| `ocr.det_arch` | `string` | **Detection Architecture (DocTR)**<br/>Choose the architecture used for table text detection.<br/> Documentation: https://mindee.github.io/doctr/latest/using_doctr/using_models.html | `"db_resnet50"` |
| `ocr.engine` | `string` | **OCR Engine**<br/>Select the OCR engine for text extraction. EasyOCR supports many languages with script families. DocTR is language-agnostic and good for documents. Surya supports multi-language. TrOCR uses transformer models. | `"easyocr"` |
| `ocr.profile` | `string` | **OCR Profile**<br/>Select a preconfigured OCR profile optimized for different languages and use cases. | `"latin"` |
| `ocr.reco_arch` | `string` | **Recognition Architecture (DocTR)**<br/>Choose the architecture used for table text recognition.<br/> Documentation: https://mindee.github.io/doctr/latest/using_doctr/using_models.html | `"crnn_vgg16_bn"` |
| `ocr.script_family` | `string` | **Script Family**<br/>Select the script family for OCR. This determines which languages are loaded for text recognition. Only applies to EasyOCR engine. | `"latin"` |
| `ocr.table_engine` | `string` | **Table OCR Engine**<br/>Select the OCR engine used for table text extraction. DocTR is optimized for document tables. EasyOCR and Surya are general-purpose alternatives. | `"doctr"` |

## Dependencies

- `img2table`
- `pillow`
- `numpy`

## Source

[<svg viewBox="0 0 16 16" width="15" height="15" fill="currentColor" aria-hidden="true" style="vertical-align:-0.15em;margin-right:0.35em"><path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z"/></svg> View source](https://github.com/rocketride-org/rocketride-server/tree/develop/nodes/src/nodes/ocr)
<!-- ROCKETRIDE:GENERATED:PARAMS END -->
