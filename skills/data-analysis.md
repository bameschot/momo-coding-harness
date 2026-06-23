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
print(df.shape)   # (rows, cols)
print(df.head())
```

### JSON array / object
```python
import json

with open("data.json", encoding="utf-8") as f:
    data = json.load(f)

# If it's a list of records:
rows = data if isinstance(data, list) else data.get("items", [])
print(f"{len(rows)} records, keys: {list(rows[0].keys())}")
```

### JSONL (newline-delimited JSON)
```python
import json

with open("data.jsonl", encoding="utf-8") as f:
    rows = [json.loads(line) for line in f if line.strip()]

print(f"{len(rows)} records")
```

### Excel
```python
import pandas as pd

df = pd.read_excel("data.xlsx", sheet_name=0)
# sheet_name="Sheet1" or sheet_name=None to load all sheets as a dict
print(df.shape)
```

### Parquet
```python
import pandas as pd

df = pd.read_parquet("data.parquet")
print(df.shape, df.dtypes)
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
    print(f"{col}: min={s.min()}, max={s.max()}, mean={s.mean():.2f}, "
          f"nulls={s.isnull().sum()}, zeros={( s == 0).sum()}")
```

### Duplicate detection
```python
n_dup = df.duplicated().sum()
print(f"Fully duplicated rows: {n_dup}")

# Check if a specific column is a unique key
key = "id"
n_dup_key = df.duplicated(subset=[key]).sum()
print(f"Duplicate {key!r} values: {n_dup_key}")
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
# Boolean filter
active = df[df["status"] == "active"]

# Multiple conditions
subset = df[(df["score"] > 50) & (df["category"] == "A")]

# Exclude rows
clean = df[df["amount"] >= 0]

# Filter by list of values
df[df["country"].isin(["NL", "DE", "BE"])]

# Filter by string pattern
df[df["name"].str.contains("Smith", case=False, na=False)]

# Drop rows where key columns are null
df = df.dropna(subset=["id", "date"])

# Select columns
df[["id", "name", "score"]]
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
print(pivot)
```

### Frequency counts with percentage
```python
vc = df["status"].value_counts()
pct = (vc / len(df) * 100).round(1)
print(pd.DataFrame({"count": vc, "pct": pct}))
```

### Rolling / time-series aggregation
```python
df = df.sort_values("date")
df["week"] = df["date"].dt.to_period("W")
weekly = df.groupby("week")["amount"].sum()
print(weekly)
```

---

## Transformation and cleaning

### Rename and retype columns
```python
df = df.rename(columns={"oldName": "new_name", "Timestamp": "ts"})
df["amount"] = pd.to_numeric(df["amount"], errors="coerce")
df["ts"] = pd.to_datetime(df["ts"], errors="coerce")
```

### Strip and normalise strings
```python
df["name"] = df["name"].str.strip().str.lower()
df["code"] = df["code"].str.upper().str.replace(r"\s+", "", regex=True)
```

### Fill or drop missing values
```python
df["score"] = df["score"].fillna(0)
df["category"] = df["category"].fillna("unknown")
df = df.dropna(subset=["id"])          # drop rows where id is null
df = df.fillna({"a": 0, "b": "n/a"})  # fill different columns differently
```

### Derive new columns
```python
df["full_name"] = df["first"] + " " + df["last"]
df["year"] = df["date"].dt.year
df["month"] = df["date"].dt.to_period("M").astype(str)
df["is_high"] = df["score"] > df["score"].quantile(0.9)
df["bucket"] = pd.cut(df["score"], bins=[0, 25, 50, 75, 100],
                      labels=["low", "mid", "high", "top"])
```

### Apply a function per row
```python
def classify(row):
    if row["amount"] > 1000:
        return "large"
    elif row["amount"] > 100:
        return "medium"
    return "small"

df["size"] = df.apply(classify, axis=1)
# For simple column operations, prefer vectorised alternatives over apply:
df["size"] = pd.cut(df["amount"], bins=[0, 100, 1000, float("inf")],
                    labels=["small", "medium", "large"])
```

### Deduplicate
```python
df = df.drop_duplicates()                         # fully identical rows
df = df.drop_duplicates(subset=["id"])            # keep first by id
df = df.sort_values("date").drop_duplicates(      # keep latest per id
    subset=["id"], keep="last"
)
```

---

## Joining and merging

```python
# Inner join (only matching rows)
merged = pd.merge(df_orders, df_customers, on="customer_id", how="inner")

# Left join (keep all orders, fill nulls where no customer match)
merged = pd.merge(df_orders, df_customers, on="customer_id", how="left")

# Join on columns with different names
merged = pd.merge(df_a, df_b, left_on="user_id", right_on="id")

