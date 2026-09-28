#!/usr/bin/env python3
"""HOURLY (cloud, no store login): SilverChef feed -> two Ablestar Inventory Sync files.

  update.csv   SKU, Price, Compare-at Price, Weight    every item in the feed (commercial fields only:
               no cost - the file is public-by-link - and NO STOCK, so an item sold on CKS is never reset to 1)
  archive.csv  SKU, Status=archived                    every asset seen in the last ARCHIVE_DAYS that has
                                                       left the feed (idempotent; unknown SKUs are no-ops)

State (state.json) = {asset: last_seen} + last feed count. Nothing is written if the feed looks broken.

  python3 build_hourly.py --out <dir> --state <state.json> [--feed <local csv>] [--no-storefront]
Exit codes: 0 ok · 2 feed guard tripped (outputs untouched) · 3 storefront pull failed (outputs untouched)
"""
import argparse, csv, datetime as dt, json, pathlib, sys
import sc_core as sc

ARCHIVE_DAYS = 30
MIN_ROWS = 500          # the all-AU feed carried 2,250 on 2026-09-22
MAX_SHRINK = 0.20       # >20% fewer items than last run = treat as a broken feed


def write_csv(path, head, rows):
    tmp = path.with_suffix('.tmp')
    with open(tmp, 'w', newline='', encoding='utf-8') as fh:
        w = csv.writer(fh); w.writerow(head); w.writerows(rows)
    tmp.replace(path)     # atomic: Ablestar never fetches a half-written file


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', required=True); ap.add_argument('--state', required=True)
    ap.add_argument('--feed'); ap.add_argument('--no-storefront', action='store_true')
    a = ap.parse_args()
    out, state_p = pathlib.Path(a.out), pathlib.Path(a.state)
    out.mkdir(parents=True, exist_ok=True)
    state = json.loads(state_p.read_text()) if state_p.exists() else {'seen': {}, 'last_count': None}
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

    upd, current, bad = [], set(), []
    for p in feed:
        try:
            c = sc.commercial(p, store if store is not None else {})
        except (ValueError, KeyError) as e:
            bad.append((p['Handle'], str(e))); continue
        current.add(p['Handle'])
        upd.append([p['Handle'] + sc.SKU_SUFFIX, f'{c["price"]:.2f}', f'{c["compare"]:.2f}', c['weight']])

    seen = state['seen']
    for asset in current:
        seen[asset] = now.isoformat()
    cutoff = now - dt.timedelta(days=ARCHIVE_DAYS)
    gone = sorted(x for x, t in seen.items() if x not in current and dt.datetime.fromisoformat(t) >= cutoff)
    for x in [x for x, t in seen.items() if dt.datetime.fromisoformat(t) < cutoff]:
        del seen[x]

    write_csv(out / 'update.csv', ['SKU', 'Price', 'Compare-at Price', 'Weight'], upd)
    write_csv(out / 'archive.csv', ['SKU', 'Status'], [[x + sc.SKU_SUFFIX, 'archived'] for x in gone])
    report = {'run_utc': now.isoformat(), 'feed_items': n, 'previous_items': prev, 'update_rows': len(upd),
              'archive_rows': len(gone), 'skipped_bad_rows': bad,
              'on_sale': sum(1 for r in upd if float(r[2]) > float(r[1])), 'storefront': store is not None}
    (out / 'report.json').write_text(json.dumps(report, indent=1))
    state['last_count'] = n
    state_p.write_text(json.dumps(state))
    print(json.dumps({k: v for k, v in report.items() if k != 'skipped_bad_rows'} | {'skipped_bad_rows': len(bad)}))


if __name__ == '__main__':
    main()
