import os
import requests
import base64
import urllib3
import json
import time
import threading
import traceback
from flask import Flask, jsonify, render_template, request
from datetime import datetime, timedelta, timezone
from collections import defaultdict

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__, template_folder=".")

CW_SITE        = os.environ.get("CW_SITE", "api-eu.myconnectwise.net")
CW_COMPANY     = os.environ.get("CW_COMPANY", "")
CW_PUBLIC_KEY  = os.environ.get("CW_PUBLIC_KEY", "")
CW_PRIVATE_KEY = os.environ.get("CW_PRIVATE_KEY", "")
CW_CLIENT_ID   = os.environ.get("CW_CLIENT_ID", "")
HTTPS_PROXY    = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or ""
REFRESH_INTERVAL = int(os.environ.get("CW_REFRESH_INTERVAL", "600"))
VERIFY_SSL     = os.environ.get("CW_VERIFY_SSL", "true").lower() != "false"
SYNC_DAYS_BACK = int(os.environ.get("SYNC_DAYS_BACK", "730"))

# Persistent Data Storage
DATA_DIR = "/data"
DATA_FILE = os.path.join(DATA_DIR, "sales_data.json")
TEMP_DATA_FILE = os.path.join(DATA_DIR, "sales_data.tmp.json")

# In-Memory Cache
DATA_STORE = {
    "opportunities": {},
    "orders": {},
    "activities": {},
    "last_sync": None
}

def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)

def get_session():
    s = requests.Session()
    if HTTPS_PROXY:
        s.proxies = {"https": HTTPS_PROXY, "http": HTTPS_PROXY}
    s.verify = VERIFY_SSL
    return s

def get_auth_header():
    creds = f"{CW_COMPANY}+{CW_PUBLIC_KEY}:{CW_PRIVATE_KEY}"
    encoded = base64.b64encode(creds.encode()).decode()
    return {
        "Authorization": f"Basic {encoded}",
        "clientId": CW_CLIENT_ID,
        "Content-Type": "application/json"
    }

def cw_get(endpoint, params=None):
    url = f"https://{CW_SITE}/v4_6_release/apis/3.0{endpoint}"
    headers = get_auth_header()
    all_results = []
    page = 1
    page_size = 1000 
    if params is None: params = {}
    session = get_session()
    while True:
        paged_params = {**params, "page": page, "pageSize": page_size}
        response = session.get(url, headers=headers, params=paged_params, timeout=120)
        response.raise_for_status()
        data = response.json()
        if not data: break
        all_results.extend(data)
        if len(data) < page_size: break
        page += 1
    return all_results

def parse_cw_date(d_str):
    """Parse any ISO-8601 datetime CW (or the dashboard) might emit.

    Handles: trailing 'Z', fractional seconds, explicit offsets like '-04:00',
    and bare 'YYYY-MM-DD' date-only values. Always returns a tz-aware UTC
    datetime (or None)."""
    if not d_str:
        return None
    if isinstance(d_str, datetime):
        return d_str if d_str.tzinfo else d_str.replace(tzinfo=timezone.utc)
    try:
        s = str(d_str).strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            # Date-only ("2024-03-15") or odd separators – fall back.
            if len(s) == 10 and s[4] == "-" and s[7] == "-":
                dt = datetime.fromisoformat(s + "T00:00:00+00:00")
            else:
                # Last resort: strip fractional seconds and any trailing offset.
                base = s.split(".")[0].split("+")[0].split("-")
                # Re-join YYYY-MM-DDTHH:MM:SS (first 3 dash-separated parts).
                if len(base) >= 3:
                    head = "-".join(base[:3])
                    dt = datetime.fromisoformat(head + "+00:00")
                else:
                    return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None

