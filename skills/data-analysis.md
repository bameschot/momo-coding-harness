# Skill: Data Analysis

## Workflow

1. **Inspect first** — sample the data, check shape, column types, null counts, and value ranges before processing.
2. **State your assumptions** — tell the user what you assume about the data (e.g., "I'm assuming `id` is unique") before proceeding.
3. **Process incrementally** — build the transformation step by step; verify each step before chaining.
4. **Save output to a file** — write derived data and summaries to new files; never overwrite source data.
5. **Report the results** — summarise key findings, row counts, and any anomalies in chat after processing.

---

## Loading data

### CSV — stdlib
```python
import csv

with open("data.csv", newline="", encoding="utf-8") as f:
    rows = list(csv.DictReader(f))

print(f"{len(rows)} rows, columns: {list(rows[0].keys())}")
```

### CSV — pandas
```python
import pandas as pd

df = pd.read_csv("data.csv")
# common options:
# pd.read_csv("data.csv", sep="\t")            # TSV
# pd.read_csv("data.csv", encoding="latin-1")  # non-UTF-8
# pd.read_csv("data.csv", parse_dates=["date"])
# pd.read_csv("data.csv", dtype={"id": str})   # force column type
# pd.read_csv("data.csv", usecols=["id","name"])  # load only some columns
# pd.read_csv("data.csv", chunksize=10000)     # iterate large files
print(df.shape)
print(df.head())
```

### JSON / JSONL
```python
import json

# JSON array
with open("data.json", encoding="utf-8") as f:
    data = json.load(f)
rows = data if isinstance(data, list) else data.get("items", [])

# JSONL (newline-delimited)
with open("data.jsonl", encoding="utf-8") as f:
    rows = [json.loads(line) for line in f if line.strip()]
```

### Excel / Parquet
```python
df = pd.read_excel("data.xlsx", sheet_name=0)
df = pd.read_parquet("data.parquet")
```

---

## Inspection

### Quick profile (pandas)
```python
print(df.shape)            # (rows, cols)
print(df.dtypes)           # column types
print(df.isnull().sum())   # nulls per column
print(df.describe())       # stats for numeric columns
print(df.head(3))
```

### Categorical columns — cardinality and top values
```python
for col in df.select_dtypes(include=["object", "category"]):
    vc = df[col].value_counts()
    print(f"{col}: {df[col].nunique()} unique | top: {vc.index[0]!r} ({vc.iloc[0]})")
```

### Numeric columns — range and outliers
```python
for col in df.select_dtypes(include="number"):
    s = df[col]
    q1, q3 = s.quantile(0.25), s.quantile(0.75)
    iqr = q3 - q1
    outliers = ((s < q1 - 1.5 * iqr) | (s > q3 + 1.5 * iqr)).sum()
    print(f"{col}: min={s.min()}, max={s.max()}, mean={s.mean():.2f}, "
          f"nulls={s.isnull().sum()}, outliers={outliers}")
```

### Duplicate detection
```python
n_dup = df.duplicated().sum()
print(f"Fully duplicated rows: {n_dup}")

key = "id"
print(f"Duplicate {key!r}: {df.duplicated(subset=[key]).sum()}")
```

### Date column parsing and range
```python
df["date"] = pd.to_datetime(df["date"], errors="coerce")
print(f"Date range: {df['date'].min()} — {df['date'].max()}")
print(f"Unparseable dates: {df['date'].isnull().sum()}")
```

---

## Filtering and selection

```python
active = df[df["status"] == "active"]
subset = df[(df["score"] > 50) & (df["category"] == "A")]
df[df["country"].isin(["NL", "DE", "BE"])]
df[df["name"].str.contains("Smith", case=False, na=False)]
df = df.dropna(subset=["id", "date"])   # drop rows where key columns are null
df[["id", "name", "score"]]             # select columns
```

---

## Aggregation

### Group by and aggregate
```python
summary = (
    df.groupby("category")
    .agg(
        count=("id", "count"),
        total=("amount", "sum"),
        avg_score=("score", "mean"),
        max_score=("score", "max"),
    )
    .reset_index()
    .sort_values("total", ascending=False)
)
print(summary)
```

### Pivot table
```python
pivot = df.pivot_table(
    values="amount",
    index="category",
    columns="status",
    aggfunc="sum",
    fill_value=0,
)
```

### Rolling / time-series aggregation
```python
df = df.sort_values("date")
df["week"] = df["date"].dt.to_period("W")
weekly = df.groupby("week")["amount"].sum()
```

---

## Transformation and cleaning

### Rename and retype
```python
df = df.rename(columns={"oldName": "new_name", "Timestamp": "ts"})
df["amount"] = pd.to_numeric(df["amount"], errors="coerce")
df["ts"] = pd.to_datetime(df["ts"], errors="coerce")
```

