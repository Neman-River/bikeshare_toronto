import pandas as pd
import re
import string
import ftfy
import time
import requests
from collections import defaultdict
import unicodedata

# ----UNIVERSAL----
# text normalization:
KNOWN_MOJIBAKE = {'GÇô': '–', 'GÇÖ': "'"}

def fix_encoding(s):
    if pd.isna(s):
        return s
    s = str(s)
    for _ in range(5):
        fixed = ftfy.fix_text(s)
        if fixed == s:
            break
        s = fixed
    for bad, good in KNOWN_MOJIBAKE.items():
        s = s.replace(bad, good)
    return s

# strip trailing "- NNNN" id suffix:
def strip_trailing_id_suffix(s):
    if pd.isna(s):
        return s
    return re.sub(r"\s*-\s*\d{4,5}$", "", str(s)).strip()


# clean punctuation:
EXTRA_PUNCT = "\u2019\u2013\u2014\u00b4\u201c\u201d"  # ’ – — ´ " "

def delete_punctuation(name):
    if pd.isna(name):
        return name
    s = fix_encoding(name)
    s = re.sub(rf"[{re.escape(string.punctuation)}{EXTRA_PUNCT}\x00-\x1f\x7f]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s 

# unicode NFKC normalization:
def normalize_unicode_form(s):
    if pd.isna(s):
        return s
    return unicodedata.normalize('NFKC', str(s))

# universal for english speeking countries
STREET_TYPE_MAP = {
    'street': 'st', 'avenue': 'ave', 'boulevard': 'blvd', 'road': 'rd',
    'drive': 'dr', 'place': 'pl', 'crescent': 'cres', 'court': 'ct', 'crt': 'ct',
    'lane': 'ln', 'terrace': 'terr',
}
DIRECTION_MAP = {'west': 'w', 'east': 'e', 'north': 'n', 'south': 's'}

def _replace_words(s, mapping):
    if pd.isna(s):
        return s
    return re.sub(r"[A-Za-z]+", lambda m: mapping.get(m.group(0).lower(), m.group(0)), str(s))

def normalize_street_types(s):
    return _replace_words(s, STREET_TYPE_MAP)

def normalize_directions(s):
    return _replace_words(s, DIRECTION_MAP)


# ----DATA SPECIFIC----
# order the parts of address:
def standardize_intersection(s, sep='/'):
    if pd.isna(s):
        return s
    s = str(s)
    if sep not in s:
        return s
    parts = [p.strip() for p in s.split(sep)]
    parts.sort(key=str.lower)
    return f' {sep} '.join(parts)

# assign the most recent name:
def station_id_name_groups(stations_df):
    """Names confirmed to be the same physical station because they share a
    station_id across snapshots. Returns {name: canonical_name}, canonical
    being the most recently-seen name for that id."""
    
    id_to_names = defaultdict(set)
    for sid, name in zip(stations_df['station_id'], stations_df['name']):
        if pd.isna(name):
            continue
        id_to_names[sid].add(name)

    name_to_canonical = {}
    for sid, names in id_to_names.items():
        if len(names) <= 1:
            continue
        canonical = stations_df[stations_df['station_id'] == sid].iloc[-1]['name']
        for n in names:
            name_to_canonical[n] = canonical
    return name_to_canonical

# fuzzy matching:
from difflib import get_close_matches, SequenceMatcher

def tokenize(s):
    return re.findall(r"[A-Za-z0-9']+", s.lower())

def is_safe_match(a, b):
    set1, set2 = set(tokenize(a)), set(tokenize(b))
    unmatched1, unmatched2 = list(set1 - set2), list(set2 - set1)
    if not unmatched1 or not unmatched2:
        return True
    return all(
        max((SequenceMatcher(None, w, w2).ratio() for w2 in unmatched2), default=0) >= 0.6
        for w in unmatched1
    )

def find_fuzzy_duplicate_pairs(names, cutoff=0.82):
    names = list(names)
    return [
        (a, b)
        for i, a in enumerate(names)
        for b in get_close_matches(a, names[i+1:], n=3, cutoff=cutoff)
        if is_safe_match(a, b)
]

# ----GEODATA----
# geocode intersection names via Nominatim (OpenStreetMap), e.g. "Islington Ave / Aviemore Dr".
# Nominatim's usage policy requires a descriptive User-Agent and max 1 req/sec -
# fill in a contact email below before running a large batch.
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
GEOCODE_HEADERS = {"User-Agent": "bike-sharing-research (contact: <natalia.lantukh@gmail.com>)"}

def _query_nominatim(query, city="Toronto, Ontario, Canada", timeout=10):
    resp = requests.get(
        NOMINATIM_URL,
        params={"q": f"{query}, {city}", "format": "json", "limit": 1},
        headers=GEOCODE_HEADERS,
        timeout=timeout,
    )
    resp.raise_for_status()
    results = resp.json()
    if results:
        return float(results[0]["lat"]), float(results[0]["lon"])
    return None

def geocode_intersection(name, city="Toronto, Ontario, Canada", sep="/"):
    """Geocode a canonical station name. Tries the parts in the given order first;
    only swaps them as a fallback if nothing is found - no reordering otherwise."""
    if pd.isna(name):
        return None
    parts = [p.strip() for p in str(name).split(sep)]
    if len(parts) != 2:
        return _query_nominatim(name, city)
    a, b = parts
    return _query_nominatim(f"{a} and {b}", city) or _query_nominatim(f"{b} and {a}", city)

def geocode_names(names, city="Toronto, Ontario, Canada", delay=1.0):
    """Geocode a list of names, respecting Nominatim's 1 req/sec rate limit.
    Returns {name: (lat, lon)} - entries that failed to geocode are omitted."""
    resolved = {}
    for name in names:
        result = geocode_intersection(name, city=city)
        if result is not None:
            resolved[name] = result
        time.sleep(delay)
    return resolved

# ----TRIPS----
# shared pieces for loading/cleaning the yearly Bike Share Toronto ridership CSVs
# (see import_clean_trips.ipynb). Centralized here because import_data.ipynb
# originally repeated this logic by hand per year, which is how 2016's
# station_id recovery silently never ran (a cell re-execution order bug).
import zipfile
from pathlib import Path
from charset_normalizer import from_path as _cn_from_path

def detect_encoding(path):
    result = _cn_from_path(path).best()
    return result.encoding if result else 'utf-8'

def read_csv_robust(path, **kwargs):
    # utf-8-sig strips a leading BOM - several years mix BOM/non-BOM files across
    # months, which otherwise produces two different "trip_id" columns after concat.
    for enc in ['utf-8-sig', detect_encoding(path), 'cp1252', 'latin-1']:
        try:
            return pd.read_csv(path, encoding=enc, **kwargs)
        except (UnicodeDecodeError, LookupError):
            continue
    raise ValueError(f"Could not decode {path} with any encoding")


def normalize_station_name(name):
    if pd.isna(name):
        return name
    s = str(name).strip()
    s = re.sub(r"[’']", "", s)
    s = re.sub(r"\.", "", s)
    s = re.sub(r"\s+", " ", s)
    return s


# 2014-2018 used a different column schema than 2019 onward.
OLD_SCHEMA_RENAME = {
    'trip_duration_seconds': 'trip_duration',
    'from_station_id': 'start_station_id',
    'trip_start_time': 'start_time',
    'from_station_name': 'start_station_name',
    'to_station_id': 'end_station_id',
    'to_station_name': 'end_station_name',
    'trip_stop_time': 'end_time',
}

def normalize_trip_columns(df):
    df = df.rename(columns=lambda c: re.sub(r"\s+", "_", c.strip().lower()))
    return df.rename(columns=OLD_SCHEMA_RENAME)


def parse_mixed_dates(s):
    # 2017's quarterly files don't agree on day-first vs month-first.
    first = pd.to_numeric(s.dropna().astype(str).str.split('/').str[0], errors='coerce')
    return pd.to_datetime(s, dayfirst=bool((first > 12).any()), format='mixed', errors='coerce')


def recover_station_ids(df, stations_vocab, stations_actual_names):
    """Fill missing start/end station_id by name, then backfill missing names
    from whatever the current canonical name is for the resulting id."""
    name_to_id = stations_vocab.drop_duplicates('name').set_index('name')['station_id']
    for prefix in ['start', 'end']:
        id_col, name_col = f'{prefix}_station_id', f'{prefix}_station_name'
        if name_col not in df.columns:
            continue
        if id_col not in df.columns:
            # 2016's source has no station id columns at all - only names
            df[id_col] = pd.NA

        # coerce before merging - e.g. 2020's corrupted-row repair leaves this column
        # as 'object' dtype after concat, which pandas refuses to merge on directly
        df[id_col] = pd.to_numeric(df[id_col], errors='coerce')

        missing = df[id_col].isna()
        if missing.any():
            df.loc[missing, id_col] = df.loc[missing, name_col].map(name_to_id)

        df = (
            df.merge(stations_actual_names, how='left', left_on=id_col, right_on='station_id')
            .assign(**{name_col: lambda d, name_col=name_col: d[name_col].combine_first(d['name'])})
            .drop(columns=['station_id', 'name'])
        )
    return df


def clean_trip_duration(df):
    """Drop rows that are a tech glitch rather than a real trip (a bike
    checked out and immediately redocked), and recover trip_duration from
    start/end time where it's missing but the gap is long enough to trust.

    trip_duration == 0 shows up every year from 2020 on and grows over time
    (tens of thousands of rows by 2024-2026) - these aren't real trips.
    A handful of rows (2019, 2020) have trip_duration missing instead; where
    the start/end gap is also <= 60s there's no way to tell a real short trip
    from the same glitch, so those get dropped too rather than filled in with
    noise. Everything else with a missing duration is filled from the gap.
    """
    diff_seconds = (df['end_time'] - df['start_time']).dt.total_seconds()

    drop_zero_no_end = (df['trip_duration'] == 0) & df['end_time'].isna()
    drop_unrecoverable = df['trip_duration'].isna() & (diff_seconds.isna() | (diff_seconds <= 60))
    df = df[~(drop_zero_no_end | drop_unrecoverable)].copy()
    diff_seconds = diff_seconds[df.index]

    missing = df['trip_duration'].isna()
    if missing.any():
        df.loc[missing, 'trip_duration'] = diff_seconds[missing].astype(df['trip_duration'].dtype)

    return df[df['trip_duration'] != 0]


def optimize_trip_dtypes(df):
    if 'start_time' in df.columns:
        df['start_time'] = pd.to_datetime(df['start_time'])
    if 'end_time' in df.columns:
        df['end_time'] = pd.to_datetime(df['end_time'])

    for col in ['bike_model', 'user_type', 'start_station_name', 'end_station_name']:
        if col in df.columns:
            df[col] = df[col].astype('category')

    for col in ['trip_id', 'trip_duration', 'start_station_id', 'end_station_id', 'bike_id']:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce')

    int_cols = df.select_dtypes(include='int64').columns
    df[int_cols] = df[int_cols].apply(pd.to_numeric, downcast='integer')
    float_cols = df.select_dtypes(include='float64').columns
    df[float_cols] = df[float_cols].apply(pd.to_numeric, downcast='float')
    return df


def _fix_2016_q4_dates(df):
    """2016 Q4's trip_start_time/trip_stop_time columns mix two representations of the
    same day-first (DD/MM/YYYY) source data: unambiguous rows (day > 12) stayed as literal
    text, while ambiguous rows (day <= 12) got silently auto-converted assuming month-first,
    swapping day and month (e.g. true Oct 1 became Jan 10). Confirmed against the raw sheet:
    every non-text value there has day <= 12, so the swap is unconditionally safe."""
    df = df.copy()
    for col in ['trip_start_time', 'trip_stop_time']:
        is_text = df[col].map(lambda v: isinstance(v, str))
        df.loc[is_text, col] = pd.to_datetime(df.loc[is_text, col], dayfirst=True)
        df.loc[~is_text, col] = df.loc[~is_text, col].map(
            lambda d: d.replace(day=d.month, month=d.day)
        )
        df[col] = pd.to_datetime(df[col])
    return df


def _repair_2020_10(path):
    """2020-10.csv: 249 rows are missing the Trip Duration field entirely, which
    shifts every later column one position to the left for those rows."""
    df = pd.read_csv(path).astype('object')
    cols = df.columns.tolist()
    bad_rows = pd.to_numeric(df[cols[2]], errors='coerce').isna() & df[cols[2]].notna()
    df.loc[bad_rows, cols[2:]] = df.loc[bad_rows, cols[1:-1]].values
    df.loc[bad_rows, cols[1]] = pd.NA
    return df


def _year_files(year, raw_dir):
    """Locate the CSV/XLSX file(s) for a year under raw_dir, recursing into
    whatever folder structure the zip extracted to, and unzipping any file
    that's itself a zip nested inside that folder (e.g. 2022's single-month zip)."""
    raw_dir = Path(raw_dir)
    for ext in ('.csv', '.xlsx'):
        flat = raw_dir / f"bikeshare-ridership-{year}{ext}"
        if flat.exists():
            return [flat]

    folder = raw_dir / f"bikeshare-ridership-{year}"
    if not folder.exists():
        raise FileNotFoundError(f"no raw data found for {year} under {raw_dir}")

    for nested_zip in list(folder.rglob("*.zip")):
        extract_dir = nested_zip.with_suffix("")
        if not extract_dir.exists():
            with zipfile.ZipFile(nested_zip) as zf:
                zf.extractall(extract_dir)

    return sorted(folder.rglob("*.csv"))


def load_year(year, raw_dir="raw_data", stations_vocab=None, stations_actual_names=None):
    """Read, standardize and station-id-recover one year of Bike Share Toronto
    ridership data. Returns a single DataFrame with normalized columns/dtypes.

    `year` is an int (2016..2026) or the string "2014-2015" for that combined file.
    Pass stations_vocab/stations_actual_names (both station_id/name DataFrames, see
    geodata.ipynb) to fill missing station_id by name and backfill names to their
    current canonical spelling - omit to skip recovery entirely.
    """
    files = _year_files(year, raw_dir)

    if files[0].suffix == '.xlsx':
        header = 1 if year == '2014-2015' else 0
        if year == '2014-2015':
            frames = [pd.read_excel(f, header=header) for f in files]
        else:
            # 2016's workbook splits trips across one sheet per quarter (Q3, Q4) -
            # reading with the default sheet_name=0 silently kept only Q3.
            frames = []
            for f in files:
                for sheet_name, sheet_df in pd.read_excel(f, sheet_name=None, header=header).items():
                    if year == 2016 and 'Q4' in sheet_name:
                        sheet_df = _fix_2016_q4_dates(sheet_df)
                    frames.append(sheet_df)
    elif year == 2020:
        frames = [
            _repair_2020_10(f) if f.name == '2020-10.csv' else read_csv_robust(f)
            for f in files
        ]
    else:
        frames = [read_csv_robust(f) for f in files]

    df = pd.concat(frames, ignore_index=True)
    df = normalize_trip_columns(df)

    if year == 2017:
        df['start_time'] = parse_mixed_dates(df['start_time'])
        df['end_time'] = parse_mixed_dates(df['end_time'])
        df = df[df['start_station_name'] != 'Base Station']  # test data, not a real trip

    for col in ['start_station_name', 'end_station_name']:
        if col in df.columns:
            df[col] = [normalize_station_name(n) for n in df[col]]

    # sanity-check timestamps instead of hardcoding a specific bad year (e.g. 2016's
    # stray year-2000 row) - drop anything that couldn't plausibly belong to this dataset
    if 'start_time' in df.columns:
        parsed_year = pd.to_datetime(df['start_time'], errors='coerce').dt.year
        df = df[parsed_year.between(2014, 2030, inclusive='both') | parsed_year.isna()]

    if stations_vocab is not None and stations_actual_names is not None:
        df = recover_station_ids(df, stations_vocab, stations_actual_names)

    df = optimize_trip_dtypes(df)
    return clean_trip_duration(df)


def load_2015_od(raw_dir="raw_data"):
    """2015's ridership file predates per-trip records - each month sheet only
    publishes station-pair counts (Start/End Terminal x Casual/Registered),
    not individual trips with a start_time/end_time/trip_duration. So this
    returns one row per (month, start_station_id, end_station_id) instead of
    forcing it into the per-trip `trips` schema the other years use.
    """
    path = Path(raw_dir) / "bikeshare-ridership-2014-2015.xlsx"
    xl = pd.ExcelFile(path)
    month_sheets = [s for s in xl.sheet_names if re.match(r"^2015-\d{2} (Trips|Routes)$", s)]

    frames = []
    for sheet in month_sheets:
        df = pd.read_excel(xl, sheet_name=sheet, header=1)
        df.columns = ["start_station_id", "end_station_id", "casual_trips", "member_trips", "total_trips"]
        df = df[df["start_station_id"] != "Total"]  # drop the sheet's trailing summary row
        df["month"] = pd.Period(sheet.split(" ")[0], freq="M")
        frames.append(df)

    od = pd.concat(frames, ignore_index=True)
    od[["casual_trips", "member_trips"]] = od[["casual_trips", "member_trips"]].fillna(0)
    for col in ["start_station_id", "end_station_id", "casual_trips", "member_trips", "total_trips"]:
        od[col] = pd.to_numeric(od[col], errors="coerce").astype("Int32")
    return od