def _log_store_diagnostics():
    """Dump a one-line summary of date fields & rep fields into the logs so we
    can diagnose 'matched 0 records' issues without needing an HTTP endpoint
    (some hosting proxies block /api/debug-style paths)."""
    try:
        orders = list(DATA_STORE["orders"].values())
        opps = list(DATA_STORE["opportunities"].values())

        def date_summary(records, fields):
            counts = {f: 0 for f in fields}
            parsed = []
            for r in records:
                for f in fields:
                    v = r.get(f)
                    if v:
                        counts[f] += 1
                d = None
                for f in fields:
                    d = parse_cw_date(r.get(f))
                    if d:
                        break
                if d:
                    parsed.append(d)
            rng = (min(parsed).date().isoformat(), max(parsed).date().isoformat()) if parsed else (None, None)
            return counts, rng, len(parsed)

        def rep_summary(records, fields):
            counts = defaultdict(int)
            for r in records:
                name = None
                for f in fields:
                    rep = r.get(f)
                    if isinstance(rep, dict):
                        name = rep.get("name") or rep.get("identifier")
                        if name: break
                    elif isinstance(rep, str) and rep.strip():
                        name = rep.strip(); break
                counts[name or "Unassigned"] += 1
            return dict(sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:5])

        def max_last_updated(records):
            best = None
            for r in records:
                d = parse_cw_date((r.get("_info") or {}).get("lastUpdated"))
                if d and (best is None or d > best):
                    best = d
            return best.isoformat() if best else None

        if orders:
            sample = orders[0]
            c, rng, parsed = date_summary(orders, ["orderDate", "dateEntered", "billDate", "shipDate"])
            log(f"[diag] orders sample keys: {sorted(sample.keys())}")
            log(f"[diag] orders date field populated counts: {c} | parseable date range: {rng[0]}..{rng[1]} ({parsed}/{len(orders)})")
            log(f"[diag] orders top reps: {rep_summary(orders, ['salesRep','primarySalesRep','owner'])}")
            log(f"[diag] orders max _info.lastUpdated: {max_last_updated(orders)}")
        if opps:
            sample = opps[0]
            c, rng, parsed = date_summary(opps, ["dateBecameLead", "dateEntered", "closedDate", "expectedCloseDate"])
            log(f"[diag] opps sample keys: {sorted(sample.keys())}")
            log(f"[diag] opps date field populated counts: {c} | parseable lead-date range: {rng[0]}..{rng[1]} ({parsed}/{len(opps)})")
            log(f"[diag] opps top reps: {rep_summary(opps, ['primarySalesRep','salesRep','owner'])}")
            log(f"[diag] opps max _info.lastUpdated: {max_last_updated(opps)}")
    except Exception as e:
        log(f"[diag] failed to log diagnostics: {e}")