# Concatenate rows from multiple files
import glob
dfs = [pd.read_csv(f) for f in glob.glob("data/*.csv")]
combined = pd.concat(dfs, ignore_index=True)
print(f"Combined: {combined.shape}")
```

---

## stdlib-only patterns (no pandas)

### Aggregate a CSV column without pandas
```python
import csv
from collections import defaultdict, Counter

totals = defaultdict(float)
counts = Counter()

with open("data.csv", newline="") as f:
    for row in csv.DictReader(f):
        cat = row["category"]
        totals[cat] += float(row["amount"] or 0)
        counts[cat] += 1

for cat in sorted(totals, key=totals.get, reverse=True):
    print(f"{cat}: total={totals[cat]:.2f}, count={counts[cat]}")
```

### Transform a JSONL file
```python
import json

with open("input.jsonl") as fin, open("output.jsonl", "w") as fout:
    for line in fin:
        if not line.strip():
            continue
        rec = json.loads(line)
        rec["full_name"] = f"{rec.get('first','')} {rec.get('last','')}".strip()
        fout.write(json.dumps(rec) + "\n")
```

### Find duplicates with stdlib
```python
import csv
from collections import Counter

with open("data.csv", newline="") as f:
    ids = [row["id"] for row in csv.DictReader(f)]

dupes = [id_ for id_, n in Counter(ids).items() if n > 1]
print(f"Duplicate ids ({len(dupes)}): {dupes[:10]}")
```

### Sample large files without loading all into memory
```python
import csv, random

sample = []
with open("big.csv", newline="") as f:
    reader = csv.DictReader(f)
    for i, row in enumerate(reader):
        if i < 1000:
            sample.append(row)
        elif random.random() < 1000 / (i + 1):   # reservoir sampling
            sample[random.randint(0, 999)] = row

print(f"Sample size: {len(sample)}")
```

---

## Writing output

### Write CSV
```python
import csv

with open("output.csv", "w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=rows[0].keys())
    writer.writeheader()
    writer.writerows(rows)
```

### Write CSV from pandas
```python
df.to_csv("output.csv", index=False, encoding="utf-8")
```

### Write JSONL
```python
import json

with open("output.jsonl", "w", encoding="utf-8") as f:
    for row in rows:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
```

### Write a Markdown summary
```python
lines = [
    "# Data Summary\n",
    f"- **Total rows:** {len(df)}",
    f"- **Columns:** {', '.join(df.columns)}",
    f"- **Date range:** {df['date'].min()} — {df['date'].max()}",
    "",
    "## By category",
    "",
    summary.to_markdown(index=False),
]
with open("summary.md", "w", encoding="utf-8") as f:
    f.write("\n".join(lines) + "\n")
```

---

## Data quality checks

```python
import pandas as pd

def quality_report(df: pd.DataFrame) -> str:
    lines = [f"Shape: {df.shape[0]} rows × {df.shape[1]} cols\n"]

    # Nulls
    nulls = df.isnull().sum()
    if nulls.any():
        lines.append("Nulls:")
        for col, n in nulls[nulls > 0].items():
            lines.append(f"  {col}: {n} ({n/len(df)*100:.1f}%)")

    # Duplicates
    n_dup = df.duplicated().sum()
    lines.append(f"\nDuplicate rows: {n_dup}")

    # Numeric ranges
    lines.append("\nNumeric ranges:")
    for col in df.select_dtypes(include="number"):
        s = df[col]
        lines.append(f"  {col}: [{s.min()}, {s.max()}]  mean={s.mean():.2f}")

    # Categoricals
    lines.append("\nCategorical cardinality:")
    for col in df.select_dtypes(include=["object", "category"]):
        lines.append(f"  {col}: {df[col].nunique()} unique values")

    return "\n".join(lines)

print(quality_report(df))
```

---

## jq patterns

```bash
# Extract a field from all objects
jq '.[].name' data.json

# Filter by condition
jq '.[] | select(.status == "active")' data.json

# Count items matching a condition
jq '[.[] | select(.score > 50)] | length' data.json

# Pick a subset of fields
jq '[.[] | {id, name, score}]' data.json

# Group and count by a field
jq 'group_by(.category) | map({category: .[0].category, count: length})' data.json

# Flatten nested array
jq '[.[].tags[]]' data.json

# Sum a numeric field
jq '[.[].amount] | add' data.json
```

---

## Output formats

- **Summaries** — write as Markdown to `.md` files for readability.
- **Derived data** — write as CSV or JSONL matching the input format unless the user specifies otherwise.
- **Reports** — if charts would help, describe what they would show; do not attempt ASCII art charts unless asked.
- Always include a header row in CSV output; always write valid JSON (no trailing commas).
