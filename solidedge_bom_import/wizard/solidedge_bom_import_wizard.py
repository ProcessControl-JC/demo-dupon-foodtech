import base64
import csv
import io
import os
import re

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class SolidEdgeBomImportWizard(models.TransientModel):
    """
    Multi-level BOM import wizard for CSV files exported from Siemens SolidEdge.

    Flow:
      Step 1 (state=upload)   → user uploads CSV and sets options
      Step 2 (state=preview)  → system parses and shows hierarchy preview + warnings
      Step 3 (state=done)     → import runs; result summary shown
    """

    _name = "solidedge.bom.import.wizard"
    _description = "SolidEdge BOM Import Wizard"

    # -------------------------------------------------------------------------
    # Fields
    # -------------------------------------------------------------------------

    state = fields.Selection(
        [("upload", "Upload"), ("preview", "Preview"), ("done", "Done")],
        default="upload",
        required=True,
    )

    file_data = fields.Binary("SolidEdge CSV File", required=True)
    filename = fields.Char("Filename")

    overwrite = fields.Boolean(
        "Overwrite existing product names",
        default=False,
        help="If enabled, product names in Odoo will be updated to match the "
             "latest description from SolidEdge. Disabled by default (safe mode).",
    )
    dry_run = fields.Boolean(
        "Validate only (no write)",
        default=True,
        help="Parse and preview the file without writing anything to the database. "
             "Uncheck to perform the actual import.",
    )

    # Preview / result text fields (HTML)
    preview_html = fields.Html("Preview", readonly=True)
    result_html = fields.Html("Result", readonly=True)

    # -------------------------------------------------------------------------
    # Actions
    # -------------------------------------------------------------------------

    def action_parse_preview(self):
        """Parse the uploaded file and move to the preview step."""
        self.ensure_one()
        rows = self._parse_file()
        self._validate_hierarchy(rows)  # raises UserError on critical error
        preview_html = self._build_preview_html(rows)
        self.write({"state": "preview", "preview_html": preview_html})
        return self._reload_action()

    def action_import(self):
        """Execute the import (or dry-run) and move to the done step."""
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
        """Go back to upload step."""
        self.ensure_one()
        self.write({"state": "upload", "preview_html": False, "result_html": False})
        return self._reload_action()

    # -------------------------------------------------------------------------
    # Parsing
    # -------------------------------------------------------------------------

    def _parse_file(self):
        """
        Decode the uploaded binary, parse the SolidEdge CSV, and return a list
        of row dicts.

        SolidEdge CSV format:
          - Encoding: UTF-8 with BOM  → open with 'utf-8-sig'
          - Delimiter: semicolon (;)
          - Row 1: metadata header (skip)
          - Row 2: column headers
          - Rows 3+: data rows
          - Trailing semicolon on every row → last element after split is empty, discard it
        """
        if not self.file_data:
            raise UserError(_("Please upload a CSV file."))

        raw = base64.b64decode(self.file_data)
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            try:
                text = raw.decode("latin-1")
            except UnicodeDecodeError as e:
                raise UserError(
                    _("Could not decode the file. Expected UTF-8 or Latin-1 encoding. "
                      "Error: %s") % str(e)
                )

        reader = csv.reader(io.StringIO(text), delimiter=";")
        all_rows = list(reader)

        if len(all_rows) < 3:
            raise UserError(
                _("The file does not contain enough rows. "
                  "Expected: row 1 = metadata, row 2 = headers, row 3+ = data.")
            )

        # Row 0 (index 0): metadata — skip
        # Row 1 (index 1): column headers
        header_row = [h.strip() for h in all_rows[1]]

        # Map header names to column indices (resilient to column reorder)
        col_map = self._map_columns(header_row)

        # Rows 2+ (index 2+): data
        rows = []
        for raw_row in all_rows[2:]:
            # Discard trailing empty element caused by trailing semicolon
            if raw_row and raw_row[-1] == "":
                raw_row = raw_row[:-1]
            if not any(raw_row):
                continue  # skip blank lines

            def get(col_name):
                idx = col_map.get(col_name)
                if idx is None or idx >= len(raw_row):
                    return ""
                return raw_row[idx].strip()

            level_raw = get("level")
            if not level_raw:
                continue  # skip rows without a level value

            try:
                level = int(level_raw.strip())
            except ValueError:
                continue  # skip non-numeric level rows

            rows.append(
                {
                    "level": level,
                    "doc_number": get("doc_number"),
                    "asunto": get("asunto"),
                    "description": get("description"),
                    "filename": get("filename"),
                    "quantity": get("quantity"),
                    "has_children": False,  # populated by _validate_hierarchy
                }
            )

        if not rows:
            raise UserError(_("No data rows found in the file."))

        return rows

    def _map_columns(self, header_row):
        """
        Map logical column names to their index positions in the header row.
        Detection is by header name (case-insensitive, partial match), not position.
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
                _("Could not find required columns in the CSV header: %s\n"
                  "Found headers: %s") % (", ".join(missing), ", ".join(header_row))
            )
        return mapping

    # -------------------------------------------------------------------------
    # Hierarchy validation
    # -------------------------------------------------------------------------

    def _validate_hierarchy(self, rows):
        """
        Validate the level sequence and populate the has_children flag.
        Raises UserError on critical structural errors (no data saved).
        """
        if not rows:
            raise UserError(_("No data rows to process."))

        if rows[0]["level"] != 1:
            raise UserError(
                _("The file must start with a Level 1 row (main assembly). "
                  "First row found at level %d.") % rows[0]["level"]
            )

        prev_level = 0
        for i, row in enumerate(rows):
            level = row["level"]
            if level > prev_level + 1:
                raise UserError(
                    _("Hierarchy Error at data row %d: jumped from level %d to level %d. "
                      "Please check the SolidEdge export.") % (i + 3, prev_level, level)
                )
            # Mark the previous row as having children if current level is deeper
            if i > 0 and level > rows[i - 1]["level"]:
                rows[i - 1]["has_children"] = True
            prev_level = level

    # -------------------------------------------------------------------------
    # Core import logic
    # -------------------------------------------------------------------------

    def _process_rows(self, rows):
        """
        Main import engine. Iterates rows using a parent stack to build the
        BOM hierarchy. Returns a result dict with counters and warnings.
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

        # Track doc_number → product to detect conflicts within this import run
        seen_doc_numbers = {}

        # stack: { level_int: (product_record, bom_record_or_None) }
        stack = {}

        for i, row in enumerate(rows):
            level = row["level"]
            doc_number = row["doc_number"]
            filename = row["filename"]
            description = row["description"]
            asunto = row["asunto"]
            qty_raw = row["quantity"]

            # Resolve unique key
            key = self._resolve_unique_key(doc_number, filename)
            if not key:
                result["warnings"].append(
                    _("Row %d: both doc_number and filename are empty — row skipped.") % (i + 3)
                )
                continue

            # Resolve description
            if not description or description == "-":
                description = os.path.splitext(filename)[0] if filename else key

            # Detect duplicate doc_number with different description
            if doc_number:
                if doc_number in seen_doc_numbers:
                    prev_desc = seen_doc_numbers[doc_number]
                    if prev_desc != description:
                        result["warnings"].append(
                            _("Row %d: doc_number '%s' already seen with different description "
                              "('%s' vs '%s'). Using first occurrence.")
                            % (i + 3, doc_number, prev_desc, description)
                        )
                        # Use the already-created product for this key
                        product = self.env["product.product"].search(
                            [("default_code", "=", key)], limit=1
                        )
                        if not product:
                            product, created = self._get_or_create_product(
                                key, description, asunto, filename, result
                            )
                        # Add to parent BOM if not at level 1
                        if level > 1 and (level - 1) in stack:
                            _, parent_bom = stack[level - 1]
                            if parent_bom:
                                qty = self._parse_qty(qty_raw, i, result)
                                self._add_bom_line(parent_bom, product, qty, result)
                        stack[level] = (product, None)
                        continue
                seen_doc_numbers[doc_number] = description

            # Resolve quantity
            qty = self._parse_qty(qty_raw, i, result)

            # Get or create product
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
                # Root assembly — always creates a BOM
                bom = self._get_or_create_bom(product, result)
                stack = {1: (product, bom)}
            else:
                if (level - 1) not in stack:
                    result["warnings"].append(
                        _("Row %d: no parent found for level %d (key='%s') — row skipped.")
                        % (i + 3, level, key)
                    )
                    continue

                _, parent_bom = stack[level - 1]
                if parent_bom:
                    self._add_bom_line(parent_bom, product, qty, result)

                if row["has_children"]:
                    sub_bom = self._get_or_create_bom(product, result)
                    stack[level] = (product, sub_bom)
                else:
                    # Leaf component — no BOM needed, route: Buy
                    self._ensure_buy_route(product)
                    stack[level] = (product, None)
                    # Clear deeper levels from stack (backtrack)
                    for lvl in list(stack.keys()):
                        if lvl > level:
                            del stack[lvl]

        return result

    # -------------------------------------------------------------------------
    # Product helpers
    # -------------------------------------------------------------------------

    def _get_or_create_product(self, key, description, asunto, filename, result):
        """
        Find an existing product by default_code or create a new one.
        Returns (product_record, was_created).
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
        # Set uom_po_id if the field exists (Odoo 16/17); removed in Odoo 19
        if "uom_po_id" in self.env["product.template"]._fields:
            product.product_tmpl_id.write({"uom_po_id": uom.id})
        # Set custom SolidEdge fields if they exist on the template
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
        """Set the Buy route on a leaf product (no BOM → purchaseable component)."""
        buy_route = self.env.ref("purchase_stock.route_warehouse0_buy", raise_if_not_found=False)
        if buy_route and buy_route not in product.product_tmpl_id.route_ids:
            product.product_tmpl_id.write({"route_ids": [(4, buy_route.id)]})

    # -------------------------------------------------------------------------
    # BOM helpers
    # -------------------------------------------------------------------------

    def _get_or_create_bom(self, product, result):
        """
        Find an existing BOM for the product or create a new one.
        Sets type='normal' and route=Manufacture.
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
        Add a component line to a BOM.
        If the same product already exists on the BOM, update the quantity
        instead of creating a duplicate.
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
    # Utility helpers
    # -------------------------------------------------------------------------

    @staticmethod
    def _resolve_unique_key(doc_number, filename):
        """
        Returns the unique identifier for a row:
          - Primary: doc_number (stripped)
          - Fallback: filename without extension, sanitized
        """
        key = (doc_number or "").strip()
        if not key:
            base = os.path.splitext((filename or "").strip())[0]
            key = re.sub(r"[^\w\s\-\.]", "_", base).strip()
        return key or None

    @staticmethod
    def _parse_qty(raw, row_index, result):
        """Parse quantity string; defaults to 1.0 and logs a warning on failure."""
        try:
            val = float((raw or "1").replace(",", "."))
            return val if val > 0 else 1.0
        except ValueError:
            result["warnings"].append(
                _("Row %d: invalid quantity '%s' — defaulting to 1.") % (row_index + 3, raw)
            )
            return 1.0

    # -------------------------------------------------------------------------
    # HTML builders
    # -------------------------------------------------------------------------

    def _build_preview_html(self, rows):
        """Build the hierarchy preview shown before the actual import."""
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
            node_type = "SUB-ASSEMBLY" if row["has_children"] else ("ROOT" if level == 1 else "LEAF")
            color = "#0066cc" if level == 1 else ("#28a745" if row["has_children"] else "#555")

            if not row["doc_number"]:
                warnings.append(
                    "Row %d: No document number — using filename as key ('%s')." % (i + 3, key)
                )
            if row["doc_number"] and row["doc_number"] in seen and seen[row["doc_number"]] != desc:
                warnings.append(
                    "Row %d: Duplicate doc# '%s' with different description (conflict)."
                    % (i + 3, row["doc_number"])
                )
            if row["doc_number"]:
                seen[row["doc_number"]] = desc

            lines.append(
                '<div style="font-family:monospace;font-size:13px;">'
                f'{indent}<span style="color:{color};">[L{level}] {key}</span>'
                f' — {desc} <small>[QTY:{qty_raw}] [{node_type}]</small>'
                "</div>"
            )

        stats_html = (
            f"<p><strong>Rows parsed:</strong> {len(rows)} &nbsp;|&nbsp; "
            f"<strong>Warnings:</strong> {len(warnings)}</p>"
        )
        tree_html = "".join(lines)
        warn_html = ""
        if warnings:
            warn_items = "".join(f"<li>{w}</li>" for w in warnings)
            warn_html = f"<h4 style='color:#e67e22;'>Warnings ({len(warnings)})</h4><ul>{warn_items}</ul>"

        return (
            "<h3>Hierarchy Preview</h3>"
            + stats_html
            + "<hr/>"
            + tree_html
            + "<hr/>"
            + (warn_html or "<p style='color:green;'>No warnings.</p>")
        )

    def _build_dryrun_html(self, rows):
        """Build a dry-run result message (no writes performed)."""
        level_counts = {}
        for row in rows:
            level_counts[row["level"]] = level_counts.get(row["level"], 0) + 1
        level_summary = ", ".join(
            f"L{k}: {v} rows" for k, v in sorted(level_counts.items())
        )
        return (
            "<h3>Dry Run — No changes saved</h3>"
            f"<p>{len(rows)} rows parsed successfully. {level_summary}.</p>"
            "<p style='color:#28a745;'>Validation passed. "
            "Uncheck <strong>Validate only</strong> and click <strong>Import</strong> "
            "to perform the actual import.</p>"
        )

    def _build_result_html(self, result):
        """Build the result summary shown after a successful import."""
        warn_items = "".join(
            f"<li>{w}</li>" for w in result.get("warnings", [])
        )
        warn_block = (
            f"<h4 style='color:#e67e22;'>Warnings ({len(result['warnings'])})</h4>"
            f"<ul>{warn_items}</ul>"
            if result.get("warnings")
            else "<p style='color:green;'>No warnings.</p>"
        )
        bom_button = ""
        if result.get("bom_ids"):
            ids_str = ",".join(str(i) for i in set(result["bom_ids"]))
            bom_button = (
                f"<p><a href='/odoo/manufacturing/bom?ids={ids_str}' target='_blank'>"
                "View created/updated BOMs →</a></p>"
            )
        return (
            "<h3>Import Complete</h3>"
            f"<p><strong>Products created:</strong> {result['products_created']}</p>"
            f"<p><strong>Products updated (name):</strong> {result['products_updated']}</p>"
            f"<p><strong>BOMs created:</strong> {result['boms_created']}</p>"
            f"<p><strong>BOM lines added:</strong> {result['bom_lines_added']}</p>"
            f"<p><strong>BOM lines updated (qty):</strong> {result['bom_lines_updated']}</p>"
            + warn_block
            + bom_button
        )

    # -------------------------------------------------------------------------
    # Internal
    # -------------------------------------------------------------------------

    def _reload_action(self):
        """Return an action that reopens this wizard record."""
        return {
            "type": "ir.actions.act_window",
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "target": "current",
        }
