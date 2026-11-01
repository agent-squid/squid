"""Isolated, bounded-lifetime JSON Schema validation worker; fixed JSON output only."""
import json
import sys


def validate(schema, arguments):
    try:
        from jsonschema import Draft202012Validator
        from referencing import Registry
        from referencing.exceptions import NoSuchResource
    except ImportError:
        return 'plan_mcp_dependency_required'
    denied = []

    def retrieve(uri):
        denied.append(True)
        raise NoSuchResource(ref=uri)

    def check_refs(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {'$ref', '$dynamicRef', '$recursiveRef'} and (
                        not isinstance(child, str) or not child.startswith('#')):
                    raise ValueError('external_reference')
                check_refs(child)
        elif isinstance(value, list):
            for child in value:
                check_refs(child)

    try:
        check_refs(schema)
    except (ValueError, RecursionError):
        return 'plan_schema_reference_unsupported'
    try:
        Draft202012Validator.check_schema(schema)
        # An explicit registry disables jsonschema's default HTTP retrieval,
        # including references rebased through nested $id declarations.
        Draft202012Validator(schema, registry=Registry(retrieve=retrieve)).validate(arguments)
    except Exception:
        return 'plan_schema_reference_unsupported' if denied else 'plan_arguments_invalid'
    return None


if __name__ == '__main__':
    try:
        request = json.load(sys.stdin)
        result = validate(request['schema'], request['arguments'])
    except Exception:
        result = 'plan_arguments_invalid'
    print(json.dumps({'error': result}))
