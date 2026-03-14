{
    'name': 'SolidEdge BOM Import',
    'version': '19.0.1.1.0',
    'summary': 'Import multi-level Bills of Materials exported from Siemens SolidEdge',
    'description': """
        Wizard to import BOM CSV files exported from Siemens SolidEdge into
        Odoo Manufacturing (mrp). Supports arbitrary nesting levels, auto-creates
        products and BOMs, and respects the assembly hierarchy defined in SolidEdge.
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
