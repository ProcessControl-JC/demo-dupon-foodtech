{
    'name': 'Importación de LdM SolidEdge',
    'version': '19.0.1.3.0',
    'summary': 'Importación de Listas de Materiales multinivel exportadas desde Siemens SolidEdge',
    'description': """
        Asistente para importar archivos CSV de lista de materiales exportados desde
        Siemens SolidEdge a la fabricación de Odoo (mrp). Soporta niveles de anidamiento
        ilimitados, crea productos y LdM automáticamente, y respeta la jerarquía de
        conjuntos definida en SolidEdge.
    """,
    'author': 'Process Control',
    'category': 'Manufacturing',
    'depends': ['mrp', 'uom'],
    'data': [
        'security/ir.model.access.csv',
        'data/product_category_data.xml',
        'views/solidedge_bom_import_wizard_views.xml',
        'views/menu.xml',
    ],
    'installable': True,
    'application': False,
    'license': 'LGPL-3',
}
