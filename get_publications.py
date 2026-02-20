import os
import json
import requests
from dotenv import load_dotenv

load_dotenv()

SHOP = os.getenv("SHOP")  # e.g. stellaaandsage.myshopify.com
TOKEN = os.getenv("CLIENT_SECRET")  # Admin API access token
API_VERSION = os.getenv("API_VERSION", "2024-07")

if not SHOP or not TOKEN:
    raise SystemExit("Missing SHOP or CLIENT_SECRET in .env")

url = f"https://{SHOP}/admin/api/{API_VERSION}/graphql.json"
headers = {
    "Content-Type": "application/json",
    "X-Shopify-Access-Token": TOKEN,
}

query = """
query {
  publications(first: 50) {
    edges {
      node {
        id
        name
      }
    }
  }
}
"""

r = requests.post(url, headers=headers, json={"query": query, "variables": {}}, timeout=60)
r.raise_for_status()
data = r.json()

if data.get("errors"):
    print("GraphQL errors:")
    print(json.dumps(data["errors"], indent=2))
    raise SystemExit(1)

pubs = [e["node"] for e in data["data"]["publications"]["edges"]]
print("\n=== Publications ===")
for p in pubs:
    print(f"- {p['name']}: {p['id']}")

online = next((p for p in pubs if p["name"].strip().lower() == "online store"), None)
if online:
    print("\n✅ ONLINE STORE PUBLICATION ID:")
    print(online["id"])
else:
    print("\n⚠️ Could not find 'Online Store' by name. Pick the right one from the list above.")
