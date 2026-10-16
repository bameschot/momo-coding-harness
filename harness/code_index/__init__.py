"""In-memory project index behind the index_* tools.

One ProjectIndex per working directory holds, for every text file in the
project: the symbols and imports code_nav extracts (keys, selectors, tables and
stages for config/markup files), an occurrence index of every identifier, and a
per-file trigram signature for full-text search.  A background thread builds it
and keeps it current; every index_* tool first calls wait_fresh(), so a result
is never computed from a stale index.

Memory is budgeted (/index-max-mem, default 100 MB).  Over budget the index
degrades in a fixed order — trigram signatures, then the occurrence index, then
it stops adding files — and says so in every affected result.

Which files it covers is decided by a gitignore-syntax filter
(ignore_rules, /index-filter) kept next to the pickle in ~/.momo-harness/index/.
It is seeded once from the project's .gitignore files; after that only the
filter decides.

Bulk work (the first build, a rebuild, a branch switch) is read and parsed in
worker processes (/index-workers): tree-sitter holds the GIL, so threads would
not run in parallel.  Results are still applied by the one indexer thread.

The index can be saved to and loaded from a pickle in ~/.momo-harness/index/,
through a restricted unpickler, so a tampered file fails to load instead of
running code.

Modules, lowest first (each imports only modules above it):

  records  FileEntry, memory estimates, trigram signatures, language families
  persist  the pickle and the filter file
  build    reading and parsing one file (indexer thread or worker process)
  store    ProjectIndex: the store, its indexer thread, budget and settings
  common   what every answer shares: size caps, path= scope, notes
  text     index_text, find_line_text
  graph    the import graph and file ranks: index_map, index_file
  search   index_search, index_find_files
  callers  index_callers (callers and callees), uses_of, query_target
  changes  index_changes: a git diff's definitions and who still uses them
  status   index_status, /index's status text and memory breakdown

Under them: resolve (import resolution by each language's rules) and defdiff
(a file's definitions before and after a change).

The query modules work on ProjectIndex's internals under its lock (same
package); the rest of the harness uses the names exported here and
ProjectIndex's public methods (rel, has_file, ...).
"""
from .callers import index_callers, query_target, uses_of
from .changes import index_changes
from .graph import index_file, index_map
from .persist import filter_path, load_filter, pickle_path, save_filter
from .records import DATA_LANGS, lang_family
from .search import index_find_files, index_search
from .status import breakdown, index_status, skip_label, status_text, workers_label
from .store import (DEFAULT_MAX_BYTES, DEFAULT_MAX_FILES, MAX_WORKERS, ProjectIndex, auto_workers,
                    parse_max_files, parse_max_mem, parse_workers)
from .text import find_line_text, index_text

__all__ = ["DATA_LANGS", "DEFAULT_MAX_BYTES", "DEFAULT_MAX_FILES", "MAX_WORKERS", "WAIT_TOOLS",
           "ProjectIndex", "auto_workers", "breakdown", "filter_path", "find_line_text",
           "index_callers", "index_changes", "index_file", "index_find_files", "index_map",
           "index_search", "index_status", "index_text", "lang_family", "load_filter",
           "parse_max_files", "parse_max_mem", "parse_workers", "pickle_path", "query_target",
           "save_filter", "skip_label", "status_text", "uses_of", "workers_label"]

# Tools that must see an up-to-date index before they run.
WAIT_TOOLS = {"index_search", "index_text", "index_callers", "index_map", "index_file",
              "index_changes", "code_outline", "find_symbol"}
