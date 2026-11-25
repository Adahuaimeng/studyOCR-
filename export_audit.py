"""Identify the original image regions retained in a document export."""

from hashlib import sha256

from exports import Picture, covers, document_items
from illustrations import pixel_area
from structured import cell_crop_bounds, table_rows
from study import BlockFormat


def source_box(sheet, bounds):
    area = sheet.scan.area
    return [bounds.left + area.left, bounds.top + area.top,
            bounds.right + area.left, bounds.bottom + area.top]


def audit_sheet(sheet, pictures, tables, *, picture_role="picture"):
    def entry(role, bounds, *, parts=(), **details):
        source = source_box(sheet, bounds)
        slices = [source_box(sheet, part) for part in parts]
        fingerprint = f"{sheet.source_id or sheet.source_name}:{role}:{source}:{slices}"
        return {"asset_id": "crop-" + sha256(fingerprint.encode()).hexdigest()[:20],
                "source_id": sheet.source_id, "source_name": sheet.source_name,
                "role": role, "bounds": source, "parts": slices, **details}

    assets = [entry(picture_role, bounds, parts=parts, captions=list(captions))
              for bounds, parts, captions in pictures]
    replacements = []
    for value, bounds, captions in tables:
        after = []
        for row_number, row in enumerate(table_rows(value), 1):
            for column_number, cell in enumerate(row, 1):
                if crop := cell_crop_bounds(cell, bounds):
                    asset = entry("table_cell", crop, table_bounds=source_box(sheet, bounds),
                                  row=row_number, column=column_number)
                    after.append(asset["asset_id"])
                    assets.append(asset)
        replacements.append({
            "source_id": sheet.source_id, "source_name": sheet.source_name,
            "bounds": source_box(sheet, bounds), "replacement": "editable_table", "captions": list(captions),
            "retained_cells": after,
            "covered_candidates": [source_box(sheet, candidate) for candidate in sheet.scan.illustrations
                                   if covers(bounds, candidate, .82)
                                   or pixel_area(bounds.intersection(candidate)) >= pixel_area(candidate) * .93],
        })
    return assets, replacements


def document_assets(spec, sections=None):
    assets, replacements = [], []
    for index, sheet in enumerate(spec.sheets, 1):
        if spec.suffix == ".pdf" and spec.original:
            bounds = sheet.scan.area.relative_to(sheet.scan.area)
            found, changes = audit_sheet(sheet, [(bounds, (), ())], [], picture_role="photo")
        elif sections is None:
            _, objects = document_items(sheet, sheet.scan.lines, spec.mode, auto_figures=spec.auto_figures)
            found, changes = audit_sheet(sheet,
                [(item.bounds, (), [line.text for line in item.captions]) for item in objects
                 if isinstance(item, Picture)],
                [(item.value, item.bounds, ()) for item in objects
                 if not isinstance(item, Picture) and item.format == BlockFormat.TABLE])
        else:
            continue
        for record in (*found, *changes):
            record["source_order"] = index
        assets.extend(found)
        replacements.extend(changes)
    for index, section in enumerate(sections or (), 1):
        found, changes = audit_sheet(section.sheet,
            [(block.bounds, block.picture_parts, block.captions) for block in section.blocks
             if block.role == "picture"],
            [(block.value, block.bounds, block.captions) for block in section.blocks if block.role == "table"])
        for record in (*found, *changes):
            record["source_order"] = index
        assets.extend(found)
        replacements.extend(changes)
    return {"assets": assets, "replacements": replacements}
