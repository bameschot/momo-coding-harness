# Skill: Data Analysis

## Core rules

- Inspect the data FIRST: shape, column types, null counts, value ranges — before any processing.
- State your assumptions to the user (e.g. "assuming `id` is unique") before proceeding.
- Build transformations step by step; verify each step before chaining the next.
- NEVER overwrite source data. Write derived data and summaries to new files.
- Report results: row counts before/after filtering, anomalies found, assumptions made.
- Write summaries as Markdown; write derived data as CSV or JSONL matching the input.

## Load

```python
import pandas as pd
df = pd.read_csv("data.csv")          # parse_dates=[...], dtype={...}, usecols=[...] as needed
# JSONL:  rows = [json.loads(l) for l in open("data.jsonl") if l.strip()]
```

For very large files, stream: `for chunk in pd.read_csv("big.csv", chunksize=10_000): ...`.

## Inspect

```python
print(df.shape)            # (rows, cols)
print(df.dtypes)
print(df.isnull().sum())   # nulls per column
print(df.describe())       # numeric stats
print(df.duplicated().sum())               # full-row dups
print(df.duplicated(subset=["id"]).sum())  # key dups
```

Parse dates explicitly and check for failures: `pd.to_datetime(df["date"], errors="coerce")`, then count nulls.

## Filter, aggregate, clean

```python
subset = df[(df["score"] > 50) & (df["category"] == "A")]
df = df.dropna(subset=["id"])                 # drop rows missing a key
df["amount"] = pd.to_numeric(df["amount"], errors="coerce")

summary = (df.groupby("category")
             .agg(count=("id", "count"), total=("amount", "sum"))
             .reset_index()
             .sort_values("total", ascending=False))

df = df.sort_values("date").drop_duplicates(subset=["id"], keep="last")
merged = pd.merge(orders, customers, on="customer_id", how="left")
```

## Write output

```python
df.to_csv("output.csv", index=False, encoding="utf-8")
Path("summary.md").write_text("# Summary\n\n" + summary.to_markdown(index=False))
```

Always include a CSV header row; always emit valid JSON (no trailing commas).

## No-pandas fallback

Use stdlib `csv.DictReader` + `collections.Counter`/`defaultdict` for simple aggregation, or `jq` for JSON:

```bash
jq '[.[] | select(.score > 50)] | length' data.json
jq 'group_by(.category) | map({category: .[0].category, count: length})' data.json
```
