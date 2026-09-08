# scripts

The deterministic parts of the pipeline: **Python 3, standard library only** —
no pip dependencies. These are the steps that must never become model
judgments — a status code or a word count already answers the question, so
plain code answers it:

- run manifest create / read / update (`manifest.py`)
- operating-context append, serialized and provenance-stamped (`context.py`)
- content length (`length.py`)
- transcript overlap % with a source, in words and phrases (`overlap.py`)
- claim diff (`claimdiff.py`)
- anti-slop lexical/structural checks (`antislop.py`)

Each ships with fixtures under `tests/` covering its false-positive case, not
only its positive one: a guard that only ever fires is a blanket rule, and
blanket rules are the main source of regressions on varied input.

Run the tests with:

```
python3 -m unittest discover -s tests
```
