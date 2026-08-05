# The Stationer's Report

A live Amazon UK Best Sellers dashboard. Runs entirely on free tiers.

- **Scraping:** GitHub Actions + ScraperAPI free tier
- **Storage:** JSON committed to this repo (no database)
- **Dashboard:** static HTML on GitHub Pages
- **Cost:** £0/month

## What it does

Every six hours a scheduled Action runs `scrape.py`, which fetches ~25 Amazon UK
best-seller categories, extracts the top 30 products from each, diffs them
against the previous run to work out rank movement, and commits the results to
`data/latest.json`. A timestamped copy also goes to `data/history/`.

The dashboard reads that JSON and lets you pick categories, search, filter,
sort, switch between list / grid / table views, browse past snapshots, and
export what you're looking at to CSV.

## Refreshing on demand

Actions tab → **Scrape Amazon Best Sellers** → **Run workflow**.
Optionally type category slugs (e.g. `cards-birthday,beauty`) to refresh only
those and save API credits.

## Changing categories

Edit the `CATEGORIES` dict at the top of `scrape.py`. Each entry is
`"slug": ("Label", "Group", "url")`. The group name controls how it's
organised in the sidebar. The dashboard rebuilds itself from whatever is there.

## Changing frequency

Edit the `cron:` line in `.github/workflows/scrape.yml`.
See https://crontab.guru. Default `0 */6 * * *` = every six hours.

## API budget

ScraperAPI's free tier is 1,000 credits/month. Each category costs a few
credits (JS rendering). ~25 categories every six hours is roughly 3,000/month —
over the limit, so either reduce the schedule to daily (`0 7 * * *`), trim the
category list, or use the `only:` input for targeted refreshes.
