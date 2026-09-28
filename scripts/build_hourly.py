#!/usr/bin/env python3
"""HOURLY (cloud, no store login): SilverChef feed -> two Ablestar Inventory Sync files + a daily Slack alert.

  update.csv   SKU, Price, Compare-at Price, Weight    every item in the feed EXCEPT frozen ones (commercial fields
               only: no cost - the file is public-by-link - and NO STOCK, so a CKS sale is never reset to 1)
  archive.csv  SKU, Status=archived                    every asset seen in the last ARCHIVE_DAYS that has
                                                       left the feed = sold/withdrawn by SilverChef (idempotent)

FREEZE (decision 2026-09-29): a SilverChef item that is SOLD OUT on CKS (sold on CKS, stock 0) but still in the SC feed
gets NO updates at all until SC drops it, then it is archived. Read from CKS's PUBLIC storefront (the 11 Refurbished
collections' products.json 'available' flag) - no login. Drafts can't sell, so published items are the whole story.
If CKS can't be read, the last known frozen list is reused: an item is never unfrozen by a failed read.

NEW-ARRIVALS ALERT (decision 2026-09-29): each asset's first appearance in the SC feed is recorded; once a day (the run
at NOTIFY_HOUR_SYD Sydney time) the arrivals since the last alert are posted to Slack via $SLACK_WEBHOOK_URL, only
when there are any. Creation itself stays on demand in Claude. The first run baselines the existing backlog.

  python3 build_hourly.py --out <dir> --state <state.json> [--feed <csv>] [--no-storefront]
                          [--cks-json <file>] [--force-notify] [--dry-notify]
Exit codes: 0 ok · 2 feed guard tripped (outputs untouched) · 3 SC storefront pull failed (outputs untouched)
"""
import argparse, csv, datetime as dt, json, os, pathlib, sys, time, urllib.request
from zoneinfo import ZoneInfo
import sc_core as sc

ARCHIVE_DAYS = 30
MIN_ROWS = 500          # the all-AU feed carried 2,250 on 2026-09-22
MAX_SHRINK = 0.20       # >20% fewer items than last run = treat as a broken feed
NOTIFY_HOUR_SYD = 8     # daily alert on the first run in this Sydney hour
CKS = 'https://commercialkitchenstore.com.au'
REFURB = ['refurbished-refrigerator-clearance', 'refurbished-benchtop-equipment-clearance',
          'refurbished-cooking-equipment-clearance', 'refurbished-ovens-clearance', 'refurbished-freezer-clearance',
          'refurbished-ice-maker-clearance', 'refurbished-dishwasher-clearance',
          'refurbished-mixers-and-bakery-equipment-clearance', 'refurbished-other-equipment-clearance',
          'refurbished-food-warmer-clearance', 'refurbished-trolley-bench-sink-clearance']


def write_csv(path, head, rows):
    tmp = path.with_suffix('.tmp')
    with open(tmp, 'w', newline='', encoding='utf-8') as fh:
        w = csv.writer(fh); w.writerow(head); w.writerows(rows)
    tmp.replace(path)     # atomic: Ablestar never fetches a half-written file


def cks_sold_out():
    """-> set of SC SKUs (…-SC) that are published on CKS and NOT available (sold out). Raises on any fetch error."""
    out, seen = set(), 0
    for h in REFURB:
        for page in range(1, 41):
            prods = json.loads(sc.curl(f'{CKS}/collections/{h}/products.json?limit=250&page={page}', 60))['products']
            if not prods:
                break
            for p in prods:
                for v in p['variants']:
                    sku = (v.get('sku') or '').upper()
                    if sku.endswith(sc.SKU_SUFFIX):
                        seen += 1
                        if not v.get('available'):
                            out.add(sku)
            time.sleep(0.3)
    return out, seen


