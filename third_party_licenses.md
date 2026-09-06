# Third-party dependency inventory

Generated 2026-09-06 from this workspace's installed Athena Python environment
using `python scripts/release_dependency_inventory.py`. This includes development
and transitive packages. It is not the historical TESS server environment and
is not an assertion that every entry is shipped in the standalone inference
bundle. Athena resolution is controlled by root uv.lock; the TESS runtime has
its own pinned requirements.

The repository's own LICENSE remains MIT. Package metadata reports PyMuPDF's
AGPL/commercial choice and qoder-agent-sdk's proprietary licensing. Their terms
must be reviewed before redistributing a bundled Athena environment; they are
not dependencies of the standalone TESS inference path. UNKNOWN entries need
review. Keep upstream licenses/notices with redistributed wheels and native
libraries. This inventory is not a license grant or legal clearance.

GUI JavaScript and Rust dependencies are not installed or bundled by the TESS
inference command. Any separate GUI binary distribution needs its own inventory
from package-lock.json/Cargo.lock and bundled assets, including fonts.

## Installed Athena Python metadata

| Distribution | Installed version | Declared license metadata |
| --- | --- | --- |
| aiofile | 3.12.3 | Apache-2.0 |
| annotated-types | 0.8.0 | MIT |
| anthropic | 1.2.0 | OSI Approved :: MIT License |
| anyio | 4.14.2 | MIT |
| argcomplete | 3.7.2 | OSI Approved :: Apache Software License |
| athena-ai4s | 0.1.0 | UNKNOWN; review upstream |
| attrs | 26.1.0 | MIT |
| Authlib | 1.7.2 | OSI Approved :: BSD License |
| beartype | 0.22.9 | OSI Approved :: MIT License |
| black | 26.5.1 | MIT |
| cachetools | 7.1.7 | MIT |
| caio | 0.12.2 | Apache-2.0 |
| certifi | 2026.7.22 | OSI Approved :: Mozilla Public License 2.0 (MPL 2.0) |
| cffi | 2.1.1 | MIT-0 |
| cfgv | 3.5.0 | MIT |
| charset-normalizer | 3.5.1 | MIT |
| click | 8.5.0 | BSD-3-Clause |
| colorama | 0.4.6 | OSI Approved :: BSD License |
| contourpy | 1.3.3 | OSI Approved :: BSD License |
| cryptography | 50.0.1 | Apache-2.0 OR BSD-3-Clause |
| cycler | 0.12.1 | OSI Approved :: BSD License |
| distlib | 0.4.3 | OSI Approved :: Python Software Foundation License |
| distro | 1.9.0 | OSI Approved :: Apache Software License |
| dnspython | 2.8.0 | OSI Approved :: ISC License (ISCL) |
| docstring_parser | 0.18.0 | OSI Approved :: MIT License |
| email-validator | 2.3.0 | OSI Approved :: The Unlicense (Unlicense) |
| exceptiongroup | 1.3.1 | OSI Approved :: MIT License |
| executing | 2.2.1 | OSI Approved :: MIT License |
| fastmcp-slim | 3.4.7 | Apache-2.0 |
| filelock | 3.32.4 | MIT |
| fonttools | 4.63.0 | MIT |
| genai-prices | 0.1.4 | MIT |
| google-auth | 2.57.0 | OSI Approved :: Apache Software License |
| google-genai | 2.20.0 | Apache-2.0 |
| googleapis-common-protos | 1.75.2 | OSI Approved :: Apache Software License |
| griffelib | 2.2.0 | ISC |
| h11 | 0.16.0 | OSI Approved :: MIT License |
| httpcore | 1.0.9 | BSD-3-Clause |
| httpcore2 | 2.12.0 | BSD-3-Clause |
| httpx | 0.28.1 | OSI Approved :: BSD License |
| httpx-sse | 0.4.3 | MIT |
| httpx2 | 2.12.0 | BSD-3-Clause |
| identify | 2.6.19 | MIT |
| idna | 3.19 | BSD-3-Clause |
| iniconfig | 2.3.0 | MIT |
| jaraco.classes | 3.4.0 | OSI Approved :: MIT License |
| jaraco.context | 6.1.2 | MIT |
| jaraco.functools | 4.6.0 | MIT |
| jiter | 0.16.0 | MIT |
| joblib | 1.5.3 | BSD-3-Clause |
| joserfc | 1.7.4 | OSI Approved :: BSD License |
| jsonschema | 4.26.0 | MIT |
| jsonschema-specifications | 2025.9.1 | MIT |
| kagglehub | 1.0.2 | OSI Approved :: Apache Software License |
| kagglesdk | 0.1.37 | OSI Approved :: Apache Software License |
| keyring | 25.7.0 | MIT |
| kiwisolver | 1.5.1 | OSI Approved :: BSD License |
| logfire | 4.41.0 | MIT |
| logfire-api | 4.41.0 | MIT |
| markdown-it-py | 4.2.0 | OSI Approved :: MIT License |
| matplotlib | 3.11.1 | OSI Approved :: Python Software Foundation License |
| mcp | 1.29.1 | OSI Approved :: MIT License |
| mdurl | 0.1.2 | OSI Approved :: MIT License |
| more-itertools | 11.1.0 | MIT |
| mypy_extensions | 1.1.0 | MIT |
| narwhals | 2.25.0 | MIT |
| nodeenv | 1.10.0 | OSI Approved :: BSD License |
| numpy | 2.5.2 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 |
| openai | 3.6.0 | Apache-2.0 |
| opentelemetry-api | 1.44.0 | Apache-2.0 |
| opentelemetry-exporter-otlp-proto-common | 1.44.0 | Apache-2.0 |
| opentelemetry-exporter-otlp-proto-http | 1.44.0 | Apache-2.0 |
| opentelemetry-instrumentation | 0.65b0 | Apache-2.0 |
| opentelemetry-instrumentation-httpx | 0.65b0 | Apache-2.0 |
| opentelemetry-proto | 1.44.0 | Apache-2.0 |
| opentelemetry-sdk | 1.44.0 | Apache-2.0 |
| opentelemetry-semantic-conventions | 0.65b0 | Apache-2.0 |
| opentelemetry-util-http | 0.65b0 | Apache-2.0 |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause |
| pandas | 3.0.5 | OSI Approved :: BSD License |
| pathspec | 1.1.1 | OSI Approved :: Mozilla Public License 2.0 (MPL 2.0) |
| pillow | 12.3.0 | MIT-CMU |
| platformdirs | 4.11.5 | MIT |
| pluggy | 1.6.0 | OSI Approved :: MIT License |
| pre_commit | 4.6.2 | MIT |
| prompt_toolkit | 3.0.53 | OSI Approved :: BSD License |
| protobuf | 7.36.0 | 3-Clause BSD License |
| py-key-value-aio | 0.4.5 | Apache-2.0 |
| pyasn1 | 0.6.4 | BSD-2-Clause |
| pyasn1_modules | 0.4.2 | OSI Approved :: BSD License |
| pycparser | 3.0 | BSD-3-Clause |
| pydantic | 2.13.5 | MIT |
| pydantic-ai | 2.36.0 | MIT |
| pydantic-ai-slim | 2.36.0 | MIT |
| pydantic-evals | 2.36.0 | MIT |
| pydantic-graph | 2.36.0 | MIT |
| pydantic-settings | 2.15.0 | MIT |
| pydantic_core | 2.46.5 | MIT |
| Pygments | 2.21.0 | BSD-2-Clause |
| PyJWT | 2.13.0 | MIT |
| pylatexenc | 2.11 | OSI Approved :: MIT License |
| pymupdf | 1.28.2 | Dual Licensed - GNU AFFERO GPL 3.0 or Artifex Commercial License |
| pyparsing | 3.3.2 | MIT |
| pyperclip | 1.11.0 | OSI Approved :: BSD License |
| pytest | 9.1.1 | MIT |
| pytest-asyncio | 1.4.0 | Apache-2.0 |
| python-dateutil | 2.9.0.post0 | OSI Approved :: BSD License; OSI Approved :: Apache Software License |
| python-discovery | 1.6.0 | OSI Approved :: MIT License |
| python-dotenv | 1.2.3 | BSD-3-Clause |
| python-multipart | 0.0.32 | Apache-2.0 |
| pytokens | 0.4.1 | OSI Approved :: MIT License |
| pywin32 | 312 | OSI Approved :: Python Software Foundation License |
| pywin32-ctypes | 0.2.3 | BSD-3-Clause |
| PyYAML | 6.0.3 | OSI Approved :: MIT License |
| qoder-agent-sdk | 1.0.12 | Other/Proprietary License |
| referencing | 0.37.0 | MIT |
| regex | 2026.7.19 | Apache-2.0 AND CNRI-Python |
| requests | 2.34.2 | OSI Approved :: Apache Software License |
| rich | 15.0.0 | OSI Approved :: MIT License |
| rpds-py | 2026.6.3 | MIT |
| ruff | 0.16.3 | MIT |
| scikit-learn | 1.9.0 | BSD-3-Clause |
| scipy | 1.18.1 | OSI Approved :: BSD License |
| six | 1.17.0 | OSI Approved :: MIT License |
| sniffio | 1.3.1 | OSI Approved :: MIT License; OSI Approved :: Apache Software License |
| sse-starlette | 3.4.8 | BSD-3-Clause |
| starlette | 1.6.0 | BSD-3-Clause |
| tenacity | 9.1.4 | OSI Approved :: Apache Software License |
| threadpoolctl | 3.6.0 | OSI Approved :: BSD License |
| tiktoken | 0.14.0 | MIT License |
| tqdm | 4.70.0 | MPL-2.0 AND MIT |
| truststore | 0.10.4 | MIT |
| typing-inspection | 0.4.4 | MIT |
| typing_extensions | 4.16.0 | PSF-2.0 |
| tzdata | 2026.3 | Apache-2.0 |
| urllib3 | 2.7.0 | MIT |
| uvicorn | 0.52.4 | BSD-3-Clause |
| virtualenv | 21.7.7 | MIT |
| wcwidth | 0.8.3 | MIT |
| websockets | 16.1.1 | BSD-3-Clause |
| wrapt | 2.3.0 | BSD-2-Clause |
