"""验证本项目工具使用的 JSON Schema 子集，无第三方运行时依赖。"""

from __future__ import annotations

import re


class ValidationError(ValueError):
    pass


def validate(value, schema: dict, location: str = "arguments") -> None:
    kind = schema.get("type")
    checks = {
        "object": lambda v: isinstance(v, dict),
        "array": lambda v: isinstance(v, list),
        "string": lambda v: isinstance(v, str),
        "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
        "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
        "boolean": lambda v: isinstance(v, bool),
        "null": lambda v: v is None,
    }
    if kind is not None and (kind not in checks or not checks[kind](value)):
        raise ValidationError(f"{location} 必须是 {kind}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValidationError(f"{location} 不在允许的取值内")
    if isinstance(value, dict):
        required = schema.get("required", [])
        missing = [key for key in required if key not in value]
        if missing:
            raise ValidationError(f"{location} 缺少参数：{', '.join(missing)}")
        properties = schema.get("properties", {})
        additional = schema.get("additionalProperties", True)
        for key, item in value.items():
            if key in properties:
                validate(item, properties[key], f"{location}.{key}")
            elif additional is False:
                raise ValidationError(f"{location} 包含未知参数：{key}")
            elif isinstance(additional, dict):
                validate(item, additional, f"{location}.{key}")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", float("inf")):
            raise ValidationError(f"{location} 的元素数量超出范围")
        for index, item in enumerate(value):
            validate(item, schema.get("items", {}), f"{location}[{index}]")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", float("inf")):
            raise ValidationError(f"{location} 的长度超出范围")
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            raise ValidationError(f"{location} 格式不正确")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value < schema.get("minimum", -float("inf")) or value > schema.get("maximum", float("inf")):
            raise ValidationError(f"{location} 数值超出范围")
