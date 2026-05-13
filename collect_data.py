#!/usr/bin/env python3
"""
BTC Options Data Collector
Runs via GitHub Actions every 5 minutes — 24/7, no browser needed.
Fetches from Deribit API and stores to Supabase.
"""

import os, math, time, requests
from datetime import datetime, timezone

# ─── CONFIG ───────────────────────────────────────────────────────────────────
SUPABASE_URL = os.environ.get('SUPABASE_URL', 'https://avppsqawmfrpmldsqzss.supabase.co')
SUPABASE_KEY = os.environ.get('SUPABASE_KEY', '')
DERIBIT      = 'https://www.deribit.com/api/v2/public/'
BINANCE      = 'https://fapi.binance.com/fapi/v1/'
BYBIT        = 'https://api.bybit.com/v5/market/'
OKX          = 'https://www.okx.com/api/v5/public/'
HL_URL       = 'https://api.hyperliquid.xyz/info'

TIMEOUT = 15

# ─── HELPERS ──────────────────────────────────────────────────────────────────
def deribit(endpoint, params=None):
    r = requests.get(DERIBIT + endpoint, params=params, timeout=TIMEOUT)
    j = r.json()
    if 'error' in j:
        raise Exception(f"Deribit error: {j['error']}")
    return j['result']

def norm_cdf(x):
    """Approximation of normal CDF (Abramowitz & Stegun)"""
    a = [0.319381530, -0.356563782, 1.781477937, -1.821255978, 1.330274429]
    k = 1.0 / (1.0 + 0.2316419 * abs(x))
    p = k * (a[0] + k * (a[1] + k * (a[2] + k * (a[3] + k * a[4]))))
    pdf = math.exp(-x*x*0.5) / math.sqrt(2 * math.pi)
    result = 1.0 - pdf * p
    return result if x >= 0 else 1.0 - result

def bs_delta(S, K, T, sigma, is_call):
    """Black-Scholes delta"""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return 0.5 if is_call else -0.5
    d1 = (math.log(S/K) + 0.5 * sigma**2 * T) / (sigma * math.sqrt(T))
    return norm_cdf(d1) if is_call else norm_cdf(d1) - 1.0

def bs_gamma(S, K, T, sigma):
    """Black-Scholes gamma"""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return 0.0
    d1 = (math.log(S/K) + 0.5 * sigma**2 * T) / (sigma * math.sqrt(T))
    nd1 = math.exp(-d1*d1*0.5) / math.sqrt(2 * math.pi)
    return nd1 / (S * sigma * math.sqrt(T))

def parse_expiry(name):
    """Parse Deribit instrument name expiry to timestamp"""
    parts = name.split('-')
    if len(parts) < 4:
        return None
    exp_str = parts[1]
    months = {'JAN':1,'FEB':2,'MAR':3,'APR':4,'MAY':5,'JUN':6,
               'JUL':7,'AUG':8,'SEP':9,'OCT':10,'NOV':11,'DEC':12}
    try:
        day = int(exp_str[:2])
        mon = months[exp_str[2:5]]
        year = 2000 + int(exp_str[5:])
        dt = datetime(year, mon, day, 8, 0, 0, tzinfo=timezone.utc)
        return dt.timestamp() * 1000  # milliseconds
    except:
        return None