### String normalisation
```python
df["name"] = df["name"].str.strip().str.lower()
df["code"] = df["code"].str.upper().str.replace(r"\s+", "", regex=True)
```

### Fill or drop missing values
```python
df["score"] = df["score"].fillna(0)
df = df.dropna(subset=["id"])
df = df.fillna({"a": 0, "b": "n/a"})
```

### Derive new columns
```python
df["year"] = df["date"].dt.year
df["month"] = df["date"].dt.to_period("M").astype(str)
df["is_high"] = df["score"] > df["score"].quantile(0.9)
df["bucket"] = pd.cut(df["score"], bins=[0, 25, 50, 75, 100],
                      labels=["low", "mid", "high", "top"])
```

### Deduplicate
```python
df = df.drop_duplicates(subset=["id"])
df = df.sort_values("date").drop_duplicates(subset=["id"], keep="last")
```

---

## Joining and merging

```python
merged = pd.merge(df_orders, df_customers, on="customer_id", how="left")
merged = pd.merge(df_a, df_b, left_on="user_id", right_on="id")

# Concatenate multiple files
import glob
dfs = [pd.read_csv(f) for f in glob.glob("data/*.csv")]
combined = pd.concat(dfs, ignore_index=True)
```

---

## Large file strategies

```python
# Chunked reading — process without loading everything into memory
for chunk in pd.read_csv("big.csv", chunksize=10_000):
    process(chunk)

# Sample large file without loading all
import csv, random
sample = []
with open("big.csv", newline="") as f:
    reader = csv.DictReader(f)
    for i, row in enumerate(reader):
        if i < 1000:
            sample.append(row)
        elif random.random() < 1000 / (i + 1):   # reservoir sampling
            sample[random.randint(0, 999)] = row
```

---

## Data quality report

```python
def quality_report(df: pd.DataFrame) -> str:
    lines = [f"Shape: {df.shape[0]} rows × {df.shape[1]} cols\n"]

    nulls = df.isnull().sum()
    if nulls.any():
        lines.append("Nulls:")
        for col, n in nulls[nulls > 0].items():
            lines.append(f"  {col}: {n} ({n/len(df)*100:.1f}%)")

    lines.append(f"\nDuplicate rows: {df.duplicated().sum()}")

    lines.append("\nNumeric ranges:")
    for col in df.select_dtypes(include="number"):
        s = df[col]
        lines.append(f"  {col}: [{s.min()}, {s.max()}]  mean={s.mean():.2f}")

    lines.append("\nCategorical cardinality:")
    for col in df.select_dtypes(include=["object", "category"]):
        lines.append(f"  {col}: {df[col].nunique()} unique values")

    return "\n".join(lines)
```

---

## stdlib-only patterns (no pandas)

```python
# Aggregate CSV column
import csv
from collections import defaultdict, Counter

totals = defaultdict(float)
counts = Counter()
with open("data.csv", newline="") as f:
    for row in csv.DictReader(f):
        cat = row["category"]
        totals[cat] += float(row["amount"] or 0)
        counts[cat] += 1

# Transform JSONL
with open("input.jsonl") as fin, open("output.jsonl", "w") as fout:
    for line in fin:
        if not line.strip():
            continue
        rec = json.loads(line)
        rec["full_name"] = f"{rec.get('first','')} {rec.get('last','')}".strip()
        fout.write(json.dumps(rec) + "\n")
```

---

## jq patterns

```bash
jq '.[].name' data.json                                    # extract a field
jq '.[] | select(.status == "active")' data.json           # filter
jq '[.[] | select(.score > 50)] | length' data.json        # count matching
jq '[.[] | {id, name, score}]' data.json                   # pick fields
jq 'group_by(.category) | map({category: .[0].category, count: length})' data.json
jq '[.[].amount] | add' data.json                          # sum
jq 'to_entries | map(select(.value > 0))' data.json        # filter object entries
```

---

## Writing output

```python
# CSV
df.to_csv("output.csv", index=False, encoding="utf-8")

# JSONL
with open("output.jsonl", "w", encoding="utf-8") as f:
    for row in rows:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")

# Markdown summary
lines = [
    "# Data Summary\n",
    f"- **Total rows:** {len(df)}",
    f"- **Date range:** {df['date'].min()} — {df['date'].max()}",
    "",
    "## By category",
    "",
    summary.to_markdown(index=False),
]
Path("summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
```

---

## Output formats

- **Summaries** — write as Markdown to `.md` files for readability.
- **Derived data** — write as CSV or JSONL matching the input format unless the user specifies otherwise.
- Always include a header row in CSV output; always write valid JSON (no trailing commas).
- Report: row counts before and after filtering, any anomalies found, and assumptions made.
