import os
import requests
import base64
import urllib3
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
DEFAULT_DAYS_BACK = int(os.environ.get("CW_DAYS_BACK", "30"))

ORDER_COST_CACHE = {}

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
    page_size = 100
    if params is None: params = {}
    session = get_session()
    while True:
        paged_params = {**params, "page": page, "pageSize": page_size}
        response = session.get(url, headers=headers, params=paged_params, timeout=90)
        response.raise_for_status()
        data = response.json()
        if not data: break
        all_results.extend(data)
        if len(data) < page_size: break
        page += 1
    return all_results

@app.route("/")
def index():
    return render_template("index.html", refresh_interval=REFRESH_INTERVAL, days_back=DEFAULT_DAYS_BACK)

@app.route("/api/sales-stats")
def sales_stats():
    timeframe_param = request.args.get('timeframe', str(DEFAULT_DAYS_BACK))
    now = datetime.now(timezone.utc)
    year = now.year

    try:
        if timeframe_param in ['Q1', 'Q2', 'Q3', 'Q4']:
            if timeframe_param == 'Q1':
                since = datetime(year, 1, 1, tzinfo=timezone.utc)
                until = datetime(year, 3, 31, 23, 59, 59, tzinfo=timezone.utc)
            elif timeframe_param == 'Q2':
                since = datetime(year, 4, 1, tzinfo=timezone.utc)
                until = datetime(year, 6, 30, 23, 59, 59, tzinfo=timezone.utc)
            elif timeframe_param == 'Q3':
                since = datetime(year, 7, 1, tzinfo=timezone.utc)
                until = datetime(year, 9, 30, 23, 59, 59, tzinfo=timezone.utc)
            elif timeframe_param == 'Q4':
                since = datetime(year, 10, 1, tzinfo=timezone.utc)
                until = datetime(year, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
            if now < since:
                year -= 1
                since, until = since.replace(year=year), until.replace(year=year)
            timeframe_label = f"{timeframe_param} {year}"
        else:
            try: days_back_val = int(timeframe_param)
            except: days_back_val = DEFAULT_DAYS_BACK
            since = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=max(0, days_back_val - 1))
            until = now
            timeframe_label = "Last 24 Hours" if days_back_val == 1 else f"Last {days_back_val} Days"

        since_str, until_str = since.strftime("%Y-%m-%dT%H:%M:%SZ"), until.strftime("%Y-%m-%dT%H:%M:%SZ")

        # Fetch Data
        created_opps = cw_get("/sales/opportunities", {"conditions": f"dateBecameLead >= [{since_str}] and dateBecameLead <= [{until_str}]", "fields": "id,primarySalesRep,dateBecameLead"})
        closed_opps = cw_get("/sales/opportunities", {"conditions": f"closedDate >= [{since_str}] and closedDate <= [{until_str}]", "fields": "id,stage,status,primarySalesRep,closedDate"})
        recent_orders = cw_get("/sales/orders", {"conditions": f"orderDate >= [{since_str}] and orderDate <= [{until_str}]", "fields": "id,total,salesRep,_info,productIds,company,opportunity"})
        recent_activities = cw_get("/sales/activities", {"conditions": f"dateStart >= [{since_str}] and dateStart <= [{until_str}]", "fields": "id,assignTo"})

        # Chart Buckets
        daily_buckets = {}
        days_range = (until.date() - since.date()).days + 1
        for i in range(days_range):
            day_dt = since + timedelta(days=i)
            daily_buckets[day_dt.strftime("%Y-%m-%d")] = {"date": day_dt.strftime("%d %b") if days_range > 7 else day_dt.strftime("%A"), "created": 0, "won": 0}

        for o in created_opps:
            k = datetime.fromisoformat(o["dateBecameLead"].replace("Z", "+00:00")).strftime("%Y-%m-%d")
            if k in daily_buckets: daily_buckets[k]["created"] += 1
        for o in closed_opps:
            k = datetime.fromisoformat(o["closedDate"].replace("Z", "+00:00")).strftime("%Y-%m-%d")
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
        for act in recent_activities: rep_data[get_rep_name(act, "assignTo")]["activities"] += 1

        for ord in recent_orders:
            order_id, last_updated = ord["id"], ord["_info"]["lastUpdated"]
            if order_id in ORDER_COST_CACHE and ORDER_COST_CACHE[order_id]["lastUpdated"] == last_updated:
                total_cost = ORDER_COST_CACHE[order_id]["cost"]
            else:
                total_cost = 0.0
                if ord.get("productIds"):
                    products = cw_get("/procurement/products", {"conditions": f"id in ({','.join(map(str, ord['productIds']))})", "fields": "cost,quantity"})
                    for p in products: total_cost += float(p.get("cost") or 0.0) * float(p.get("quantity") or 1.0)
                ORDER_COST_CACHE[order_id] = {"lastUpdated": last_updated, "cost": total_cost}

            name = get_rep_name(ord, "salesRep")
            rev = float(ord.get("total", 0.0))
            rep_data[name]["revenue"] += rev
            rep_data[name]["cost"] += total_cost
            rep_data[name]["orders"].append({"id": order_id, "title": f"{ord.get('company',{}).get('name','Unknown')} - {ord.get('opportunity',{}).get('name','Direct')}", "total": rev, "profit": rev - total_cost})

        final_users = []
        for name, d in rep_data.items():
            if name.lower() == "unassigned" or d["revenue"] <= 0: continue
            profit = d["revenue"] - d["cost"]
            # PROFIT MARGIN CALCULATION
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
    except Exception as e: return jsonify({"error": str(e)}), 500

if __name__ == "__main__": app.run(host="0.0.0.0", port=5000)
