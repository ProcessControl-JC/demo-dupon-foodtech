from odoo import fields, models


class ProductTemplate(models.Model):
    _inherit = "product.template"

    x_solidedge_file = fields.Char(
        string="Archivo SolidEdge",
        help="Nombre del archivo CAD de origen para trazabilidad con SolidEdge",
    )
    x_solidedge_asunto = fields.Char(
        string="Asunto SolidEdge",
        help="Código de referencia/asunto del archivo exportado desde SolidEdge",
    )
