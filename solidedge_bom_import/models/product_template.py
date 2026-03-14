from odoo import fields, models


class ProductTemplate(models.Model):
    _inherit = "product.template"

    x_solidedge_file = fields.Char(
        string="SolidEdge Filename",
        help="CAD source filename for traceability back to SolidEdge",
    )
    x_solidedge_asunto = fields.Char(
        string="SolidEdge Asunto",
        help="Subject/reference code from SolidEdge BOM export",
    )
