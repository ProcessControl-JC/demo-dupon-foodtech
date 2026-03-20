{
    "name": "MRP Planned Orders - Group by Subassembly",
    "version": "19.0.1.0.0",
    "license": "LGPL-3",
    "author": "Process Control",
    "summary": "Adds parent subassembly field to MRP planned orders for grouping",
    "depends": ["mrp_multi_level"],
    "data": [
        "views/mrp_planned_order_views.xml",
        "views/mrp_inventory_views.xml",
    ],
    "installable": True,
}
