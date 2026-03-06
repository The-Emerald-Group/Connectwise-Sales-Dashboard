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
REFRESH_INTERVAL = int(os.environ.get("CW_REFRESH_INTERVAL", "300"))
VERIFY_SSL     = os.environ.get("CW_VERIFY_SSL", "true").lower() != "false"
SYNC_DAYS_BACK = int(os.environ.get("SYNC_DAYS_BACK", "730")) # Grab 2 years of history by default

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
    page_size = 1000 # Larger page size for faster background sync
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
    if not d_str: return None
    try:
        clean_str = d_str.split('.')[0].replace("Z", "")
        dt = datetime.fromisoformat(clean_str + "+00:00")
        return dt
    except Exception:
        return None

# --- BACKGROUND HARVESTER THREAD ---
def harvest_data():
    global DATA_STORE
    
    os.makedirs(DATA_DIR, exist_ok=True)
    
    while True:
        try:
            # 1. Load existing data from disk if memory is empty
            if not DATA_STORE.get("last_sync") and os.path.exists(DATA_FILE):
                with open(DATA_FILE, 'r') as f:
                    DATA_STORE.update(json.load(f))
                    
            sync_since = DATA_STORE.get("last_sync")
            
            if not sync_since:
                # First ever run: Calculate how far back to look
                sync_since = (datetime.now(timezone.utc) - timedelta(days=SYNC_DAYS_BACK)).strftime("%Y-%m-%dT%H:%M:%SZ")
                log(f"Starting initial historical harvest (Last {SYNC_DAYS_BACK} days). This may take several minutes...")
            else:
                log(f"Harvesting changes since {sync_since}...")

            # Fetch OPPS updated since last run
            opps = cw_get("/sales/opportunities", {"conditions": f"lastUpdated >= [{sync_since}]"})
            for o in opps: DATA_STORE["opportunities"][str(o["id"])] = o

            # Fetch ORDERS updated since last run
            orders = cw_get("/sales/orders", {"conditions": f"lastUpdated >= [{sync_since}]"})
            for o in orders:
                # Automatically calculate sub-cost for new or modified orders
                cost = 0.0
                if o.get("productIds"):
                    products = cw_get("/procurement/products", {"conditions": f"id in ({','.join(map(str, o['productIds']))})", "fields": "cost,quantity"})
                    for p in products:
                        cost += float(p.get("cost") or 0.0) * float(p.get("quantity") or 1.0)
                o["_calculated_cost"] = cost
                DATA_STORE["orders"][str(o["id"])] = o

            # Fetch ACTIVITIES updated since last run
            acts = cw_get("/sales/activities", {"conditions": f"lastUpdated >= [{sync_since}]"})
            for a in acts: DATA_STORE["activities"][str(a["id"])] = a

            DATA_STORE["last_sync"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

            # Save cleanly to disk
            with open(TEMP_DATA_FILE, 'w') as f:
                json.dump(DATA_STORE, f)
            os.replace(TEMP_DATA_FILE, DATA_FILE)
            
            log(f"Harvest complete. Opps: {len(DATA_STORE['opportunities'])}, Orders: {len(DATA_STORE['orders'])}, Acts: {len(DATA_STORE['activities'])}")

        except Exception as e:
            log(f"!! Harvest error: {str(e)}")
            log(traceback.format_exc())

        time.sleep(REFRESH_INTERVAL)

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

        # Memory Arrays for specific date range
        created_opps = []
        closed_opps = []
        recent_orders = []
        recent_activities = []

        # Instantly filter records in memory
        for opp in DATA_STORE["opportunities"].values():
            dl = parse_cw_date(opp.get("dateBecameLead"))
            dc = parse_cw_date(opp.get("closedDate"))
            if dl and since <= dl <= until: created_opps.append(opp)
            if dc and since <= dc <= until: closed_opps.append(opp)
                
        for ord in DATA_STORE["orders"].values():
            od = parse_cw_date(ord.get("orderDate"))
            if od and since <= od <= until: recent_orders.append(ord)
                
        for act in DATA_STORE["activities"].values():
            ds = parse_cw_date(act.get("dateStart"))
            if ds and since <= ds <= until: recent_activities.append(act)

        # Chart Buckets
        daily_buckets = {}
        days_range = (until.date() - since.date()).days + 1
        
        # Prevent huge graphs if viewing a year+
        chart_bucket_format = "%Y-%m" if days_range > 100 else "%Y-%m-%d"
        chart_label_format = "%b %Y" if days_range > 100 else ("%d %b" if days_range > 7 else "%A")

        # Prep empty buckets
        temp_date = since
        while temp_date <= until:
            key = temp_date.strftime(chart_bucket_format)
            if key not in daily_buckets:
                daily_buckets[key] = {"date": temp_date.strftime(chart_label_format), "created": 0, "won": 0}
            # Step by days or months
            temp_date += timedelta(days=32 if days_range > 100 else 1)
            if days_range > 100: temp_date = temp_date.replace(day=1)

        for o in created_opps:
            k = parse_cw_date(o["dateBecameLead"]).strftime(chart_bucket_format)
            if k in daily_buckets: daily_buckets[k]["created"] += 1
            
        for o in closed_opps:
            k = parse_cw_date(o["closedDate"]).strftime(chart_bucket_format)
            if k in daily_buckets:
                if "won" in o.get("stage", {}).get("name", "").lower() or "won" in o.get("status", {}).get("name", "").lower():
                    daily_buckets[k]["won"] += 1

        # Aggregation
        rep_data = defaultdict(lambda: {"created": 0, "won": 0, "lost": 0, "revenue": 0.0, "cost": 0.0, "activities": 0, "orders": []})
        
        def get_rep_name(obj, field="primarySalesRep"):
            rep = obj.get(field)
            return rep.get("name", "Unassigned") if isinstance(rep, dict) else "Unassigned"

        for o in created_opps: rep_data[get_rep_name(o)]["created"] += 1
        for o in closed_opps:
            key = "won" if ("won" in o.get("stage",{}).get("name","").lower() or "won" in o.get("status",{}).get("name","").lower()) else "lost"
            rep_data[get_rep_name(o)][key] += 1
            
        for act in recent_activities: 
            rep_data[get_rep_name(act, "assignTo")]["activities"] += 1

        for ord in recent_orders:
            name = get_rep_name(ord, "salesRep")
            rev = float(ord.get("total", 0.0))
            total_cost = float(ord.get("_calculated_cost", 0.0))
            
            rep_data[name]["revenue"] += rev
            rep_data[name]["cost"] += total_cost
            rep_data[name]["orders"].append({"id": ord["id"], "title": f"{ord.get('company',{}).get('name','Unknown')} - {ord.get('opportunity',{}).get('name','Direct')}", "total": rev, "profit": rev - total_cost})

        final_users = []
        for name, d in rep_data.items():
            if name.lower() == "unassigned" or d["revenue"] <= 0: continue
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

if __name__ == "__main__": 
    # Start the harvester thread before running the web server
    threading.Thread(target=harvest_data, daemon=True).start()
    app.run(host="0.0.0.0", port=5000)
