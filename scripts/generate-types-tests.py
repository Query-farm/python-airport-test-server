# Generate a bunch of tests for the airport extension
# from a list of basic types.

import math


def unescape_value(value):
    """Unescape the value for display."""
    if value.startswith("'") and value.endswith("'"):
        return value[1:-1].replace("''", "'")  # Remove single quotes
    return value


def parse_to_float(val):
    if isinstance(val, str):
        v = val.replace("'", "")  # Remove single quotes
        try:
            return float(v)
        except ValueError:
            return float("nan")  # fallback if an unexpected string
    return val  # already a float


def sort_key(val):
    x = parse_to_float(val)
    if math.isnan(x):
        return (3,)
    elif x == float("-inf"):
        return (0,)
    elif x == float("inf"):
        return (2,)
    else:
        return (1, x)


def custom_sorted(values):
    if "'inf'" in values or "'-inf'" in values or "'nan'" in values:
        # Sort using the custom key
        return sorted(values, key=sort_key)

    return sorted(values)


data = [
    {"field_name": "bigint", "type": "bigint", "values": ["1234567890123456789"], "type_code": "I"},
    {"field_name": "binary", "type": "binary", "values": ["'1234567890abcdef'"], "type_code": "T"},
    {"field_name": "bit", "type": "bit", "values": ["'1'"], "type_code": "T"},
    {"field_name": "bitstring", "type": "bitstring", "values": ["'101010'"], "type_code": "T"},
    {"field_name": "blob", "type": "blob", "values": ["'This is a blob'"], "type_code": "T"},
    {"field_name": "bool", "type": "bool", "values": ["1", "0"], "type_code": "I"},
    {"field_name": "boolean", "type": "boolean", "values": ["true", "false"], "type_code": "I"},
    {"field_name": "bpchar", "type": "bpchar", "values": ["'test'"], "type_code": "T"},
    {"field_name": "bytea", "type": "bytea", "values": ["'1234567890abcdef'"], "type_code": "T"},
    {"field_name": "char", "type": "char", "values": ["'A'"], "type_code": "T"},
    {"field_name": "date", "type": "date", "values": ["'2023-10-01'", "'infinity'", "'-infinity'"], "type_code": "T"},
    {"field_name": "datetime", "type": "datetime", "values": ["'2023-10-01 12:00:00'"], "type_code": "T"},
    {"field_name": "dec", "type": "dec", "values": ["123.45"], "type_code": "R"},
    {"field_name": "decimal", "type": "decimal", "values": ["123.45"], "type_code": "R"},
    {
        "field_name": "double",
        "type": "double",
        "values": ["123.456789", "'inf'", "'-inf'", "'nan'"],
        "type_code": "R",
    },
    {
        "field_name": "float",
        "type": "float",
        "values": ["123.456", "'inf'", "'-inf'", "'nan'"],
        "type_code": "R",
    },
    {
        "field_name": "float4",
        "type": "float4",
        "values": ["123.456", "'inf'", "'-inf'", "'nan'"],
        "type_code": "R",
    },
    {
        "field_name": "float8",
        "type": "float8",
        "values": ["123.456789", "'inf'", "'-inf'", "'nan'"],
        "type_code": "R",
    },
    {"field_name": "guid", "type": "guid", "values": ["'123e4567-e89b-12d3-a456-426614174000'"], "type_code": "T"},
    {"field_name": "hugeint", "type": "hugeint", "values": ["123456789012345678901234567890"], "type_code": "I"},
    {"field_name": "int", "type": "int", "values": ["123456"], "type_code": "I"},
    {"field_name": "int1", "type": "int1", "values": ["1"], "type_code": "I"},
    {"field_name": "int128", "type": "int128", "values": ["123456789012345678901234567890123456"], "type_code": "I"},
    {"field_name": "int16", "type": "int16", "values": ["12345", "-122"], "type_code": "I"},
    {"field_name": "int2", "type": "int2", "values": ["1234"], "type_code": "I"},
    {"field_name": "int32", "type": "int32", "values": ["12345678", "-222292"], "type_code": "I"},
    {"field_name": "int4", "type": "int4", "values": ["123456789"], "type_code": "I"},
    {"field_name": "int64", "type": "int64", "values": ["1234567890123456789"], "type_code": "I"},
    {"field_name": "int8", "type": "int8", "values": ["123"], "type_code": "I"},
    {"field_name": "integer", "type": "integer", "values": ["123456"], "type_code": "I"},
    {"field_name": "integral", "type": "integral", "values": ["123456"], "type_code": "I"},
    {"field_name": "interval", "type": "interval", "values": ["'1 day 02:03:04'"], "type_code": "T"},
    {"field_name": "logical", "type": "logical", "values": ["true", "false"], "type_code": "I"},
    {"field_name": "long", "type": "long", "values": ["1234567890123456789"], "type_code": "I"},
    {"field_name": "numeric", "type": "numeric", "values": ["123.456", "-987.321"], "type_code": "R"},
    {"field_name": "nvarchar", "type": "nvarchar", "values": ["'test'"], "type_code": "T"},
    {"field_name": "oid", "type": "oid", "values": ["123456"], "type_code": "I"},
    {"field_name": "real", "type": "real", "values": ["123.456"], "type_code": "R"},
    {"field_name": "short", "type": "short", "values": ["12345"], "type_code": "I"},
    {"field_name": "signed", "type": "signed", "values": ["123456", "-5"], "type_code": "I"},
    {"field_name": "smallint", "type": "smallint", "values": ["12345"], "type_code": "I"},
    {"field_name": "string", "type": "string", "values": ["'test'"], "type_code": "T"},
    {
        "field_name": "struct",
        "type": "STRUCT(k1 int32, k2 int64)",
        "values": ["{'k1': 5555, 'k2': 123}"],
        "type_code": "T",
    },
    {"field_name": "text", "type": "text", "values": ["'This is a text field'"], "type_code": "T"},
    {"field_name": "time", "type": "time", "values": ["'12:34:56'"], "type_code": "T"},
    {"field_name": "timestamp", "type": "timestamp", "values": ["'2023-10-01 12:34:56'"], "type_code": "T"},
    {"field_name": "timestamp_ms", "type": "timestamp_ms", "values": ["'2023-10-01 12:34:56.789'"], "type_code": "T"},
    {
        "field_name": "timestamp_ns",
        "type": "timestamp_ns",
        "values": ["'2023-10-01 12:34:56.789123456'"],
        "type_code": "T",
    },
    {"field_name": "timestamp_s", "type": "timestamp_s", "values": ["'2023-10-01 12:34:56'"], "type_code": "T"},
    {
        "field_name": "timestamp_us",
        "type": "timestamp_us",
        "values": ["'2023-10-01 12:34:56.789123'"],
        "type_code": "T",
    },
    {
        "field_name": "timestamptz",
        "type": "timestamptz",
        "values": ["'2023-10-01 12:34:56+00'"],
        "type_code": "T",
    },
    {"field_name": "timetz", "type": "timetz", "values": ["'12:34:56'"], "type_code": "T"},
    {"field_name": "tinyint", "type": "tinyint", "values": ["123"], "type_code": "I"},
    {"field_name": "ubigint", "type": "ubigint", "values": ["1234567890123456789"], "type_code": "I"},
    {"field_name": "uhugeint", "type": "uhugeint", "values": ["123456789012345678901234567890"], "type_code": "I"},
    {"field_name": "uint128", "type": "uint128", "values": ["123456789012345678901234567890123456"], "type_code": "I"},
    {"field_name": "uint16", "type": "uint16", "values": ["12345"], "type_code": "I"},
    {"field_name": "uint32", "type": "uint32", "values": ["12345678"], "type_code": "I"},
    {"field_name": "uint64", "type": "uint64", "values": ["1234567890123456789"], "type_code": "I"},
    {"field_name": "uint8", "type": "uint8", "values": ["123"], "type_code": "I"},
    {"field_name": "uinteger", "type": "uinteger", "values": ["123456"], "type_code": "I"},
    {"field_name": "usmallint", "type": "usmallint", "values": ["12345"], "type_code": "I"},
    {"field_name": "utinyint", "type": "utinyint", "values": ["123"], "type_code": "I"},
    {
        "field_name": "uuid",
        "type": "uuid",
        "values": ["'123e4567-e89b-12d3-a456-426614174000'", "'ffffffff-ffff-ffff-ffff-ffffffffffff'"],
        "type_code": "T",
    },
    {"field_name": "varbinary", "type": "varbinary", "values": ["'1234567890abcdef'"], "type_code": "T"},
    {
        "field_name": "varchar",
        "type": "varchar",
        "values": ["'test'", "'rusty''s favorite'", "'🦆'"],
        "type_code": "T",
    },
    {"field_name": "varint", "type": "varint", "values": ["1234567890123456789"], "type_code": "I"},
]


