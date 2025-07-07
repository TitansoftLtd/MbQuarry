import frappe
from frappe import _

@frappe.whitelist()
def get_all_items(search=""):
    """
    Fetch items based on search criteria across multiple fields,
    including retail and minimum selling prices and stock levels.
    Optimized for speed.
    """

    search_fields = [
        "item_code", "item_name", "brand", "custom_store_location",
    ]

    search_condition = "1=1"
    search_params = {}

    if isinstance(search, str) and search.strip():
        search_terms = search.split()
        search_conditions_list = []
        for i, term in enumerate(search_terms):
            # Ensure the table alias is used for item fields in the WHERE clause
            term_conditions = [f"`tabItem`.{field} LIKE %(search_{i})s" for field in search_fields]
            search_conditions_list.append(f"({' OR '.join(term_conditions)})")
            search_params[f"search_{i}"] = f"%{term}%"
        search_condition = " AND ".join(search_conditions_list)

    # 1. Fetch all relevant item data
    items_query = f"""
        SELECT item_code, item_name, stock_uom, brand, custom_store_location
        FROM `tabItem`
        WHERE {search_condition}
        ORDER BY item_code ASC
    """
    items = frappe.db.sql(items_query, search_params, as_dict=True)

    if not items:
        return {"items": []}

    item_codes = tuple(item["item_code"] for item in items)
    placeholders = ", ".join(["%s"] * len(item_codes))

    stock_query = f"""
        SELECT tb.item_code, tb.warehouse, tb.actual_qty, tw.warehouse_type
        FROM `tabBin` tb
        JOIN `tabWarehouse` tw ON tb.warehouse = tw.name
        WHERE tb.item_code IN ({placeholders})
    """
    stock_data = frappe.db.sql(stock_query, item_codes, as_dict=True)

    # 3. Fetch latest price data for these items
    price_query = f"""
        SELECT ip.item_code, ip.price_list, ip.price_list_rate, ip.valid_from
        FROM `tabItem Price` ip
        INNER JOIN (
            SELECT item_code, price_list, MAX(valid_from) AS latest_date
            FROM `tabItem Price`
            WHERE item_code IN ({placeholders})
            AND price_list IN ("Retail Selling", "Minimum Selling")
            AND selling = 1
            GROUP BY item_code, price_list
        ) latest ON
            latest.item_code = ip.item_code
            AND latest.price_list = ip.price_list
            AND latest.latest_date = ip.valid_from
        WHERE ip.selling = 1
    """
    price_data = frappe.db.sql(price_query, item_codes, as_dict=True)


    # Map prices for quick lookup
    price_map = {}
    for price in price_data:
        price_map.setdefault(price["item_code"], {})[price["price_list"]] = price["price_list_rate"]

    # Aggregate stock data by item, type, and specific warehouses
    item_stock_details_map = {}
    for stock_bin in stock_data:
        item_code = stock_bin["item_code"]
        warehouse_name = stock_bin["warehouse"]
        qty = stock_bin["actual_qty"]
        warehouse_type = stock_bin["warehouse_type"]

        # Initialize structure for item if not exists
        item_info = item_stock_details_map.setdefault(item_code, {
            "total_available_qty": 0,
            "main_warehouses": {}
        })

        item_info["total_available_qty"] += qty

        if warehouse_type == "Main":
            item_info["main_warehouses"][warehouse_name] = qty

    # Combine all data into the final list
    item_data = []
    for item in items:
        stock_info = item_stock_details_map.get(item["item_code"], {})
        prices = price_map.get(item["item_code"], {})

        main_warehouse_name = None
        main_warehouse_qty = 0
        for wh_name, qty in stock_info.get("main_warehouses", {}).items():
            if qty > 0:
                main_warehouse_name = wh_name
                main_warehouse_qty = qty
                break

        item_data.append({
            "item_code": item["item_code"],
            "item_name": item["item_name"],
            "brand": item["brand"],
            "available_qty": stock_info.get("total_available_qty", 0),
            "retail_price": prices.get("Retail Selling", 0),
            "minimum_price": prices.get("Minimum Selling", 0),
            "stock_uom": item["stock_uom"],
            "warehouse": main_warehouse_name,
            "main_warehouse": main_warehouse_qty
        })

    return {"items": item_data}