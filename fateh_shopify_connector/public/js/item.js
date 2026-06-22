// Copyright (c) 2024, siva@enfono.com and contributors
// For license information, please see license.txt

frappe.ui.form.on("Item", {
	refresh(frm) {
		if (frm.is_new()) return;

		frappe.call({
			method: "frappe.client.get_count",
			args: {
				doctype: "Shopify Store",
				filters: { enabled: 1, enable_item_sync: 1 },
			},
			callback(r) {
				if (!r.message) return;

				frm.add_custom_button(
					__("Sync to Shopify"),
					function () {
						frappe.call({
							method: "fateh_shopify_connector.fateh_shopify_connector.product.manual_sync_item_to_shopify",
							args: { item_code: frm.doc.name },
							freeze: true,
							freeze_message: __("Queuing Shopify sync..."),
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
	},
});
