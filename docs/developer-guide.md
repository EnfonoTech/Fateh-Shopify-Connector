# Developer Guide

Technical reference for developers working on or integrating with the Fateh Shopify Connector.

## DocTypes

| DocType | Description |
|---------|-------------|
| **Shopify Store** | Main configuration — auth, company, tax accounts, write-off account, sync settings |
| Shopify Store Warehouse Mapping | Maps Shopify locations to ERPNext warehouses |
| Shopify Store Item Field | Field mapping (standard fields and metafields) |
| Shopify Store Collection Mapping | ERPNext values to Shopify collections |
| Shopify Store Item Filter | Auto-eligibility rules |
| Shopify Store Tax Account | Tax mapping (Shopify tax → ERPNext account + item tax templates) |
| Shopify Store Payment Method Mapping | Maps Shopify payment gateways to ERPNext Mode of Payment |
| Item Shopify Store | Per-item, per-store mapping (child of Item) |

## Custom Fields Added

| DocType | Fields |
|---------|--------|
| **Item** | `shopify_stores` (table → Item Shopify Store) |
| **Customer** | `shopify_customer_id` |
| **Sales Order** | `shopify_store`, `shopify_order_id`, `shopify_order_number`, `shopify_financial_status`, `shopify_fulfillment_status` |
| **Sales Order Item** | `shopify_item_discount` |
| **Delivery Note** | `shopify_store`, `shopify_order_id`, `shopify_order_number` |
| **Sales Invoice** | `shopify_store`, `shopify_order_id`, `shopify_order_number` |

## Permissions

| Role | Access |
|------|--------|
| **Sales Manager** | Full access (create, edit, delete, import/export) |
| **Sales User** | Read-only + reports |
| **Accounts Manager** | Read-only + reports |
| **Accounts User** | Read-only + reports |
| **System Manager** | Full administrative access |

## Architecture

```
fateh_shopify_connector/
├── fateh_shopify_connector/
│   ├── connection.py      # shopify_session decorator, webhook endpoint, get_access_token
│   ├── oauth.py           # OAuth authorize & callback endpoints
│   ├── order.py           # Order sync logic (webhooks & manual sync)
│   ├── fulfillment.py     # Fulfillment webhook -> Delivery Note creation
│   ├── product.py         # Product/item sync ERPNext → Shopify
│   ├── inventory.py       # Inventory sync to Shopify
│   ├── inventory_graphql.py  # GraphQL inventory mutations (inventorySetQuantities)
│   ├── utils/
│   │   ├── __init__.py    # Logging, eligibility helpers, create_shopify_log
│   │   └── logger.py      # get_logger()
│   ├── tax/               # Tax calculation module
│   │   ├── __init__.py
│   │   ├── builder.py     # TaxBuilder: orchestrates tax row creation
│   │   ├── detector.py    # TaxDetector: identifies zero-rated items
│   │   ├── shipping.py    # ShippingTaxHandler: shipping charges & GST
│   │   └── rounding.py    # Rounding adjustment for total matching
│   └── doctype/
│       ├── shopify_store/
│       ├── shopify_store_warehouse_mapping/
│       ├── shopify_store_item_field/
│       ├── shopify_store_collection_mapping/
│       ├── shopify_store_item_filter/
│       ├── shopify_store_tax_account/
│       ├── shopify_store_payment_method_mapping/
│       └── item_shopify_store/
└── fixtures/
    └── custom_field.json  # Custom fields for Item, Customer, SO, DN, SI
```

## Installation

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app https://github.com/enfono/fateh_shopify_connector --branch version-15
bench --site [sitename] install-app fateh_shopify_connector
bench --site [sitename] migrate
bench build --app fateh_shopify_connector
bench restart
```

## Authentication

Two methods are supported:

### OAuth (recommended)

```
Shopify Store form → Connect to Shopify → Shopify auth page → Callback → token stored in access_token field
```

**Setup:**
1. Shopify Admin → Settings → Develop Apps → Dev Dashboard → Create app
2. Add redirect URI: `https://{your-site}/api/method/fateh_shopify_connector.fateh_shopify_connector.oauth.callback`
3. Note Client ID and Client Secret
4. Shopify Store form → set Auth Method = **OAuth**, enter Client ID + Client Secret, Save
5. Actions → **Connect to Shopify** → authorize on Shopify
6. Verify banner shows "Connected to Shopify via OAuth as {user}"

Scopes requested automatically: `read_orders`, `write_orders`, `read_customers`, `write_customers`, `read_products`, `write_products`, `read_inventory`, `write_inventory`, `read_locations`, `read_fulfillments`, `write_fulfillments`

