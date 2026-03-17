import base64
import csv
import io
import os
import re

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class SolidEdgeBomImportWizard(models.TransientModel):
    """
    Asistente de importación de LdM multinivel para archivos CSV exportados desde SolidEdge.

    Flujo:
      Paso 1 (state=upload)   → el usuario sube el CSV y configura opciones
      Paso 2 (state=preview)  → el sistema parsea y muestra la jerarquía + avisos
      Paso 3 (state=done)     → se ejecuta la importación; se muestra el resumen
    """

    _name = "solidedge.bom.import.wizard"
    _description = "Asistente de Importación de LdM SolidEdge"

    # -------------------------------------------------------------------------
    # Campos
    # -------------------------------------------------------------------------

    state = fields.Selection(
        [("upload", "Subida"), ("preview", "Vista previa"), ("done", "Completado")],
        default="upload",
        required=True,
    )

    file_data = fields.Binary("Archivo CSV de SolidEdge", required=True)
    filename = fields.Char("Nombre de archivo")

    overwrite = fields.Boolean(
        "Sobrescribir nombres de productos existentes",
        default=False,
        help="Si está activado, los nombres de los productos en Odoo se actualizarán "
             "para coincidir con la última descripción de SolidEdge. "
             "Desactivado por defecto (modo seguro).",
    )
    dry_run = fields.Boolean(
        "Solo validar (sin guardar)",
        default=True,
        help="Analiza y previsualiza el archivo sin escribir nada en la base de datos. "
             "Desmarca esta opción para realizar la importación real.",
    )

    # Campos HTML de vista previa / resultado
    preview_html = fields.Html("Vista previa", readonly=True)
    result_html = fields.Html("Resultado", readonly=True)

    # -------------------------------------------------------------------------
    # Acciones
    # -------------------------------------------------------------------------

    def action_parse_preview(self):
        """Parsea el archivo subido y avanza al paso de vista previa."""
        self.ensure_one()
        rows = self._parse_file()
        self._validate_hierarchy(rows)  # lanza UserError si hay error crítico
        preview_html = self._build_preview_html(rows)
        self.write({"state": "preview", "preview_html": preview_html})
        return self._reload_action()

    def action_import(self):
        """Ejecuta la importación (o validación en seco) y avanza al paso final."""
        self.ensure_one()
        rows = self._parse_file()
        self._validate_hierarchy(rows)

        if self.dry_run:
            result_html = self._build_dryrun_html(rows)
        else:
            with self.env.cr.savepoint():
                result = self._process_rows(rows)
            result_html = self._build_result_html(result)

        self.write({"state": "done", "result_html": result_html})
        return self._reload_action()

    def action_back(self):
        """Vuelve al paso de subida."""
        self.ensure_one()
        self.write({"state": "upload", "preview_html": False, "result_html": False})
        return self._reload_action()

    # -------------------------------------------------------------------------
    # Parseo
    # -------------------------------------------------------------------------

    def _parse_file(self):
        """
        Decodifica el binario subido, parsea el CSV de SolidEdge y devuelve
        una lista de dicts de filas.
        """
        if not self.file_data:
            raise UserError(_("Por favor, sube un archivo CSV."))

        raw = base64.b64decode(self.file_data)
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            try:
                text = raw.decode("latin-1")
            except UnicodeDecodeError as e:
                raise UserError(
                    _("No se pudo decodificar el archivo. Se esperaba codificación UTF-8 o Latin-1. "
                      "Error: %s") % str(e)
                )

        reader = csv.reader(io.StringIO(text), delimiter=";")
        all_rows = list(reader)

        if len(all_rows) < 3:
            raise UserError(
                _("El archivo no contiene suficientes filas. "
                  "Se esperaba: fila 1 = metadatos, fila 2 = cabeceras, fila 3+ = datos.")
            )

        header_row = [h.strip() for h in all_rows[1]]
        col_map = self._map_columns(header_row)

        rows = []
        for raw_row in all_rows[2:]:
            if raw_row and raw_row[-1] == "":
                raw_row = raw_row[:-1]
            if not any(raw_row):
                continue

            def get(col_name):
                idx = col_map.get(col_name)
                if idx is None or idx >= len(raw_row):
                    return ""
                return raw_row[idx].strip()

            level_raw = get("level")
            if not level_raw:
                continue

            try:
                level = int(level_raw.strip())
            except ValueError:
                continue

            rows.append(
                {
                    "level": level,
                    "doc_number": get("doc_number"),
                    "asunto": get("asunto"),
                    "description": get("description"),
                    "filename": get("filename"),
                    "quantity": get("quantity"),
                    "has_children": False,
                }
            )

        if not rows:
            raise UserError(_("No se encontraron filas de datos en el archivo."))

        return rows

    def _map_columns(self, header_row):
        """
        Mapea nombres lógicos de columnas a sus posiciones en la fila de cabecera.
        Detección por nombre (sin distinguir mayúsculas, coincidencia parcial), no por posición.
        """
        mapping = {}
        keywords = {
            "level":       ["nivel"],
            "doc_number":  ["número de documento", "numero de documento", "document number"],
            "asunto":      ["asunto", "subject"],
            "description": ["descripción", "descripcion", "description"],
            "filename":    ["nombre de archivo", "filename", "file name"],
            "quantity":    ["cantidad", "quantity", "qty"],
        }
        for i, header in enumerate(header_row):
            h_lower = header.lower()
            for key, candidates in keywords.items():
                if key not in mapping:
                    for candidate in candidates:
                        if candidate in h_lower:
                            mapping[key] = i
                            break

        required = ["level", "doc_number", "description", "filename", "quantity"]
        missing = [k for k in required if k not in mapping]
        if missing:
            raise UserError(
                _("No se encontraron las columnas requeridas en la cabecera del CSV: %s\n"
                  "Cabeceras encontradas: %s") % (", ".join(missing), ", ".join(header_row))
            )
        return mapping

    # -------------------------------------------------------------------------
    # Validación de jerarquía
    # -------------------------------------------------------------------------

    def _validate_hierarchy(self, rows):
        """
        Valida la secuencia de niveles y rellena el flag has_children.
        Lanza UserError en errores estructurales críticos (sin guardar datos).
        """
        if not rows:
            raise UserError(_("No hay filas de datos para procesar."))

        if rows[0]["level"] != 1:
            raise UserError(
                _("El archivo debe comenzar con una fila de Nivel 1 (conjunto principal). "
                  "Primera fila encontrada en el nivel %d.") % rows[0]["level"]
            )

        prev_level = 0
        for i, row in enumerate(rows):
            level = row["level"]
            if level > prev_level + 1:
                raise UserError(
                    _("Error de jerarquía en la fila %d: salto del nivel %d al nivel %d. "
                      "Comprueba el archivo exportado desde SolidEdge.") % (i + 3, prev_level, level)
                )
            if i > 0 and level > rows[i - 1]["level"]:
                rows[i - 1]["has_children"] = True
            prev_level = level

    # -------------------------------------------------------------------------
    # Motor principal de importación
    # -------------------------------------------------------------------------

    def _process_rows(self, rows):
        """
        Motor principal de importación. Itera las filas usando una pila de padres
        para construir la jerarquía de LdM. Devuelve un dict de resultado con contadores y avisos.
        """
        result = {
            "products_created": 0,
            "products_updated": 0,
            "boms_created": 0,
            "bom_lines_added": 0,
            "bom_lines_updated": 0,
            "warnings": [],
            "bom_ids": [],
        }

        seen_doc_numbers = {}
        stack = {}

        for i, row in enumerate(rows):
            level = row["level"]
            doc_number = row["doc_number"]
            filename = row["filename"]
            description = row["description"]
            asunto = row["asunto"]
            qty_raw = row["quantity"]

            key = self._resolve_unique_key(doc_number, filename)
            if not key:
                result["warnings"].append(
                    _("Fila %d: número de documento y nombre de archivo vacíos — fila omitida.") % (i + 3)
                )
                continue

            if not description or description == "-":
                description = os.path.splitext(filename)[0] if filename else key

            if doc_number:
                if doc_number in seen_doc_numbers:
                    prev_desc = seen_doc_numbers[doc_number]
                    if prev_desc != description:
                        result["warnings"].append(
                            _("Fila %d: número de documento '%s' ya visto con descripción diferente "
                              "('%s' vs '%s'). Se usa la primera ocurrencia.")
                            % (i + 3, doc_number, prev_desc, description)
                        )
                        product = self.env["product.product"].search(
                            [("default_code", "=", key)], limit=1
                        )
                        if not product:
                            product, created = self._get_or_create_product(
                                key, description, asunto, filename, result
                            )
                        if level > 1 and (level - 1) in stack:
                            _parent_prod, parent_bom = stack[level - 1]
                            if parent_bom:
                                qty = self._parse_qty(qty_raw, i, result)
                                self._add_bom_line(parent_bom, product, qty, result)
                        stack[level] = (product, None)
                        continue
                seen_doc_numbers[doc_number] = description

            qty = self._parse_qty(qty_raw, i, result)

            product, created = self._get_or_create_product(
                key, description, asunto, filename, result
            )
            if created:
                result["products_created"] += 1
            else:
                if self.overwrite:
                    product.write({"name": description})
                    result["products_updated"] += 1

            if level == 1:
                bom = self._get_or_create_bom(product, result)
                stack = {1: (product, bom)}
            else:
                if (level - 1) not in stack:
                    result["warnings"].append(
                        _("Fila %d: no se encontró padre para el nivel %d (clave='%s') — fila omitida.")
                        % (i + 3, level, key)
                    )
                    continue

                _parent_prod, parent_bom = stack[level - 1]
                if parent_bom:
                    self._add_bom_line(parent_bom, product, qty, result)

                if row["has_children"]:
                    sub_bom = self._get_or_create_bom(product, result)
                    stack[level] = (product, sub_bom)
                else:
                    self._ensure_buy_route(product)
                    stack[level] = (product, None)
                    for lvl in list(stack.keys()):
                        if lvl > level:
                            del stack[lvl]

        return result

    # -------------------------------------------------------------------------
    # Helpers de producto
    # -------------------------------------------------------------------------

    def _get_or_create_product(self, key, description, asunto, filename, result):
        """
        Busca un producto existente por default_code o crea uno nuevo.
        Devuelve (registro_producto, fue_creado).
        """
        Product = self.env["product.product"]
        product = Product.search([("default_code", "=", key)], limit=1)
        if product:
            return product, False

        category = self.env.ref(
            "solidedge_bom_import.product_categ_solidedge", raise_if_not_found=False
        )
        categ_id = category.id if category else self.env.ref("product.product_category_all").id

        uom = self.env.ref("uom.product_uom_unit")

        vals = {
            "name": description,
            "default_code": key,
            "type": "consu",
            "categ_id": categ_id,
            "uom_id": uom.id,
        }

        product = Product.create(vals)
        if "uom_po_id" in self.env["product.template"]._fields:
            product.product_tmpl_id.write({"uom_po_id": uom.id})
        tmpl_fields = self.env["product.template"]._fields
        tmpl_vals = {}
        if filename and "x_solidedge_file" in tmpl_fields:
            tmpl_vals["x_solidedge_file"] = filename
        if asunto and asunto != "-" and "x_solidedge_asunto" in tmpl_fields:
            tmpl_vals["x_solidedge_asunto"] = asunto
        if tmpl_vals:
            product.product_tmpl_id.write(tmpl_vals)
        return product, True

    def _ensure_buy_route(self, product):
        """Asigna la ruta Comprar a un producto hoja (sin LdM → componente a comprar)."""
        buy_route = self.env.ref("purchase_stock.route_warehouse0_buy", raise_if_not_found=False)
        if buy_route and buy_route not in product.product_tmpl_id.route_ids:
            product.product_tmpl_id.write({"route_ids": [(4, buy_route.id)]})

    # -------------------------------------------------------------------------
    # Helpers de LdM
    # -------------------------------------------------------------------------

    def _get_or_create_bom(self, product, result):
        """
        Busca una LdM existente para el producto o crea una nueva.
        Establece type='normal' y ruta=Fabricar.
        """
        Bom = self.env["mrp.bom"]
        bom = Bom.search(
            [("product_tmpl_id", "=", product.product_tmpl_id.id), ("type", "=", "normal")],
            limit=1,
        )
        if bom:
            result["bom_ids"].append(bom.id)
            return bom

        manufacture_route = self.env.ref(
            "mrp.route_warehouse0_manufacture", raise_if_not_found=False
        )
        route_ids = [(4, manufacture_route.id)] if manufacture_route else []
        product.product_tmpl_id.write({"route_ids": route_ids})

        bom = Bom.create(
            {
                "product_tmpl_id": product.product_tmpl_id.id,
                "type": "normal",
                "product_qty": 1.0,
            }
        )
        result["boms_created"] += 1
        result["bom_ids"].append(bom.id)
        return bom

    def _add_bom_line(self, bom, component_product, qty, result):
        """
        Añade una línea de componente a una LdM.
        Si el mismo producto ya existe en la LdM, actualiza la cantidad
        en lugar de crear un duplicado.
        """
        BomLine = self.env["mrp.bom.line"]
        existing = bom.bom_line_ids.filtered(
            lambda l: l.product_id == component_product
        )
        if existing:
            existing[:1].write({"product_qty": existing[:1].product_qty + qty})
            result["bom_lines_updated"] += 1
        else:
            BomLine.create(
                {
                    "bom_id": bom.id,
                    "product_id": component_product.id,
                    "product_qty": qty,
                    "product_uom_id": component_product.uom_id.id,
                }
            )
            result["bom_lines_added"] += 1

    # -------------------------------------------------------------------------
    # Helpers de utilidad
    # -------------------------------------------------------------------------

    @staticmethod
    def _resolve_unique_key(doc_number, filename):
        """
        Devuelve el identificador único para una fila:
          - Principal: doc_number (sin espacios)
          - Alternativo: nombre de archivo sin extensión, saneado
        """
        key = (doc_number or "").strip()
        if not key:
            base = os.path.splitext((filename or "").strip())[0]
            key = re.sub(r"[^\w\s\-\.]", "_", base).strip()
        return key or None

    @staticmethod
    def _parse_qty(raw, row_index, result):
        """Parsea la cadena de cantidad; por defecto 1.0 y registra un aviso si falla."""
        try:
            val = float((raw or "1").replace(",", "."))
            return val if val > 0 else 1.0
        except ValueError:
            result["warnings"].append(
                _("Fila %d: cantidad '%s' no válida — se usa 1 por defecto.") % (row_index + 3, raw)
            )
            return 1.0

    # -------------------------------------------------------------------------
    # Constructores HTML
    # -------------------------------------------------------------------------

    def _build_preview_html(self, rows):
        """Construye la vista previa de jerarquía mostrada antes de la importación real."""
        warnings = []
        seen = {}
        lines = []

        for i, row in enumerate(rows):
            level = row["level"]
            key = self._resolve_unique_key(row["doc_number"], row["filename"])
            desc = row["description"] if row["description"] and row["description"] != "-" \
                else os.path.splitext(row["filename"])[0]
            qty_raw = row["quantity"]

            indent = "&nbsp;" * (level - 1) * 4
            node_type = "SUBCONJUNTO" if row["has_children"] else ("RAÍZ" if level == 1 else "COMPONENTE")
            color = "#0066cc" if level == 1 else ("#28a745" if row["has_children"] else "#555")

            if not row["doc_number"]:
                warnings.append(
                    "Fila %d: Sin número de documento — se usa el nombre de archivo como clave ('%s')." % (i + 3, key)
                )
            if row["doc_number"] and row["doc_number"] in seen and seen[row["doc_number"]] != desc:
                warnings.append(
                    "Fila %d: Número de doc '%s' duplicado con descripción diferente (conflicto)."
                    % (i + 3, row["doc_number"])
                )
            if row["doc_number"]:
                seen[row["doc_number"]] = desc

            lines.append(
                '<div style="font-family:monospace;font-size:13px;">'
                f'{indent}<span style="color:{color};">[N{level}] {key}</span>'
                f' — {desc} <small>[CANT:{qty_raw}] [{node_type}]</small>'
                "</div>"
            )

        stats_html = (
            f"<p><strong>Filas procesadas:</strong> {len(rows)} &nbsp;|&nbsp; "
            f"<strong>Avisos:</strong> {len(warnings)}</p>"
        )
        tree_html = "".join(lines)
        warn_html = ""
        if warnings:
            warn_items = "".join(f"<li>{w}</li>" for w in warnings)
            warn_html = f"<h4 style='color:#e67e22;'>Avisos ({len(warnings)})</h4><ul>{warn_items}</ul>"

        return (
            "<h3>Vista previa de jerarquía</h3>"
            + stats_html
            + "<hr/>"
            + tree_html
            + "<hr/>"
            + (warn_html or "<p style='color:green;'>Sin avisos.</p>")
        )

    def _build_dryrun_html(self, rows):
        """Construye el mensaje de resultado de validación en seco (sin escrituras)."""
        level_counts = {}
        for row in rows:
            level_counts[row["level"]] = level_counts.get(row["level"], 0) + 1
        level_summary = ", ".join(
            f"N{k}: {v} filas" for k, v in sorted(level_counts.items())
        )
        return (
            "<h3>Validación — Sin cambios guardados</h3>"
            f"<p>{len(rows)} filas procesadas correctamente. {level_summary}.</p>"
            "<p style='color:#28a745;'>Validación correcta. "
            "Desmarca <strong>Solo validar</strong> y pulsa <strong>Importar</strong> "
            "para realizar la importación real.</p>"
        )

    def _build_result_html(self, result):
        """Construye el resumen de resultado mostrado tras una importación exitosa."""
        warn_items = "".join(
            f"<li>{w}</li>" for w in result.get("warnings", [])
        )
        warn_block = (
            f"<h4 style='color:#e67e22;'>Avisos ({len(result['warnings'])})</h4>"
            f"<ul>{warn_items}</ul>"
            if result.get("warnings")
            else "<p style='color:green;'>Sin avisos.</p>"
        )
        bom_button = ""
        if result.get("bom_ids"):
            ids_str = ",".join(str(i) for i in set(result["bom_ids"]))
            bom_button = (
                f"<p><a href='/odoo/manufacturing/bom?ids={ids_str}' target='_blank'>"
                "Ver LdM creadas/actualizadas \u2192</a></p>"
            )
        return (
            "<h3>Importación completada</h3>"
            f"<p><strong>Productos creados:</strong> {result['products_created']}</p>"
            f"<p><strong>Productos actualizados (nombre):</strong> {result['products_updated']}</p>"
            f"<p><strong>LdM creadas:</strong> {result['boms_created']}</p>"
            f"<p><strong>Líneas de LdM añadidas:</strong> {result['bom_lines_added']}</p>"
            f"<p><strong>Líneas de LdM actualizadas (cant.):</strong> {result['bom_lines_updated']}</p>"
            + warn_block
            + bom_button
        )

    # -------------------------------------------------------------------------
    # Interno
    # -------------------------------------------------------------------------

    def _reload_action(self):
        """Devuelve una acción que reabre este registro del asistente."""
        return {
            "type": "ir.actions.act_window",
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "target": "current",
        }
