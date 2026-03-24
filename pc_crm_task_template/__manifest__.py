# -*- coding: utf-8 -*-
{
    'name': 'CRM - Crear Tarea desde Plantilla de Presupuesto',
    'version': '19.0.1.0.0',
    'category': 'Sales/CRM',
    'summary': 'Crea tareas de proyecto desde el CRM usando plantillas de presupuesto',
    'description': """
        Añade un botón en el formulario de oportunidad/lead del CRM para crear
        una tarea de proyecto a partir de una plantilla de presupuesto (sale.order.template).
        Un wizard permite seleccionar la plantilla, el proyecto destino y los datos de la tarea.
    """,
    'author': 'Process Control',
    'depends': ['crm', 'project', 'sale_management'],
    'data': [
        'security/ir.model.access.csv',
        'wizard/crm_lead_create_task_views.xml',
        'views/crm_lead_views.xml',
    ],
    'installable': True,
    'application': False,
    'license': 'LGPL-3',
}
