from odoo import api, fields, models


class MrpInventory(models.Model):
    _inherit = "mrp.inventory"

    parent_assembly_id = fields.Many2one(
        comodel_name="product.product",
        string="Subassembly",
        compute="_compute_parent_assembly_id",
        store=True,
    )

    @api.depends("product_id", "mrp_area_id")
    def _compute_parent_assembly_id(self):
        for record in self:
            demand_move = self.env["mrp.move"].search(
                [
                    ("product_id", "=", record.product_id.id),
                    ("mrp_type", "=", "d"),
                    ("planned_order_up_ids", "!=", False),
                    ("mrp_area_id", "=", record.mrp_area_id.id),
                ],
                limit=1,
            )
            if demand_move and demand_move.planned_order_up_ids:
                parent_po = demand_move.planned_order_up_ids[:1]
                record.parent_assembly_id = parent_po.product_id
            else:
                record.parent_assembly_id = False