### Legacy (Access Token)

For private apps or direct API tokens from Shopify Admin → Apps → Develop apps → API credentials.

1. Shopify Store form → Auth Method = **Legacy (Access Token)**
2. Paste the token in the **Access Token** field
3. Save → Test Connection

> **Note:** `access_token` is stored as a plain Data field (not an encrypted Password field). Avoid exposing DB dumps.

## Item Sync: ERPNext → Shopify

### How Eligibility Works

An item is eligible for a store if:
1. It has an `Item Shopify Store` row with `shopify_store = <store>` and `enabled = 1`, **OR**
2. It matches all rows in the store's **Item Eligibility Filters**

### Sync Triggers

| Trigger | Condition |
|---------|-----------|
| Auto on item save | Store: `Enable Item Sync` + `Update Shopify on Item Update` both checked |
| Auto on price change | Same conditions, only if price list matches |
| Manual per item | Item form → Shopify → **Sync to Shopify** |
| Manual bulk | Shopify Store → Sync → **Sync All Items** |

### Sync All Items — Real-Time Progress

**Sync All Items** enqueues a single orchestrator job (`_sync_all_items_orchestrator`) that:
- Processes items sequentially
- Publishes `shopify_item_sync_progress` realtime events after each item
- The browser dialog shows a live progress bar + item log

Progress event payload:
```json
{
  "store": "mystore.myshopify.com",
  "total": 859,
  "done": 42,
  "errors": 0,
  "current_item": "ITEM-0042",
  "status": "running"   // "running" | "done"
}
```

### What Gets Synced

| ERPNext field | Shopify field |
|---------------|---------------|
| `item_name` | Product title |
| `description` | Body HTML |
| `item_code` | Variant SKU |
| Selling price (store's Price List) | Variant price |
| Item image | Product image (if Enable Image Sync) |
| Stock qty | Inventory level (if Enable Inventory Sync + warehouse mapping) |
| Item Field Map rows | product_type, vendor, tags, barcode, metafields, collections, etc. |

### Mapping Item Group to Shopify

Add a row to **Item Field Mapping** on the Shopify Store:

| ERPNext Field | Shopify Field Type | Shopify Standard Field |
|---|---|---|
| `item_group` | Standard Field | `product_type` |

Or map to `collections` to create Shopify collections (enable **Auto-create Missing Collections**).

### Linking Items to Stores

**Option A — Item Filters (bulk):**
Shopify Store → Item Eligibility Filters → add rule (e.g. `item_group` = `All Item Groups`) → Sync All Items

**Option B — SKU mapping (existing Shopify products):**
Shopify Store → Actions → **Fetch Products & Map by SKU**
Matches Shopify variant SKU to ERPNext `item_code`. Requires SKUs to be set on Shopify variants.

**Option C — Manual per item:**
Item → Shopify Stores table → add row with store + Enabled

## Inventory Sync

- Uses Shopify GraphQL `inventorySetQuantities` mutation (batched, 250 items/call)
- Reads from ERPNext `Bin` (warehouse stock)
- Requires Warehouse Mapping: Shopify Store → Warehouse Mapping → map each Shopify location to an ERPNext warehouse
- Trigger: Shopify Store → Sync → **Sync Inventory**, or automatic via scheduler if `Enable Inventory Sync` is on

## Order Sync: Shopify → ERPNext

Requires:
- `Sync Orders` enabled on the store
- Webhooks registered (Actions → Register Webhooks)
- OAuth Connected (webhooks deliver to your site)

Flow: Shopify webhook → `connection.store_request_data` → enqueued → `order.sync_new_orders`

Manual pull: Shopify Store → Sync → **Sync Orders**

## Webhook Events

| Setting | Topics registered |
|---------|-------------------|
| Process Order Create Webhooks | `orders/create` |
| Process Order Paid Webhooks | `orders/paid` |
| Process Order Cancelled Webhooks | `orders/cancelled` |
| Process Fulfillment Webhooks | `orders/fulfilled`, `orders/partially_fulfilled` |

Register: Actions → **Register Webhooks**
Verify: Actions → **Fetch Webhooks** (shows dialog with registered topics)

## Fateh Shopify Log

All sync operations write to **Fateh Shopify Log** (visible under Fateh Shopify Connector menu).

Status values: `Queued` | `Success` | `Warning` | `Error`

Filter by store + status to diagnose sync failures.

## Contributing

```bash
cd apps/fateh_shopify_connector
pre-commit install
```

Tools: ruff, eslint, prettier, pyupgrade