def notify(new_rows, dry=False):
    """Post the day's new SC arrivals to Slack. Returns a status string; never raises (an alert must not break a sync)."""
    url = os.environ.get('SLACK_WEBHOOK_URL', '').strip()
    top = sorted(new_rows, key=lambda r: -r['price'])[:15]
    lines = [f"*{len(new_rows)} new SilverChef item(s)* in the feed since the last alert, not yet created on CKS.",
             "Say *\"SilverChef new items\"* to Claude to build the checked create file.", ""]
    lines += [f"• `{r['sku']}` {r['title']} (${r['price']:,.2f} ex-GST, {r['city']})" for r in top]
    if len(new_rows) > len(top):
        lines.append(f"…and {len(new_rows) - len(top)} more")
    text = '\n'.join(lines)
    if dry or not url:
        print(('DRY' if dry else 'NO SLACK_WEBHOOK_URL') + ' - message would be:\n' + text)
        return 'dry' if dry else 'no-webhook'
    try:
        req = urllib.request.Request(url, data=json.dumps({'text': text}).encode(), headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=30) as r:
            return f'sent {r.status}'
    except Exception as e:
        print(f'SLACK FAILED: {e}', file=sys.stderr)
        return f'failed: {e}'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', required=True); ap.add_argument('--state', required=True)
    ap.add_argument('--feed'); ap.add_argument('--no-storefront', action='store_true')
    ap.add_argument('--cks-json', help='TEST ONLY: JSON list of sold-out SKUs instead of reading the CKS storefront')
    ap.add_argument('--force-notify', action='store_true'); ap.add_argument('--dry-notify', action='store_true')
    a = ap.parse_args()
    out, state_p = pathlib.Path(a.out), pathlib.Path(a.state)
    out.mkdir(parents=True, exist_ok=True)
    state = json.loads(state_p.read_text()) if state_p.exists() else {}
    for k, v in (('seen', {}), ('first_seen', {}), ('frozen', []), ('last_count', None), ('last_notified', None)):
        state.setdefault(k, v)
    now = dt.datetime.now(dt.timezone.utc)

    feed = sc.load_feed(a.feed)
    n, prev = len(feed), state.get('last_count')
    if n < MIN_ROWS or (prev and n < prev * (1 - MAX_SHRINK)):
        print(f'GUARD: feed has {n} items (previous {prev}) - nothing published', file=sys.stderr)
        sys.exit(2)
    try:
        store = None if a.no_storefront else sc.load_storefront()
    except Exception as e:                      # never publish compare-at blind: it would clear live promos
        print(f'STOREFRONT FAILED: {e} - nothing published', file=sys.stderr)
        sys.exit(3)

    # FREEZE: sold out on CKS -> no updates. A failed read keeps the last known list (never unfreezes).
    try:
        if a.cks_json:
            sold_out, cks_seen = set(json.load(open(a.cks_json))), -1
        else:
            sold_out, cks_seen = cks_sold_out()
        cks_read = 'ok'
    except Exception as e:
        sold_out, cks_seen, cks_read = set(state['frozen']), None, f'FAILED ({e}) - reused last frozen list'
        print(f'CKS STOREFRONT: {cks_read}', file=sys.stderr)

    upd, current, bad, frozen = [], set(), [], []
    for p in feed:
        sku = p['Handle'] + sc.SKU_SUFFIX
        current.add(p['Handle'])
        if sku.upper() in sold_out:
            frozen.append(sku); continue
        try:
            c = sc.commercial(p, store if store is not None else {})
        except (ValueError, KeyError) as e:
            bad.append((p['Handle'], str(e))); continue
        upd.append([sku, f'{c["price"]:.2f}', f'{c["compare"]:.2f}', c['weight']])

    # ARCHIVE: seen within ARCHIVE_DAYS but gone from the feed (frozen ones included: SC dropping them = archive)
    seen = state['seen']
    for asset in current:
        seen[asset] = now.isoformat()
    cutoff = now - dt.timedelta(days=ARCHIVE_DAYS)
    gone = sorted(x for x, t in seen.items() if x not in current and dt.datetime.fromisoformat(t) >= cutoff)
    for x in [x for x, t in seen.items() if dt.datetime.fromisoformat(t) < cutoff]:
        del seen[x]

    # NEW ARRIVALS: first appearance in the SC feed. First-ever run baselines the backlog (no alert for it).
    first = state['first_seen']
    baseline = not first
    for p in feed:
        first.setdefault(p['Handle'], 'baseline' if baseline else now.isoformat())
    for x in [x for x in first if x not in current and x not in seen]:
        del first[x]                                # long gone: forget
    if state['last_notified'] is None:
        state['last_notified'] = now.isoformat()
    since = dt.datetime.fromisoformat(state['last_notified'])
    arrivals = [p for p in feed if first[p['Handle']] != 'baseline' and dt.datetime.fromisoformat(first[p['Handle']]) > since]
    notified = None
    syd = now.astimezone(ZoneInfo('Australia/Sydney'))
    due = a.force_notify or (syd.hour == NOTIFY_HOUR_SYD and syd.date().isoformat() != state.get('last_notify_day'))
    if due:
        if arrivals:
            notified = notify([{'sku': p['Handle'] + sc.SKU_SUFFIX, 'title': p['Title'], 'price': round(float(p['Variant Price']) / sc.GST, 2),
                                'city': p['Metafield: custom.city'].title()} for p in arrivals], dry=a.dry_notify)
        else:
            notified = 'nothing new - silent'
        if not a.dry_notify and not (notified or '').startswith('failed'):
            state['last_notified'] = now.isoformat()   # a failed post is retried next day with the same items
            state['last_notify_day'] = syd.date().isoformat()

    write_csv(out / 'update.csv', ['SKU', 'Price', 'Compare-at Price', 'Weight'], upd)
    write_csv(out / 'archive.csv', ['SKU', 'Status'], [[x + sc.SKU_SUFFIX, 'archived'] for x in gone])
    report = {'run_utc': now.isoformat(), 'feed_items': n, 'previous_items': prev, 'update_rows': len(upd),
              'archive_rows': len(gone), 'frozen_sold_out_on_cks': sorted(frozen), 'cks_storefront': cks_read,
              'cks_sc_variants_seen': cks_seen, 'new_since_last_alert': len(arrivals), 'alert': notified,
              'baseline_run': baseline, 'skipped_bad_rows': bad,
              'on_sale': sum(1 for r in upd if float(r[2]) > float(r[1])), 'storefront': store is not None}
    (out / 'report.json').write_text(json.dumps(report, indent=1))
    state['last_count'] = n
    state['frozen'] = sorted(sold_out)
    state_p.write_text(json.dumps(state))
    print(json.dumps({k: (len(v) if isinstance(v, list) else v) for k, v in report.items()}))


if __name__ == '__main__':
    main()
