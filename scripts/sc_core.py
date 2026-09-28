"""SilverChef Certified Used feed -> CKS product rows (rules set 2026-09-23..28; see ../SKILL.md).

Pure functions + loaders only: no store writes, no network except the two public SilverChef reads.
Deliberately contains NO cost logic: this module is published with the hourly job, so cost lives
only in build_new.py, which never leaves the local machine.
"""
import csv, html, io, json, pathlib, re, subprocess, time

HERE = pathlib.Path(__file__).resolve().parent
CONFIG = HERE.parent / 'config'
FEED_URL = 'https://silvercheffeedsauprod.blob.core.windows.net/feeds/au/all-products-feed-shopify.csv'
STORE_URL = 'https://www.silverchef.com.au/collections/certified-used-equipment/products.json?limit=250&page={}'
UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36'

GST = 1.1                 # SC prices are inc-GST; CKS stores ex-GST (shop.taxes_included = false)
CUBIC_RATE = 325          # shipping weight = product m3 x 325, never below SC net kg (shipping_class_1)
SKU_SUFFIX = '-SC'        # Jan-2025 CKS SilverChef draft convention

FIXED_TAGS = ['2nds', 'Condition_Used', 'SC-USED', 'Supplier_SilverChef', 'shipping_class_1']
FALLBACK = ('Other Equipment - Clearance', ['Kitchen Appliances - Clearance', 'Other Kitchen Appliances - Clearance'])

DIM_RE = re.compile(r'^\s*\d+\s*mm\s*W\s*x\s*\d+\s*mm\s*D\s*x\s*\d+\s*mm\s*H\s*$', re.I)
KG_RE = re.compile(r'^\s*(\d+(?:\.\d+)?)\s*kg\s*$', re.I)
COND_RE = re.compile(r'scratch|dent|mark|wear|chip|rust|discolou|missing|cosmetic|crack|damage|'
                     r'\bstain(ed|s)?\b|faded|repair', re.I)


# ---------- loaders ----------
def curl(url, timeout=120):
    # curl, not urllib: urllib gets HTTP 400 from SilverChef's storefront on some pages
    return subprocess.run(['curl', '-sfL', '-A', UA, url], capture_output=True, check=True, timeout=timeout).stdout


def load_feed(path=None):
    """Product rows of SC's Shopify CSV (image-only continuation rows dropped; images are in 'images', pipe-separated)."""
    raw = pathlib.Path(path).read_bytes() if path else curl(FEED_URL)
    rows = list(csv.DictReader(io.StringIO(raw.decode('utf-8-sig'))))
    prod = [r for r in rows if r.get('Title')]
    for p in prod:
        p['Title'] = re.sub(r'\s+', ' ', p['Title']).strip()
    return prod


def load_storefront():
    """SC's own storefront, keyed by image path -> {price, compare_at_price}. SC storefront SKUs are not
    the asset number, so the join is on the shared CDN image file (2,173/2,250 matched, 0 price mismatches, 24-09)."""
    out, page = {}, 1
    while True:
        prods = json.loads(curl(STORE_URL.format(page), 90))['products']
        if not prods:
            return out
        for p in prods:
            v = p['variants'][0]
            for img in p.get('images', []):
                out[_path(img['src'])] = {'price': v['price'], 'compare_at_price': v.get('compare_at_price')}
        page += 1
        time.sleep(0.5)


def load_category_map():
    """config/category_map.csv: subcategory,title_regex,type,leaves (leaves '|'-separated). First match wins;
    a row with a title_regex only applies when the title matches it."""
    rules = []
    with open(CONFIG / 'category_map.csv', encoding='utf-8') as fh:
        for r in csv.DictReader(fh):
            rules.append((r['subcategory'].strip().lower(), r['title_regex'].strip(), r['type'].strip(),
                          [x.strip() for x in r['leaves'].split('|') if x.strip()]))
    return rules


# ---------- rules ----------
def _path(u):
    return re.sub(r'\?.*$', '', u or '')


def slug(s):
    return re.sub(r'-+', '-', re.sub(r'[^a-z0-9]+', '-', s.lower())).strip('-')


def model_of(p):
    """SC `model`, minus a repeated brand (10/2,250: 'Giorik KB061WT' -> 'KB061WT')."""
    m, v = p['Metafield: custom.model'].strip(), p['Vendor'].strip()
    return m[len(v) + 1:] if v and m.lower().startswith(v.lower() + ' ') else m


def classify(p, rules):
    sub, title = p['Metafield: custom.subcategory'].strip().lower(), p['Title']
    for s, rx, ptype, leaves in rules:
        if s == sub and (not rx or re.search(rx, title, re.I)):
            return ptype, leaves, True
    return FALLBACK[0], FALLBACK[1], False


