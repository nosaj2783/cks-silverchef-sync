# cks-silverchef-sync (hourly job)

Reads SilverChef's public Certified Used feed and storefront every hour and publishes CSVs on GitHub Pages
for Ablestar Inventory Sync:

- `update.csv`: `SKU, Price, Compare-at Price, Weight` for every feed item, except items sold out on CKS (frozen)
- `archive.csv`: `SKU, Status=archived` for items that have left the feed in the last 30 days
- `report.json`: run summary (feed size, frozen, archived, new arrivals, alert status)

Stock is never written. Sold-out status comes from CKS's public storefront. Once a day at 08:xx Sydney time, new
feed arrivals are posted to Slack via the `SLACK_WEBHOOK_URL` repo secret. No store credentials. If the feed looks
broken (under 500 items, a shrink of more than 20%, or a storefront failure) the run fails and the last published
files stay in place.
