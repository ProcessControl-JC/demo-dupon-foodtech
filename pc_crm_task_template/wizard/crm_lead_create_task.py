# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import UserError


class CrmLeadCreateTask(models.TransientModel):
    _name = 'crm.lead.create.task'
    _description = 'Crear tarea de proyecto desde lead/oportunidad con plantilla de presupuesto'

    @api.model
    def default_get(self, field_names):
        result = super().default_get(field_names)
        lead_id = result.get('lead_id') or self.env.context.get('active_id')
        if lead_id:
            lead = self.env['crm.lead'].browse(lead_id)
            result['lead_id'] = lead.id
            result['task_name'] = lead.name
            result['partner_id'] = lead.partner_id.id or False
            result['user_ids'] = [(6, 0, lead.user_id.ids)] if lead.user_id else []
            result['date_deadline'] = lead.date_deadline or False
        return result

    # --- Origen ---
    lead_id = fields.Many2one(
        'crm.lead',
        string='Lead / Oportunidad',
        required=True,
        readonly=True,
    )

    # --- Plantilla de presupuesto ---
    template_id = fields.Many2one(
        'sale.order.template',
        string='Plantilla de presupuesto',
        required=True,
        help='Selecciona la plantilla de presupuesto en la que se basa la tarea.',
    )
    template_line_ids = fields.Many2many(
        'sale.order.template.line',
        string='Líneas de la plantilla',
        compute='_compute_template_line_ids',
        help='Líneas de productos/servicios de la plantilla seleccionada.',
    )

    # --- Datos de la tarea ---
    task_name = fields.Char(
        string='Nombre de la tarea',
        required=True,
    )
    project_id = fields.Many2one(
        'project.project',
        string='Proyecto',
        required=True,
    )
    partner_id = fields.Many2one(
        'res.partner',
        string='Cliente',
    )
    user_ids = fields.Many2many(
        'res.users',
        string='Responsables',
        domain=[('share', '=', False)],
    )
    date_deadline = fields.Date(
        string='Fecha límite',
    )
    tag_ids = fields.Many2many(
        'project.tags',
        string='Etiquetas',
    )
    description = fields.Html(
        string='Descripción adicional',
        help='Descripción adicional. El contenido de la plantilla se añadirá automáticamente.',
    )

    # --- Opciones ---
    include_template_lines = fields.Boolean(
        string='Incluir líneas de la plantilla en la descripción',
        default=True,
    )

    @api.depends('template_id')
    def _compute_template_line_ids(self):
        for rec in self:
            rec.template_line_ids = rec.template_id.sale_order_template_line_ids if rec.template_id else False

    @api.onchange('template_id')
    def _onchange_template_id(self):
        if self.template_id and not self.task_name:
            self.task_name = self.template_id.name

    def _build_description(self):
        self.ensure_one()
        lines_html = ''
        if self.include_template_lines and self.template_id.sale_order_template_line_ids:
            rows = ''
            for line in self.template_id.sale_order_template_line_ids:
                product_name = line.product_id.display_name if line.product_id else line.name or ''
                qty = line.product_uom_qty
                uom = line.product_uom_id.name if line.product_uom_id else ''
                rows += (
                    f'<tr>'
                    f'<td>{product_name}</td>'
                    f'<td style="text-align:right;">{qty} {uom}</td>'
                    f'</tr>'
                )
            lines_html = (
                f'<p><strong>{_("Contenido de la plantilla")} — {self.template_id.name}</strong></p>'
                f'<table style="width:100%;border-collapse:collapse;">'
                f'<thead><tr>'
                f'<th style="text-align:left;">{_("Producto / Servicio")}</th>'
                f'<th style="text-align:right;">{_("Cantidad")}</th>'
                f'</tr></thead>'
                f'<tbody>{rows}</tbody>'
                f'</table>'
            )

        extra = self.description or ''
        return lines_html + extra if lines_html else extra

    def action_create_task(self):
        self.ensure_one()
        lead = self.lead_id

        if not self.project_id:
            raise UserError(_('Debes seleccionar un proyecto destino.'))

        task_vals = {
            'name': self.task_name,
            'project_id': self.project_id.id,
            'partner_id': self.partner_id.id or False,
            'user_ids': [(6, 0, self.user_ids.ids)],
            'date_deadline': self.date_deadline or False,
            'tag_ids': [(6, 0, self.tag_ids.ids)],
            'description': self._build_description(),
        }

        task = self.env['project.task'].with_context(
            mail_create_nosubscribe=True,
            mail_create_nolog=True,
        ).create(task_vals)

        # Nota en el lead indicando la tarea creada
        lead._message_log(
            body=_(
                'Tarea creada a partir de la plantilla <b>%(template)s</b>: %(link)s',
                template=self.template_id.name,
                link=task._get_html_link(),
            )
        )

        return {
            'name': _('Tarea creada'),
            'type': 'ir.actions.act_window',
            'res_model': 'project.task',
            'view_mode': 'form',
            'res_id': task.id,
            'context': self.env.context,
        }
