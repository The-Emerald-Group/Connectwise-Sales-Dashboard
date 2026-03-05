# ConnectWise Sales Dashboard

A self-hosted, auto-refreshing web dashboard that visualizes your ConnectWise Manage sales pipeline, revenue, and rep performance. 

Designed to be displayed on a wall monitor or used as a quick-reference tool, this dashboard pulls real-time data from ConnectWise Opportunities, Orders, and Activities.

---

## 🚀 Features

- **Dynamic Timeframes:** Instantly switch between Last 24 Hours, 7 Days, 30 Days, or 90 Days without reloading the page.
- **Top-Level KPIs:** Track Total Revenue, Estimated Profit, Deals Won, and overall Sales Activities (calls, emails, meetings).
- **Pipeline Trend Chart:** A sleek line graph comparing the volume of New Leads vs. Deals Won over your selected timeframe.
- **Sales Rep Leaderboard:** Individual cards for revenue-generating reps showing their specific Win Rate, Hustle (activities), Revenue, Cost, and Profit.
- **Smart Caching:** To avoid hitting ConnectWise API limits, order line-item costs are intelligently cached in memory after the first fetch.

---

## 📦 Quick Start (Docker Compose)

The easiest way to deploy this dashboard is via Docker. 

1. Clone this repository and navigate into the folder.
2. Edit the `docker-compose.yml` file to include your ConnectWise API credentials.
3. Run the following command:

    ```bash
    docker compose up -d
    ```

4. Open `http://localhost:5002` in your browser.

---

## 🔑 Getting Your API Keys

To connect the dashboard to your instance, you need a set of ConnectWise API keys.

1. **Create API Keys:** Go to **System → Members → (select a member) → API Keys**. Click **New Item** and save the Public and Private keys.
2. **Get a Client ID:** Register at [https://developer.connectwise.com/ClientID](https://developer.connectwise.com/ClientID) to generate a Client ID.
3. **Find Your Site & Company:** - **Site**: Your API hostname (e.g., `api-eu.myconnectwise.net`).
   - **Company**: Your short company login identifier.

---

## ⚙️ Environment Variables

Configure these inside your `docker-compose.yml` file:

| Variable | Description | Default |
|---|---|---|
| `CW_SITE` | ConnectWise API hostname | `api-eu.myconnectwise.net` |
| `CW_COMPANY` | Company login ID | *(required)* |
| `CW_PUBLIC_KEY` | API public key | *(required)* |
| `CW_PRIVATE_KEY` | API private key | *(required)* |
| `CW_CLIENT_ID` | Developer client ID | *(required)* |
| `CW_VERIFY_SSL` | Verify SSL certificates (`true`/`false`) | `true` |
| `HTTPS_PROXY` | Proxy URL if required by your network | *(none)* |
| `CW_REFRESH_INTERVAL` | Dashboard auto-refresh in seconds | `300` |
| `CW_DAYS_BACK` | Default timeframe loaded on launch | `30` |

---

## 💡 How "Cost" and "Profit" are Calculated

ConnectWise **Orders** do not expose the overall "Cost" of an order at the top level via the API. To accurately calculate your Estimated Profit, this dashboard does the following:
1. Fetches your recently updated Sales Orders.
2. Makes a sub-query to read the **Line Items** attached to that specific order.
3. Multiplies the `unitCost` by the `quantity` of each line item to find the exact cost.
4. **Caches the result.** The dashboard stores this calculation in memory so it doesn't have to fetch line items again unless the order is actively updated in ConnectWise.

*Note: Because of this line-item check, the very first time you load the dashboard it may take 2-5 seconds. Subsequent refreshes will be nearly instant.*

---

## 📡 API Endpoints

If you wish to interact with the Python backend directly:

| Endpoint | Description |
|---|---|
| `GET /` | Serves the main HTML dashboard |
| `GET /api/sales-stats?days=30` | Returns aggregated revenue, leads, and user stats (JSON) |
| `GET /api/config-check` | Verifies environment variables are set |

---

## 🛑 Stopping & Removing

To stop the dashboard:
```bash
docker compose down
docker compose down -v
