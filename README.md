# Toronto Bike Share — Growth, Rhythms & Capacity

Analysis of Toronto's bike share system, 2016–2026. Report: **[neman-river.github.io/bikeshare_toronto](https://neman-river.github.io/bikeshare_toronto/overview.html)**

Work in progress.
Planned sections:
1. EDA (done)
2. Regression (in progress)
3. Time-series analysis (not started)
4. Clustering (not started)
5. Capacity analysis (not started)

## 1. EDA

- **Fleet expansion**: from 200 to 1,098 stations across all 25 wards, ridership up 5x (1.5M trips in 2017 to 7.8M in 2025)
- **Trips per station**: the expand/absorb cycle: fast fleet expansion dilutes demand per station, slower growth lets it catch up
- **Cannibalization check**: do new stations just split demand from old ones?
- **Segment rhythms**: Member vs. Casual by hour, day of week, and month 
- **Rides by ward**: station-level map, 2026
- **Busiest stations & routes**, daily-resolution anomalies (holidays, one-off events)

## Stack

Python (pandas, plotly, folium, geopandas) for analysis, [Quarto](https://quarto.org) for the site, [uv](https://docs.astral.sh/uv/) for dependencies.

## Reproducing

```
uv sync
# run notebooks in order: import_data -> geodata -> load_clean_trips -> eda
cd site && quarto render
```

Raw trip data isn't included (too large for the repo) — notebooks expect it under `raw_data/` / `data/`.
