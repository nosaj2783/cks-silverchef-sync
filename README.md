# cks-silverchef-sync (hourly job)

Keeps SilverChef *Certified Used* items listed on a Shopify store up to date through **Ablestar Inventory Sync**.
It runs hourly on GitHub Actions, reads only public sources, and holds **no store credentials**.

## What it publishes (GitHub Pages)

| File | Contents | Applied by |
|---|---|---|
| `update.csv` | `SKU, Price, Compare-at Price, Weight` for every item in SilverChef's feed, **except** items sold out on the store (frozen) | Ablestar Inventory Sync #1, hourly at :20, identifier SKU |
| `archive.csv` | `SKU, Status=archived` for items that have left SilverChef's feed in the last 30 days (sold by SilverChef) | Ablestar Inventory Sync #2, hourly at :40, identifier SKU |
| `report.json` | run summary: feed size, updates, archives, frozen, new arrivals, alert status | read by the operator |

## Rules

- **Stock is never written**, so a sale on the store is never reset.
- **Frozen:** an item sold out on the store (public storefront `available: false`) but still in SilverChef's feed gets no updates until
  SilverChef removes it, then it is archived. If the storefront can't be read, the last known frozen list is kept.
- **New arrivals:** each item's first appearance in the feed is recorded. Once a day, on the 08:xx Sydney run, the new ones are
  posted to Slack via the repository secret `SLACK_WEBHOOK_URL`. Items are created on the store separately, on demand.
- **Safety stop:** if the feed has fewer than 500 items, shrinks by more than 20% in an hour, or SilverChef's storefront fails, the run
  fails, nothing is published, and the last good files stay in place.

## Run it by hand

Actions → **silverchef-hourly** → *Run workflow*, or `gh workflow run silverchef-hourly.yml`.
