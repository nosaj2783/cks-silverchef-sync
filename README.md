# cks-silverchef-sync (hourly job)

Reads SilverChef's public Certified Used feed and storefront every hour and publishes two CSVs on
GitHub Pages for an Ablestar Inventory Sync:

- `update.csv`: `SKU, Price, Compare-at Price, Weight` (ex-GST price, promo compare-at, shipping weight)
- `archive.csv`: `SKU, Status=archived` for items that have left the feed in the last 30 days
- `report.json`: run summary

No store credentials. If the feed looks broken (fewer than 500 items, a shrink of more than 20% vs the last run, or a storefront failure) the run
fails and the previously published files stay in place.
