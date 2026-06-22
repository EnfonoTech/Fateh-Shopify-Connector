// Copyright (c) 2024, siva@enfono.com and contributors
// For license information, please see license.txt

frappe.ui.form.on("Item", {
	refresh(frm) {
		if (frm.is_new()) return;

		// Check for stores with item sync enabled
		frappe.call({
			method: "frappe.client.get_count",
			args: { doctype: "Shopify Store", filters: { enabled: 1, enable_item_sync: 1 } },
			callback(r) {
				if (!r.message) return;

				frm.add_custom_button(
					__("Sync to Shopify"),
					function () {
						frappe.call({
							method: "fateh_shopify_connector.fateh_shopify_connector.product.manual_sync_item_to_shopify",
							args: { item_code: frm.doc.name },
							freeze: true,
							freeze_message: __("Syncing to Shopify..."),
							callback(r) {
								if (!r.exc) {
									frappe.show_alert({
										message: __("Sync queued for {0}", [frm.doc.name]),
										indicator: "green",
									});
								}
							},
						});
					},
					__("Shopify")
				);
			},
		});

		// Check if this item already has a Shopify variant mapping (needed for inventory sync)
		frappe.call({
			method: "frappe.client.get_count",
			args: {
				doctype: "Item Shopify Store",
				filters: {
					parent: frm.doc.name,
					enabled: 1,
					shopify_variant_id: ["!=", ""],
				},
			},
			callback(r) {
				if (!r.message) return;

				frm.add_custom_button(
					__("Sync Inventory to Shopify"),
					function () {
						frappe.call({
							method: "fateh_shopify_connector.fateh_shopify_connector.inventory.manual_sync_item_inventory",
							args: { item_code: frm.doc.name },
							freeze: true,
							freeze_message: __("Syncing inventory..."),
							callback(r) {
								if (r.exc) return;

								let results = r.message || [];
								if (!results.length) {
									frappe.msgprint(__("No stores to sync inventory to."));
									return;
								}

								// Build result table
								let rows = results.map(res => {
									let color = res.status === "Success" ? "green"
										: res.status === "Error" ? "red" : "grey";
									return `<tr>
										<td>${frappe.utils.escape_html(res.store)}</td>
										<td><span class="indicator ${color}">${frappe.utils.escape_html(res.status)}</span></td>
										<td>${frappe.utils.escape_html(res.message || "")}</td>
									</tr>`;
								}).join("");

								let anyError = results.some(r => r.status === "Error");
								let allOk = results.every(r => r.status === "Success");

								frappe.msgprint({
									title: __("Inventory Sync Result"),
									message: `
										<table class="table table-bordered" style="margin-top:8px;">
											<thead><tr>
												<th>${__("Store")}</th>
												<th>${__("Status")}</th>
												<th>${__("Detail")}</th>
											</tr></thead>
											<tbody>${rows}</tbody>
										</table>
									`,
									indicator: anyError ? "red" : (allOk ? "green" : "orange"),
								});
							},
						});
					},
					__("Shopify")
				);
			},
		});
	},
});
