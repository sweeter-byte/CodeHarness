def parse_fields(text):
    fields = {}
    for item in text.split(","):
        key, value = item.split(":")
        fields[key] = value
    return fields