def split_body(body):
    """'Upright Fridge. Features a, b, c' -> (intro, features, condition_notes, dropped_dims, net_kg)."""
    body = re.sub(r'�\s*([cC])\b', '°C', body.strip())   # degree sign lost upstream
    m = re.match(r'^(.*?)\.\s*Features\s+(.*)$', body, re.S)
    if not m:
        return body, [], [], None, None
    feats, cond, dims, kg = [], [], None, None
    for f in (x.strip().rstrip('.') for x in m.group(2).split(',')):
        if not f:
            continue
        if DIM_RE.match(f):
            dims = f                      # the structured W/D/H fields win (SC storefront agrees with them)
        elif KG_RE.match(f):
            kg = float(KG_RE.match(f).group(1))
        else:
            (cond if COND_RE.search(f) else feats).append(f)
    return m.group(1).strip(), feats, cond, dims, kg


def body_html(asset, intro, feats, cond, w, d, h):
    """Moffat Warehouse Clearance layout; SC has no serial, so the last block is the asset number. No warranty."""
    e = html.escape
    desc = intro + ('. Features ' + ', '.join(feats) if feats else '') + '.'
    condition = 'SilverChef Certified Used' + ('. ' + ', '.join(c[0].upper() + c[1:] for c in cond) if cond else '') + '.'
    return (f'<p><strong>Product Description:</strong><br>{e(desc)}</p>'
            f'<p><strong>Condition of Equipment:</strong><br>{e(condition)}</p>'
            f'<p><strong>Dimensions:</strong><br>W: {w}mm D: {d}mm H: {h}mm</p>'
            f'<p><strong>Asset Number:</strong> {e(asset)}</p>')


def commercial(p, store):
    """Price / compare-at / shipping weight: the commercial fields updated hourly on existing items."""
    inc = float(p['Variant Price'])
    price = round(inc / GST, 2)
    sc = store.get(_path(p['Image Src'])) if store is not None else None
    was = float(sc['compare_at_price']) if sc and sc.get('compare_at_price') else 0.0
    # promo 'was' price only while SC shows one; otherwise compare-at = price, which clears any
    # strikethrough (a blank cell's behaviour in Ablestar is unverified, equal-to-price is not)
    compare = round(was / GST, 2) if was > inc else price
    w, d, h = (int(p[f'Metafield: custom.{k}']) for k in ('width', 'depth', 'height'))
    _, _, _, _, net = split_body(p['Body (HTML)'])
    weight = max(1, round(max(w * d * h / 1e9 * CUBIC_RATE, net or 0)))
    return {'price': price, 'compare': compare, 'weight': weight,
            'net_kg': net, 'on_sale': was > inc, 'store_matched': sc is not None}


def product_row(p, rules, store):
    """Full CKS-shaped row for a NEW product (Ablestar Create)."""
    a = p['Handle']
    ptype, leaves, mapped = classify(p, rules)
    w, d, h = p['Metafield: custom.width'], p['Metafield: custom.depth'], p['Metafield: custom.height']
    intro, feats, cond, _, net = split_body(p['Body (HTML)'])
    model = model_of(p)
    c = commercial(p, store)
    tags = (['Category_Clearance and Used Equipment'] + [f'Category_{x}' for x in leaves] +
            [f'Final Category_{leaves[-1]}'] + FIXED_TAGS +
            [f'Brand_{p["Vendor"]}', f'MPN_{model}', f'Product-Width_{w}', f'Product-Depth_{d}',
             f'Product-Height_{h}', f'Product-Dimensions_{w}(W) x {d}(D) x {h}(H)mm'])
    tags = [t.replace(',', ' ') for t in tags]          # Shopify splits tags on commas
    title = f'Used {p["Title"]} - {a}'
    return {
        'Handle': slug(f'used-{p["Vendor"]}-{model}-{a}'),
        'SKU': f'{a}{SKU_SUFFIX}', 'Title': title, 'Description': body_html(a, intro, feats, cond, w, d, h),
        'Vendor': p['Vendor'], 'Type': ptype, 'Tags': ', '.join(tags), 'Status': 'draft',
        'Price': f'{c["price"]:.2f}', 'Compare-at Price': f'{c["compare"]:.2f}',   # Cost: added by build_new
        'Weight': c['weight'], 'Weight Unit': 'kg', 'Inventory Quantity': 1, 'Track Inventory': 'TRUE',
        'Continue Selling When Out of Stock': 'FALSE', 'Requires Shipping': 'TRUE', 'Taxable': 'TRUE',
        'Images': '|'.join(u for u in p['images'].split('|') if u), 'Image Alt Text': title,
        'Width': w, 'Depth': d, 'Height': h, 'Product Dimensions': f'{w}W x {d}D x {h}Hmm',
        'Product Weight': f'{net:g}' if net else '', 'Power (global.power)': p['Metafield: custom.power'],
        'MPN (global.mpn)': model, 'Brand (custom.brand)': p['Vendor'], 'Supplier (global.supplier)': 'SilverChef',
        '_mapped': mapped, '_subcategory': p['Metafield: custom.subcategory'],
    }
