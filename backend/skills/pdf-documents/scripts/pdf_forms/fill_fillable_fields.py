import json
from pypdf import PdfReader, PdfWriter
from .extract_form_field_info import get_field_info

def fill_pdf_fields(input_pdf_path: str, fields_json_path: str, output_pdf_path: str):
    with open(fields_json_path, encoding='utf-8-sig') as f:
        fields = json.load(f)
    fields_by_page = {}
    for field in fields:
        if 'value' in field:
            field_id = field['field_id']
            page = field['page']
            if page not in fields_by_page:
                fields_by_page[page] = {}
            fields_by_page[page][field_id] = field['value']
    reader = PdfReader(input_pdf_path)
    errors = []
    field_info = get_field_info(reader)
    fields_by_ids = {f['field_id']: f for f in field_info}
    for field in fields:
        existing_field = fields_by_ids.get(field['field_id'])
        if not existing_field:
            errors.append(f"未知字段：{field['field_id']}")
        elif field['page'] != existing_field['page']:
            errors.append(f"字段 {field['field_id']} 页码应为 {existing_field['page']}，收到 {field['page']}")
        elif 'value' in field:
            err = validation_error_for_field_value(existing_field, field['value'])
            if err:
                errors.append(err)
    if errors:
        raise RuntimeError('PDF表单字段校验失败：' + '；'.join(errors))
    writer = PdfWriter(clone_from=reader)
    for page, field_values in fields_by_page.items():
        writer.update_page_form_field_values(writer.pages[page - 1], field_values, auto_regenerate=False)
    writer.set_need_appearances_writer(True)
    with open(output_pdf_path, 'wb') as f:
        writer.write(f)

def validation_error_for_field_value(field_info, field_value):
    field_type = field_info['type']
    field_id = field_info['field_id']
    if field_type == 'checkbox':
        checked_val = field_info['checked_value']
        unchecked_val = field_info['unchecked_value']
        if field_value != checked_val and field_value != unchecked_val:
            return f'ERROR: Invalid value "{field_value}" for checkbox field "{field_id}". The checked value is "{checked_val}" and the unchecked value is "{unchecked_val}"'
    elif field_type == 'radio_group':
        option_values = [opt['value'] for opt in field_info['radio_options']]
        if field_value not in option_values:
            return f'ERROR: Invalid value "{field_value}" for radio group field "{field_id}". Valid values are: {option_values}'
    elif field_type == 'choice':
        choice_values = [opt['value'] for opt in field_info['choice_options']]
        if field_value not in choice_values:
            return f'ERROR: Invalid value "{field_value}" for choice field "{field_id}". Valid values are: {choice_values}'
    return None
