import os
import requests

API_URL = "https://api.invoapp.com/v1_0/investments/get_investments"

def parse_open_trades(response):
    if not response.get("success") or response.get("error"):
        return []

    return [
        {
            "id": t.get("id"),
            "ticker": t.get("ticker"),
            "asset_type": t.get("assetTypeId"),
            "direction": "long" if t.get("directionLong") else "short",
            "leverage": t.get("leverage"),
            "entry_price": t.get("entryPrice"),
            "current_price": t.get("currentPrice"),
            "position_size": t.get("positionSize"),
            "price_target": t.get("priceTarget"),
            "stop_loss": t.get("stopLoss"),
            "liquidation_price": t.get("liquidationPrice"),
            "is_open": t.get("isOpen"),
            "is_liquidated": t.get("isLiquidated"),
            "owner": (t.get("owner") or {}).get("username"),
            "portfolio": (t.get("portfolio") or {}).get("title"),
            "created_at": t.get("createdAt"),
            "updated_at": t.get("updatedAt"),
        }
        for t in response.get("investmentsTicker", [])
    ]

def fetch_open_trades(portfolio_id, jwt_token, page=1, size=10):
    payload = {
        "portfolioId": portfolio_id,
        "isOpen": True,
        "params": {"page": page, "size": size}
    }
    headers = {
        "Authorization": f"Bearer {jwt_token}",
        "Content-Type": "application/json"
    }
    resp = requests.post(API_URL, json=payload, headers=headers)
    resp.raise_for_status()
    return parse_open_trades(resp.json())

if __name__ == "__main__":
    portfolio_id = "ab15d342-c3c6-4b51-b890-5385eae301ae"
    jwt_token = os.environ["INVOAPP_JWT"]
    trades = fetch_open_trades(portfolio_id, jwt_token)
    for trade in trades:
        print(f"[{trade['ticker']}] \n\t{trade['direction'].upper():<6} \n\tEntry: {trade['entry_price']:<6,.2f} \n\tLev: {trade['leverage']:<3} \n\tSize: {trade['position_size']:<6,.2f} \n\tTarget: {trade['price_target']} \n\tStop: {trade['stop_loss']} \n\tCreated: {trade['created_at']} \n\tUpdated: {trade['updated_at']}")