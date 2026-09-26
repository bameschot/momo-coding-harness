# Web Search Sources Configuration Summary

## Overview

This document lists all web search sources configured for the momo-coding-harness project.

Sources are organized into two categories:

| Category | Description |
|----------|-------------|
| **Already existed** | Sources that were pre-configured in the harness |
| **Added for this project** | Sources added based on project analysis |

---

## Already Existed

| Name | What it finds | Registry |
|------|---------------|----------|
| crates | Rust crates with latest stable version, description | crates.io |
| github | GitHub repositories (libraries, tools, example projects) | GitHub |
| maven | Maven Central artifacts with latest version | Maven Central |
| mdn | HTML, CSS, JavaScript and Web API docs | MDN Web Docs |
| npm | npm packages with latest version, description | npm |
| osv | Known vulnerabilities (query: `Ecosystem:package@version`) | OSV |
| pypi | PyPI packages (exact package-name lookup) | PyPI |
| stackoverflow | Stack Overflow questions; `read=true` returns top answers | Stack Overflow |
| wikipedia | English Wikipedia articles: concepts, algorithms, protocols, history | Wikipedia |

---

## Added for This Project

### 1. dockerhub

**What it finds:** Docker Hub images/repositories

**Spec:**
```json
{
  "name": "dockerhub",
  "description": "Docker Hub images",
  "url": "https://hub.docker.com/v2/search/repositories/?query={query}&page_size={n}",
  "results": "results",
  "title": "repo_name",
  "link": "https://hub.docker.com/r/{repo_name}",
  "snippet": "short_description",
  "extra": {"stars": "star_count"}
}
```

**Rationale:** The project includes `tree-sitter-dockerfile` in requirements.txt and the README explicitly mentions Dockerfile support. This source enables the model to look up Docker images when working with containerization.

**Status:** ✅ Added

**Test query:** `nginx` → returned official Nginx build and related images

---

### 2. wikidata

**What it finds:** Wikidata entities (people, places, things) with one-line descriptions

**Spec:**
```json
{
  "name": "wikidata",
  "description": "Wikidata entities: people, places, things, with a one-line description",
  "url": "https://www.wikidata.org/w/api.php?action=wbsearchentities&search={query}&language=en&format=json&limit={n}",
  "results": "search",
  "title": "label",
  "link": "concepturi",
  "snippet": "description"
}
```

**Rationale:** Provides general subject matter knowledge for factual concepts, people, places, historical events, etc. Useful when the model needs to answer questions about real-world topics or when working on projects with domain-specific terminology.

**Status:** ✅ Added

**Test query:** `dragon` → returned multiple Wikidata entities including the legendary creature

---

### 3. gomod

**What it finds:** Go module information via deps.dev

**Spec:**
```json
{
  "name": "gomod",
  "description": "Go module lookup by exact module path",
  "url": "https://api.deps.dev/v3/systems/go/packages/{query}",
  "results": "",
  "title": "packageKey.name",
  "link": "https://pkg.go.dev/{packageKey.name}"
}
```

**Rationale:** The project supports multiple languages including Go (`.go` files detected in file type analysis). deps.dev provides comprehensive Go module metadata including versions, dependencies, and vulnerability information.

**Status:** ✅ Added

**Test query:** `github.com/gorilla/mux` → returned correct package information

---

## Final Summary Table

| Name | What it finds | Status |
|------|---------------|--------|
| crates | Rust crates (crates.io) | ✅ Added |
| github | GitHub repositories | ✅ Added |
| maven | Maven Central artifacts | ✅ Added |
| mdn | HTML/CSS/JS/Web API docs | ✅ Added |
| npm | npm packages | ✅ Added |
| osv | Known vulnerabilities | ✅ Added |
| pypi | PyPI packages | ✅ Added |
| stackoverflow | Stack Overflow Q&A | ✅ Added |
| wikipedia | Wikipedia articles | ✅ Added |
| **dockerhub** | Docker Hub images | ✅ **Added** |
| **wikidata** | Wikidata entities | ✅ **Added** |
| **gomod** | Go modules (deps.dev) | ✅ **Added** |
| rtd-project | Read the Docs search | ❌ Not added (not needed) |

**Total active sources:** 13

---

## How to Use

Sources are available through the `web_search` tool:

```python
web_search(query="python asyncio timeout", source="pypi")
web_search(query="asyncio await", source="stackoverflow", read=true)
web_search(query="docker nginx", source="dockerhub")
web_search(query="dragon", source="wikidata")
web_search(query="gorilla/mux", source="gomod")
```

## Persisting Sources

Session sources are forgotten on exit. To keep sources for future sessions:

```bash
/search-sources save all
```

Or save individual sources:

```bash
/search-sources save dockerhub
/search-sources save wikidata
/search-sources save gomod
```

Sources are stored in `~/.momo-harness/search_sources/`.

---

## Rationale for Choices

### Why these three were added

1. **dockerhub** - Essential for the project's Dockerfile support (`tree-sitter-dockerfile` grammar). The harness can parse Dockerfiles and this source helps the model understand available container images.

2. **wikidata** - Complements the code-focused sources with general knowledge. Useful for:
   - Answering questions about real-world concepts
   - Looking up definitions of domain-specific terminology
   - Research-oriented tasks

3. **gomod** - The project supports multiple languages. Go is one of the supported languages (tree-sitter-golang would be ideal, but deps.dev provides Go module metadata). This enables dependency management for Go projects.

### Why rtd-project was not added

The project does not appear to use Read the Docs based on its file structure (no `docs/` with Sphinx/reStructuredText configuration, no `.readthedocs.yaml`). Adding this source would be unnecessary overhead.

### Why no other sources were added

The 9 pre-existing sources already cover:
- All major package registries (npm, crates, maven, pypi)
- All major documentation sources (mdn, github, stackoverflow, wikipedia)
- Vulnerability database (osv)

These comprehensively address both code/package lookup and general knowledge needs.

---

## Open Questions

None — all decisions resolved based on project analysis.