for record in data:
    with open(
        f"/Users/rusty/Development/duckdb-arrow-flight-extension/test/sql/airport-test-types-{record['field_name']}.test",
        "w",
    ) as f:
        print(
            f"""# name: test/sql/airport-test-types-{record["field_name"]}.test
# description: test airport with all data types
# group: [airport]

# Require statement will ensure this test is run with this extension loaded
require airport

# Create the initial secret, the token value doesn't matter.
statement ok
CREATE SECRET airport_testing (
  type airport,
  auth_token 'example_token',
  scope 'grpc://localhost:50003/');

# Reset the test server
statement ok
CALL airport_action('grpc://localhost:50003/', 'reset');

# Create the initial database
statement ok
CALL airport_action('grpc://localhost:50003/', 'create_database', 'test1');

statement ok
ATTACH 'test1' (TYPE  AIRPORT, location 'grpc://localhost:50003/');
""",
            file=f,
        )

        for schema_name in ["test_non_predicate", "test_predicate_pushdown"]:
            print(
                f"""
statement ok
CREATE SCHEMA test1.{schema_name};

statement ok
use test1.{schema_name};
""",
                file=f,
            )

            table_name = f"test_type_{record['field_name']}"
            print(
                f"""statement ok
create table {table_name} (v {record["type"]});

statement ok
insert into {table_name} values (null);
""",
                file=f,
            )
            for value in record["values"]:
                print(
                    f"""statement ok
insert into {table_name} values ({value});

""",
                    file=f,
                )

            print(f"query {record['type_code']}", file=f)
            print(f"select * from {table_name} order by 1", file=f)
            print("----", file=f)
            for value in custom_sorted(record["values"]):
                print(unescape_value(value), file=f)
            print("NULL", file=f)
            print("", file=f)

            for value in custom_sorted(record["values"]):
                print(f"query {record['type_code']}", file=f)
                print(f"select v from {table_name} where v = {value};", file=f)
                print("----", file=f)
                print(unescape_value(value), file=f)
                print("", file=f)
