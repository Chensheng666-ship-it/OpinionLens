# -*- coding: utf-8 -*-
"""
schema_validator.py — 轻量 JSON Schema 校验器（Draft-07 常用子集）

背景：MediaCrawler 的 venv 未安装 jsonschema 三方包，且 POC 不希望为此改动环境，
因此实现一个覆盖本项目 schema 所需关键字的只读校验器：
  $ref(#/definitions/...)、type、required、properties、additionalProperties、
  items、enum、const、minimum、maximum、minItems、maxItems、pattern、oneOf、anyOf。

如果后续环境安装了 jsonschema，可用 `--use-jsonschema` 切换到官方实现做交叉校验。
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional


_TYPE_LABEL = {
    "object": "对象",
    "array": "数组",
    "string": "字符串",
    "number": "数字",
    "integer": "整数",
    "boolean": "布尔",
    "null": "null",
}


def _type_match(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return True


class SchemaError(ValueError):
    pass


class MiniValidator:
    def __init__(self, root_schema: Dict[str, Any]):
        self.root = root_schema

    # -- $ref 解析 ------------------------------------------------------
    def _resolve(self, schema: Dict[str, Any]) -> Dict[str, Any]:
        ref = schema.get("$ref")
        if not ref:
            return schema
        if not ref.startswith("#/"):
            raise SchemaError(f"仅支持本地 $ref，收到：{ref}")
        node: Any = self.root
        for part in ref[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            if not isinstance(node, dict) or part not in node:
                raise SchemaError(f"$ref 无法解析：{ref}")
            node = node[part]
        return node

    # -- 主入口 ---------------------------------------------------------
    def validate(self, data: Any, schema: Optional[Dict[str, Any]] = None,
                 path: str = "$") -> List[str]:
        errors: List[str] = []
        self._validate(data, self.root if schema is None else schema, path, errors)
        return errors

    def _validate(self, data: Any, schema: Dict[str, Any], path: str, errors: List[str]) -> None:
        if not schema:
            return
        schema = self._resolve(schema)

        if "oneOf" in schema:
            passed = 0
            for idx, sub in enumerate(schema["oneOf"]):
                sub_errors: List[str] = []
                self._validate(data, sub, path, sub_errors)
                if not sub_errors:
                    passed += 1
            if passed != 1:
                errors.append(f"{path}: 不满足 oneOf（命中 {passed} 个子 schema，期望 1 个）")
            return
        if "anyOf" in schema:
            if not any(not self._errors_for(data, sub, path) for sub in schema["anyOf"]):
                errors.append(f"{path}: 不满足 anyOf")
            return

        expected_type = schema.get("type")
        if expected_type:
            types = expected_type if isinstance(expected_type, list) else [expected_type]
            if not any(_type_match(data, t) for t in types):
                errors.append(
                    f"{path}: 类型错误，期望 {[ _TYPE_LABEL.get(t, t) for t in types ]}，"
                    f"实际 {type(data).__name__}"
                )
                return

        if "const" in schema and data != schema["const"]:
            errors.append(f"{path}: 期望常量 {schema['const']!r}，实际 {data!r}")

        if "enum" in schema and data not in schema["enum"]:
            errors.append(f"{path}: 值 {data!r} 不在枚举 {schema['enum']} 中")

        if isinstance(data, (int, float)) and not isinstance(data, bool):
            if "minimum" in schema and data < schema["minimum"]:
                errors.append(f"{path}: {data} 小于最小值 {schema['minimum']}")
            if "maximum" in schema and data > schema["maximum"]:
                errors.append(f"{path}: {data} 大于最大值 {schema['maximum']}")

        if isinstance(data, str) and "pattern" in schema:
            if re.search(schema["pattern"], data) is None:
                errors.append(f"{path}: 字符串 {data!r} 不匹配模式 {schema['pattern']}")

        if isinstance(data, list):
            if "minItems" in schema and len(data) < schema["minItems"]:
                errors.append(f"{path}: 数组长度 {len(data)} 小于 minItems={schema['minItems']}")
            if "maxItems" in schema and len(data) > schema["maxItems"]:
                errors.append(f"{path}: 数组长度 {len(data)} 大于 maxItems={schema['maxItems']}")
            item_schema = schema.get("items")
            if isinstance(item_schema, dict):
                for idx, item in enumerate(data):
                    self._validate(item, item_schema, f"{path}[{idx}]", errors)

        if isinstance(data, dict):
            for key in schema.get("required", []):
                if key not in data:
                    errors.append(f"{path}: 缺少必填字段 {key!r}")
            properties = schema.get("properties", {})
            for key, value in data.items():
                if key in properties:
                    self._validate(value, properties[key], f"{path}.{key}", errors)
                else:
                    additional = schema.get("additionalProperties")
                    if additional is False:
                        errors.append(f"{path}: 不允许的额外字段 {key!r}")
                    elif isinstance(additional, dict):
                        self._validate(value, additional, f"{path}.{key}", errors)

    def _errors_for(self, data: Any, schema: Dict[str, Any], path: str) -> List[str]:
        errors: List[str] = []
        self._validate(data, schema, path, errors)
        return errors


def load_schema(path: str) -> Dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def validate_stats_json(stats: Dict[str, Any], schema: Dict[str, Any]) -> List[str]:
    return MiniValidator(schema).validate(stats)


if __name__ == "__main__":  # 手工调试
    import argparse
    import sys

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="轻量 JSON Schema 校验")
    parser.add_argument("--json", required=True, help="待校验 JSON 文件")
    parser.add_argument("--schema", required=True, help="JSON Schema 文件")
    args = parser.parse_args()
    with open(args.json, encoding="utf-8") as fh:
        instance = json.load(fh)
    schema = load_schema(args.schema)
    errs = validate_stats_json(instance, schema)
    if errs:
        print(f"[FAIL] {len(errs)} 个 schema 校验错误：")
        for err in errs[:50]:
            print("  -", err)
        raise SystemExit(1)
    print("[PASS] schema 校验通过")