# --- BACKGROUND HARVESTER THREAD ---
def harvest_data():
    global DATA_STORE
    
    os.makedirs(DATA_DIR, exist_ok=True)
    
    while True:
        try:
            if not DATA_STORE.get("last_sync") and os.path.exists(DATA_FILE):
                with open(DATA_FILE, 'r') as f:
                    DATA_STORE.update(json.load(f))
                    
            sync_since = DATA_STORE.get("last_sync")
            
            if not sync_since:
                sync_since = (datetime.now(timezone.utc) - timedelta(days=SYNC_DAYS_BACK)).strftime("%Y-%m-%dT%H:%M:%SZ")
                log(f"Starting initial historical harvest (Last {SYNC_DAYS_BACK} days). This may take several minutes...")
            else:
                log(f"Harvesting changes since {sync_since}...")

            opps = cw_get("/sales/opportunities", {"conditions": f"lastUpdated >= [{sync_since}]"})
            for o in opps: DATA_STORE["opportunities"][str(o["id"])] = o

            orders = cw_get("/sales/orders", {"conditions": f"lastUpdated >= [{sync_since}]"})
            for o in orders:
                cost = 0.0
                if o.get("productIds"):
                    products = cw_get("/procurement/products", {"conditions": f"id in ({','.join(map(str, o['productIds']))})", "fields": "cost,quantity"})
                    for p in products:
                        cost += float(p.get("cost") or 0.0) * float(p.get("quantity") or 1.0)
                o["_calculated_cost"] = cost
                DATA_STORE["orders"][str(o["id"])] = o

            acts = cw_get("/sales/activities", {"conditions": f"lastUpdated >= [{sync_since}]"})
            for a in acts: DATA_STORE["activities"][str(a["id"])] = a

            DATA_STORE["last_sync"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

            with open(TEMP_DATA_FILE, 'w') as f:
                json.dump(DATA_STORE, f)
            os.replace(TEMP_DATA_FILE, DATA_FILE)
            
            log(f"Harvest complete. Opps: {len(DATA_STORE['opportunities'])}, Orders: {len(DATA_STORE['orders'])}, Acts: {len(DATA_STORE['activities'])}")
            _log_store_diagnostics()

        except Exception as e:
            log(f"!! Harvest error: {str(e)}")
            log(traceback.format_exc())

        time.sleep(REFRESH_INTERVAL)

harvester_thread = threading.Thread(target=harvest_data, daemon=True)
harvester_thread.start()

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/sales-stats")
def sales_stats():
    if not DATA_STORE.get("last_sync"):
        return jsonify({"error": "Initial data sync in progress... Please wait."}), 503

    try:
        since_str = request.args.get('since')
        until_str = request.args.get('until')
        timeframe_label = request.args.get('label', 'Custom Range')

        since = parse_cw_date(since_str)
        until = parse_cw_date(until_str)

        if not since or not until:
            return jsonify({"error": "Invalid date format"}), 400

        created_opps = []
        closed_opps = []
        recent_orders = []
        recent_activities = []

        for opp in DATA_STORE["opportunities"].values():
            dl = parse_cw_date(opp.get("dateBecameLead") or opp.get("dateEntered"))
            dc = parse_cw_date(opp.get("closedDate"))
            if dl and since <= dl <= until: created_opps.append(opp)
            if dc and since <= dc <= until: closed_opps.append(opp)

        for ord in DATA_STORE["orders"].values():
            od = parse_cw_date(ord.get("orderDate") or ord.get("dateEntered"))
            if od and since <= od <= until: recent_orders.append(ord)

        for act in DATA_STORE["activities"].values():
            ds = parse_cw_date(act.get("dateStart") or act.get("dateEntered"))
            if ds and since <= ds <= until: recent_activities.append(act)

        log(f"[sales-stats] {timeframe_label} {since.isoformat()} -> {until.isoformat()} | "
            f"matched opps(created/closed)={len(created_opps)}/{len(closed_opps)} "
            f"orders={len(recent_orders)} activities={len(recent_activities)}")

        # Chart Buckets Setup
        daily_buckets = {}
        days_range = (until.date() - since.date()).days + 1
        
        chart_bucket_format = "%Y-%m" if days_range > 100 else "%Y-%m-%d"
        chart_label_format = "%b %Y" if days_range > 100 else ("%d %b" if days_range > 7 else "%A")

        temp_date = since
        while temp_date <= until:
            key = temp_date.strftime(chart_bucket_format)
            if key not in daily_buckets:
                # Set up Revenue and Profit tracking for the chart instead of Leads/Won
                daily_buckets[key] = {"date": temp_date.strftime(chart_label_format), "revenue": 0.0, "profit": 0.0}
            temp_date += timedelta(days=32 if days_range > 100 else 1)
            if days_range > 100: temp_date = temp_date.replace(day=1)

        # Process orders to build the new Chart data
        for ord in recent_orders:
            od = parse_cw_date(ord.get("orderDate"))
            if od:
                k = od.strftime(chart_bucket_format)
                if k in daily_buckets:
                    rev = float(ord.get("total", 0.0))
                    cost = float(ord.get("_calculated_cost", 0.0))
                    daily_buckets[k]["revenue"] += rev
                    daily_buckets[k]["profit"] += (rev - cost)

        # Rep Aggregation (Unchanged)
        rep_data = defaultdict(lambda: {"created": 0, "won": 0, "lost": 0, "revenue": 0.0, "cost": 0.0, "activities": 0, "orders": []})

        # CW order entities use a few different field names for the rep depending
        # on tenant/version; try each in order and fall back to "Unassigned".
        REP_FIELD_CANDIDATES = {
            "opportunity": ("primarySalesRep", "salesRep", "owner"),
            "order":       ("salesRep", "primarySalesRep", "owner"),
            "activity":    ("assignTo", "ownerResource", "assignedBy"),
        }

        def get_rep_name(obj, kind):
            for field in REP_FIELD_CANDIDATES.get(kind, ()):
                rep = obj.get(field)
                if isinstance(rep, dict):
                    name = rep.get("name") or rep.get("identifier")
                    if name:
                        return name
                elif isinstance(rep, str) and rep.strip():
                    return rep.strip()
            return "Unassigned"

        for o in created_opps:
            rep_data[get_rep_name(o, "opportunity")]["created"] += 1
        for o in closed_opps:
            key = "won" if ("won" in o.get("stage",{}).get("name","").lower() or "won" in o.get("status",{}).get("name","").lower()) else "lost"
            rep_data[get_rep_name(o, "opportunity")][key] += 1

        for act in recent_activities:
            rep_data[get_rep_name(act, "activity")]["activities"] += 1

        for ord in recent_orders:
            name = get_rep_name(ord, "order")
            rev = float(ord.get("total", 0.0) or 0.0)
            total_cost = float(ord.get("_calculated_cost", 0.0) or 0.0)

            rep_data[name]["revenue"] += rev
            rep_data[name]["cost"] += total_cost
            rep_data[name]["orders"].append({"id": ord["id"], "title": f"{ord.get('company',{}).get('name','Unknown')} - {ord.get('opportunity',{}).get('name','Direct')}", "total": rev, "profit": rev - total_cost})

        # Keep "Unassigned" so revenue/profit/activity that isn't tied to a named
        # rep is still reflected in the dashboard. Only drop reps with no activity
        # at all (no revenue, no opps, no activities).
        final_users = []
        for name, d in rep_data.items():
            has_any = d["revenue"] > 0 or d["created"] > 0 or d["won"] > 0 or d["lost"] > 0 or d["activities"] > 0
            if not has_any:
                continue
            profit = d["revenue"] - d["cost"]
            margin_pct = round((profit / d["revenue"]) * 100) if d["revenue"] > 0 else 0
            final_users.append({**d, "name": name, "profit": profit, "profit_margin": margin_pct, "orders": sorted(d["orders"], key=lambda x: x["total"], reverse=True)})

        final_users.sort(key=lambda u: u["revenue"], reverse=True)
        total_rev = sum(u["revenue"] for u in final_users)
        total_profit = sum(u["profit"] for u in final_users)

        return jsonify({
            "totals": {
                "created": sum(u["created"] for u in final_users),
                "won": sum(u["won"] for u in final_users),
                "revenue": total_rev,
                "profit": total_profit,
                "margin": round((total_profit / total_rev) * 100) if total_rev > 0 else 0,
                "activities": sum(u["activities"] for u in final_users)
            },
            "users": final_users,
            "daily": list(daily_buckets.values()),
            "timeframeLabel": timeframe_label
        })
    except Exception as e: 
        log(f"API Error: {str(e)}\n{traceback.format_exc()}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/probe-cw")
def probe_cw():
    """Hit CW directly with a battery of condition syntaxes against a date we
    KNOW must return rows. Whichever variants return non-zero tell us the
    exact syntax our harvester should be using."""
    try:
        url = f"https://{CW_SITE}/v4_6_release/apis/3.0/sales/orders"
        headers = get_auth_header()
        session = get_session()

        # Allow override via ?since=YYYY-MM-DDTHH:MM:SSZ but default to a date
        # we are confident MUST return rows (one of the rows from the previous
        # probe was lastUpdated 2026-05-11T10:40:39Z).
        since = request.args.get("since") or "2026-05-01T00:00:00Z"
        since_no_z = since.rstrip("Z")
        since_date_only = since.split("T")[0]

        def fetch(params):
            r = session.get(url, headers=headers, params=params, timeout=60)
            try:
                body = r.json() if r.ok else r.text[:500]
            except Exception:
                body = r.text[:500]
            return {"status": r.status_code, "body": body, "url": r.url}

        latest_orders  = fetch({"orderBy": "_info/lastUpdated desc", "pageSize": 5})
        latest_by_date = fetch({"orderBy": "orderDate desc",         "pageSize": 5})

        variants = {
            "A_lastUpdated_with_Z":           {"conditions": f"lastUpdated >= [{since}]"},
            "B_lastUpdated_no_Z":             {"conditions": f"lastUpdated >= [{since_no_z}]"},
            "C_lastUpdated_date_only":        {"conditions": f"lastUpdated >= [{since_date_only}]"},
            "D_lastUpdated_strict_gt_with_Z": {"conditions": f"lastUpdated > [{since}]"},
            "E_info_slash_with_Z":            {"conditions": f"_info/lastUpdated >= [{since}]"},
            "F_info_slash_no_Z":              {"conditions": f"_info/lastUpdated >= [{since_no_z}]"},
            "G_info_dot_with_Z":              {"conditions": f"_info.lastUpdated >= [{since}]"},
            "H_orderDate_with_Z":             {"conditions": f"orderDate >= [{since}]"},
            "I_dateEntered_with_Z":           {"conditions": f"dateEntered >= [{since}]"},
        }
        variant_results = {}
        for name, params in variants.items():
            params = {**params, "pageSize": 5, "orderBy": "_info/lastUpdated desc"}
            variant_results[name] = fetch(params)

        def summarize(result):
            if not isinstance(result, dict):
                return result
            body = result.get("body")
            if isinstance(body, list):
                return {
                    "status": result["status"],
                    "count": len(body),
                    "rows": [{
                        "id": r.get("id"),
                        "orderDate": r.get("orderDate"),
                        "lastUpdated": (r.get("_info") or {}).get("lastUpdated"),
                        "company": (r.get("company") or {}).get("name"),
                    } for r in body],
                }
            return {"status": result.get("status"), "error_body": body}

        return jsonify({
            "harvester_last_sync": DATA_STORE.get("last_sync"),
            "probe_since": since,
            "orders_orderby_lastUpdated_desc": summarize(latest_orders),
            "orders_orderby_orderDate_desc":   summarize(latest_by_date),
            "condition_variants": {k: summarize(v) for k, v in variant_results.items()},
        })
    except Exception as e:
        log(f"probe-cw error: {e}\n{traceback.format_exc()}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/resync", methods=["POST", "GET"])
def force_resync():
    """Wipe last_sync so the next harvester cycle does a full historical pull.
    Use this to recover from a corrupted incremental cursor."""
    DATA_STORE["last_sync"] = None
    try:
        with open(TEMP_DATA_FILE, "w") as f:
            json.dump(DATA_STORE, f)
        os.replace(TEMP_DATA_FILE, DATA_FILE)
    except Exception as e:
        log(f"resync persist warning: {e}")
    log("[resync] last_sync cleared; next harvest cycle will pull SYNC_DAYS_BACK history.")
    return jsonify({"ok": True, "message": "last_sync cleared; full re-harvest will occur on next cycle (within REFRESH_INTERVAL seconds)."})


@app.route("/api/inspect")
def debug_store():
    """Diagnostic snapshot of what the harvester actually pulled.

    Helps answer questions like 'is orderDate populated?' or 'do orders have a
    salesRep set?' without dumping the full dataset."""
    def date_range(records, *fields):
        dates = []
        for r in records:
            for f in fields:
                d = parse_cw_date(r.get(f))
                if d:
                    dates.append(d)
                    break
        if not dates:
            return {"min": None, "max": None, "parsed": 0, "total": len(records)}
        return {
            "min": min(dates).isoformat(),
            "max": max(dates).isoformat(),
            "parsed": len(dates),
            "total": len(records),
        }

    def top_reps(records, *fields, limit=10):
        counts = defaultdict(int)
        for r in records:
            name = None
            for f in fields:
                rep = r.get(f)
                if isinstance(rep, dict):
                    name = rep.get("name") or rep.get("identifier")
                    if name:
                        break
                elif isinstance(rep, str) and rep.strip():
                    name = rep.strip()
                    break
            counts[name or "Unassigned"] += 1
        return sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:limit]

    opps = list(DATA_STORE["opportunities"].values())
    orders = list(DATA_STORE["orders"].values())
    acts = list(DATA_STORE["activities"].values())

    sample_order = orders[0] if orders else None
    sample_opp = opps[0] if opps else None

    return jsonify({
        "last_sync": DATA_STORE.get("last_sync"),
        "counts": {"opportunities": len(opps), "orders": len(orders), "activities": len(acts)},
        "orders": {
            "orderDate_range": date_range(orders, "orderDate", "dateEntered"),
            "rep_breakdown":   top_reps(orders, "salesRep", "primarySalesRep", "owner"),
            "with_total_gt_0": sum(1 for o in orders if float(o.get("total") or 0) > 0),
            "sample_keys":     sorted(sample_order.keys()) if sample_order else [],
            "sample":          sample_order,
        },
        "opportunities": {
            "dateBecameLead_range": date_range(opps, "dateBecameLead", "dateEntered"),
            "closedDate_range":     date_range(opps, "closedDate"),
            "rep_breakdown":        top_reps(opps, "primarySalesRep", "salesRep", "owner"),
            "sample_keys":          sorted(sample_opp.keys()) if sample_opp else [],
            "sample":               sample_opp,
        },
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