# ─── MAIN COLLECTION ──────────────────────────────────────────────────────────
def collect():
    now_ms  = time.time() * 1000
    now_ts  = int(now_ms)

    print(f"[{datetime.now(timezone.utc).isoformat()}] Starting collection...")

    # 1. BTC spot price
    idx   = deribit('get_index_price', {'index_name': 'btc_usd'})
    price = float(idx['index_price'])
    print(f"  BTC price: ${price:,.0f}")

    # 2. All BTC options
    raw_opts = deribit('get_book_summary_by_currency', {'currency': 'BTC', 'kind': 'option'})
    opts = [o for o in raw_opts if o.get('open_interest', 0) > 0]
    print(f"  Options loaded: {len(opts)} instruments")

    # 3. DVOL
    dvol = 60.0  # fallback
    try:
        dv = deribit('ticker', {'instrument_name': 'BTC-DVOL'})
        dvol = float(dv.get('mark_price') or dv.get('index_price') or 60)
    except:
        ivs = [float(o['mark_iv']) for o in opts if o.get('mark_iv', 0) > 0]
        if ivs:
            dvol = sum(ivs) / len(ivs)
    print(f"  DVOL: {dvol:.1f}")

    # 4. Parse options
    parsed = []
    for o in opts:
        expiry_ms = parse_expiry(o['instrument_name'])
        if not expiry_ms:
            continue
        strike = int(o['instrument_name'].split('-')[2])
        opt_type = o['instrument_name'].split('-')[3]
        parsed.append({
            'expiry_ms': expiry_ms,
            'strike':    strike,
            'type':      opt_type,
            'oi':        float(o.get('open_interest', 0)),
            'iv':        float(o.get('mark_iv', 0)),
        })

    # 5. Find expiries
    expiries = sorted(set(o['expiry_ms'] for o in parsed))
    future_exp = [e for e in expiries if e > now_ms]
    if not future_exp:
        raise Exception("No future expiries found")

    next_exp   = future_exp[0]

    # Weekly (next Friday)
    friday_exp = next((e for e in future_exp
                       if datetime.fromtimestamp(e/1000, tz=timezone.utc).weekday() == 4), None)
    weekly_exp = friday_exp or next_exp

    # Far expiry for term structure
    far_exp = expiries[-1] if len(expiries) > 1 else None

    # 6. Max Pain (weekly)
    weekly_opts = [o for o in parsed if o['expiry_ms'] == weekly_exp]
    strikes = sorted(set(o['strike'] for o in weekly_opts))
    max_pain = 0
    if strikes:
        min_pain, mp_strike = float('inf'), strikes[0]
        for s in strikes:
            pain = 0
            for o in weekly_opts:
                if o['type'] == 'C' and o['strike'] < s:
                    pain += (s - o['strike']) * o['oi']
                if o['type'] == 'P' and o['strike'] > s:
                    pain += (o['strike'] - s) * o['oi']
            if pain < min_pain:
                min_pain = pain
                mp_strike = s
        max_pain = mp_strike
    mp_delta = (max_pain - price) / price * 100 if max_pain > 0 else 0
    print(f"  Max Pain: ${max_pain:,} ({mp_delta:+.1f}%)")

    # 7. PCR
    all_calls = sum(o['oi'] for o in parsed if o['type'] == 'C')
    all_puts  = sum(o['oi'] for o in parsed if o['type'] == 'P')
    pcr = all_puts / all_calls if all_calls > 0 else 1.0
    print(f"  PCR: {pcr:.3f}")

    # 8. IV Skew (ATM ±5%)
    atm_calls = [o for o in weekly_opts if o['type']=='C' and
                 price*0.97 < o['strike'] < price*1.03 and o['iv'] > 0]
    atm_puts  = [o for o in weekly_opts if o['type']=='P' and
                 price*0.97 < o['strike'] < price*1.03 and o['iv'] > 0]
    avg_c_iv = sum(o['iv'] for o in atm_calls)/len(atm_calls) if atm_calls else 0
    avg_p_iv = sum(o['iv'] for o in atm_puts) /len(atm_puts)  if atm_puts  else 0
    iv_skew  = avg_p_iv - avg_c_iv if avg_c_iv > 0 and avg_p_iv > 0 else 0
    print(f"  IV Skew: {iv_skew:+.2f}%")

    # 9. Term Structure
    term_str = 0.0
    if far_exp:
        far_opts = [o for o in parsed if o['expiry_ms'] == far_exp]
        near_atm = [o for o in weekly_opts if abs(o['strike']-price) < price*0.05 and o['iv']>0]
        far_atm  = [o for o in far_opts   if abs(o['strike']-price) < price*0.05 and o['iv']>0]
        n_iv = sum(o['iv'] for o in near_atm)/len(near_atm) if near_atm else 0
        f_iv = sum(o['iv'] for o in far_atm) /len(far_atm)  if far_atm  else 0
        term_str = n_iv - f_iv if n_iv > 0 and f_iv > 0 else 0
    print(f"  Term Str: {term_str:+.2f}%")

    # 10. GEX (Black-Scholes gamma)
    gex_total = 0.0
    for o in weekly_opts:
        if o['oi'] <= 0 or o['iv'] <= 0:
            continue
        T = max(0, (o['expiry_ms'] - now_ms) / (365 * 24 * 3600 * 1000))
        g = bs_gamma(price, o['strike'], T, o['iv']/100)
        contrib = g * o['oi'] * price**2 / 100
        gex_total += contrib if o['type'] == 'C' else -contrib
    print(f"  GEX: {gex_total/1e6:+.1f}M")

    # 11. Aggregated Funding Rate
    funding_rates = {}
    try:
        r = requests.get(BINANCE + 'premiumIndex', params={'symbol':'BTCUSDT'}, timeout=TIMEOUT)
        funding_rates['binance'] = float(r.json().get('lastFundingRate', 0)) * 100
    except: pass
    try:
        r = requests.get(BYBIT + 'tickers', params={'category':'linear','symbol':'BTCUSDT'}, timeout=TIMEOUT)
        item = r.json().get('result',{}).get('list',[{}])[0]
        funding_rates['bybit'] = float(item.get('fundingRate', 0)) * 100
    except: pass
    try:
        r = requests.get(OKX + 'funding-rate', params={'instId':'BTC-USDT-SWAP'}, timeout=TIMEOUT)
        item = r.json().get('data',[{}])[0]
        funding_rates['okx'] = float(item.get('fundingRate', 0)) * 100
    except: pass
    try:
        r = requests.post(HL_URL, json={'type':'metaAndAssetCtxs'}, timeout=TIMEOUT)
        meta, ctxs = r.json()
        btc_idx = next((i for i,u in enumerate(meta['universe']) if u['name']=='BTC'), None)
        if btc_idx is not None:
            funding_rates['hyperliquid'] = float(ctxs[btc_idx].get('funding', 0)) * 100
    except: pass

    valid_rates = [v for v in funding_rates.values() if v is not None]
    funding_avg = sum(valid_rates)/len(valid_rates) if valid_rates else 0.0
    print(f"  Funding (avg {len(valid_rates)} exchanges): {funding_avg:+.4f}%")

    # 12. 25-delta RR for 7d and 30d tenors
    def get_25d_rr(target_ms):
        target_opts = [o for o in parsed if o['expiry_ms'] == target_ms]
        T = max(0, (target_ms - now_ms) / (365*24*3600*1000))
        if T < 1e-6 or len(target_opts) < 4:
            return 0.0, 0.0, 0.0
        best_c, best_p = None, None
        min_dc, min_dp = 99, 99
        for o in target_opts:
            if o['iv'] <= 0: continue
            d = bs_delta(price, o['strike'], T, o['iv']/100, o['type']=='C')
            diff = abs(abs(d) - 0.25)
            if o['type'] == 'C' and diff < min_dc: min_dc=diff; best_c=o
            if o['type'] == 'P' and diff < min_dp: min_dp=diff; best_p=o
        if not best_c or not best_p: return 0.0, 0.0, 0.0
        atm_iv_list = [o['iv'] for o in target_opts
                       if abs(o['strike']-price) < price*0.03 and o['iv']>0]
        atm_iv = sum(atm_iv_list)/len(atm_iv_list) if atm_iv_list else (best_c['iv']+best_p['iv'])/2
        rr = best_p['iv'] - best_c['iv']
        bf = (best_p['iv'] + best_c['iv'])/2 - atm_iv
        return rr, bf, atm_iv

    # Find ~7d and ~30d expiries
    ms7d  = now_ms + 7  * 24*3600*1000
    ms30d = now_ms + 30 * 24*3600*1000
    exp7d  = min(future_exp, key=lambda e: abs(e - ms7d))
    exp30d = min(future_exp, key=lambda e: abs(e - ms30d))

    rr7d, bf7d, atm7d   = get_25d_rr(exp7d)
    rr30d, _,   atm30d  = get_25d_rr(exp30d)
    rr1d, _,    _       = get_25d_rr(future_exp[0])  # nearest
    print(f"  25d RR 7d: {rr7d:+.2f}% | 30d: {rr30d:+.2f}%")

    # 13. Insert to Supabase
    row = {
        'ts':       now_ts,
        'price':    price,
        'pcr':      pcr,
        'iv_skew':  iv_skew,
        'dvol':     dvol,
        'term_str': term_str,
        'mp_delta': mp_delta,
        'gex':      gex_total,
        'funding':  funding_avg,
        'rr1d':     rr1d,
        'rr7d':     rr7d,
        'rr30d':    rr30d,
        'bf7d':     bf7d,
        'atm7d':    atm7d,
        'atm30d':   atm30d,
    }

    if SUPABASE_KEY:
        headers = {
            'apikey':       SUPABASE_KEY,
            'Authorization':'Bearer ' + SUPABASE_KEY,
            'Content-Type': 'application/json',
            'Prefer':       'return=minimal',
        }
        resp = requests.post(
            SUPABASE_URL + '/rest/v1/options_history',
            headers=headers,
            json=row,
            timeout=TIMEOUT,
        )
        if resp.status_code in (200, 201):
            print(f"  ✅ Saved to Supabase (ts={now_ts})")
        else:
            print(f"  ❌ Supabase error: {resp.status_code} {resp.text[:200]}")
    else:
        print(f"  ⚠ No SUPABASE_KEY — dry run only")
        print(f"  Row: {row}")

    print(f"  Done in {time.time() - now_ms/1000 + now_ms/1000:.1f}s")

if __name__ == '__main__':
    collect()
