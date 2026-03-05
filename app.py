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

# --- IN-MEMORY CACHE FOR ORDER COSTS ---
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

    if params is None:
        params = {}

    session = get_session()

    while True:
        paged_params = {**params, "page": page, "pageSize": page_size}
        response = session.get(url, headers=headers, params=paged_params, timeout=90)
        response.raise_for_status()
        data = response.json()
        if not data:
            break
        all_results.extend(data)
        if len(data) < page_size:
            break
        page += 1

    return all_results

@app.route("/")
def index():
    return render_template("index.html", refresh_interval=REFRESH_INTERVAL, days_back=DEFAULT_DAYS_BACK)

@app.route("/api/sales-stats")
def sales_stats():
    days_param = request.args.get('days')
    try:
        days_back_val = int(days_param)
    except (TypeError, ValueError):
        days_back_val = DEFAULT_DAYS_BACK

    try:
        now = datetime.now(timezone.utc)
        start_of_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        since = start_of_today - timedelta(days=max(0, days_back_val - 1))
        since_str = since.strftime("%Y-%m-%dT%H:%M:%SZ")

        # 1. Fetch Opportunities
        created_params = {"conditions": f"dateBecameLead >= [{since_str}]", "fields": "id,primarySalesRep,dateBecameLead"}
        created_opps = cw_get("/sales/opportunities", created_params)

        closed_params = {"conditions": f"closedDate >= [{since_str}]", "fields": "id,stage,status,primarySalesRep,closedDate"}
        closed_opps = cw_get("/sales/opportunities", closed_params)

        # 2. Fetch Orders (Added company and opportunity fields for the dropdown)
        orders_params = {"conditions": f"orderDate >= [{since_str}]", "fields": "id,total,salesRep,_info,productIds,company,opportunity"}
        recent_orders = cw_get("/sales/orders", orders_params)

        # 3. Fetch Activities
        activities_params = {"conditions": f"dateStart >= [{since_str}]", "fields": "id,assignTo"}
        recent_activities = cw_get("/sales/activities", activities_params)

        # --- Build Daily Buckets ---
        daily_buckets = {}
        for i in range(days_back_val):
            day_dt = since + timedelta(days=i)
            day_key_str = day_dt.strftime("%Y-%m-%d")
            day_name = day_dt.strftime("%d %b") if days_back_val > 7 else day_dt.strftime("%A")
            daily_buckets[day_key_str] = {"date": day_name, "created": 0, "won": 0}

        def get_day_key(iso):
            try:
                if not iso: return None
                return datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime("%Y-%m-%d")
            except:
                return None

        for o in created_opps:
            k = get_day_key(o.get("dateBecameLead"))
            if k and k in daily_buckets: daily_buckets[k]["created"] += 1

        for o in closed_opps:
            k = get_day_key(o.get("closedDate"))
            if k and k in daily_buckets:
                stage_name = o.get("stage", {}).get("name", "").lower()
                status_name = o.get("status", {}).get("name", "").lower()
                if "won" in stage_name or "won" in status_name:
                    daily_buckets[k]["won"] += 1

        # --- Per-Sales Rep Aggregation ---
        rep_created = defaultdict(int)
        rep_won = defaultdict(int)
        rep_lost = defaultdict(int)
        rep_revenue = defaultdict(float)
        rep_cost = defaultdict(float)
        rep_activities = defaultdict(int)
        rep_orders = defaultdict(list) # NEW: Store individual orders

        def get_rep(obj, field="primarySalesRep"):
            rep = obj.get(field)
            if isinstance(rep, dict): return rep.get("name", "Unassigned")
            return "Unassigned"

        for o in created_opps: rep_created[get_rep(o)] += 1
        
        for o in closed_opps:
            stage_name = o.get("stage", {}).get("name", "").lower()
            status_name = o.get("status", {}).get("name", "").lower()
            if "won" in stage_name or "won" in status_name:
                rep_won[get_rep(o)] += 1
            else:
                rep_lost[get_rep(o)] += 1
                
        # --- PROCESS ORDERS & CACHED PRODUCT COSTS ---
        for ord in recent_orders:
            order_id = ord.get("id")
            last_updated = ord.get("_info", {}).get("lastUpdated", "")
            
            # Check cache
            if order_id in ORDER_COST_CACHE and ORDER_COST_CACHE[order_id]["lastUpdated"] == last_updated:
                total_cost = ORDER_COST_CACHE[order_id]["cost"]
            else:
                total_cost = 0.0
                product_ids = ord.get("productIds", [])
                
                if product_ids:
                    try:
                        for i in range(0, len(product_ids), 50):
                            chunk = product_ids[i:i+50]
                            cond = "id in (" + ",".join(map(str, chunk)) + ")"
                            products = cw_get("/procurement/products", {"conditions": cond, "fields": "id,cost,quantity"})
                            
                            for p in products:
                                c = float(p.get("cost") or 0.0)
                                q = float(p.get("quantity") or 1.0)
                                total_cost += (c * q)
                    except Exception as e:
                        print(f"Failed to fetch products for order {order_id}: {str(e)}")
                        
                ORDER_COST_CACHE[order_id] = {
                    "lastUpdated": last_updated,
                    "cost": total_cost
                }

            rep_name = get_rep(ord, "salesRep")
            order_total = float(ord.get("total", 0.0))
            
            rep_revenue[rep_name] += order_total
            rep_cost[rep_name] += total_cost
            
            # NEW: Save individual order details for the dropdown
            comp_name = ord.get("company", {}).get("name", "Unknown Company")
            opp_name = ord.get("opportunity", {}).get("name", "Direct Order")
            
            rep_orders[rep_name].append({
                "id": order_id,
                "title": f"{comp_name} - {opp_name}",
                "total": order_total,
                "profit": order_total - total_cost
            })
            
        for act in recent_activities:
            rep_activities[get_rep(act, "assignTo")] += 1

        all_reps = set(rep_created.keys()) | set(rep_won.keys()) | set(rep_lost.keys()) | set(rep_revenue.keys()) | set(rep_activities.keys())

        users_result = []
        for name in sorted(all_reps):
            if name.lower() == "unassigned": continue
            if rep_revenue[name] <= 0: continue 

            total_closed = rep_won[name] + rep_lost[name]
            win_rate = round((rep_won[name] / total_closed * 100)) if total_closed > 0 else 0
            profit = rep_revenue[name] - rep_cost[name]
            
            # Sort individual orders from highest revenue to lowest
            sorted_orders = sorted(rep_orders[name], key=lambda x: x["total"], reverse=True)

            users_result.append({
                "name": name,
                "created": rep_created[name],
                "won": rep_won[name],
                "lost": rep_lost[name],
                "win_rate": win_rate,
                "revenue": rep_revenue[name],
                "cost": rep_cost[name],
                "profit": profit,
                "activities": rep_activities[name],
                "orders": sorted_orders # NEW: Include the orders array
            })

        # Sort reps by most Revenue
        users_result.sort(key=lambda u: u["revenue"], reverse=True)

        total_won = sum(rep_won.values())
        total_lost = sum(rep_lost.values())
        total_closed = total_won + total_lost
        total_rev = sum(rep_revenue.values())
        total_cst = sum(rep_cost.values())

        return jsonify({
            "totals": {
                "created": sum(rep_created.values()),
                "won": total_won,
                "winRate": round((total_won / total_closed * 100)) if total_closed > 0 else 0,
                "revenue": total_rev,
                "cost": total_cst,
                "profit": total_rev - total_cst,
                "activities": sum(rep_activities.values())
            },
            "users": users_result,
            "daily": list(daily_buckets.values()),
            "asOf": now.isoformat(),
            "daysBack": days_back_val
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/config-check")
def config_check():
    configured = all([CW_COMPANY, CW_PUBLIC_KEY, CW_PRIVATE_KEY, CW_CLIENT_ID])
    return jsonify({"configured": configured})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
